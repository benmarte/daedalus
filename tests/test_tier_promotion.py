"""Tier promotion: sub-issue re-evaluation after dependency closure.

Tests the core.tier_promotion module:
  * compute_tiers — tier computation from dependency map with cycle detection
  * DependencySnapshot — snapshot + promotable computation
  * promote_waiting_tiers — end-to-end: apply Ready label on promotion
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.providers.base import IssueSummary, VCSProvider, parse_depends_on  # noqa: E402
from core.providers.http import ProviderError  # noqa: E402
from core import tier_promotion  # noqa: E402


# ── Stub Provider ─────────────────────────────────────────────────────────────

class _StubProvider(VCSProvider):
    """Minimal concrete provider for unit testing tier promotion.

    Stores per-issue: body, labels, state, blockers (explicit override).
    sub_issues_of() parses bodies for ``Epic: #<N>`` references.
    Never hits the network — all state is in-memory.
    """
    name = "stub-tier"

    def __init__(self):
        # Minimal init; no config needed for testing since we override everything.
        self._bodies: Dict[int, str] = {}
        self._labels: Dict[int, List[str]] = {}
        self._states: Dict[int, Optional[str]] = {}
        self._blockers: Dict[int, Optional[List[int]]] = {}  # explicit; None = compute
        self.label_calls: List[tuple] = []  # (issue_number, label_name)
        self.comment_calls: List[tuple] = []  # (issue_number, comment_body)

    # Abstract methods (required by VCSProvider)
    def list_issues(self, state="open", labels=None, limit=50):
        return []

    def close_issue(self, issue_number):
        self._states[issue_number] = "closed"
        return True

    def list_prs(self, state="all", limit=50):
        return []

    # Getters
    def get_issue(self, issue_number):
        if issue_number not in self._states:
            return None
        labels = self._labels.get(issue_number, [])
        return IssueSummary(
            number=issue_number,
            body=self._bodies.get(issue_number, ""),
            labels=labels,
        )

    def get_issue_state(self, issue_number):
        return self._states.get(issue_number)

    # Tier-promotion-specific methods
    def has_label(self, issue_number: int, label_name: str) -> bool:
        return label_name.lower() in [lbl.lower() for lbl in self._labels.get(issue_number, [])]

    def add_label(self, issue_number: int, label_name: str) -> bool:
        self._labels.setdefault(issue_number, []).append(label_name)
        self.label_calls.append((issue_number, label_name))
        return True

    def post_issue_comment(self, issue_number, body):
        self.comment_calls.append((issue_number, body))
        return True

    def blockers(self, issue_number: int) -> List[int]:
        if issue_number in self._blockers and self._blockers[issue_number] is not None:
            out = self._blockers[issue_number]
            return list(out) if out else []  # type: ignore[return-value]
        # Fallback: parse body for Depends on refs that are still open
        body = self._bodies.get(issue_number, "")
        refs = parse_depends_on(body)
        return [n for n in refs if self._states.get(n) == "open"]

    def sub_issues_of(self, epic_number: int) -> List[int]:
        # Parse all issues whose body contains ``Part of epic #<epic_number>``
        results: List[int] = []
        import re
        # Match both "Part of epic #100" and "Epic: #100" conventions
        pattern = re.compile(
            rf"(?m)(?:part\s+of\s+epic\s+#|epic\s*:\s*#){epic_number}(?::|\b)",
            re.IGNORECASE
        )
        for issue_number, body in self._bodies.items():
            if pattern.search(body):
                state = self._states.get(issue_number)
                if state in ("open", "closed"):  # include closed — they were sub-issues too
                    results.append(issue_number)
        return results

    # Test helpers
    def set_issue(self, issue_number: int, body: str, state: str = "open",
                  labels: Optional[List[str]] = None):
        self._bodies[issue_number] = body
        self._states[issue_number] = state
        if labels is not None:
            self._labels[issue_number] = list(labels)

    def set_blockers(self, issue_number: int, blockers: Optional[List[int]]):
        """Explicitly set blockers for an issue (overrides body-derived computation)."""
        self._blockers[issue_number] = blockers


# ── compute_tiers ─────────────────────────────────────────────────────────────

def test_compute_tiers_no_deps_all_zero():
    dep_map = {1: [], 2: [], 3: []}
    tiers = tier_promotion.compute_tiers(dep_map)
    assert tiers == {1: 0, 2: 0, 3: 0}


def test_compute_tiers_chain():
    dep_map = {1: [], 2: [1], 3: [2]}  # 3→2→1 (1 is tier 0, 3 is tier 2)
    tiers = tier_promotion.compute_tiers(dep_map)
    assert tiers == {1: 0, 2: 1, 3: 2}


def test_compute_tiers_fan_out():
    dep_map = {1: [], 2: [1], 3: [1], 4: [2, 3]}  # 4 depends on 2 AND 3
    tiers = tier_promotion.compute_tiers(dep_map)
    assert tiers == {1: 0, 2: 1, 3: 1, 4: 2}


def test_compute_tiers_cycle_detected():
    dep_map = {1: [], 2: [3], 3: [2]}  # cycle: 2↔3
    tiers = tier_promotion.compute_tiers(dep_map)
    # 1 is fine (tier 0); 2 and 3 are in cycle — excluded from tiers
    assert 1 in tiers and tiers[1] == 0
    assert 2 not in tiers
    assert 3 not in tiers


def test_detect_cycles_simple():
    dep_map = {1: [2], 2: [3], 3: [1]}  # full cycle
    cycles = tier_promotion.detect_cycles(dep_map)
    assert len(cycles) >= 1
    cycle_nodes = set()
    for cyc in cycles:
        cycle_nodes.update(cyc)
    assert {1, 2, 3}.issubset(cycle_nodes)


def test_compute_tiers_external_dep_no_block():
    # 2 depends on 99 — not in dep_map (external)
    dep_map = {1: [], 2: [99]}
    tiers = tier_promotion.compute_tiers(dep_map)
    # 2 has dependency on external #99 → treated as tier 0 (no in-graph dep)
    assert tiers[2] == 0


# ── DependencySnapshot ───────────────────────────────────────────────────────

def _build_snapshot(provider, epic_number, just_closed=None):
    return tier_promotion.DependencySnapshot(
        epic_number=epic_number,
        provider=provider,
        just_closed=frozenset(just_closed or []),
    )


def test_snapshot_build_and_compute_tiers():
    provider = _StubProvider()
    # Epic 10; sub-issues: 20 (tiers-1, depends on 30), 30 (tier-0, no deps)
    provider.set_issue(10, "Epic")
    provider.set_issue(20, "Epic: #10\nDepends on: #30")
    provider.set_issue(30, "Epic: #10")
    provider._states[30] = "closed"
    provider.set_blockers(20, [])
    provider.set_blockers(30, [])

    snap = _build_snapshot(provider, epic_number=10, just_closed={30})
    tiers = snap.compute_tiers()
    # 30 is tier-0 (no deps), 20 is tier-1 (depends on 30)
    assert tiers[30] == 0
    assert tiers[20] == 1


def test_snapshot_promotable_returns_empty_when_blockers_remain():
    provider = _StubProvider()
    provider.set_issue(10, "Epic")
    provider.set_issue(20, "Epic: #10\nDepends on: #30")
    provider.set_issue(30, "Epic: #10")
    provider._states[30] = "open"  # still open!
    provider.set_blockers(20, [30])  # 30 blocks 20
    provider.set_blockers(30, [])

    snap = _build_snapshot(provider, epic_number=10, just_closed=set())
    promotable = snap.promotable(already_ready=set())
    # 20 blocked by 30 (open) → not promotable
    assert promotable == []


def test_snapshot_promotable_after_dep_closes():
    provider = _StubProvider()
    provider.set_issue(10, "Epic")
    provider.set_issue(20, "Epic: #10\nDepends on: #30")
    provider.set_issue(30, "Epic: #10")
    provider._states[30] = "closed"
    provider.set_blockers(20, [])  # 30 closed, so no blockers
    provider.set_blockers(30, [])

    snap = _build_snapshot(provider, epic_number=10, just_closed={30})
    promotable = snap.promotable(already_ready=set())
    # 20's only dep (30) was closed this tick → 20 is promotable
    assert 20 in promotable


def test_snapshot_promotable_skips_already_ready():
    provider = _StubProvider()
    provider.set_issue(10, "Epic")
    provider.set_issue(20, "Epic: #10", labels=["Ready"])
    provider.set_blockers(20, [])

    snap = _build_snapshot(provider, epic_number=10, just_closed=set())
    promotable = snap.promotable(already_ready={20})
    # 20 is already Ready → excluded
    assert promotable == []


# ── promote_waiting_tiers (end-to-end) ────────────────────────────────────────

def test_promote_single_dependency():
    """When a sub-issue's dep closes, it gets the Ready label."""
    provider = _StubProvider()
    provider.set_issue(10, "Epic parent")  # the epic itself
    provider.set_issue(20, "Epic: #10\nDepends on: #30")  # tier 1
    provider.set_issue(30, "Epic: #10")  # tier 0 (no deps)
    provider._states[30] = "closed"  # just closed
    provider.set_blockers(20, [])  # 30 now closed → no blockers

    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[30])
    assert 20 in result.promoted
    assert (20, "Ready") in provider.label_calls
    assert result.errors == []


def test_promote_multiple_in_same_tier():
    """Multiple sub-issues all depending on same closed issue → all promoted."""
    provider = _StubProvider()
    provider.set_issue(10, "Epic")
    for n in [20, 21, 22]:
        provider.set_issue(n, f"Epic: #10\nDepends on: #30")
    provider.set_issue(30, "Epic: #10")
    provider._states[30] = "closed"
    for n in [20, 21, 22]:
        provider.set_blockers(n, [])  # 30 closed → no blockers

    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[30])
    assert set(result.promoted) == {20, 21, 22}


def test_no_promotion_when_deps_remaining():
    """Dep still open → no label applied."""
    provider = _StubProvider()
    provider.set_issue(10, "Epic")
    provider.set_issue(20, "Epic: #10\nDepends on: #30")
    provider.set_issue(30, "Epic: #10")
    # 30 still open!
    provider.set_blockers(20, [30])

    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[])
    assert result.promoted == []
    assert (20, "Ready") not in provider.label_calls


def test_tier_chain_promotion():
    """When one dep closes, only the next tier is promoted; further tiers wait."""
    provider = _StubProvider()
    provider.set_issue(10, "Epic")
    provider.set_issue(20, "Epic: #10\nDepends on: #30")  # tier 2
    provider.set_issue(30, "Epic: #10\nDepends on: #40")  # tier 1 → just promotable
    provider.set_issue(40, "Epic: #10")  # tier 0 (just closed)
    provider._states[40] = "closed"
    provider.set_blockers(20, [30])  # 30 still open → 20 NOT promotable
    provider.set_blockers(30, [])  # 40 closed → 30 IS promotable
    provider.set_blockers(40, [])

    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[40])
    assert 30 in result.promoted
    assert 20 not in result.promoted


def test_circular_dependency_detected():
    """Cycle in deps → warning, no promotion for cyclic nodes."""
    provider = _StubProvider()
    provider.set_issue(10, "Epic")
    provider.set_issue(20, "Epic: #10\nDepends on: #40")
    provider.set_issue(30, "Epic: #10\nDepends on: #20")
    provider.set_issue(40, "Epic: #10\nDepends on: #30")
    # All in cycle — all have open blockers
    provider.set_blockers(20, [40])
    provider.set_blockers(30, [20])
    provider.set_blockers(40, [30])
    # 20 is the one that closed (triggers re-evaluation of its epic)
    provider._states[20] = "closed"

    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[20])
    assert result.promoted == []
    assert len(result.cycles) > 0


def test_no_epic_noop():
    """No sub-issues found for an epic → no-op."""
    provider = _StubProvider()
    provider.set_issue(10, "Epic")
    provider.set_blockers(10, [])

    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[10])
    assert result.promoted == []
    assert provider.label_calls == []


def test_provider_error_graceful():
    """add_label failing mid-promote should not crash the whole function."""
    provider = _StubProvider()
    provider.set_issue(10, "Epic")
    provider.set_issue(20, "Epic: #10\nDepends on: #30")
    provider.set_issue(30, "Epic: #10")
    provider._states[30] = "closed"
    provider.set_blockers(20, [])
    provider.set_blockers(30, [])

    # Make add_label raise for issue 20
    real_add_label = provider.add_label

    def flaky_add_label(issue_number, label_name):
        if issue_number == 20:
            raise RuntimeError("simulated provider error")
        return real_add_label(issue_number, label_name)

    provider.add_label = flaky_add_label
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[30])
    # Should not crash; error recorded
    assert 20 not in result.promoted
    assert len(result.errors) >= 1


def test_promotion_idempotent():
    """Already-labeled-Ready issue is excluded (via already_ready check)."""
    provider = _StubProvider()
    provider.set_issue(10, "Epic")
    provider.set_issue(20, "Epic: #10", labels=["Ready"])  # already Ready
    provider.set_blockers(20, [])

    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[999])
    # 20 already has Ready → should not be re-promoted
    assert 20 not in result.promoted
    assert all(n != 20 for n, _ in provider.label_calls)

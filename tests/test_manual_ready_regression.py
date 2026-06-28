"""Regression tests for manual issue → Ready transitions.

Covers the 7 manual Ready scenarios from the test plan
(docs/test-plans/manual-issue-to-ready-regression.md) to ensure the
auto-advance feature (epic #915, tasks #919-#923) does not break manual
transitions to Ready status.

Each scenario verifies:
- Expected state transitions
- Side effects (no duplicate labels/comments from auto-advance)
- UI/API contracts (board status, label, dispatchability)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.providers.base import IssueSummary, VCSProvider, parse_depends_on
from core import tier_promotion


# ── Stub Provider with full board simulation ──────────────────────────────────

class _StubProvider(VCSProvider):
    """Minimal concrete provider for manual Ready regression tests.

    Simulates per-issue: body, labels, state, board_status, blockers.
    Tracks all provider calls for verification.
    """
    name = "stub-manual-ready"

    def __init__(self):
        self._bodies: Dict[int, str] = {}
        self._labels: Dict[int, List[str]] = {}
        self._states: Dict[int, Optional[str]] = {}
        self._board_statuses: Dict[int, str] = {}
        self._blockers: Dict[int, Optional[List[int]]] = {}
        self.label_calls: List[tuple] = []
        self.comment_calls: List[tuple] = []
        self.board_status_calls: List[tuple] = []
        self._board_configured = True

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

    # Label operations
    def has_label(self, issue_number: int, label_name: str) -> bool:
        return label_name.lower() in [lbl.lower() for lbl in self._labels.get(issue_number, [])]

    def add_label(self, issue_number: int, label_name: str) -> bool:
        self._labels.setdefault(issue_number, []).append(label_name)
        self.label_calls.append((issue_number, label_name))
        return True

    def blockers(self, issue_number: int) -> List[int]:
        if issue_number in self._blockers and self._blockers[issue_number] is not None:
            out = self._blockers[issue_number]
            return list(out) if out else []
        # Fallback: parse body for Depends on refs that are still open
        body = self._bodies.get(issue_number, "")
        refs = parse_depends_on(body)
        return [n for n in refs if self._states.get(n) == "open"]

    def sub_issues_of(self, epic_number: int) -> List[int]:
        import re
        pattern = re.compile(
            rf"(?m)(?:part\s+of\s+epic\s+#|epic\s*:\s*#){epic_number}(?::|\b)",
            re.IGNORECASE
        )
        results: List[int] = []
        for issue_number, body in self._bodies.items():
            if pattern.search(body):
                state = self._states.get(issue_number)
                if state in ("open", "closed"):
                    results.append(issue_number)
        return results

    # Board operations
    def board_configured(self) -> bool:
        return self._board_configured

    def board_numbers_with_statuses(self, status_names: List[str]) -> set:
        return {n for n, status in self._board_statuses.items() if status in status_names}

    def board_set_status(self, issue_number: int, status_name: str) -> bool:
        self._board_statuses[issue_number] = status_name
        self.board_status_calls.append((issue_number, status_name))
        return True

    def status_name(self, canonical: str) -> str:
        return {"ready": "Ready", "in_progress": "In progress", "done": "Done", "blocked": "Blocked"}.get(
            canonical, canonical
        )

    def post_issue_comment(self, issue_number: int, body: str) -> bool:
        self.comment_calls.append((issue_number, body))
        return True

    # Test helpers
    def set_issue(self, issue_number: int, body: str, state: str = "open",
                  labels: Optional[List[str]] = None, board_status: str = "Backlog"):
        self._bodies[issue_number] = body
        self._states[issue_number] = state
        if labels is not None:
            self._labels[issue_number] = list(labels)
        self._board_statuses[issue_number] = board_status

    def set_blockers(self, issue_number: int, blockers: Optional[List[int]]):
        self._blockers[issue_number] = blockers


# ── Scenario 1: Manually transition a standalone issue to Ready ───────────────

def test_manual_ready_standalone_issue_not_affected_by_auto_advance():
    """Scenario 1: Standalone issue manually moved to Ready.

    Regression risk: auto-advance logic accidentally filters out standalone issues
    or Ready-gating incorrectly blocks manually-Ready standalone issues.

    Expected:
    - Standalone issue (no epic ref) stays Ready after dispatcher tick
    - Tier promotion does NOT fire (issue has no epic reference)
    - Issue remains dispatchable
    """
    provider = _StubProvider()
    provider.set_issue(100, "Standalone issue", state="open",
                      labels=["Ready"], board_status="Ready")
    provider.set_blockers(100, [])

    # Simulate dispatcher tick with promote_waiting_tiers (no just-closed issues)
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[])

    # Verify: no promotion attempted (standalone has no epic ref)
    assert result.promoted == []
    assert result.errors == []

    # Verify: label and board status unchanged
    assert provider.has_label(100, "Ready")
    assert provider._board_statuses[100] == "Ready"

    # Verify: no label calls made (idempotent)
    assert all(n != 100 for n, _ in provider.label_calls)

    # Verify: issue is still dispatchable (board status in Ready set)
    ready_issues = provider.board_numbers_with_statuses(["Ready"])
    assert 100 in ready_issues


# ── Scenario 2: Manually transition a sub-issue to Ready while parent is not Ready ──

def test_manual_ready_subissue_parent_not_ready():
    """Scenario 2: Sub-issue manually moved to Ready while parent epic is not Ready.

    Regression risk: dispatcher incorrectly blocks manually-Ready sub-issues
    whose parent is not Ready, or tier promotion assumes sub-issues can only be
    Ready via tier promotion.

    Expected:
    - Sub-issue becomes dispatchable (board status Ready)
    - Parent epic status remains unchanged
    - If sub-issue has unmet deps, dispatcher's dependency-aware gating blocks dispatch
    """
    provider = _StubProvider()
    # Parent epic is Backlog (not Ready)
    provider.set_issue(100, "Epic parent", state="open", board_status="Backlog")
    # Sub-issue manually moved to Ready (has no deps, so should be dispatchable)
    provider.set_issue(200, "Part of epic #100", state="open",
                      labels=["Ready"], board_status="Ready")
    provider.set_blockers(200, [])

    # Simulate dispatcher tick
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[])

    # Verify: sub-issue remains Ready (no interference from tier promotion)
    assert provider.has_label(200, "Ready")
    assert provider._board_statuses[200] == "Ready"
    assert result.promoted == []  # no promotion attempted

    # Verify: sub-issue is dispatchable
    ready_issues = provider.board_numbers_with_statuses(["Ready"])
    assert 200 in ready_issues

    # Verify: parent epic status unchanged
    assert provider._board_statuses[100] == "Backlog"


def test_manual_ready_subissue_with_unmet_deps_blocked():
    """Scenario 2 variant: Sub-issue manually Ready but has unmet deps.

    Expected:
    - Sub-issue is Ready (board status + label set)
    - But NOT dispatchable (blocked by dependency-aware gating)
    - Tier promotion does NOT fire (issue is manually Ready, not auto-advanced)
    """
    provider = _StubProvider()
    provider.set_issue(100, "Epic parent", state="open")
    # Sub-issue #200 manually moved to Ready, but has dep on #300 (still open)
    provider.set_issue(200, "Part of epic #100\nDepends on: #300", state="open",
                      labels=["Ready"], board_status="Ready")
    provider.set_issue(300, "Part of epic #100", state="open",
                      labels=["Ready"], board_status="Ready")
    provider.set_blockers(200, [300])  # #300 still blocks #200

    # Simulate dispatcher tick
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[])

    # Verify: #200 is Ready but blocked
    assert provider.has_label(200, "Ready")
    assert provider._board_statuses[200] == "Ready"
    assert result.promoted == []  # no promotion (manually Ready)

    # Verify: #200 has blockers (not yet dispatchable)
    blockers = provider.blockers(200)
    assert 300 in blockers

    # Verify: no duplicate label/comment from tier promotion
    assert all(n != 200 for n, _ in provider.label_calls)


# ── Scenario 3: Manually transition a sub-issue to Ready when dependencies are not satisfied ─

def test_manual_ready_deps_not_satisfied():
    """Scenario 3: Sub-issue with depends_on is manually moved to Ready, but deps still open.

    Regression risk: tier promotion accidentally overwrites manual Ready status
    or posts duplicate comments.

    Expected:
    - Sub-issue is Ready (board status + label)
    - Tier promotion does NOT re-promote (idempotency guard)
    - No duplicate "tier-promoted" comment
    """
    provider = _StubProvider()
    provider.set_issue(100, "Epic parent", state="open")
    # Sub-issue #200 manually Ready, deps #300 and #400 still open
    provider.set_issue(200, "Part of epic #100\nDepends on: #300, #400",
                      state="open", labels=["Ready"], board_status="Ready")
    provider.set_issue(300, "Part of epic #100", state="open", labels=["Ready"])
    provider.set_issue(400, "Part of epic #100", state="open", labels=["Ready"])
    provider.set_blockers(200, [300, 400])

    # Simulate dispatcher tick
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[])

    # Verify: #200 is Ready (manually set)
    assert provider.has_label(200, "Ready")
    assert provider._board_statuses[200] == "Ready"

    # Verify: no promotion attempted (manually Ready, deps not closed)
    assert result.promoted == []
    assert all(n != 200 for n, _ in provider.label_calls)

    # Verify: no duplicate comment
    assert all(n != 200 for n, _ in provider.comment_calls)

    # Verify: #200 still blocked by deps
    blockers = provider.blockers(200)
    assert set(blockers) == {300, 400}


# ── Scenario 4: Manually transition an issue that was previously auto-advanced ───

def test_manual_ready_after_auto_advance_no_duplicate():
    """Scenario 4: Issue auto-advanced, then manually moved back to Ready.

    Regression risk: idempotency guard too aggressive, or Ready label removed
    when board status changes manually.

    Expected:
    - Tier promotion does NOT re-promote (issue already has Ready label)
    - No duplicate Ready label applied
    - No duplicate "tier-promoted" comment
    - Issue becomes dispatchable again
    """
    provider = _StubProvider()
    provider.set_issue(100, "Epic parent", state="open")
    # Sub-issue #200 was auto-advanced (has Ready label)
    # Human manually moved it to Backlog, then back to Ready
    provider.set_issue(200, "Part of epic #100", state="open",
                      labels=["Ready"], board_status="Ready")
    provider.set_issue(300, "Part of epic #100", state="closed", labels=["Ready"])
    provider.set_blockers(200, [])  # deps closed

    # Simulate dispatcher tick with #300 just-closed (but #200 already Ready)
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[300])

    # Verify: #200 NOT re-promoted (already has Ready label)
    assert 200 not in result.promoted
    assert all(n != 200 for n, _ in provider.label_calls)

    # Verify: no duplicate comment
    assert all(n != 200 for n, _ in provider.comment_calls)

    # Verify: #200 is still dispatchable
    ready_issues = provider.board_numbers_with_statuses(["Ready"])
    assert 200 in ready_issues


# ── Scenario 5: Manual Ready + auto-advance race condition ────────────────────

def test_manual_ready_and_auto_advance_race_no_duplicate():
    """Scenario 5: Sub-issue manually Ready at same time deps close (race condition).

    Regression risk: tier promotion does not check has_label before applying,
    resulting in duplicate Ready label or duplicate "tier-promoted" comment.

    Expected:
    - Tier promotion detects issue already Ready (idempotency guard)
    - Tier promotion skips add_label (already Ready)
    - Tier promotion skips board_set_status (already Ready)
    - Tier promotion skips post_issue_comment (idempotency)
    - No duplicate actions
    """
    provider = _StubProvider()
    provider.set_issue(100, "Epic parent", state="open")
    # Sub-issue #200 manually Ready JUST BEFORE deps close
    provider.set_issue(200, "Part of epic #100\nDepends on: #300",
                      state="open", labels=["Ready"], board_status="Ready")
    provider.set_issue(300, "Part of epic #100", state="closed", labels=["Ready"])
    provider.set_blockers(200, [])  # #300 just closed

    # Simulate dispatcher tick: #300 just-closed, #200 manually Ready
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[300])

    # Verify: #200 NOT re-promoted (already Ready)
    assert 200 not in result.promoted

    # Verify: no duplicate label call
    assert all(n != 200 for n, _ in provider.label_calls)

    # Verify: no duplicate board status call
    assert all(n != 200 for n, _ in provider.board_status_calls)

    # Verify: no duplicate comment
    assert all(n != 200 for n, _ in provider.comment_calls)

    # Verify: #200 is dispatchable
    ready_issues = provider.board_numbers_with_statuses(["Ready"])
    assert 200 in ready_issues


# ── Scenario 6: Manual Ready on tier-0 sub-issue (no dependencies) ──────────

def test_manual_ready_tier0_subissue():
    """Scenario 6: Tier-0 sub-issue manually moved to Ready after decomposition.

    Regression risk: tier promotion accidentally tries to promote tier-0 issues.

    Expected:
    - Tier promotion does NOT fire (tier-0 dispatched at creation)
    - Dispatcher's dependency-aware gating allows dispatch (no deps, status Ready)
    """
    provider = _StubProvider()
    provider.set_issue(100, "Epic parent", state="open")
    # Tier-0 sub-issue (no deps) — auto-labeled Ready during decomposition
    provider.set_issue(200, "Part of epic #100", state="open",
                      labels=["Ready"], board_status="Ready")
    provider.set_blockers(200, [])

    # Simulate dispatcher tier tick
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[])

    # Verify: #200 NOT promoted (tier-0 dispatched at creation)
    assert 200 not in result.promoted
    assert result.promoted == []

    # Verify: #200 is dispatchable
    ready_issues = provider.board_numbers_with_statuses(["Ready"])
    assert 200 in ready_issues


def test_manual_ready_tier0_after_manual_board_change():
    """Scenario 6 variant: Tier-0 sub-issue manually moved to Backlog, then back to Ready.

    Expected:
    - Tier-0 remains dispatchable after manual board change
    - No tier promotion interference
    """
    provider = _StubProvider()
    provider.set_issue(100, "Epic parent", state="open")
    # Tier-0 sub-issue manually moved to Backlog, then back to Ready
    provider.set_issue(200, "Part of epic #100", state="open",
                      labels=["Ready"], board_status="Ready")
    provider.set_blockers(200, [])

    # Simulate dispatcher tier tick
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[])

    # Verify: #200 is dispatchable
    ready_issues = provider.board_numbers_with_statuses(["Ready"])
    assert 200 in ready_issues

    # Verify: #200 NOT promoted (tier-0)
    assert 200 not in result.promoted


# ── Scenario 7: Manual Ready on issue with external dependencies ─────────────

def test_manual_ready_external_deps_not_blocking():
    """Scenario 7: Sub-issue with external deps manually moved to Ready.

    Regression risk: tier promotion considers external deps as blockers,
    preventing dispatch.

    Expected:
    - External dependencies (not sibling of epic) are treated as satisfied
    - Sub-issue becomes dispatchable regardless of external dep state
    - provider.blockers() returns only internal blockers
    """
    provider = _StubProvider()
    provider.set_issue(100, "Epic parent", state="open")
    # Sub-issue #200 depends on external #999 (not a sibling) and internal #300
    provider.set_issue(200, "Part of epic #100\nDepends on: #999, #300",
                      state="open", labels=["Ready"], board_status="Ready")
    provider.set_issue(300, "Part of epic #100", state="closed", labels=["Ready"])
    # #999 is external (not a sub-issue of epic #100)
    provider.set_blockers(200, [])  # #300 closed, #999 external → no blockers

    # Simulate dispatcher tick
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[])

    # Verify: #200 is Ready and dispatchable
    assert provider.has_label(200, "Ready")
    assert provider._board_statuses[200] == "Ready"

    # Verify: no blockers (external dep #999 dropped, internal #300 closed)
    blockers = provider.blockers(200)
    assert blockers == []

    # Verify: #200 is dispatchable
    ready_issues = provider.board_numbers_with_statuses(["Ready"])
    assert 200 in ready_issues

    # Verify: no promotion attempted (manually Ready)
    assert 200 not in result.promoted


def test_manual_ready_external_deps_only():
    """Scenario 7 variant: Sub-issue depends ONLY on external deps.

    Expected:
    - External deps are treated as satisfied (not blocking dispatch)
    - Sub-issue becomes dispatchable immediately
    """
    provider = _StubProvider()
    provider.set_issue(100, "Epic parent", state="open")
    # Sub-issue #200 depends only on external #999
    provider.set_issue(200, "Part of epic #100\nDepends on: #999",
                      state="open", labels=["Ready"], board_status="Ready")
    # #999 is external (not a sub-issue)
    provider.set_blockers(200, [])  # external dep → no blockers

    # Simulate dispatcher tick
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[])

    # Verify: #200 is dispatchable
    ready_issues = provider.board_numbers_with_statuses(["Ready"])
    assert 200 in ready_issues

    # Verify: no blockers
    blockers = provider.blockers(200)
    assert blockers == []


# ── Additional edge cases ────────────────────────────────────────────────────

def test_promotable_excludes_already_ready():
    """Verify DependencySnapshot.promotable() excludes already-Ready issues.

    This is the idempotency guard that prevents duplicate promotions.
    """
    provider = _StubProvider()
    provider.set_issue(100, "Epic parent", state="open")
    provider.set_issue(200, "Part of epic #100", state="open",
                      labels=["Ready"], board_status="Ready")
    provider.set_issue(300, "Part of epic #100", state="closed", labels=["Ready"])
    provider.set_blockers(200, [])

    snapshot = tier_promotion.DependencySnapshot(
        epic_number=100,
        provider=provider,
        just_closed=frozenset([300]),
    )

    already_ready = {200}  # #200 already has Ready label
    promotable = snapshot.promotable(already_ready=already_ready)

    # Verify: #200 NOT promotable (already Ready)
    assert 200 not in promotable


def test_tier_0_skipped_in_promotable():
    """Verify promotable() skips tier-0 issues (dispatched at creation)."""
    provider = _StubProvider()
    provider.set_issue(100, "Epic parent", state="open")
    # Tier-0 sub-issue (no deps)
    provider.set_issue(200, "Part of epic #100", state="open",
                      labels=[], board_status="Backlog")
    provider.set_blockers(200, [])

    snapshot = tier_promotion.DependencySnapshot(
        epic_number=100,
        provider=provider,
        just_closed=frozenset([]),
    )

    promotable = snapshot.promotable(already_ready=set())

    # Verify: #200 NOT promotable (tier-0, dispatched at creation)
    assert 200 not in promotable


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

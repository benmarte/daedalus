"""Tests for core.label_projection — pure projection function and reconciler."""
from __future__ import annotations

from typing import Any, Collection, Dict, List, Optional, Set, Tuple

import pytest

from core.label_projection import (
    DAEDALUS_NS,
    GATE_PREFIX,
    STAGE_PREFIX,
    STATE_PREFIX,
    project_labels,
    reconcile_label_projection,
)


def _card(assignee: str, status: str, *, block_kind: str = "", verdict: str = "") -> Dict[str, Any]:
    """Build a minimal kanban card dict."""
    c: Dict[str, Any] = {
        "id": f"t_{assignee[:4]}",
        "assignee": assignee,
        "status": status,
        "title": "#42 something broken",
    }
    if block_kind:
        c["block_kind"] = block_kind
    if verdict:
        c["run_metadata"] = {"verdict": verdict, "role": "validator"}
    return c


class _SpyProvider:
    """Minimal provider spy that records label add/remove calls."""

    def __init__(self, current_labels: Optional[List[str]] = None):
        self._current: Set[str] = set(current_labels or [])
        self.added: List[Tuple[int, str]] = []
        self.removed: List[Tuple[int, str]] = []

    def add_label(self, issue_number: int, label_name: str) -> bool:
        self.added.append((issue_number, label_name))
        self._current.add(label_name)
        return True

    def remove_label(self, issue_number: int, label_name: str) -> bool:
        self.removed.append((issue_number, label_name))
        self._current.discard(label_name)
        return True

    def list_issue_labels(self, issue_number: int) -> List[str]:
        return list(self._current)


# ── pure projection tests ────────────────────────────────────────────────────

def test_single_running_developer():
    cards = [_card("developer-daedalus", "running")]
    to_add, to_remove = project_labels(cards, [])
    assert STATE_PREFIX + "running" in to_add
    assert STAGE_PREFIX + "developer" in to_add
    assert not to_remove


def test_all_done_state():
    cards = [
        _card("developer-daedalus", "done"),
        _card("qa-daedalus", "done"),
    ]
    to_add, to_remove = project_labels(cards, [])
    assert STATE_PREFIX + "done" in to_add
    assert not any(lbl.startswith(STAGE_PREFIX) for lbl in to_add)


def test_parallel_review_phase():
    """Reviewer + security + accessibility all running → three stage labels."""
    cards = [
        _card("reviewer-daedalus", "running"),
        _card("security-analyst-daedalus", "running"),
        _card("accessibility-daedalus", "running"),
    ]
    to_add, to_remove = project_labels(cards, [])
    assert STAGE_PREFIX + "reviewer" in to_add
    assert STAGE_PREFIX + "security" in to_add
    assert STAGE_PREFIX + "accessibility" in to_add
    assert STATE_PREFIX + "running" in to_add


def test_gate_needs_info_from_run_metadata():
    cards = [
        _card("validator-daedalus", "done", verdict="needs_more_info"),
    ]
    to_add, to_remove = project_labels(cards, [])
    assert GATE_PREFIX + "needs-info" in to_add
    assert GATE_PREFIX + "needs-human" not in to_add


def test_gate_needs_human_from_run_metadata():
    cards = [
        _card("validator-daedalus", "done", verdict="block_for_review"),
    ]
    to_add, to_remove = project_labels(cards, [])
    assert GATE_PREFIX + "needs-human" in to_add
    assert GATE_PREFIX + "needs-info" not in to_add


def test_gate_security_threat_maps_to_needs_human():
    cards = [
        _card("validator-daedalus", "done", verdict="security_threat"),
    ]
    to_add, to_remove = project_labels(cards, [])
    assert GATE_PREFIX + "needs-human" in to_add


def test_dependency_blocked_cards_ignored_for_state():
    """Cards blocked by DAG dependency do not count as 'blocked' state."""
    cards = [
        _card("developer-daedalus", "blocked", block_kind="dependency"),
        _card("validator-daedalus", "done"),
    ]
    to_add, to_remove = project_labels(cards, [])
    # Dependency-blocked card + done card: not all_terminal (dep-blocked ≠ terminal)
    # and no non-dep blocked cards → no state label.
    assert STATE_PREFIX + "blocked" not in to_add
    assert STATE_PREFIX + "running" not in to_add


def test_no_pipeline_removes_all_daedalus_labels():
    current = ["daedalus:stage/developer", "daedalus:state/running", "some-other-label"]
    to_add, to_remove = project_labels([], current)
    assert "daedalus:stage/developer" in to_remove
    assert "daedalus:state/running" in to_remove
    assert "some-other-label" not in to_remove  # non-daedalus labels untouched
    assert not to_add


def test_diff_only_when_nothing_changed():
    """No diff when current labels already match wanted state."""
    cards = [_card("developer-daedalus", "running")]
    current = {STATE_PREFIX + "running", STAGE_PREFIX + "developer"}
    to_add, to_remove = project_labels(cards, current)
    assert not to_add
    assert not to_remove


def test_tamper_repair_wrong_stage():
    """Provider reports stale daedalus:stage/pm while developer is running."""
    cards = [_card("developer-daedalus", "running")]
    current = {STATE_PREFIX + "running", STAGE_PREFIX + "pm"}  # stale pm label
    to_add, to_remove = project_labels(cards, current)
    assert STAGE_PREFIX + "developer" in to_add
    assert STAGE_PREFIX + "pm" in to_remove


# ── reconciler tests ─────────────────────────────────────────────────────────

def test_reconcile_applies_diff():
    provider = _SpyProvider(current_labels=[])
    cards = [_card("developer-daedalus", "running")]
    adds, removes = reconcile_label_projection("test-slug", 42, provider, cards=cards)
    assert adds > 0
    assert removes == 0
    assert any("developer" in lbl for _, lbl in provider.added)


def test_reconcile_no_write_when_nothing_changed():
    """No provider calls when current labels already match."""
    provider = _SpyProvider(current_labels=[
        STATE_PREFIX + "running",
        STAGE_PREFIX + "developer",
    ])
    cards = [_card("developer-daedalus", "running")]
    adds, removes = reconcile_label_projection("test-slug", 42, provider, cards=cards)
    assert adds == 0
    assert removes == 0
    assert not provider.added
    assert not provider.removed


def test_reconcile_never_raises_on_provider_failure():
    """Provider failure does not propagate — returns (0, 0)."""

    class _BrokenProvider:
        def add_label(self, *a: Any) -> bool:
            raise RuntimeError("network error")

        def remove_label(self, *a: Any) -> bool:
            raise RuntimeError("network error")

        def list_issue_labels(self, *a: Any) -> List[str]:
            raise RuntimeError("network error")

    adds, removes = reconcile_label_projection(
        "test-slug",
        42,
        _BrokenProvider(),
        cards=[_card("developer-daedalus", "running")],
    )
    assert adds == 0
    assert removes == 0


def test_reconcile_empty_cards_no_calls_when_no_existing_labels():
    """Empty cards + no existing daedalus: labels → nothing to do."""
    provider = _SpyProvider()
    adds, removes = reconcile_label_projection("test-slug", 42, provider, cards=[])
    assert adds == 0
    assert removes == 0
    assert not provider.added
    assert not provider.removed


def test_reconcile_removes_stale_on_empty_cards():
    """Empty cards + stale daedalus: labels → removes them."""
    provider = _SpyProvider(current_labels=[STATE_PREFIX + "running"])
    adds, removes = reconcile_label_projection("test-slug", 42, provider, cards=[])
    assert removes == 1
    assert adds == 0
    assert any(STATE_PREFIX + "running" in lbl for _, lbl in provider.removed)


def test_reconcile_dry_run_no_api_calls():
    """dry_run=True: computes diff but makes no provider calls."""
    provider = _SpyProvider(current_labels=[])
    cards = [_card("developer-daedalus", "running")]
    adds, removes = reconcile_label_projection(
        "test-slug", 42, provider, cards=cards, dry_run=True
    )
    assert adds > 0  # diff was computed
    assert not provider.added  # no real API calls
    assert not provider.removed

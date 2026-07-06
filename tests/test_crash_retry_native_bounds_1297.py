"""Tests for crash_retry native_bounds boundary (issue #1297).

The ``native_bounds`` flag (#1289) causes ``_reconcile_card`` to skip
``timeout``-class cards so the crash-retry reconciler does not double-handle
a ``CODING_AGENT_TIMEOUT`` that Hermes' ``--max-runtime`` requeue already
owns.  Only the timeout class is affected; all other crash classes continue
through the reconciler as normal.

Multi-tick simulation caveat
─────────────────────────────
A complete end-to-end simulation of the crash_retry-skip vs native-requeue
boundary would require driving the Hermes-core kanban state machine across
multiple ticks (gave_up → unblocked-by-native-requeue → re-dispatched) and
verifying that the reconciler does not re-fire while native requeue is in
flight.  That state machine lives in ``hermes/kanban_db.py`` (Hermes core,
not this plugin) and is not importable in these unit tests.

What we CAN verify locally:
  1. ``classify()`` correctly classifies timeout evidence as ``"timeout"``.
  2. ``_reconcile_card`` returns ``None`` (skip) for timeout-class cards
     when ``native_bounds=True``.
  3. Non-timeout crash classes are NOT skipped even when ``native_bounds=True``.
  4. When ``native_bounds=False``, timeout-class cards are handled normally
     (NOT skipped) — candidate_ids is populated.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from core.crash_retry import classify


# ── (1) classify() correctly identifies timeout evidence ─────────────────────

@pytest.mark.parametrize("evidence", [
    "coding-agent-failed: CODING_AGENT_TIMEOUT",
    "CODING_AGENT_TIMEOUT was raised",
    "coding_agent_timeout occurred during run",
])
def test_classify_timeout_evidence(evidence):
    """classify() returns 'timeout' for any timeout-class evidence string."""
    result = classify(evidence)
    assert result == "timeout", (
        f"expected 'timeout', got {result!r} for evidence={evidence!r}"
    )


@pytest.mark.parametrize("evidence", [
    "coding-agent-failed: exited with code 1",
    "coding_agent_died",
    "agent crash detected",
    "pid not alive",
])
def test_classify_crash_evidence(evidence):
    """classify() returns 'crash' for non-timeout crash evidence."""
    result = classify(evidence)
    assert result == "crash", (
        f"expected 'crash', got {result!r} for evidence={evidence!r}"
    )


def test_classify_non_crash_returns_none():
    """classify() returns None for review-required / qa-failed (pipeline-owned blocks)."""
    assert classify("review-required: PR #42 — fix/issue-42-test") is None
    assert classify("qa-failed: 3 tests red") is None
    assert classify("") is None


# ── helpers for _reconcile_card tests ─────────────────────────────────────────

def _make_card(
    title: str = "fix issue #42",
    status: str = "gave_up",
    summary: str = "coding-agent-failed: CODING_AGENT_TIMEOUT",
) -> dict:
    return {
        "id": "t-nb-1",
        "title": title,
        "status": status,
        "summary": summary,
        "assignee": "developer-daedalus",
        "last_failure_error": summary,
    }


def _base_cfg() -> dict:
    return {
        "crash_retry_enabled": True,
        "max_crash_retries": 5,
        "crash_retry_backoff_minutes": [0, 15, 30, 60, 120],
        "crash_retry_cooldown_minutes": 120,
        "crash_retry_window_hours": 6,
    }


# ── (2) _reconcile_card skips timeout when native_bounds=True ─────────────────

def test_native_bounds_skips_timeout_class():
    """native_bounds=True + timeout-class card → _reconcile_card returns None.

    The reconciler must return None BEFORE adding the card to candidate_ids so
    native --max-runtime requeue is the sole owner and there is no double-dispatch.
    """
    from core.crash_retry import _reconcile_card

    card = _make_card(summary="coding-agent-failed: CODING_AGENT_TIMEOUT")
    candidate_ids: set = set()

    with patch("core.crash_retry.dispatch_state") as mock_state, \
         patch("core.crash_retry.kanban") as mock_kanban:
        mock_state.get_crash_retry.return_value = {}
        mock_kanban.get_latest_summary.return_value = ""
        result = _reconcile_card(
            "test-board",
            "/tmp/fake-workdir",
            _base_cfg(),
            card,
            time.time(),
            False,          # dry_run
            candidate_ids,
            None,           # failover
            True,           # native_bounds
        )

    assert result is None, (
        f"expected None (skip), got {result!r}; "
        "native_bounds=True should skip timeout-class cards"
    )
    # Card must NOT be added to candidate_ids (no double-dispatch)
    assert "t-nb-1" not in candidate_ids, (
        "timeout-class card must not be in candidate_ids under native_bounds"
    )


# ── (3) non-timeout crash classes are NOT skipped under native_bounds ─────────

def test_native_bounds_does_not_skip_crash_class():
    """native_bounds=True + crash-class (not timeout) → reconciler still evaluates.

    candidate_ids must be populated — the card is queued for a retry attempt.
    """
    from core.crash_retry import _reconcile_card

    card = _make_card(summary="coding-agent-failed: exited with code 1")
    candidate_ids: set = set()

    with patch("core.crash_retry.dispatch_state") as mock_state, \
         patch("core.crash_retry.kanban") as mock_kanban:
        mock_state.get_crash_retry.return_value = {}
        mock_kanban.get_latest_summary.return_value = ""
        _reconcile_card(
            "test-board",
            "/tmp/fake-workdir",
            _base_cfg(),
            card,
            time.time(),
            False,
            candidate_ids,
            None,
            True,  # native_bounds=True — but crash class, not timeout
        )

    assert "t-nb-1" in candidate_ids, (
        "non-timeout crash-class card must be added to candidate_ids "
        "even when native_bounds=True"
    )


# ── (4) native_bounds=False handles timeout normally ─────────────────────────

def test_native_bounds_false_processes_timeout():
    """native_bounds=False → timeout-class card goes through the reconciler.

    candidate_ids must be populated — the reconciler owns the retry when
    native --max-runtime requeue is not in effect.
    """
    from core.crash_retry import _reconcile_card

    card = _make_card(summary="coding-agent-failed: CODING_AGENT_TIMEOUT")
    candidate_ids: set = set()

    with patch("core.crash_retry.dispatch_state") as mock_state, \
         patch("core.crash_retry.kanban") as mock_kanban:
        mock_state.get_crash_retry.return_value = {}
        mock_kanban.get_latest_summary.return_value = ""
        _reconcile_card(
            "test-board",
            "/tmp/fake-workdir",
            _base_cfg(),
            card,
            time.time(),
            False,
            candidate_ids,
            None,
            False,  # native_bounds=False
        )

    assert "t-nb-1" in candidate_ids, (
        "timeout-class card must NOT be skipped when native_bounds=False"
    )

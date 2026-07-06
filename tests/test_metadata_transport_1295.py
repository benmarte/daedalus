"""Tests for _guard_prefix_on_done with metadata_transport=True (issue #1295).

The ``metadata_transport`` branch (#1288) short-circuits the normal prefix /
JSON-block checks when the closing kanban run carries a valid structured
outcome record whose ``role`` matches the card's assignee.  These tests cover:

  (a) run_outcome returns a role-matching dict  → guard passes, no archive
      (card is well-formed via native transport even if summary has no prefix)
  (b) run_outcome returns None                  → guard falls through to prefix
      check; summary has no valid prefix        → guard fires (count 1)
  (c) run_outcome returns a role-MISMATCHED dict → falls through → guard fires
  (d) metadata_transport=False (flag off)        → run_outcome NOT called;
      summary has no prefix → guard fires (count 1)
  (e) metadata_transport=False + valid prefix    → guard passes (flag-off baseline)

Note: _guard_prefix_on_done only covers profiles in _DONE_GUARD_PREFIXES:
  qa-daedalus, reviewer-daedalus, security-analyst-daedalus,
  accessibility-daedalus, documentation-daedalus.
Tests use "qa-daedalus" (→ role "qa") throughout.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_kanban_mock(
    *,
    assignee: str = "qa-daedalus",
    summary: str = "bare summary with no valid prefix",
    task_id: str = "t-md-1",
    run_outcome_return=None,
) -> MagicMock:
    """Build a minimal kanban mock for _guard_prefix_on_done tests.

    ``run_outcome_return`` is what ``mk.run_outcome(slug, tid)`` returns.
    Pass a dict to simulate a native outcome record from the closing run;
    pass ``None`` to simulate a card whose run has no recorded outcome.
    """
    mk = MagicMock()
    task = {
        "id": task_id,
        "title": "fix issue #10",
        "assignee": assignee,
        "status": "done",
        "summary": summary,
    }
    mk.list_tasks.return_value = [task]
    mk.show_card.return_value = {"latest_summary": summary, "comments": []}
    mk.archive_task.return_value = True
    mk.create_task.return_value = "guard-blocked-id"
    mk.block_task.return_value = True
    mk.run_outcome.return_value = run_outcome_return
    return mk


def _valid_outcome_dict(role: str = "qa", verdict: str = "passed") -> dict:
    """Return a minimal valid outcome dict that parse_dict() will accept."""
    return {
        "daedalus_outcome": 1,
        "role": role,
        "verdict": verdict,
        "refs": {"issue": 10, "pr": 5},
        "evidence": {},
        "note": "",
    }


# ── (a) run_outcome role match → guard passes ─────────────────────────────────

def test_metadata_transport_run_outcome_matching_role_passes():
    """metadata_transport=True + run_outcome carries matching role → no archive.

    The card summary has no recognised prefix, but the closing run's outcome
    record confirms the correct role (qa).  The guard skips the card.
    """
    from core.dispatch.checks import _guard_prefix_on_done

    with patch("core.dispatch.checks._kanban") as mk_fn:
        mk = _make_kanban_mock(
            assignee="qa-daedalus",
            summary="bare summary — no qa prefix",
            run_outcome_return=_valid_outcome_dict("qa", "passed"),
        )
        mk_fn.return_value = mk

        count = _guard_prefix_on_done("slug", metadata_transport=True)

    assert count == 0, (
        f"expected 0 (guard skipped), got {count}; "
        "run_outcome returned a role-matching record"
    )
    # run_outcome must have been called with slug and task id
    mk.run_outcome.assert_called_once_with("slug", "t-md-1")
    # archive must NOT be called
    mk.archive_task.assert_not_called()


# ── (b) run_outcome=None → falls through, no prefix → guard fires ─────────────

def test_metadata_transport_run_outcome_none_falls_through():
    """metadata_transport=True + run_outcome=None → prefix check; no prefix → archive.

    When the closing run has no recorded outcome (run_outcome returns None),
    the guard falls through to the normal prefix check.  A summary with no
    valid role prefix triggers the guard.
    """
    from core.dispatch.checks import _guard_prefix_on_done

    with patch("core.dispatch.checks._kanban") as mk_fn:
        mk = _make_kanban_mock(
            assignee="qa-daedalus",
            summary="completely wrong summary text",
            run_outcome_return=None,
        )
        mk_fn.return_value = mk
        # provider=None → _is_issue_closed_cached returns False (open issue)
        count = _guard_prefix_on_done("slug", metadata_transport=True, provider=None)

    assert count == 1, (
        f"expected 1 (guard fired), got {count}; "
        "run_outcome=None should fall through to prefix check"
    )
    mk.run_outcome.assert_called_once_with("slug", "t-md-1")
    mk.archive_task.assert_called_once()


# ── (c) run_outcome role mismatch → falls through → guard fires ───────────────

def test_metadata_transport_run_outcome_wrong_role_falls_through():
    """metadata_transport=True + run_outcome role mismatch → not skipped → archive.

    A closing run whose outcome record claims role "developer" for a
    "qa-daedalus" card does not satisfy the guard; falls through to prefix
    matching.
    """
    from core.dispatch.checks import _guard_prefix_on_done

    with patch("core.dispatch.checks._kanban") as mk_fn:
        mk = _make_kanban_mock(
            assignee="qa-daedalus",
            summary="wrong-role summary — no qa prefix",
            run_outcome_return=_valid_outcome_dict("developer", "pr_opened"),  # mismatch
        )
        mk_fn.return_value = mk
        count = _guard_prefix_on_done("slug", metadata_transport=True, provider=None)

    assert count == 1, (
        f"expected 1 (guard fired), got {count}; "
        "role mismatch should not satisfy the guard"
    )
    mk.archive_task.assert_called_once()


# ── (d) metadata_transport=False → run_outcome never called ──────────────────

def test_metadata_transport_flag_off_run_outcome_not_called():
    """metadata_transport=False (default) → run_outcome is never called.

    Behaviour must be byte-identical to the pre-#1288 code path:
    - run_outcome is not called
    - a summary with no valid prefix still triggers the guard
    """
    from core.dispatch.checks import _guard_prefix_on_done

    with patch("core.dispatch.checks._kanban") as mk_fn:
        mk = _make_kanban_mock(
            assignee="qa-daedalus",
            summary="bare summary — no qa prefix",
            # run_outcome_return doesn't matter; should not be called
            run_outcome_return=_valid_outcome_dict("qa", "passed"),
        )
        mk_fn.return_value = mk
        count = _guard_prefix_on_done("slug", metadata_transport=False, provider=None)

    assert count == 1, (
        f"expected 1 (guard fired), got {count}; "
        "metadata_transport=False must not call run_outcome"
    )
    mk.run_outcome.assert_not_called()
    mk.archive_task.assert_called_once()


# ── (e) flag-off regression: valid prefix still passes without run_outcome ────

def test_metadata_transport_flag_off_valid_prefix_passes():
    """metadata_transport=False + valid prefix → guard passes (no change from baseline)."""
    from core.dispatch.checks import _guard_prefix_on_done

    with patch("core.dispatch.checks._kanban") as mk_fn:
        mk = _make_kanban_mock(
            assignee="qa-daedalus",
            summary="qa-passed: all tests green",
            run_outcome_return=None,
        )
        mk_fn.return_value = mk
        count = _guard_prefix_on_done("slug", metadata_transport=False)

    assert count == 0, (
        f"expected 0 (guard skipped), got {count}; valid prefix should pass flag-off path"
    )
    mk.run_outcome.assert_not_called()
    mk.archive_task.assert_not_called()

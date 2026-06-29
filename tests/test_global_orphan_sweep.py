"""Integration tests for global_reconcile_orphan_cards.

Covers the dispatcher's safety-net sweep that completes non-terminal kanban
cards whose parent issue has reached Done before the pipeline finished.

Scenarios:
  - Non-terminal card with #N in title and issue Done on board → completed
  - Non-terminal card with #N only in body and issue Done on board → completed
  - Card whose issue is still open → NOT completed (safety)
  - Already-terminal cards → skipped (idempotency)
  - dry_run → no mutations

Run: python3 tests/test_global_orphan_sweep.py
"""

import sys
import re
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest
from conftest import FakeProvider, _load_dispatch, check
from core import kanban, iterate
import scripts.daedalus_dispatch as disp


# ── Word boundary regression (#957) ───────────────────────────────────────────


def test_close_issue_tasks_word_boundary_matches_957():
    """Plain #957 matches the substring '957' exactly."""
    tasks = [
        {"id": "t1", "title": "#957 fix the thing", "status": "todo"},
        {"id": "t2", "title": "#957 fix another thing", "status": "ready"},
    ]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        with mock.patch.object(disp.kanban, "complete", return_value=True) as mk:
            closed = disp.kanban.close_issue_tasks("slug", 957, summary="closed")
    check("closed 2 cards for #957", len(closed) == 2)
    check("complete called twice", mk.call_count == 2)


def test_close_issue_tasks_word_boundary_rejects_9571():
    """#9571 must NOT match when we ask to close #957."""
    tasks = [
        {"id": "t1", "title": "#9571 unrelated issue", "status": "todo"},
        {"id": "t2", "body": "fix #9571 thing", "status": "todo"},
    ]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        with mock.patch.object(disp.kanban, "complete", return_value=True) as mk:
            closed = disp.kanban.close_issue_tasks("slug", 957, summary="closed")
    check("closed 0 cards (957 ≠ 9571)", len(closed) == 0)
    check("complete never called", mk.call_count == 0)


def test_close_issue_tasks_word_boundary_rejects_1957():
    """#1957 must NOT match when we ask to close #957 (left boundary)."""
    tasks = [
        {"id": "t1", "title": "#1957 another issue", "status": "todo"},
    ]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        with mock.patch.object(disp.kanban, "complete", return_value=True) as mk:
            closed = disp.kanban.close_issue_tasks("slug", 957, summary="closed")
    check("closed 0 cards (957 ≠ 1957)", len(closed) == 0)
    check("complete never called", mk.call_count == 0)


# ── Body/handoff fallback ─────────────────────────────────────────────────────


def test_close_issue_tasks_body_fallback():
    """close_issue_tasks matches a card that lacks #N in title but has it in body."""
    tasks = [
        {"id": "t1", "title": "Developer: fix bug", "body": "Issue #957 — fix the thing", "status": "todo"},
        {"id": "t2", "title": "QA: review PR", "body": "review-required: PR #42 — #957", "status": "blocked"},
    ]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        with mock.patch.object(disp.kanban, "complete", return_value=True) as mk:
            closed = disp.kanban.close_issue_tasks("slug", 957, summary="skipped")
    check("closed 2 cards via body fallback", len(closed) == 2)
    check("complete called for body-match card", mk.call_count == 2)


# ── Global orphan sweep ───────────────────────────────────────────────────────


def test_global_sweep_completes_card_without_title_prefix():
    """A non-terminal card with #N only in body and issue Done → completed."""
    cards = [
        {"id": "t_dev", "title": "Developer: fix bug", "body": "Issue #957 — fix", "status": "todo"},
        {"id": "t_qa", "title": "QA card", "body": "parent #957", "status": "ready"},
    ]
    provider = mock.Mock()
    provider.board_numbers_with_statuses.return_value = {957}
    provider.status_name.return_value = "Done"
    with mock.patch.object(disp.kanban, "list_tasks", return_value=cards):
        with mock.patch.object(disp.kanban, "complete", return_value=True) as mk:
            disp._global_reconcile_orphan_cards("slug", provider, dry_run=False)
    check("complete called twice", mk.call_count == 2)


def test_global_sweep_skips_open_issue():
    """Cards whose issue is NOT Done on the board → NOT completed (safety guard)."""
    cards = [
        {"id": "t_dev", "title": "#957 fix", "status": "todo"},
    ]
    provider = mock.Mock()
    provider.board_numbers_with_statuses.return_value = set()  # #957 not Done
    provider.status_name.return_value = "Done"
    with mock.patch.object(disp.kanban, "list_tasks", return_value=cards):
        with mock.patch.object(disp.kanban, "complete", return_value=True) as mk:
            disp._global_reconcile_orphan_cards("slug", provider, dry_run=False)
    check("complete never called when issue still open", mk.call_count == 0)


def test_global_sweep_skips_terminal_cards():
    """Idempotency: cards already done/cancelled are skipped."""
    cards = [
        {"id": "t_dev", "title": "#957 fix", "status": "done"},
        {"id": "t_cancelled", "title": "#957 qa", "status": "cancelled"},
        {"id": "t_active", "title": "#957 security", "status": "todo"},
    ]
    provider = mock.Mock()
    provider.board_numbers_with_statuses.return_value = {957}
    provider.status_name.return_value = "Done"
    with mock.patch.object(disp.kanban, "list_tasks", return_value=cards):
        with mock.patch.object(disp.kanban, "complete", return_value=True) as mk:
            disp._global_reconcile_orphan_cards("slug", provider, dry_run=False)
    check("complete called only for non-terminal card", mk.call_count == 1)


def test_global_sweep_dry_run_no_mutation():
    """Dry-run logs but never completes cards."""
    cards = [
        {"id": "t_dev", "title": "#957 fix", "status": "todo"},
    ]
    provider = mock.Mock()
    provider.board_numbers_with_statuses.return_value = {957}
    provider.status_name.return_value = "Done"
    with mock.patch.object(disp.kanban, "list_tasks", return_value=cards):
        with mock.patch.object(disp.kanban, "complete") as mk:
            disp._global_reconcile_orphan_cards("slug", provider, dry_run=True)
    check("dry_run never calls complete", mk.call_count == 0)


# ── Runner ────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print("global orphan sweep tests (#957)")
    test_close_issue_tasks_word_boundary_matches_957()
    test_close_issue_tasks_word_boundary_rejects_9571()
    test_close_issue_tasks_word_boundary_rejects_1957()
    test_close_issue_tasks_body_fallback()
    test_global_sweep_completes_card_without_title_prefix()
    test_global_sweep_skips_open_issue()
    test_global_sweep_skips_terminal_cards()
    test_global_sweep_dry_run_no_mutation()
    print("all passed")

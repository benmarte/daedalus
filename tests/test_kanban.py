"""Tests for core/kanban.py — close_issue_tasks with blocked/review-required children (issue #25).

Exercises the task-tree walk that auto-completes blocked children with review-required
summaries when a parent issue closes:
  - single blocked child is completed with summary
  - multiple blocked children are all completed
  - no blocked children → no-op (only title-matched tasks completed)
  - already-done child is skipped (idempotent)
  - non-review-required blocked child is NOT completed by tree walk
  - dry_run logs but does not act
  - complete() accepts optional summary parameter
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

# Ensure project root is on sys.path BEFORE importing core.kanban
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import check  # noqa: E402,F401
from core import kanban  # noqa: E402
from core.kanban import close_issue_tasks, complete, show_card, _hk  # noqa: E402


# ── Helper to build fake task dicts ──────────────────────────────────────────


def _fake_task(tid, title, status="running", summary=None):
    """Build a minimal task dict as returned by list_tasks()."""
    return {"id": tid, "title": title, "status": status}


def _fake_show_card(tid, status="running", children=None, latest_summary=None):
    """Build a fake show_card() response."""
    return {
        "task": {"id": tid, "status": status},
        "children": children or [],
        "latest_summary": latest_summary,
    }


# ── Test: single blocked child completed with summary ────────────────────────


def test_close_issue_tasks_single_blocked_child():
    """When one child is blocked/review-required, it gets completed with summary."""
    tasks = [
        _fake_task("t_root", "#42 Some feature", status="running"),
    ]
    # Root has one child that's blocked with review-required
    root_card = _fake_show_card(
        "t_root", status="running",
        children=["t_child"],
    )
    child_card = _fake_show_card(
        "t_child", status="blocked",
        latest_summary="review-required: needs human review",
    )

    def mock_show_card(slug, tid):
        if tid == "t_root":
            return root_card
        if tid == "t_child":
            return child_card
        return None

    # Patch _hk for subprocess calls (list_tasks uses it)
    def mock_hk(args, timeout=60):
        if "list" in args and "--json" in args:
            import json
            return 0, json.dumps(tasks), ""
        if "complete" in args:
            return 0, "", ""
        return 0, "", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk), \
         mock.patch("core.kanban.show_card", side_effect=mock_show_card), \
         mock.patch("core.kanban.complete", return_value=True) as mock_complete:

        result = close_issue_tasks("test-board", 42, summary="closed: parent issue #42 merged and closed")

    check("returns 2 completed IDs (root + child)", len(result) == 2)
    check("root task completed", "t_root" in result)
    check("child task completed", "t_child" in result)
    check("complete() called 2 times", mock_complete.call_count == 2)
    if mock_complete.call_count >= 1:
        # Root task completed without summary (first pass)
        root_call = mock_complete.call_args_list[0]
        check("root complete called without summary arg", root_call == mock.call("test-board", "t_root"))
    if mock_complete.call_count >= 2:
        # Child completed with summary (second pass)
        child_call = mock_complete.call_args_list[1]
        check("child complete called with summary",
              child_call == mock.call("test-board", "t_child", summary="closed: parent issue #42 merged and closed"))


# ── Test: multiple blocked children ──────────────────────────────────────────


def test_close_issue_tasks_multiple_blocked_children():
    """All blocked/review-required children are completed."""
    tasks = [
        _fake_task("t_root", "#7 Another fix", status="running"),
    ]
    root_card = _fake_show_card(
        "t_root", status="running",
        children=["t_child1", "t_child2"],
    )
    child1_card = _fake_show_card(
        "t_child1", status="blocked",
        latest_summary="review-required: waiting for deploy",
    )
    child2_card = _fake_show_card(
        "t_child2", status="blocked",
        latest_summary="review-required: CI pending",
    )

    def mock_show_card(slug, tid):
        return {"t_root": root_card, "t_child1": child1_card, "t_child2": child2_card}.get(tid)

    def mock_hk(args, timeout=60):
        if "list" in args and "--json" in args:
            import json
            return 0, json.dumps(tasks), ""
        return 0, "", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk), \
         mock.patch("core.kanban.show_card", side_effect=mock_show_card), \
         mock.patch("core.kanban.complete", return_value=True) as mock_complete:

        result = close_issue_tasks("slug", 7, summary="closed: parent issue #7 merged and closed")

    check("returns 3 completed IDs", len(result) == 3)
    check("root + child1 + child2 all completed",
          set(result) == {"t_root", "t_child1", "t_child2"})
    check("complete() called 3 times", mock_complete.call_count == 3)


# ── Test: no blocked children (no-op for tree walk) ──────────────────────────


def test_close_issue_tasks_no_blocked_children():
    """When children exist but none are blocked/review-required, tree walk is no-op."""
    tasks = [
        _fake_task("t_root", "#99 Some work", status="running"),
    ]
    root_card = _fake_show_card(
        "t_root", status="running",
        children=["t_child_done"],  # child already done
    )
    child_card = _fake_show_card(
        "t_child_done", status="done",
        latest_summary="all tests passed",
    )

    def mock_show_card(slug, tid):
        return {"t_root": root_card, "t_child_done": child_card}.get(tid)

    def mock_hk(args, timeout=60):
        if "list" in args and "--json" in args:
            import json
            return 0, json.dumps(tasks), ""
        return 0, "", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk), \
         mock.patch("core.kanban.show_card", side_effect=mock_show_card), \
         mock.patch("core.kanban.complete", return_value=True) as mock_complete:

        result = close_issue_tasks("slug", 99, summary="closed: parent issue #99 merged and closed")

    check("returns 1 completed ID (root only)", len(result) == 1)
    check("only root completed", result == ["t_root"])
    check("complete() called only for root, not for done child", mock_complete.call_count == 1)


# ── Test: already-done child skipped (idempotent) ────────────────────────────


def test_close_issue_tasks_already_done_child_skipped():
    """Already-done children are not re-completed."""
    tasks = [
        _fake_task("t_root", "#5 work", status="done"),  # root already done
    ]

    def mock_hk(args, timeout=60):
        if "list" in args and "--json" in args:
            import json
            return 0, json.dumps(tasks), ""
        return 0, "", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk), \
         mock.patch("core.kanban.complete", return_value=True) as mock_complete:

        result = close_issue_tasks("slug", 5, summary="closed: parent issue #5 merged and closed")

    check("returns empty list (root already done)", len(result) == 0)
    check("complete() never called", mock_complete.call_count == 0)


# ── Test: non-review-required blocked child not completed by tree walk ───────


def test_close_issue_tasks_non_review_required_blocked_not_completed():
    """Blocked children without review-required summary are NOT completed by tree walk."""
    tasks = [
        _fake_task("t_root", "#10 blocked issue", status="running"),
    ]
    root_card = _fake_show_card(
        "t_root", status="running",
        children=["t_child_blocked"],
    )
    child_card = _fake_show_card(
        "t_child_blocked", status="blocked",
        latest_summary="waiting for human input: need API key",  # NOT review-required
    )

    def mock_show_card(slug, tid):
        return {"t_root": root_card, "t_child_blocked": child_card}.get(tid)

    def mock_hk(args, timeout=60):
        if "list" in args and "--json" in args:
            import json
            return 0, json.dumps(tasks), ""
        return 0, "", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk), \
         mock.patch("core.kanban.show_card", side_effect=mock_show_card), \
         mock.patch("core.kanban.complete", return_value=True) as mock_complete:

        result = close_issue_tasks("slug", 10, summary="closed: parent issue #10 merged and closed")

    check("returns 1 completed ID (root only)", len(result) == 1)
    check("only root completed", result == ["t_root"])
    check("complete() called only for root", mock_complete.call_count == 1)


# ── Test: no summary arg → skip tree walk ────────────────────────────────────


def test_close_issue_tasks_no_summary_no_child_completion():
    """Without summary arg, the tree walk is skipped (backward compatible)."""
    tasks = [
        _fake_task("t_root", "#15 feature", status="running"),
    ]
    root_card = _fake_show_card(
        "t_root", status="running",
        children=["t_child"],
    )
    child_card = _fake_show_card(
        "t_child", status="blocked",
        latest_summary="review-required: something",
    )

    def mock_show_card(slug, tid):
        return {"t_root": root_card, "t_child": child_card}.get(tid)

    def mock_hk(args, timeout=60):
        if "list" in args and "--json" in args:
            import json
            return 0, json.dumps(tasks), ""
        return 0, "", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk), \
         mock.patch("core.kanban.show_card", side_effect=mock_show_card) as mock_show, \
         mock.patch("core.kanban.complete", return_value=True) as mock_complete:

        result = close_issue_tasks("slug", 15)  # no summary arg

    check("returns 1 completed ID (root only)", len(result) == 1)
    check("complete() called only for root", mock_complete.call_count == 1)
    # show_card should not be called if summary="" — backward compat
    check("show_card NOT called (tree walk skipped)", mock_show.call_count == 0)


# ── Test: dry_run logs but does not act ───────────────────────────────────────


def test_close_issue_tasks_dry_run():
    """In dry_run mode, tasks are logged but not completed."""
    tasks = [
        _fake_task("t_root", "#3 dry run test", status="running"),
    ]
    root_card = _fake_show_card(
        "t_root", status="running",
        children=["t_child"],
    )
    child_card = _fake_show_card(
        "t_child", status="blocked",
        latest_summary="review-required: needs review",
    )

    def mock_show_card(slug, tid):
        return {"t_root": root_card, "t_child": child_card}.get(tid)

    def mock_hk(args, timeout=60):
        if "list" in args and "--json" in args:
            import json
            return 0, json.dumps(tasks), ""
        return 0, "", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk), \
         mock.patch("core.kanban.show_card", side_effect=mock_show_card), \
         mock.patch("core.kanban.complete", return_value=True) as mock_complete:

        result = close_issue_tasks(
            "slug", 3,
            summary="closed: parent issue #3 merged and closed",
            dry_run=True,
        )

    check("returns 2 IDs (what would be completed)", len(result) == 2)
    check("complete() NOT called in dry_run", mock_complete.call_count == 0)


# ── Test: complete() accepts summary parameter ──────────────────────────────


def test_complete_accepts_summary():
    """complete(slug, task_id, summary='...') passes --summary to hermes kanban."""
    def mock_hk(args, timeout=60):
        return 0, "", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk) as mock_hk_obj:
        complete("slug", "t_abc", summary="my summary")

    check("hermes kanban complete called", mock_hk_obj.call_count == 1)
    call_args = mock_hk_obj.call_args[0][0]
    check("--summary in args", "--summary" in call_args)
    check("summary value in args", "my summary" in call_args)
    check("task_id in args", "t_abc" in call_args)


# ── Test: complete() without summary (backward compat) ───────────────────────


def test_complete_no_summary():
    """complete(slug, task_id) still works without summary arg."""
    def mock_hk(args, timeout=60):
        return 0, "", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk) as mock_hk_obj:
        complete("slug", "t_abc")

    call_args = mock_hk_obj.call_args[0][0]
    check("--summary NOT in args when no summary given", "--summary" not in call_args)


# ── Run all tests ─────────────────────────────────────────────────────────────


if __name__ == "__main__":
    tests = [
        test_close_issue_tasks_single_blocked_child,
        test_close_issue_tasks_multiple_blocked_children,
        test_close_issue_tasks_no_blocked_children,
        test_close_issue_tasks_already_done_child_skipped,
        test_close_issue_tasks_non_review_required_blocked_not_completed,
        test_close_issue_tasks_no_summary_no_child_completion,
        test_close_issue_tasks_dry_run,
        test_complete_accepts_summary,
        test_complete_no_summary,
    ]
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        try:
            t()
        except Exception as e:
            conftest._failed += 1
            print(f"  FAIL  (raised {type(e).__name__}: {e})")

    print(f"\n{'='*60}")
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    if conftest._failed:
        sys.exit(1)

"""Tests for auto-promotion of sub-issues to project board after planner decomposition.

Covers issue #915: sub-issues are automatically added to the project board
with appropriate status after the planner decomposes an epic:
  - No dependencies → Ready label + board Ready status
  - With dependencies → board Backlog status (no Ready label)
Edge cases: board not configured, provider without board methods, failures.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _load_dispatch  # noqa: E402
from core import iterate  # noqa: E402
from core.iterate import _execute_planner_decompose  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_card(title: str = "#1 Some epic", body: str = "", issue_n: int = 1) -> dict:
    body_with_ref = body if f"#{issue_n}" in body else f"Issue #{issue_n}\n{body}"
    return {"id": "t_test", "title": title, "body": body_with_ref, "assignee": "planner-daedalus"}


def _make_issue_obj(number: int = 1, title: str = "Epic", body: str = "", labels=None):
    class _Obj:
        def as_dict(self_):
            return {"number": number, "title": title, "body": body,
                    "labels": labels or [], "url": f"https://github.com/x/y/issues/{number}"}
    return _Obj()


def _make_provider(
    *,
    issue_obj=None,
    comments=None,
    created_numbers=None,
    add_label_ret: bool = True,
    board_configured: bool = True,
    board_set_status_ret: bool = True,
    board_ensure_backlog_ret: bool = True,
):
    prov = mock.MagicMock()
    prov.get_issue.return_value = issue_obj
    prov.get_issue_comments.return_value = comments or []
    _created = iter(created_numbers or [101, 102, 103])
    prov.create_issue.side_effect = lambda *a, **k: next(_created, None)
    prov.post_issue_comment.return_value = True
    prov.add_label.return_value = add_label_ret

    prov.board_configured = mock.MagicMock(return_value=board_configured)
    prov.board_set_status = mock.MagicMock(return_value=board_set_status_ret)
    prov.board_ensure_backlog = mock.MagicMock(return_value=board_ensure_backlog_ret)

    return prov


def _run_decompose(
    body: str,
    created_numbers,
    *,
    board_configured: bool = True,
    board_set_status_ret: bool = True,
    board_ensure_backlog_ret: bool = True,
):
    issue = _make_issue_obj(1, "Epic", body)
    prov = _make_provider(
        issue_obj=issue,
        created_numbers=created_numbers,
        board_configured=board_configured,
        board_set_status_ret=board_set_status_ret,
        board_ensure_backlog_ret=board_ensure_backlog_ret,
    )

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body=body, issue_n=1), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    return prov


# ── Helpers to extract board calls from the mock ─────────────────────────────

def _ready_label_calls(prov):
    return [(n, lbl) for c in prov.add_label.call_args_list
            for n, lbl in [(c.args[0], c.args[1])] if lbl == "Ready"]


def _board_ready_calls(prov):
    return [(n, s) for c in prov.board_set_status.call_args_list
            for n, s in [(c.args[0], c.args[1])]]


def _backlog_calls(prov):
    return [c.args[0] for c in prov.board_ensure_backlog.call_args_list]


# ══════════════════════════════════════════════════════════════════════════════
# Test Case 1: Sequential decomposition — first sub-issue is Ready, rest Backlog
# ══════════════════════════════════════════════════════════════════════════════

def test_first_sub_issue_gets_ready_label_and_board_status():
    """First sub-issue (no sequential deps) gets Ready label + board Ready status."""
    body = "- [ ] Task A"
    prov = _run_decompose(body, [10])

    # First created number → no deps → Ready label + board Ready
    ready_labels = _ready_label_calls(prov)
    assert (10, "Ready") in ready_labels

    board_set = _board_ready_calls(prov)
    assert (10, "Ready") in board_set

    # No Backlog calls (only 1 sub-issue, no sequential deps)
    assert len(_backlog_calls(prov)) == 0


def test_three_checklist_items_sequential_ordering():
    """Three checklist items: first gets Ready, second and third get Backlog.
    
    The decomposition code creates sub-issues sequentially with each depending
    on all previously-created issues. So:
      - Task A (created=#10): created_numbers=[] → no deps → Ready
      - Task B (created=#11): created_numbers=[10] → depends_on: #10 → Backlog
      - Task C (created=#12): created_numbers=[10,11] → depends_on: #10, #11 → Backlog
    """
    body = "- [ ] Task A\n- [ ] Task B\n- [ ] Task C"
    prov = _run_decompose(body, [10, 11, 12])

    # Only #10 should be Ready (no deps in first position)
    ready_labels = _ready_label_calls(prov)
    ready_numbers = {n for n, _ in ready_labels}
    assert ready_numbers == {10}, f"Expected only #10 Ready, got {ready_numbers}"

    # Board Ready only for #10
    board_set = _board_ready_calls(prov)
    board_ready_numbers = {n for n, _ in board_set}
    assert board_ready_numbers == {10}

    # #11 and #12 should be in Backlog (they depend on prior sub-issues)
    backlog = set(_backlog_calls(prov))
    assert backlog == {11, 12}


def test_two_sub_issues_sequential_deps():
    """Two checklist items: first gets Ready, second gets Backlog."""
    body = "- [ ] Task A\n- [ ] Task B"
    prov = _run_decompose(body, [10, 11])

    ready_labels = _ready_label_calls(prov)
    assert (10, "Ready") in ready_labels
    assert (11, "Ready") not in ready_labels

    board_ready = _board_ready_calls(prov)
    assert (10, "Ready") in board_ready
    assert (11, "Ready") not in board_ready

    backlog = _backlog_calls(prov)
    assert backlog == [11]


# ══════════════════════════════════════════════════════════════════════════════
# Test Case 2: Board not configured
# ══════════════════════════════════════════════════════════════════════════════

def test_board_not_configured_only_labels_applied():
    """When board is not configured, only label is applied, no board operations."""
    body = "- [ ] Task A"
    prov = _run_decompose(body, [10], board_configured=False)

    # Label still applied
    ready_labels = _ready_label_calls(prov)
    assert (10, "Ready") in ready_labels

    # No board operations (board_set_status called only if board_configured() returns True)
    prov.board_set_status.assert_not_called()
    prov.board_ensure_backlog.assert_not_called()


def test_board_not_configured_backlog_not_called_for_dependent_sub_issues():
    """When board not configured, even dependent sub-issues don't get board ops."""
    body = "- [ ] Task A\n- [ ] Task B"
    prov = _run_decompose(body, [10, 11], board_configured=False)

    ready_labels = _ready_label_calls(prov)
    assert (10, "Ready") in ready_labels

    prov.board_set_status.assert_not_called()
    prov.board_ensure_backlog.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# Test Case 3: Error handling
# ══════════════════════════════════════════════════════════════════════════════

def test_board_set_status_failure_still_applies_label():
    """If board_set_status fails, Ready label is still applied (non-fatal)."""
    body = "- [ ] Task A"
    prov = _run_decompose(body, [10], board_set_status_ret=False)

    # Label still applied
    ready_labels = _ready_label_calls(prov)
    assert (10, "Ready") in ready_labels

    # board_set_status was called but returned False
    assert prov.board_set_status.called


def test_board_ensure_backlog_failure_doesnt_fail_decomposition():
    """If board_ensure_backlog fails, decomposition continues normally."""
    body = "- [ ] Task A\n- [ ] Task B"
    prov = _run_decompose(body, [10, 11], board_ensure_backlog_ret=False)

    # board_ensure_backlog was called for the dependent sub-issue
    backlog = _backlog_calls(prov)
    assert 11 in backlog


def test_board_set_status_exception_is_caught():
    """Exception in board_set_status is caught and logged, decomposition still succeeds."""
    body = "- [ ] Task A"
    prov = _run_decompose(body, [10])
    prov.board_set_status.side_effect = RuntimeError("Board API unavailable")

    ready_labels = _ready_label_calls(prov)
    assert (10, "Ready") in ready_labels


def test_board_ensure_backlog_exception_is_caught():
    """Exception in board_ensure_backlog is caught and logged, decomposition still succeeds."""
    body = "- [ ] Task A\n- [ ] Task B"
    prov = _run_decompose(body, [10, 11])
    prov.board_ensure_backlog.side_effect = RuntimeError("Board API unavailable")

    # Both labels created successfully
    ready_labels = _ready_label_calls(prov)
    assert (10, "Ready") in ready_labels


# ══════════════════════════════════════════════════════════════════════════════
# Test Case 4: Edge cases
# ══════════════════════════════════════════════════════════════════════════════

def test_no_sub_issues_created_no_board_operations():
    """When create_issue returns None, no board operations happen."""
    body = "- [ ] Task A"
    prov = _run_decompose(body, [None])

    assert len(_ready_label_calls(prov)) == 0
    assert len(_board_ready_calls(prov)) == 0
    assert len(_backlog_calls(prov)) == 0


def test_idempotency_marker_stops_all_operations():
    """When decomposed marker exists in parent, no board operations happen."""
    body = "<!-- daedalus:decomposed:1234567890 -->\n- [ ] Task A"
    prov = _run_decompose(body, [10])

    assert len(_ready_label_calls(prov)) == 0
    assert len(_board_ready_calls(prov)) == 0
    assert len(_backlog_calls(prov)) == 0


def test_legacy_marker_stops_all_operations():
    """When legacy sub-issues marker exists in parent comments, no board operations happen."""
    parent_comments = [{"body": "<!-- daedalus:sub-issues:[10,11] -->"}]
    body = "- [ ] Task A"
    issue = _make_issue_obj(1, "Epic", body)
    prov = _make_provider(
        issue_obj=issue,
        comments=parent_comments,
        created_numbers=[10, 11],
    )

    with mock.patch.object(iterate.kanban, "complete", return_value=True):
        _execute_planner_decompose(
            "slug", _make_card(body=body, issue_n=1), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    assert len(_ready_label_calls(prov)) == 0
    assert len(_board_ready_calls(prov)) == 0
    assert len(_backlog_calls(prov)) == 0


def test_single_sub_issue_gets_ready_no_backlog():
    """Single sub-issue in checklist gets Ready, no Backlog calls."""
    body = "- [ ] Only task"
    prov = _run_decompose(body, [10])

    ready_labels = _ready_label_calls(prov)
    assert (10, "Ready") in ready_labels

    board_ready = _board_ready_calls(prov)
    assert (10, "Ready") in board_ready

    backlog = _backlog_calls(prov)
    assert len(backlog) == 0


def test_four_sub_issues_tier_structure():
    """Four sub-issues: first Ready, rest all in Backlog (sequential tier ordering)."""
    body = "- [ ] Task A\n- [ ] Task B\n- [ ] Task C\n- [ ] Task D"
    prov = _run_decompose(body, [10, 11, 12, 13])

    ready_labels = _ready_label_calls(prov)
    ready_numbers = {n for n, _ in ready_labels}
    assert ready_numbers == {10}

    board_ready = _board_ready_calls(prov)
    board_ready_numbers = {n for n, _ in board_ready}
    assert board_ready_numbers == {10}

    backlog = set(_backlog_calls(prov))
    assert backlog == {11, 12, 13}

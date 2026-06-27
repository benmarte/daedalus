"""Tests for Phase 3 epic sub-issue creation (issue #151).

Tests cover _execute_planner_decompose() in core/iterate.py:
- Case A: parent has checklist items → one sub-issue per item (capped at 10)
- Case B: no checklist → 3 default sub-issues
- Idempotency via marker comment
- provider=None graceful skip
- dry_run: no mutations
- All provider/kanban side effects verified
- Routing: PLANNING COMPLETE → PLANNER_DECOMPOSE; other → PM_ROUTE
- Non-epic paths unchanged
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _load_dispatch  # noqa: E402
from core import iterate  # noqa: E402
from core.iterate import (  # noqa: E402
    PLANNER_DECOMPOSE,
    PM_ROUTE,
    APPROVE_ADVANCE,
    _execute_planner_decompose,
    _extract_sub_issues_from_body,
    _default_sub_issue_titles,
    classify_blocked,
)

disp = _load_dispatch()  # noqa: F841 (used by planner body tests)


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_card(title: str = "#1 Some epic", body: str = "", issue_n: int = 1) -> dict:
    # body must include a #N reference so _extract_issue_number_from_card can find it
    body_with_ref = body if f"#{issue_n}" in body else f"Issue #{issue_n}\n{body}"
    return {"id": "t_test", "title": title, "body": body_with_ref, "assignee": "planner-daedalus"}


def _make_issue_obj(number: int = 1, title: str = "Epic", body: str = "",
                    labels=None):
    class _Obj:
        def as_dict(self_):
            return {"number": number, "title": title, "body": body,
                    "labels": labels or [], "url": f"https://github.com/x/y/issues/{number}"}
    return _Obj()


def _make_provider(*, issue_obj=None, comments=None, created_numbers=None,
                   add_label_ret=True):
    prov = mock.MagicMock()
    prov.get_issue.return_value = issue_obj
    prov.get_issue_comments.return_value = comments or []
    _created = iter(created_numbers or [101, 102, 103])
    prov.create_issue.side_effect = lambda *a, **k: next(_created, None)
    prov.post_issue_comment.return_value = True
    prov.add_label.return_value = add_label_ret
    return prov


# ── _extract_sub_issues_from_body ────────────────────────────────────────────

def test_extract_checklist_dash_items():
    body = "- [ ] Task A\n- [ ] Task B\n- [x] Task C\n"
    assert _extract_sub_issues_from_body(body) == ["Task A", "Task B", "Task C"]


def test_extract_checklist_capped_at_10():
    body = "\n".join(f"- [ ] item {i}" for i in range(15))
    result = _extract_sub_issues_from_body(body)
    assert len(result) == 10


def test_extract_empty_body_returns_empty():
    assert _extract_sub_issues_from_body("") == []
    assert _extract_sub_issues_from_body("just prose, no checkboxes") == []


def test_extract_asterisk_and_plus_markers():
    body = "* [ ] Alpha\n+ [X] Beta\n"
    assert _extract_sub_issues_from_body(body) == ["Alpha", "Beta"]


# ── _default_sub_issue_titles ────────────────────────────────────────────────

def test_default_titles_returns_three():
    titles = _default_sub_issue_titles(42, "Big Feature")
    assert len(titles) == 3
    assert all("42" in t or "Big Feature" in t for t in titles)


# ── _execute_planner_decompose: checklist path ───────────────────────────────

def test_checklist_case_creates_sub_issues():
    body = "\n".join(f"- [ ] task {i}" for i in range(5))
    issue = _make_issue_obj(1, "Epic", body)
    prov = _make_provider(issue_obj=issue, created_numbers=[10, 11, 12, 13, 14])

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        ok = _execute_planner_decompose(
            "slug", _make_card(body=body), "o/r", "PLANNING COMPLETE: ready",
            provider=prov, workdir="/tmp"
        )

    assert ok is True
    assert prov.create_issue.call_count == 5


def test_checklist_capped_at_10():
    body = "\n".join(f"- [ ] task {i}" for i in range(15))
    issue = _make_issue_obj(1, "Epic", body)
    prov = _make_provider(issue_obj=issue, created_numbers=list(range(200, 215)))

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body=body), "o/r", "PLANNING COMPLETE",
            provider=prov, workdir="/tmp"
        )

    assert prov.create_issue.call_count == 10


# ── _execute_planner_decompose: default path ─────────────────────────────────

def test_no_checklist_creates_3_defaults():
    issue = _make_issue_obj(1, "My Epic", body="just prose")
    prov = _make_provider(issue_obj=issue, created_numbers=[20, 21, 22])

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        ok = _execute_planner_decompose(
            "slug", _make_card(body=""), "o/r", "PLANNING COMPLETE: go",
            provider=prov,
        )

    assert ok is True
    assert prov.create_issue.call_count == 3


# ── idempotency ───────────────────────────────────────────────────────────────

def test_idempotency_no_duplicate_on_retick():
    issue = _make_issue_obj(1, "Epic", "- [ ] t\n" * 5)
    marker_comment = {"body": "<!-- daedalus:sub-issues:[10] --> Daedalus created 1 sub-issue"}
    prov = _make_provider(issue_obj=issue, comments=[marker_comment])

    with mock.patch.object(iterate.kanban, "complete", return_value=True) as mk_complete:
        ok = _execute_planner_decompose(
            "slug", _make_card(), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    assert ok is True
    prov.create_issue.assert_not_called()
    mk_complete.assert_called_once()


# ── provider=None ─────────────────────────────────────────────────────────────

def test_provider_none_returns_false():
    ok = _execute_planner_decompose(
        "slug", _make_card(), "o/r", "PLANNING COMPLETE",
        provider=None,
    )
    assert ok is False


# ── dry_run ───────────────────────────────────────────────────────────────────

def test_dry_run_no_vcs_calls():
    issue = _make_issue_obj(1, "Epic", "- [ ] t\n" * 5)
    prov = _make_provider(issue_obj=issue)

    ok = _execute_planner_decompose(
        "slug", _make_card(body="- [ ] t\n" * 5), "o/r", "PLANNING COMPLETE",
        provider=prov, dry_run=True,
    )

    assert ok is True
    prov.create_issue.assert_not_called()
    prov.post_issue_comment.assert_not_called()
    prov.add_label.assert_not_called()


# ── side effects ──────────────────────────────────────────────────────────────

def test_epic_label_applied():
    issue = _make_issue_obj(1, "Epic", "- [ ] t\n" * 5)
    prov = _make_provider(issue_obj=issue, created_numbers=[10, 11, 12, 13, 14])

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body="- [ ] t\n" * 5), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    prov.add_label.assert_called_once_with(1, "epic")


def test_marker_comment_posted():
    issue = _make_issue_obj(1, "Epic", "- [ ] t\n" * 3)
    prov = _make_provider(issue_obj=issue, created_numbers=[10, 11, 12])

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body="- [ ] t\n" * 3), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    assert prov.post_issue_comment.call_count == 1
    posted_body = prov.post_issue_comment.call_args[0][1]
    assert "<!-- daedalus:sub-issues:" in posted_body


def test_kanban_triage_created_per_subissue():
    issue = _make_issue_obj(1, "Epic", "- [ ] t\n" * 3)
    prov = _make_provider(issue_obj=issue, created_numbers=[10, 11, 12])
    sub_issue = _make_issue_obj(10, "sub", "body")
    prov.get_issue.side_effect = lambda n: (issue if n == 1 else sub_issue)

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x") as mk_triage, \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body="- [ ] t\n" * 3), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    assert mk_triage.call_count == 3


def test_planner_card_completed():
    issue = _make_issue_obj(1, "Epic", "- [ ] t\n" * 3)
    prov = _make_provider(issue_obj=issue, created_numbers=[10, 11, 12])

    with mock.patch.object(iterate.kanban, "complete", return_value=True) as mk_complete, \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body="- [ ] t\n" * 3), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    mk_complete.assert_called_once()
    summary_arg = mk_complete.call_args[1].get("summary") or mk_complete.call_args[0][2]
    assert "Decomposed" in summary_arg


# ── routing ───────────────────────────────────────────────────────────────────

def test_planning_complete_prefix_routes_to_decompose():
    action = classify_blocked(
        "planner-daedalus",
        "PLANNING COMPLETE: ready for decomposition",
        ci_green=False,
    )
    assert action == PLANNER_DECOMPOSE


def test_other_planner_handoff_routes_to_pm():
    action = classify_blocked(
        "planner-daedalus",
        "planner-detected: #1 — some scope",
        ci_green=False,
    )
    assert action == PM_ROUTE


def test_non_epic_path_unchanged():
    action = classify_blocked(
        "reviewer-daedalus",
        "reviewed:approved",
        ci_green=True,
    )
    assert action == APPROVE_ADVANCE

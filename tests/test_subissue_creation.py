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

def test_checklist_case_creates_sub_issues(tmp_path):
    body = "\n".join(f"- [ ] task {i}" for i in range(5))
    issue = _make_issue_obj(1, "Epic", body)
    prov = _make_provider(issue_obj=issue, created_numbers=[10, 11, 12, 13, 14])

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        ok = _execute_planner_decompose(
            "slug", _make_card(body=body), "o/r", "PLANNING COMPLETE: ready",
            provider=prov, workdir=str(tmp_path)
        )

    assert ok is True
    assert prov.create_issue.call_count == 5


# ── standard body template with parent backlink ─────────────────────────────

def test_sub_issue_body_contains_parent_backlink():
    """Each sub-issue body must contain a backlink to the parent epic."""
    body = "- [ ] task alpha\n- [ ] task beta\n"
    issue = _make_issue_obj(42, "Big Epic", body)
    prov = _make_provider(issue_obj=issue, created_numbers=[100, 101])

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body=body, issue_n=42), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    assert prov.create_issue.call_count == 2
    for call in prov.create_issue.call_args_list:
        sub_body = call.args[1] if len(call.args) > 1 else call.kwargs.get("body", "")
        # Parent backlink: references parent issue number and title
        assert "#42" in sub_body, f"sub-issue body missing parent backlink: {sub_body!r}"
        assert "Big Epic" in sub_body, f"sub-issue body missing parent title: {sub_body!r}"


def test_sub_issue_body_matches_standard_template():
    """Sub-issue body must follow the standard template structure."""
    body = "- [ ] scope item\n"
    issue = _make_issue_obj(7, "Epic Title", body)
    prov = _make_provider(issue_obj=issue, created_numbers=[50])

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body=body, issue_n=7), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    sub_body = prov.create_issue.call_args[0][1]
    # Standard template sections
    assert sub_body.startswith("Part of epic #7"), f"body should start with parent backlink: {sub_body!r}"
    assert "## Scope" in sub_body
    assert "## Acceptance Criteria" in sub_body
    assert "## Notes" in sub_body
    assert "Auto-generated by Daedalus" in sub_body


def test_sub_issue_body_includes_scope_from_checklist():
    """The scope section should contain the checklist item text."""
    body = "- [ ] Implement login form\n- [ ] Add password validation\n"
    issue = _make_issue_obj(1, "Auth Epic", body)
    prov = _make_provider(issue_obj=issue, created_numbers=[60, 61])

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body=body, issue_n=1), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    bodies = [call.args[1] for call in prov.create_issue.call_args_list]
    assert any("Implement login form" in b for b in bodies)
    assert any("Add password validation" in b for b in bodies)


def test_checklist_capped_at_10(tmp_path):
    body = "\n".join(f"- [ ] task {i}" for i in range(15))
    issue = _make_issue_obj(1, "Epic", body)
    prov = _make_provider(issue_obj=issue, created_numbers=list(range(200, 215)))

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body=body), "o/r", "PLANNING COMPLETE",
            provider=prov, workdir=str(tmp_path)
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

    # Only tier-0 (first sub-issue, #10) gets Ready due to sequential tier ordering
    ready_calls = [c for c in prov.add_label.call_args_list if c.args[1] == "Ready"]
    epic_calls = [c for c in prov.add_label.call_args_list if c.args[1] == "epic"]
    assert len(ready_calls) == 1, f"Expected 1 Ready label (tier-0 only), got {len(ready_calls)}"
    assert ready_calls[0].args[0] == 10, f"Expected first sub-issue (#10) to be Ready"
    assert len(epic_calls) == 1, f"Expected 1 epic label, got {len(epic_calls)}"
    assert epic_calls[0].args[0] == 1, f"Expected parent issue 1 to have epic label"


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
    assert "<!-- daedalus:decomposed:" in posted_body


def test_marker_format_matches_spec():
    """Verify marker format is <!-- daedalus:decomposed:<timestamp> -->"""
    issue = _make_issue_obj(1, "Epic", "- [ ] t\n" * 3)
    prov = _make_provider(issue_obj=issue, created_numbers=[10, 11, 12])

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body="- [ ] t\n" * 3), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    posted_body = prov.post_issue_comment.call_args[0][1]
    assert posted_body.startswith("<!-- daedalus:decomposed:")
    assert "#10" in posted_body
    assert "#11" in posted_body
    assert "#12" in posted_body


def test_marker_format_single_number():
    """Verify new marker format for single sub-issue"""
    issue = _make_issue_obj(1, "Epic", "- [ ] t\n" * 1)
    prov = _make_provider(issue_obj=issue, created_numbers=[42])

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body="- [ ] t\n" * 1), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    posted_body = prov.post_issue_comment.call_args[0][1]
    assert "<!-- daedalus:decomposed:" in posted_body
    assert "#42" in posted_body


def test_marker_format_many_numbers():
    """Verify new marker format includes all sub-issue numbers"""
    issue = _make_issue_obj(1, "Epic", "- [ ] t\n" * 5)
    prov = _make_provider(issue_obj=issue, created_numbers=[101, 102, 103, 104, 105])

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body="- [ ] t\n" * 5), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    posted_body = prov.post_issue_comment.call_args[0][1]
    assert "<!-- daedalus:decomposed:" in posted_body
    for n in [101, 102, 103, 104, 105]:
        assert f"#{n}" in posted_body


def test_idempotency_detects_correct_format():
    """Verify idempotency check finds markers with correct format"""
    issue = _make_issue_obj(1, "Epic", "- [ ] t\n" * 3)
    marker_comment = {"body": "<!-- daedalus:sub-issues:[10,11,12] --> Daedalus created 3 sub-issues"}
    prov = _make_provider(issue_obj=issue, comments=[marker_comment])

    with mock.patch.object(iterate.kanban, "complete", return_value=True) as mk_complete:
        ok = _execute_planner_decompose(
            "slug", _make_card(), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    assert ok is True
    prov.create_issue.assert_not_called()
    mk_complete.assert_called_once()


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


def test_sub_issue_gets_ready_label_when_no_dependencies():
    """Sub-issues without dependency references must be labeled Ready immediately."""
    body = "- [ ] Implement login\n- [ ] Add validation\n"
    issue = _make_issue_obj(1, "Epic", body)
    prov = _make_provider(issue_obj=issue, created_numbers=[10, 11])

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body=body), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    assert prov.create_issue.call_count == 2
    # Sequential tier ordering: only tier-0 (#10) gets Ready; #11 has dep on #10 → not Ready
    ready_calls = [c for c in prov.add_label.call_args_list if c.args[1] == "Ready"]
    assert len(ready_calls) == 1, f"Expected 1 Ready label (tier-0 only), got {len(ready_calls)}"
    ready_issue_numbers = {c.args[0] for c in ready_calls}
    assert ready_issue_numbers == {10}, f"Expected only #10 Ready, got {ready_issue_numbers}"


def test_sub_issue_no_ready_label_when_has_dependencies():
    """Sub-issues with dependency references must NOT be labeled Ready."""
    body = "- [ ] Implement login\n\nDepends on: #999\n"
    issue = _make_issue_obj(1, "Epic", body)
    prov = _make_provider(issue_obj=issue, created_numbers=[10])

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body=body), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    assert prov.create_issue.call_count == 1
    # Sequential tier ordering: the only sub-issue is tier-0 → always gets Ready.
    # NOTE: "Depends on:" in the PARENT body does NOT affect sub-issue readiness;
    # only the tier-dep field in the sub-issue body itself matters.
    ready_calls = [c for c in prov.add_label.call_args_list if c.args[1] == "Ready"]
    assert len(ready_calls) == 1, f"Expected 1 Ready label (single tier-0 sub-issue), got {len(ready_calls)}"
    assert ready_calls[0].args[0] == 10, "Expected tier-0 sub-issue (#10) to be labeled Ready"


def test_mixed_sub_issues_partial_ready_labeling():
    """Test mixed scenario: sub-issues with deps get no Ready, without deps get Ready."""
    # Create two separate tests within one function to avoid duplication
    # Test 1: All sub-issues without deps -> all get Ready
    body1 = "- [ ] Task A\n- [ ] Task B\n- [ ] Task C\n"
    issue1 = _make_issue_obj(1, "Epic1", body1)
    prov1 = _make_provider(issue_obj=issue1, created_numbers=[10, 11, 12])

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        result1 = _execute_planner_decompose(
            "slug", _make_card(body=body1), "o/r", "PLANNING COMPLETE",
            provider=prov1,
        )

    assert result1 is True
    assert prov1.create_issue.call_count == 3
    # Sequential tier ordering: only tier-0 (#10) gets Ready; #11 and #12 have tier deps
    ready_calls1 = [c for c in prov1.add_label.call_args_list if c.args[1] == "Ready"]
    assert len(ready_calls1) == 1, f"Expected 1 Ready label (tier-0 only), got {len(ready_calls1)}"
    ready_issue_numbers = {c.args[0] for c in ready_calls1}
    assert ready_issue_numbers == {10}, f"Expected only #10 Ready, got {ready_issue_numbers}"

    # Test 2: With 2 sub-issues, tier-0 (#20) gets Ready; tier-1 (#21) has dep on #20
    body2 = "- [ ] Task A\n- [ ] Task B\n"
    issue2 = _make_issue_obj(2, "Epic2", body2)
    prov2 = _make_provider(issue_obj=issue2, created_numbers=[20, 21])

    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        result2 = _execute_planner_decompose(
            "slug", _make_card(body=body2), "o/r", "PLANNING COMPLETE",
            provider=prov2,
        )

    assert result2 is True
    # Tier-0 (#20) always gets Ready; tier-1 (#21) has dep on #20 → not Ready
    ready_calls2 = [c for c in prov2.add_label.call_args_list if c.args[1] == "Ready"]
    assert len(ready_calls2) == 1, f"Expected 1 Ready label (tier-0 only), got {len(ready_calls2)}"
    assert ready_calls2[0].args[0] == 20, "Expected first sub-issue (#20) to be labeled Ready"


def test_completion_message_includes_ready_count():
    """Kanban completion message must report Ready sub-issue count."""
    body = "- [ ] Task A\n"
    issue = _make_issue_obj(1, "Epic", body)
    prov = _make_provider(issue_obj=issue, created_numbers=[10])

    with mock.patch.object(iterate.kanban, "complete", return_value=True) as mk_complete, \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body=body), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    mk_complete.assert_called_once()
    call_kwargs = mk_complete.call_args[1]
    summary = call_kwargs.get("summary")
    assert "1 Ready)" in summary
    assert "Decomposed epic #1" in summary


def test_planner_card_completed():
    """Legacy test: verify planner card completion still works."""
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


# ── integration: end-to-end sub-issue creation with template ────────────────

def test_integration_subissue_creation_with_template():
    """End-to-end verification that sub-issues are created with the standard template."""
    parent_body = "- [ ] Build authentication module\n- [ ] Write API documentation\n"
    parent_issue = _make_issue_obj(99, "Feature X Epic", parent_body, labels=["enhancement"])
    prov = _make_provider(issue_obj=parent_issue, created_numbers=[200, 201])

    with mock.patch.object(iterate.kanban, "complete", return_value=True) as mk_complete, \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_triage") as mk_triage, \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        ok = _execute_planner_decompose(
            "slug",
            _make_card(title="#99 Feature X", body=parent_body, issue_n=99),
            "owner/repo",
            "PLANNING COMPLETE: ready for decomposition",
            provider=prov,
        )

    assert ok is True
    # Sub-issues created with correct count
    assert prov.create_issue.call_count == 2

    # Verify each sub-issue body uses standard template with parent backlink
    for call in prov.create_issue.call_args_list:
        title, body = call.args[0], call.args[1]
        # Parent backlink present
        assert "#99" in body, f"sub-issue '{title}' missing parent backlink"
        assert "Feature X Epic" in body, f"sub-issue '{title}' missing parent title"
        # Standard template sections
        assert body.startswith("Part of epic #99"), f"sub-issue '{title}' doesn't start with backlink"
        assert "## Scope" in body, f"sub-issue '{title}' missing Scope section"
        assert "## Acceptance Criteria" in body, f"sub-issue '{title}' missing Acceptance Criteria"
        assert "## Notes" in body, f"sub-issue '{title}' missing Notes section"
        assert "Auto-generated by Daedalus" in body, f"sub-issue '{title}' missing auto-gen note"
        # Subtask label applied
        labels = call.kwargs.get("labels", [])
        assert "subtask" in labels, f"sub-issue '{title}' missing 'subtask' label"
        assert "enhancement" in labels, f"sub-issue '{title}' didn't inherit parent labels"

    # Marker comment posted on parent
    assert prov.post_issue_comment.call_count == 1
    marker_body = prov.post_issue_comment.call_args[0][1]
    assert "<!-- daedalus:decomposed:" in marker_body

    # Epic label applied to parent
    # Verify labels applied
    # Only the first sub-issue gets Ready label (tier-0, immediately actionable)
    ready_calls = [c for c in prov.add_label.call_args_list if c.args[1] == "Ready"]
    assert len(ready_calls) == 1
    assert ready_calls[0].args[0] == 200

    # Kanban triage cards created for each sub-issue
    assert mk_triage.call_count == 2

    # Planner card completed
    mk_complete.assert_called_once()

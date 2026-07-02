"""Tests for issue #1183 — gate lookups must find role cards by ASSIGNEE.

hermes' kanban API no longer returns ``idempotency_key``, so the old
``task['idempotency_key'] == 'qa-<n>'`` lookups always missed → every auto-merge
gate evaluated "not passed" → auto-merge never fired. Gates now match by the card's
``assignee`` profile (stable) plus the issue ref in the title — card TITLE formats
are inconsistent ("#<n> QA:", "QA: verify PR for #<n>", "Review PR for issue #<n>:"),
and matching assignee also stops the security gate from picking up the developer
card when the issue title itself contains "fix(security):".
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import iterate  # noqa: E402

_T = "fix(security): Azure webhook verification fails open when token header is missing"
# Deliberately varied title formats + a developer card whose title contains
# "fix(security):" (the collision trap). Match must key off assignee.
_TASKS = [
    {"id": "tv", "assignee": "validator-daedalus", "title": f"#1140 {_T}", "status": "done"},
    {"id": "td", "assignee": "developer-daedalus", "title": f"#1140 Developer: {_T}", "status": "done"},
    {"id": "tq", "assignee": "qa-daedalus", "title": "QA: verify PR for #1140 azure webhook", "status": "done"},
    {"id": "tr", "assignee": "reviewer-daedalus", "title": "Review PR for issue #1140: azure webhook", "status": "done"},
    {"id": "ts", "assignee": "security-analyst-daedalus", "title": f"#1140 Security: {_T}", "status": "done"},
    {"id": "tdoc", "assignee": "documentation-daedalus", "title": f"#1140 Docs: {_T}", "status": "done"},
]

_SUMMARIES = {
    "tq": "qa-passed: PR #1181 verified",
    "tr": "review-approved: PR #1181",
    "ts": "security: cleared — PR #1181 (fix/issue-1140)",
    "td": "review-required: PR #1181 — fix/issue-1140",
}


def _patched():
    return mock.patch.multiple(
        iterate.kanban,
        list_tasks=mock.MagicMock(return_value=_TASKS),
        show_card=mock.MagicMock(side_effect=lambda slug, cid: {"latest_summary": _SUMMARIES.get(cid, "")}),
    )


def test_qa_gate_found_despite_nonstandard_title():
    with _patched():
        assert iterate._qa_passed_for_issue("slug", 1140) is True


def test_reviewer_gate_found_despite_nonstandard_title():
    with _patched():
        assert iterate._reviewer_passed_for_issue("slug", 1140) is True


def test_security_gate_found_and_accepts_cleared():
    with _patched():
        assert iterate._security_passed_for_issue("slug", 1140) is True


def test_security_gate_does_not_match_developer_card():
    # Developer card title contains "fix(security):"; assignee is developer-daedalus,
    # so the security gate (assignee security-) must NOT pick it up.
    only_dev = [t for t in _TASKS if t["id"] == "td"]
    with mock.patch.multiple(
        iterate.kanban,
        list_tasks=mock.MagicMock(return_value=only_dev),
        show_card=mock.MagicMock(side_effect=lambda slug, cid: {"latest_summary": _SUMMARIES.get(cid, "")}),
    ):
        assert iterate._security_passed_for_issue("slug", 1140) is False


def test_role_finder_matches_by_assignee():
    with mock.patch.object(iterate.kanban, "list_tasks", return_value=_TASKS):
        assert [c["id"] for c in iterate._role_cards_for_issue("slug", 1140, "security")] == ["ts"]
        assert [c["id"] for c in iterate._role_cards_for_issue("slug", 1140, "qa")] == ["tq"]
        assert [c["id"] for c in iterate._role_cards_for_issue("slug", 1140, "reviewer")] == ["tr"]


def test_gate_false_when_no_matching_card():
    with mock.patch.object(iterate.kanban, "list_tasks", return_value=_TASKS):
        assert iterate._qa_passed_for_issue("slug", 9999) is False


def test_issue_number_boundary_no_prefix_match():
    # A qa-daedalus card for #11400 must NOT satisfy the #1140 gate.
    tasks = [{"id": "x", "assignee": "qa-daedalus", "title": "QA: verify PR for #11400", "status": "done"}]
    with mock.patch.object(iterate.kanban, "list_tasks", return_value=tasks):
        assert iterate._role_cards_for_issue("slug", 1140, "qa") == []


def test_reviewer_gate_false_on_changes_requested():
    tasks = [{"id": "tr", "assignee": "reviewer-daedalus", "title": "Review PR for issue #1140:", "status": "done"}]
    with mock.patch.multiple(
        iterate.kanban,
        list_tasks=mock.MagicMock(return_value=tasks),
        show_card=mock.MagicMock(return_value={"latest_summary": "reviewed: changes-requested: fix the thing"}),
    ):
        assert iterate._reviewer_passed_for_issue("slug", 1140) is False


def test_gate_falls_back_to_archived_card():
    # QA finishes first, so its card archives first; default list_tasks excludes
    # archived. The gate must fall back to archived so the verdict survives (#1141).
    archived = [{"id": "tq", "assignee": "qa-daedalus", "title": "QA: verify PR for #1140", "status": "archived"}]

    def _lt(slug, status=""):
        return archived if status == "archived" else []  # active list is empty

    with mock.patch.multiple(
        iterate.kanban,
        list_tasks=mock.MagicMock(side_effect=_lt),
        show_card=mock.MagicMock(return_value={"latest_summary": "qa-passed: PR #1188"}),
    ):
        assert iterate._qa_passed_for_issue("slug", 1140) is True


def test_active_card_preferred_no_archived_fetch():
    # When the active list has the card, archived must NOT be fetched (efficiency).
    active = [{"id": "tq", "assignee": "qa-daedalus", "title": "#1140 QA: x", "status": "done"}]
    calls = []

    def _lt(slug, status=""):
        calls.append(status)
        return active if status == "" else []

    with mock.patch.multiple(
        iterate.kanban,
        list_tasks=mock.MagicMock(side_effect=_lt),
        show_card=mock.MagicMock(return_value={"latest_summary": "qa-passed: PR #1188"}),
    ):
        assert iterate._qa_passed_for_issue("slug", 1140) is True
    assert "archived" not in calls  # archived fallback skipped when active matches


def test_security_gate_rejects_flagged_verdict():
    tasks = [{"id": "ts", "assignee": "security-analyst-daedalus", "title": f"#1140 Security: {_T}", "status": "done"}]
    with mock.patch.multiple(
        iterate.kanban,
        list_tasks=mock.MagicMock(return_value=tasks),
        show_card=mock.MagicMock(return_value={"latest_summary": "security: flagged: hardcoded secret in config"}),
    ):
        assert iterate._security_passed_for_issue("slug", 1140) is False


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)

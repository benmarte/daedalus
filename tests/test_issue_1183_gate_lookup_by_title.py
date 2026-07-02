"""Tests for issue #1183 — gate lookups must find role cards by title.

hermes' kanban API no longer returns ``idempotency_key``, so the old
``task['idempotency_key'] == 'qa-<n>'`` lookups always missed → every auto-merge
gate evaluated "not passed" → auto-merge never fired. Gates now match by title
(``#<n> <Role>:``), anchored after the ``#<n>`` ref so the issue's own title text
(e.g. ``fix(security):``) can't false-match another role's card.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import iterate  # noqa: E402

# Realistic board: role cards titled "#1140 <Role>: <issue title>" where the
# issue title itself contains "fix(security):" — the collision trap.
_TITLE = "fix(security): Azure webhook verification fails open when token header is missing"
_TASKS = [
    {"id": "tv", "title": f"#1140 {_TITLE}", "status": "done"},              # validator
    {"id": "td", "title": f"#1140 Developer: {_TITLE}", "status": "done"},   # developer
    {"id": "tq", "title": f"#1140 QA: {_TITLE}", "status": "done"},
    {"id": "tr", "title": f"#1140 Reviewer: {_TITLE}", "status": "done"},
    {"id": "ts", "title": f"#1140 Security: {_TITLE}", "status": "done"},
    {"id": "tdoc", "title": f"#1140 Docs: {_TITLE}", "status": "done"},
]

_SUMMARIES = {
    "tq": "qa-passed: PR #1181",
    "tr": "review-approved: PR #1181",
    "ts": "security: cleared — no findings for PR #1181",
    "td": "review-required: PR #1181 — fix/issue-1140",
}


def _patched():
    return mock.patch.multiple(
        iterate.kanban,
        list_tasks=mock.MagicMock(return_value=_TASKS),
        show_card=mock.MagicMock(side_effect=lambda slug, cid: {"latest_summary": _SUMMARIES.get(cid, "")}),
    )


def test_qa_gate_found_by_title():
    with _patched():
        assert iterate._qa_passed_for_issue("slug", 1140) is True


def test_reviewer_gate_found_by_title():
    with _patched():
        assert iterate._reviewer_passed_for_issue("slug", 1140) is True


def test_security_gate_found_by_title():
    with _patched():
        assert iterate._security_passed_for_issue("slug", 1140) is True


def test_security_gate_does_not_match_developer_card():
    # The developer card title contains "fix(security):" — the security gate must
    # NOT pick it up. Its summary is a review-required handoff, not a clearance.
    only_dev = [t for t in _TASKS if t["id"] == "td"]
    with mock.patch.multiple(
        iterate.kanban,
        list_tasks=mock.MagicMock(return_value=only_dev),
        show_card=mock.MagicMock(side_effect=lambda slug, cid: {"latest_summary": _SUMMARIES.get(cid, "")}),
    ):
        # No Security: card present → gate must be False (not fooled by fix(security):).
        assert iterate._security_passed_for_issue("slug", 1140) is False


def test_role_card_finder_anchors_after_issue_ref():
    with mock.patch.object(iterate.kanban, "list_tasks", return_value=_TASKS):
        sec = iterate._role_cards_for_issue("slug", 1140, "security")
        assert [c["id"] for c in sec] == ["ts"]  # only the Security: card
        qa = iterate._role_cards_for_issue("slug", 1140, "qa")
        assert [c["id"] for c in qa] == ["tq"]


def test_gate_false_when_no_matching_card():
    with mock.patch.object(iterate.kanban, "list_tasks", return_value=_TASKS):
        assert iterate._qa_passed_for_issue("slug", 9999) is False


def test_issue_number_boundary_no_prefix_match():
    # #114 must not match #1140's cards, and vice-versa.
    tasks = [{"id": "x", "title": "#11400 QA: other", "status": "done"}]
    with mock.patch.object(iterate.kanban, "list_tasks", return_value=tasks):
        assert iterate._role_cards_for_issue("slug", 1140, "qa") == []


def test_reviewer_gate_false_on_changes_requested():
    tasks = [{"id": "tr", "title": f"#1140 Reviewer: {_TITLE}", "status": "done"}]
    with mock.patch.multiple(
        iterate.kanban,
        list_tasks=mock.MagicMock(return_value=tasks),
        show_card=mock.MagicMock(return_value={"latest_summary": "reviewed: changes-requested: fix the thing"}),
    ):
        assert iterate._reviewer_passed_for_issue("slug", 1140) is False


def test_security_gate_accepts_bare_cleared_verdict():
    # The security agent emits "security: cleared" (no "no findings" text) — must pass.
    tasks = [{"id": "ts", "title": f"#1140 Security: {_TITLE}", "status": "done"}]
    with mock.patch.multiple(
        iterate.kanban,
        list_tasks=mock.MagicMock(return_value=tasks),
        show_card=mock.MagicMock(return_value={"latest_summary": "security: cleared — PR #1181 (fix/issue-1140)"}),
    ):
        assert iterate._security_passed_for_issue("slug", 1140) is True


def test_security_gate_rejects_flagged_verdict():
    tasks = [{"id": "ts", "title": f"#1140 Security: {_TITLE}", "status": "done"}]
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

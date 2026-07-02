"""Regression tests for issue #1161 — auto-adopt PM spec comment on stale cards.

A hermes premature-completion bug can complete the PM kanban card with an empty
summary even though the PM posted a valid ``## Implementation Spec`` comment on
the GitHub issue (observed live on #1160). The dispatcher's stale paths must
self-heal by adopting that comment as the card's ``SPEC:`` summary instead of
retrying to the cap and stalling. The retry-cap notification must only fire
when no spec comment exists either.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock


def _load_dispatch():
    p = Path(__file__).resolve().parent.parent / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp_1161", str(p))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load dispatch module from {p}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _minimal_resolved():
    return {
        "platform": "github",
        "repo": "owner/repo",
        "workdir": "/tmp",
        "notifications": None,
    }


_SPEC_COMMENT_BODY = (
    "**Agent: project-manager**\n\n"
    "## Implementation Spec\n\n"
    "Auto-adopt the spec comment when the PM card lacks a SPEC: summary.\n\n"
    "### Root Cause\nDetails here.\n"
)


class FakeProvider:
    """Provider stub with configurable issue comments."""

    def __init__(self, issue_comments=None):
        self.issue_comments = issue_comments or []
        self.posted: list[tuple[int, str]] = []

    def post_issue_comment(self, issue_number: int, body: str) -> bool:
        self.posted.append((issue_number, body))
        return True

    def get_issue_state(self, issue_number: int) -> str:
        return "open"

    def get_issue_comments(self, issue_number: int) -> list:
        return self.issue_comments


class TestPmRoleSlug(unittest.TestCase):
    """Attribution marker derivation must keep multi-word roles intact."""

    def setUp(self):
        self.disp = _load_dispatch()

    def test_project_manager_daedalus_keeps_full_role(self):
        # split("-")[0] would yield "project" — the #1161 gotcha.
        self.assertEqual(
            self.disp._pm_role_slug("project-manager-daedalus"), "project-manager"
        )

    def test_single_segment_profile_unchanged(self):
        self.assertEqual(self.disp._pm_role_slug("pm"), "pm")

    def test_validator_daedalus(self):
        self.assertEqual(self.disp._pm_role_slug("validator-daedalus"), "validator")


class TestPmSpecComment(unittest.TestCase):
    """_pm_spec_comment scans issue comments for an attributed spec."""

    def setUp(self):
        self.disp = _load_dispatch()

    def test_returns_head_from_implementation_spec_comment(self):
        provider = FakeProvider([{"body": _SPEC_COMMENT_BODY}])
        head = self.disp._pm_spec_comment(provider, 42, "project-manager-daedalus")
        self.assertIn("Auto-adopt the spec comment", head)

    def test_matches_spec_dash_issue_heading_variant(self):
        # Live PM comments also use "## Spec — Issue #NNN: ..." (seen on #1161).
        body = (
            "**Agent: project-manager**\n\n"
            "## Spec — Issue #42: fix the thing\n\n"
            "### Root Cause\nThe root cause line.\n"
        )
        provider = FakeProvider([{"body": body}])
        head = self.disp._pm_spec_comment(provider, 42, "project-manager-daedalus")
        self.assertTrue(head)

    def test_no_provider_returns_empty(self):
        self.assertEqual(
            self.disp._pm_spec_comment(None, 42, "project-manager-daedalus"), ""
        )

    def test_provider_exception_returns_empty(self):
        class Boom:
            def get_issue_comments(self, n):
                raise RuntimeError("api down")

        self.assertEqual(
            self.disp._pm_spec_comment(Boom(), 42, "project-manager-daedalus"), ""
        )

    def test_unattributed_comment_ignored(self):
        provider = FakeProvider(
            [{"body": "## Implementation Spec\n\nNo attribution marker here.\n"}]
        )
        self.assertEqual(
            self.disp._pm_spec_comment(provider, 42, "project-manager-daedalus"), ""
        )

    def test_attributed_comment_without_spec_heading_ignored(self):
        provider = FakeProvider(
            [{"body": "**Agent: project-manager**\n\n_Completed — no summary._\n"}]
        )
        self.assertEqual(
            self.disp._pm_spec_comment(provider, 42, "project-manager-daedalus"), ""
        )

    def test_head_truncated_to_200_chars(self):
        long_line = "x" * 500
        body = f"**Agent: project-manager**\n\n## Implementation Spec\n\n{long_line}\n"
        provider = FakeProvider([{"body": body}])
        head = self.disp._pm_spec_comment(provider, 42, "project-manager-daedalus")
        self.assertLessEqual(len(head), 200)

    def test_newest_matching_comment_wins(self):
        provider = FakeProvider(
            [
                {
                    "body": "**Agent: project-manager**\n\n## Implementation Spec\n\nOld spec.\n"
                },
                {
                    "body": "**Agent: project-manager**\n\n## Implementation Spec\n\nNew spec.\n"
                },
            ]
        )
        head = self.disp._pm_spec_comment(provider, 42, "project-manager-daedalus")
        self.assertIn("New spec", head)


def _stale_board_tasks():
    """A CONFIRMED validator card plus one stale done PM card for issue #42."""
    return [
        {
            "title": "#42 fix bug",
            "assignee": "validator-daedalus",
            "status": "done",
            "id": "t_v42",
            "summary": "CONFIRMED: valid issue",
        },
        {
            "title": "#42 fix bug",
            "assignee": "project-manager-daedalus",
            "status": "done",
            "id": "t_pm42",
            "summary": "",
        },
    ]


class TestAdoptOnPrimaryStalePath(unittest.TestCase):
    """Primary stale path (_check_confirmed_validators, CONFIRMED summary)."""

    def setUp(self):
        self.disp = _load_dispatch()

    def _run(self, provider, *, stale_count=4, dry_run=False, tasks=None):
        calls = {}
        fake_tasks = tasks if tasks is not None else _stale_board_tasks()

        def fake_summary(task, slug):
            return task.get("summary") or ""

        with mock.patch.object(
            self.disp.kanban, "list_tasks", return_value=fake_tasks
        ), mock.patch.object(
            self.disp.kanban, "show_card", return_value=None
        ), mock.patch.object(
            self.disp, "_get_task_summary", side_effect=fake_summary
        ), mock.patch.object(
            self.disp,
            "_pm_task_state",
            return_value=("stale", stale_count),
        ), mock.patch.object(
            self.disp.kanban, "edit_summary", return_value=True
        ) as edit, mock.patch.object(
            self.disp.kanban, "create_task", return_value="t_new"
        ) as create, mock.patch.object(
            self.disp, "_send_retry_cap_notification"
        ) as cap_notif, mock.patch.object(
            self.disp, "_send_retry_attempt_notification"
        ) as retry_notif, mock.patch.object(
            self.disp, "_has_notified_block", return_value=False
        ), mock.patch.object(self.disp, "_mark_notified_block"):
            self.disp._check_confirmed_validators(
                "slug",
                "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                3,
                "/tmp",
                "",
                "main",
                "github",
                provider=provider,
                resolved=_minimal_resolved(),
                dry_run=dry_run,
            )
        calls["edit"] = edit
        calls["create"] = create
        calls["cap_notif"] = cap_notif
        calls["retry_notif"] = retry_notif
        return calls

    def test_spec_comment_adopted_at_cap_no_notification(self):
        provider = FakeProvider([{"body": _SPEC_COMMENT_BODY}])
        calls = self._run(provider, stale_count=4)
        calls["edit"].assert_called_once()
        _slug, tid, summary = calls["edit"].call_args[0]
        self.assertEqual(tid, "t_pm42")
        self.assertTrue(
            summary.startswith("SPEC: (adopted from issue comment)"), summary
        )
        calls["cap_notif"].assert_not_called()
        calls["retry_notif"].assert_not_called()
        calls["create"].assert_not_called()
        self.assertEqual(provider.posted, [], "no retry-cap GitHub comment expected")

    def test_spec_comment_adopted_under_cap_no_retry_card(self):
        provider = FakeProvider([{"body": _SPEC_COMMENT_BODY}])
        calls = self._run(provider, stale_count=1)
        calls["edit"].assert_called_once()
        calls["create"].assert_not_called()
        calls["retry_notif"].assert_not_called()

    def test_no_spec_comment_at_cap_keeps_notification(self):
        provider = FakeProvider([])  # no comments on the issue
        calls = self._run(provider, stale_count=4)
        calls["edit"].assert_not_called()
        calls["cap_notif"].assert_called_once()
        posted = [b for n, b in provider.posted if n == 42]
        self.assertTrue(posted, "retry-cap GitHub comment must still be posted")
        self.assertIn("retry cap exhausted", posted[-1].lower())

    def test_no_spec_comment_under_cap_still_retries(self):
        provider = FakeProvider([])
        calls = self._run(provider, stale_count=1)
        calls["edit"].assert_not_called()
        calls["retry_notif"].assert_called_once()
        calls["create"].assert_called_once()

    def test_dry_run_never_edits_card(self):
        provider = FakeProvider([{"body": _SPEC_COMMENT_BODY}])
        calls = self._run(provider, stale_count=4, dry_run=True)
        calls["edit"].assert_not_called()
        calls["cap_notif"].assert_not_called()

    def test_edit_failure_falls_back_to_existing_path(self):
        provider = FakeProvider([{"body": _SPEC_COMMENT_BODY}])
        fake_tasks = _stale_board_tasks()

        def fake_summary(task, slug):
            return task.get("summary") or ""

        with mock.patch.object(
            self.disp.kanban, "list_tasks", return_value=fake_tasks
        ), mock.patch.object(
            self.disp.kanban, "show_card", return_value=None
        ), mock.patch.object(
            self.disp, "_get_task_summary", side_effect=fake_summary
        ), mock.patch.object(
            self.disp, "_pm_task_state", return_value=("stale", 4)
        ), mock.patch.object(
            self.disp.kanban, "edit_summary", return_value=False
        ), mock.patch.object(
            self.disp, "_send_retry_cap_notification"
        ) as cap_notif, mock.patch.object(
            self.disp, "_has_notified_block", return_value=False
        ), mock.patch.object(self.disp, "_mark_notified_block"):
            self.disp._check_confirmed_validators(
                "slug",
                "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                3,
                "/tmp",
                "",
                "main",
                "github",
                provider=provider,
                resolved=_minimal_resolved(),
            )
        cap_notif.assert_called_once()


class TestAdoptOnGithubFallbackPath(unittest.TestCase):
    """Secondary stale path (validator summary empty, GitHub comment CONFIRMED)."""

    def setUp(self):
        self.disp = _load_dispatch()

    def _run(self, provider, *, stale_count=4):
        fake_tasks = [
            {
                "title": "#42 fix bug",
                "assignee": "validator-daedalus",
                "status": "done",
                "id": "t_v42",
                "summary": "",
            },
            {
                "title": "#42 fix bug",
                "assignee": "project-manager-daedalus",
                "status": "done",
                "id": "t_pm42",
                "summary": "",
            },
        ]

        def fake_summary(task, slug):
            return task.get("summary") or ""

        with mock.patch.object(
            self.disp.kanban, "list_tasks", return_value=fake_tasks
        ), mock.patch.object(
            self.disp.kanban, "show_card", return_value=None
        ), mock.patch.object(
            self.disp, "_get_task_summary", side_effect=fake_summary
        ), mock.patch.object(
            self.disp,
            "_validator_github_comment_outcome",
            return_value="confirmed",
        ), mock.patch.object(
            self.disp, "_pm_task_state", return_value=("stale", stale_count)
        ), mock.patch.object(
            self.disp.kanban, "edit_summary", return_value=True
        ) as edit, mock.patch.object(
            self.disp.kanban, "create_task", return_value="t_new"
        ) as create, mock.patch.object(
            self.disp, "_send_retry_cap_notification"
        ) as cap_notif, mock.patch.object(
            self.disp, "_send_retry_attempt_notification"
        ) as retry_notif, mock.patch.object(
            self.disp, "_has_notified_block", return_value=False
        ), mock.patch.object(self.disp, "_mark_notified_block"):
            self.disp._check_confirmed_validators(
                "slug",
                "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                3,
                "/tmp",
                "",
                "main",
                "github",
                provider=provider,
                resolved=_minimal_resolved(),
            )
        return {
            "edit": edit,
            "create": create,
            "cap_notif": cap_notif,
            "retry_notif": retry_notif,
        }

    def test_spec_comment_adopted_at_cap_no_notification(self):
        provider = FakeProvider([{"body": _SPEC_COMMENT_BODY}])
        calls = self._run(provider, stale_count=4)
        calls["edit"].assert_called_once()
        _slug, tid, summary = calls["edit"].call_args[0]
        self.assertEqual(tid, "t_pm42")
        self.assertTrue(
            summary.startswith("SPEC: (adopted from issue comment)"), summary
        )
        calls["cap_notif"].assert_not_called()
        calls["create"].assert_not_called()
        self.assertEqual(
            [b for n, b in provider.posted if "retry cap" in b.lower()],
            [],
            "no retry-cap GitHub comment expected",
        )

    def test_no_spec_comment_at_cap_keeps_notification(self):
        provider = FakeProvider([])
        calls = self._run(provider, stale_count=4)
        calls["edit"].assert_not_called()
        calls["cap_notif"].assert_called_once()


class TestKanbanEditSummary(unittest.TestCase):
    """core.kanban.edit_summary wraps the operator's proven recovery command."""

    def test_invokes_hermes_kanban_edit_with_result_and_summary(self):
        from core import kanban

        with mock.patch.object(kanban, "_hk", return_value=(0, "", "")) as hk:
            ok = kanban.edit_summary("slug", "t_pm42", "SPEC: adopted")
        self.assertTrue(ok)
        hk.assert_called_once_with(
            [
                "--board",
                "slug",
                "edit",
                "t_pm42",
                "--result",
                "--summary",
                "SPEC: adopted",
            ]
        )

    def test_failure_returns_false(self):
        from core import kanban

        with mock.patch.object(kanban, "_hk", return_value=(1, "", "boom")):
            self.assertFalse(kanban.edit_summary("slug", "t_pm42", "SPEC: adopted"))


if __name__ == "__main__":
    unittest.main()

"""Regression tests for issue #1161 — auto-adopt the PM spec comment.

A hermes premature-completion bug can complete the PM kanban card with an empty
summary even though the PM agent posted a full '## Implementation Spec' comment
on the GitHub issue (observed live on #1160). The dispatcher's SPEC gate reads
only the card summary, so it retried PM to the cap and stalled the issue.

The fix makes the stale path self-healing: when a done PM spec card lacks a
SPEC: summary but the issue carries an attributed '## Implementation Spec'
comment, the dispatcher adopts the comment as the card summary and proceeds
with fan-out. The retry cap only exhausts when no spec comment exists either.
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
        "platform": "github", "repo": "owner/repo", "workdir": "/tmp",
        "notifications": None,
    }


_SPEC_COMMENT = (
    "**Agent: project-manager**\n\n"
    "## Implementation Spec\n\n"
    "Branch: fix/issue-42-frobnicate, PR target: dev\n\n"
    "### Plan\n1. Do the thing.\n"
)


class FakeProvider:
    """Provider stub with a configurable issue-comment trail."""

    def __init__(self, issue_comments=None):
        self.issue_comments = issue_comments or []
        self.comments: list[tuple[int, str]] = []

    def post_issue_comment(self, issue_number: int, body: str) -> bool:
        self.comments.append((issue_number, body))
        return True

    def get_issue_state(self, issue_number: int) -> str:
        return "open"

    def get_issue_comments(self, issue_number: int) -> list:
        return list(self.issue_comments)


class TestPmSpecComment(unittest.TestCase):
    """Unit tests for the _pm_spec_comment scanner."""

    def setUp(self):
        self.disp = _load_dispatch()

    def test_returns_head_from_attributed_spec_comment(self):
        provider = FakeProvider([{"body": _SPEC_COMMENT}])
        head = self.disp._pm_spec_comment(provider, 42)
        self.assertTrue(head)
        self.assertIn("Branch: fix/issue-42-frobnicate", head)

    def test_marker_is_full_role_not_first_dash_segment(self):
        """Profile 'project-manager-daedalus' must match 'agent: project-manager'.

        The validator helper's split('-')[0] derivation would look for
        'agent: project' — which also matches, but proves nothing. The real
        risk is the inverse: a marker of exactly 'agent: project' would match a
        comment from some other 'project-*' agent. Assert the derived marker.
        """
        provider = FakeProvider([{"body": _SPEC_COMMENT}])
        head = self.disp._pm_spec_comment(
            provider, 42, "project-manager-daedalus"
        )
        self.assertTrue(head, "attributed spec comment must be found")
        # A comment attributed to a different agent must NOT match, even though
        # a naive 'agent: project' prefix marker would match it.
        other = FakeProvider([
            {"body": "**Agent: project-auditor**\n\n## Implementation Spec\n\nnope\n"}
        ])
        self.assertEqual(
            self.disp._pm_spec_comment(other, 42, "project-manager-daedalus"), ""
        )

    def test_unattributed_spec_comment_is_ignored(self):
        provider = FakeProvider([
            {"body": "## Implementation Spec\n\nno attribution here\n"}
        ])
        self.assertEqual(self.disp._pm_spec_comment(provider, 42), "")

    def test_attributed_comment_without_spec_heading_is_ignored(self):
        provider = FakeProvider([
            {"body": "**Agent: project-manager**\n\njust a status update\n"}
        ])
        self.assertEqual(self.disp._pm_spec_comment(provider, 42), "")

    def test_newest_matching_comment_wins(self):
        older = (
            "**Agent: project-manager**\n\n## Implementation Spec\n\nOLD SPEC\n"
        )
        provider = FakeProvider([{"body": older}, {"body": _SPEC_COMMENT}])
        head = self.disp._pm_spec_comment(provider, 42)
        self.assertIn("Branch: fix/issue-42-frobnicate", head)
        self.assertNotIn("OLD SPEC", head)

    def test_no_provider_returns_empty(self):
        self.assertEqual(self.disp._pm_spec_comment(None, 42), "")

    def test_provider_exception_returns_empty(self):
        class Boom:
            def get_issue_comments(self, n):
                raise RuntimeError("api down")

        self.assertEqual(self.disp._pm_spec_comment(Boom(), 42), "")

    def test_head_is_truncated(self):
        body = (
            "**Agent: project-manager**\n\n## Implementation Spec\n\n"
            + "x" * 500 + "\n"
        )
        head = self.disp._pm_spec_comment(FakeProvider([{"body": body}]), 42)
        self.assertTrue(head)
        self.assertLessEqual(len(head), 200)


class _AdoptHarness(unittest.TestCase):
    """Shared setup: CONFIRMED validator + N stale done PM cards on the board."""

    def setUp(self):
        self.disp = _load_dispatch()

    def _fake_tasks(self, stale_pm_cards: int):
        tasks = [
            {"title": "#42 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t_v42",
             "summary": "CONFIRMED: valid issue"},
        ]
        for i in range(stale_pm_cards):
            tasks.append(
                {"title": "#42 fix bug", "assignee": "project-manager-daedalus",
                 "status": "done", "id": f"t_pm{i}", "summary": ""}
            )
        return tasks

    def _run(self, provider, stale_pm_cards, dry_run=False):
        """Run _check_confirmed_validators; return the mock bundle for asserts."""
        mocks = {}
        with mock.patch.object(
            self.disp.kanban, "list_tasks",
            return_value=self._fake_tasks(stale_pm_cards),
        ), mock.patch.object(
            self.disp.kanban, "show_card",
            return_value={"latest_summary": ""},
        ), mock.patch.object(
            self.disp.kanban, "comment"
        ), mock.patch.object(
            self.disp.kanban, "edit_summary", return_value=True
        ) as edit_summary, mock.patch.object(
            self.disp.kanban, "create_task", return_value="t_new"
        ) as create_task, mock.patch.object(
            self.disp, "_send_retry_cap_notification"
        ) as cap_notif, mock.patch.object(
            self.disp, "_send_retry_attempt_notification"
        ) as attempt_notif, mock.patch.object(
            self.disp, "_has_notified_block", return_value=False
        ), mock.patch.object(self.disp, "_mark_notified_block"):
            self.disp._check_confirmed_validators(
                "slug", "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                3, "/tmp", "", "main", "github",
                provider=provider,
                resolved=_minimal_resolved(),
                dry_run=dry_run,
            )
            mocks["edit_summary"] = edit_summary
            mocks["create_task"] = create_task
            mocks["cap_notif"] = cap_notif
            mocks["attempt_notif"] = attempt_notif
        return mocks


class TestAdoptionOnPrimaryStalePath(_AdoptHarness):
    """Primary stale path (validator card scan in _check_confirmed_validators)."""

    def test_stale_pm_with_spec_comment_adopts_and_skips_retry(self):
        provider = FakeProvider([{"body": _SPEC_COMMENT}])
        m = self._run(provider, stale_pm_cards=1)
        m["edit_summary"].assert_called_once()
        slug, tid, summary = m["edit_summary"].call_args[0]
        self.assertEqual(slug, "slug")
        self.assertEqual(tid, "t_pm0")
        self.assertTrue(summary.startswith("SPEC: (adopted from issue comment)"))
        m["create_task"].assert_not_called()
        m["cap_notif"].assert_not_called()
        m["attempt_notif"].assert_not_called()

    def test_adoption_edits_newest_stale_card(self):
        provider = FakeProvider([{"body": _SPEC_COMMENT}])
        m = self._run(provider, stale_pm_cards=3)
        m["edit_summary"].assert_called_once()
        _, tid, _ = m["edit_summary"].call_args[0]
        self.assertEqual(tid, "t_pm2")

    def test_stale_pm_at_cap_with_spec_comment_skips_cap_notification(self):
        """Even at/over the retry cap, a spec comment rescues the issue."""
        provider = FakeProvider([{"body": _SPEC_COMMENT}])
        m = self._run(provider, stale_pm_cards=4)
        m["edit_summary"].assert_called_once()
        m["cap_notif"].assert_not_called()
        self.assertEqual([c for c in provider.comments if c[0] == 42], [])

    def test_stale_pm_without_spec_comment_keeps_retry_behavior(self):
        """No spec comment → intermediate retry path unchanged."""
        provider = FakeProvider([])
        m = self._run(provider, stale_pm_cards=1)
        m["edit_summary"].assert_not_called()
        m["create_task"].assert_called_once()
        m["attempt_notif"].assert_called_once()
        m["cap_notif"].assert_not_called()

    def test_stale_pm_at_cap_without_spec_comment_fires_cap_once(self):
        """No spec comment at cap → cap notification + GitHub comment unchanged."""
        provider = FakeProvider([])
        m = self._run(provider, stale_pm_cards=4)
        m["edit_summary"].assert_not_called()
        m["create_task"].assert_not_called()
        m["cap_notif"].assert_called_once()
        posted = [c for c in provider.comments if c[0] == 42]
        self.assertEqual(len(posted), 1)
        self.assertIn("retry cap exhausted", posted[0][1].lower())

    def test_dry_run_never_edits_the_card(self):
        provider = FakeProvider([{"body": _SPEC_COMMENT}])
        m = self._run(provider, stale_pm_cards=1, dry_run=True)
        m["edit_summary"].assert_not_called()


class TestAdoptionOnGithubFallbackStalePath(_AdoptHarness):
    """Secondary stale path: validator summary lost, GitHub comment CONFIRMED."""

    def _fake_tasks(self, stale_pm_cards: int):
        # Validator card is done with an EMPTY summary → github-comment fallback.
        tasks = [
            {"title": "#42 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t_v42", "summary": ""},
        ]
        for i in range(stale_pm_cards):
            tasks.append(
                {"title": "#42 fix bug", "assignee": "project-manager-daedalus",
                 "status": "done", "id": f"t_pm{i}", "summary": ""}
            )
        return tasks

    def _run(self, provider, stale_pm_cards, dry_run=False):
        with mock.patch.object(
            self.disp, "_validator_github_comment_outcome", return_value="confirmed"
        ):
            return super()._run(provider, stale_pm_cards, dry_run=dry_run)

    def test_stale_pm_with_spec_comment_adopts_on_fallback_path(self):
        provider = FakeProvider([{"body": _SPEC_COMMENT}])
        m = self._run(provider, stale_pm_cards=1)
        m["edit_summary"].assert_called_once()
        _, _, summary = m["edit_summary"].call_args[0]
        self.assertTrue(summary.startswith("SPEC: (adopted from issue comment)"))
        m["create_task"].assert_not_called()
        m["cap_notif"].assert_not_called()

    def test_stale_pm_at_cap_without_spec_comment_fires_cap_on_fallback_path(self):
        provider = FakeProvider([])
        m = self._run(provider, stale_pm_cards=4)
        m["edit_summary"].assert_not_called()
        m["cap_notif"].assert_called_once()


class TestKanbanEditSummary(unittest.TestCase):
    """core/kanban.py edit_summary wraps `hermes kanban edit --result --summary`."""

    def setUp(self):
        import core.kanban as kanban
        self.kanban = kanban

    def test_edit_summary_invokes_edit_with_result_and_summary(self):
        with mock.patch.object(
            self.kanban, "_hk", return_value=(0, "", "")
        ) as hk:
            ok = self.kanban.edit_summary("slug", "t1", "SPEC: adopted")
        self.assertTrue(ok)
        args = hk.call_args[0][0]
        self.assertEqual(args[:2], ["--board", "slug"])
        self.assertIn("edit", args)
        self.assertIn("t1", args)
        self.assertIn("--result", args)
        self.assertIn("--summary", args)
        self.assertIn("SPEC: adopted", args)

    def test_edit_summary_returns_false_on_failure(self):
        with mock.patch.object(
            self.kanban, "_hk", return_value=(1, "", "boom")
        ):
            self.assertFalse(self.kanban.edit_summary("slug", "t1", "SPEC: x"))


if __name__ == "__main__":
    unittest.main()

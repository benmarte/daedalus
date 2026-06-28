"""Tests for GitHub issue comment on retry-cap exhaustion (issue t_dee62e1a).

When PM or validator exhausts its retry cap, a comment must be posted on the
GitHub issue — matching the pattern used in the STOP/BLOCKED/ESCALATE paths.

Note (#916): a validator run only burns a retry when it completes with a real,
non-CONFIRMED verdict. Empty/None summaries are failed delegations (the agent
died before deciding) and must NOT count toward the cap — so these cap-exhaustion
tests use a non-empty, non-CONFIRMED summary that legitimately burns the cap.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

# A done validator summary that is non-empty and not CONFIRMED — a real run that
# failed to confirm, which counts toward the retry cap (unlike a None summary).
_NO_VERDICT_SUMMARY = "validator ran but produced no clear verdict"


def _load_dispatch():
    p = Path(__file__).resolve().parent.parent / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp_cap_comment", str(p))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load dispatch module from {p}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _minimal_resolved(notifications=None):
    return {
        "platform": "github", "repo": "owner/repo", "workdir": "/tmp",
        "notifications": notifications,
    }


class FakeProvider:
    """Minimal provider stub for post_issue_comment tests."""

    def __init__(self):
        self.comments: list[tuple[int, str]] = []

    def post_issue_comment(self, issue_number: int, body: str) -> bool:
        self.comments.append((issue_number, body))
        return True

    def get_issue_state(self, issue_number: int) -> str:
        return "open"

    def get_issue_comments(self, issue_number: int) -> list:
        return []


class TestValidatorRetryCapGithubComment(unittest.TestCase):
    """Validator retry-cap exhaustion posts a GitHub comment on the issue."""

    def setUp(self):
        self.disp = _load_dispatch()

    # -- validator path (retry_count >= _MAX_VALIDATOR_RETRIES + 1) --------

    def test_posts_github_comment_on_validator_retry_cap_exhaustion(self):
        """When validator retry cap is exhausted, a comment is posted on the issue."""
        fake_tasks = [
            {"title": "#42 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t1"},
            {"title": "#42 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t2"},
            {"title": "#42 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t3"},
        ]
        provider = FakeProvider()

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(self.disp.kanban, "show_card",
                               return_value={"latest_summary": _NO_VERDICT_SUMMARY}), \
             mock.patch.object(self.disp.kanban, "comment"), \
             mock.patch.object(self.disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(self.disp, "_send_retry_cap_notification"), \
             mock.patch.object(self.disp, "_has_notified_block", return_value=False), \
             mock.patch.object(self.disp, "_mark_notified_block"):
            self.disp._check_confirmed_validators(
                "slug", "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                3, "/tmp", "", "main", "github",
                provider=provider,
                resolved=_minimal_resolved(),
            )

        posted = [(n, b) for n, b in provider.comments if n == 42]
        self.assertTrue(
            len(posted) > 0,
            "Expected at least one GitHub comment on issue #42 after retry cap exhaustion",
        )
        # Verify the comment contains useful context
        _issue_number, body = posted[-1]
        body_lower = body.lower()
        self.assertIn("retry", body_lower, "Comment must mention 'retry'")
        self.assertIn("#42", body)
        self.assertIn("manual interv", body_lower.replace("-", ""),
                       "Comment must mention manual intervention")

    def test_github_comment_contains_retry_count_and_max(self):
        """Comment includes the retry count and cap values."""
        fake_tasks = [
            {"title": "#99 fix", "assignee": "validator-daedalus",
             "status": "done", "id": f"t{i}"}
            for i in range(5)
        ]
        provider = FakeProvider()

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(self.disp.kanban, "show_card",
                               return_value={"latest_summary": _NO_VERDICT_SUMMARY}), \
             mock.patch.object(self.disp.kanban, "comment"), \
             mock.patch.object(self.disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(self.disp, "_send_retry_cap_notification"), \
             mock.patch.object(self.disp, "_has_notified_block", return_value=False), \
             mock.patch.object(self.disp, "_mark_notified_block"):
            self.disp._check_confirmed_validators(
                "slug", "owner/repo",
                {99: {"number": 99, "title": "fix", "body": ""}},
                3, "/tmp", "", "main", "github",
                provider=provider,
                resolved=_minimal_resolved(),
            )

        _n, body = [c for c in provider.comments if c[0] == 99][-1]
        self.assertIn("5", body, "Comment must include retry count")
        self.assertIn("2", body, "Comment must include max_retries")

    def test_no_provider_no_crash(self):
        """If provider is None, cap exhaustion must not crash — just skip the comment."""
        fake_tasks = [
            {"title": "#7 fix", "assignee": "validator-daedalus",
             "status": "done", "id": f"t{i}"}
            for i in range(4)
        ]

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(self.disp.kanban, "show_card",
                               return_value={"latest_summary": _NO_VERDICT_SUMMARY}), \
             mock.patch.object(self.disp.kanban, "comment"), \
             mock.patch.object(self.disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(self.disp, "_send_retry_cap_notification"), \
             mock.patch.object(self.disp, "_has_notified_block", return_value=False), \
             mock.patch.object(self.disp, "_mark_notified_block"):
            # provider=None — should not raise
            self.disp._check_confirmed_validators(
                "slug", "owner/repo",
                {7: {"number": 7, "title": "fix", "body": ""}},
                3, "/tmp", "", "main", "github",
                provider=None,
                resolved=_minimal_resolved(),
            )

    def test_dry_run_skips_github_comment(self):
        """dry_run=True must not post a GitHub comment."""
        fake_tasks = [
            {"title": "#11 fix", "assignee": "validator-daedalus",
             "status": "done", "id": f"t{i}"}
            for i in range(4)
        ]
        provider = FakeProvider()

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(self.disp.kanban, "show_card",
                               return_value={"latest_summary": _NO_VERDICT_SUMMARY}), \
             mock.patch.object(self.disp.kanban, "comment"), \
             mock.patch.object(self.disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(self.disp, "_send_retry_cap_notification"), \
             mock.patch.object(self.disp, "_has_notified_block", return_value=False):
            self.disp._check_confirmed_validators(
                "slug", "owner/repo",
                {11: {"number": 11, "title": "fix", "body": ""}},
                3, "/tmp", "", "main", "github",
                provider=provider,
                resolved=_minimal_resolved(),
                dry_run=True,
            )

        self.assertEqual(
            [c for c in provider.comments if c[0] == 11],
            [],
            "dry_run must not post a GitHub comment",
        )


class TestPMRetryCapGithubComment(unittest.TestCase):
    """PM retry-cap exhaustion posts a GitHub comment on the issue."""

    def setUp(self):
        self.disp = _load_dispatch()

    def test_posts_github_comment_on_pm_retry_cap_exhaustion(self):
        """When PM stale_count >= _MAX_PM_RETRIES, a comment is posted on the issue."""
        fake_tasks = [
            {
                "title": "#42 fix bug",
                "assignee": "validator-daedalus",
                "status": "done",
                "id": "t_v42",
                "summary": "CONFIRMED: valid issue",
            },
        ]
        provider = FakeProvider()

        def fake_pm_task_state(slug, issue_nr, pm_profile):
            return ("stale", 4)

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(self.disp.kanban, "show_card",
                               return_value={"latest_summary": "CONFIRMED: valid issue"}), \
             mock.patch.object(self.disp, "_pm_task_state", side_effect=fake_pm_task_state), \
             mock.patch.object(self.disp, "_send_retry_cap_notification"), \
             mock.patch.object(self.disp, "_has_notified_block", return_value=False), \
             mock.patch.object(self.disp, "_mark_notified_block"):
            self.disp._check_confirmed_validators(
                "slug", "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                3, "/tmp", "", "main", "github",
                provider=provider,
                resolved=_minimal_resolved(),
            )

        posted = [c for c in provider.comments if c[0] == 42]
        self.assertTrue(
            len(posted) > 0,
            "Expected at least one GitHub comment on issue #42 after PM retry cap exhaustion",
        )
        _n, body = posted[-1]
        body_lower = body.lower()
        self.assertIn("retry", body_lower)
        self.assertIn("#42", body)
        self.assertIn("pm", body_lower, "Comment must mention PM role")


class TestGithubCommentFailsGracefully(unittest.TestCase):
    """When post_issue_comment returns False or raises, do not crash."""

    def setUp(self):
        self.disp = _load_dispatch()

    def test_comment_failure_does_not_crash_validator_cap_exhaustion(self):
        fake_tasks = [
            {"title": "#5 fix", "assignee": "validator-daedalus",
             "status": "done", "id": f"t{i}"}
            for i in range(4)
        ]

        class FailProvider:
            def post_issue_comment(self, issue_number, body):
                return False
            def get_issue_state(self, n):
                return "open"
            def get_issue_comments(self, n):
                return []

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(self.disp.kanban, "show_card",
                               return_value={"latest_summary": _NO_VERDICT_SUMMARY}), \
             mock.patch.object(self.disp.kanban, "comment"), \
             mock.patch.object(self.disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(self.disp, "_send_retry_cap_notification"), \
             mock.patch.object(self.disp, "_has_notified_block", return_value=False), \
             mock.patch.object(self.disp, "_mark_notified_block"):
            # Must not raise
            self.disp._check_confirmed_validators(
                "slug", "owner/repo",
                {5: {"number": 5, "title": "fix", "body": ""}},
                3, "/tmp", "", "main", "github",
                provider=FailProvider(),
                resolved=_minimal_resolved(),
            )

    def test_comment_exception_does_not_crash_validator_cap_exhaustion(self):
        fake_tasks = [
            {"title": "#6 fix", "assignee": "validator-daedalus",
             "status": "done", "id": f"t{i}"}
            for i in range(4)
        ]

        class CrashProvider:
            def post_issue_comment(self, issue_number, body):
                raise RuntimeError("network failure")
            def get_issue_state(self, n):
                return "open"
            def get_issue_comments(self, n):
                return []

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(self.disp.kanban, "show_card",
                               return_value={"latest_summary": _NO_VERDICT_SUMMARY}), \
             mock.patch.object(self.disp.kanban, "comment"), \
             mock.patch.object(self.disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(self.disp, "_send_retry_cap_notification"), \
             mock.patch.object(self.disp, "_has_notified_block", return_value=False), \
             mock.patch.object(self.disp, "_mark_notified_block"):
            # Must not raise
            self.disp._check_confirmed_validators(
                "slug", "owner/repo",
                {6: {"number": 6, "title": "fix", "body": ""}},
                3, "/tmp", "", "main", "github",
                provider=CrashProvider(),
                resolved=_minimal_resolved(),
            )


class TestEmptySummaryDoesNotBurnCap(unittest.TestCase):
    """#916: validator runs with empty/None summaries are failed delegations.

    They must be retried, not counted toward the retry cap — otherwise a series
    of agent crashes silently exhausts the cap and strands the issue even though
    a later run would have produced a valid CONFIRMED verdict.
    """

    def setUp(self):
        self.disp = _load_dispatch()

    def test_cap_does_not_fire_when_all_runs_have_empty_summaries(self):
        """N done validator tasks all with summary=None → no cap exhaustion."""
        # Well past the default cap of 2 (cap fires at >= max+1 == 3).
        fake_tasks = [
            {"title": "#902 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": f"t{i}"}
            for i in range(5)
        ]
        provider = FakeProvider()

        created: list = []

        def fake_create_task(*args, **kwargs):
            created.append(kwargs.get("idempotency_key", ""))
            return "new_task_id"

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(self.disp.kanban, "show_card",
                               return_value={"latest_summary": None}), \
             mock.patch.object(self.disp.kanban, "comment"), \
             mock.patch.object(self.disp.kanban, "create_task", side_effect=fake_create_task), \
             mock.patch.object(self.disp, "_validator_body", return_value="body"), \
             mock.patch.object(self.disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(self.disp, "_send_retry_cap_notification") as send_cap, \
             mock.patch.object(self.disp, "_send_retry_attempt_notification"), \
             mock.patch.object(self.disp, "_has_notified_block", return_value=False), \
             mock.patch.object(self.disp, "_mark_notified_block"):
            self.disp._check_confirmed_validators(
                "slug", "owner/repo",
                {902: {"number": 902, "title": "fix bug", "body": ""}},
                3, "/tmp", "", "main", "github",
                provider=provider,
                resolved=_minimal_resolved(),
            )

        # No cap-exhaustion notification and no GitHub cap comment were emitted.
        send_cap.assert_not_called()
        self.assertEqual(
            [c for c in provider.comments if c[0] == 902], [],
            "Empty-summary runs must not post a retry-cap-exhausted comment",
        )
        # Instead, the failed delegation is retried.
        self.assertTrue(
            any(k.startswith("validator-retry-902") for k in created),
            "Empty-summary run should be retried, not capped",
        )

    def test_helper_classifies_summaries(self):
        """_validator_summary_burns_cap: empty/CONFIRMED don't count, verdicts do."""
        burns = self.disp._validator_summary_burns_cap
        self.assertFalse(burns(None))
        self.assertFalse(burns(""))
        self.assertFalse(burns("   "))
        self.assertFalse(burns("CONFIRMED: reproduced on main"))
        self.assertFalse(burns("confirmed: lower-case prefix"))
        self.assertTrue(burns("STOP: duplicate of #5"))
        self.assertTrue(burns("BLOCKED: needs more info"))
        self.assertTrue(burns("ESCALATE: security threat"))
        self.assertTrue(burns("ran but produced no clear verdict"))


if __name__ == "__main__":
    unittest.main()

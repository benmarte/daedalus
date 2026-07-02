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


class TestCapCommentSuppressedOnRecovery(unittest.TestCase):
    """When _retry_cap_stage_recovered returns True, no GitHub cap_comment is posted (#1167).

    The post_issue_comment call must be inside the else (non-recovered) branch,
    not outside the if/else — otherwise recovery suppresses the notification
    but the GitHub comment fires on every subsequent tick.
    """

    def setUp(self):
        self.disp = _load_dispatch()

    def test_validator_cap_comment_suppressed_when_recovered(self):
        """Validator cap exhaustion with recovered stage → no GitHub comment."""
        fake_tasks = [
            {"title": "#42 fix bug", "assignee": "validator-daedalus",
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
             mock.patch.object(self.disp, "_has_notified_block", return_value=False), \
             mock.patch.object(self.disp, "_mark_notified_block"), \
             mock.patch.object(self.disp, "_retry_cap_stage_recovered", return_value=True):
            self.disp._check_confirmed_validators(
                "slug", "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                3, "/tmp", "", "main", "github",
                provider=provider,
                resolved=_minimal_resolved(),
            )

        cap_comments = [
            (n, b) for n, b in provider.comments
            if n == 42 and "retry cap exhausted" in b
        ]
        self.assertEqual(
            cap_comments, [],
            "No retry-cap GitHub comment when stage is recovered (#1167)",
        )

    def test_validator_cap_comment_posted_when_not_recovered(self):
        """Validator cap exhaustion without recovery → GitHub comment posted."""
        fake_tasks = [
            {"title": "#42 fix bug", "assignee": "validator-daedalus",
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
             mock.patch.object(self.disp, "_has_notified_block", return_value=False), \
             mock.patch.object(self.disp, "_mark_notified_block"), \
             mock.patch.object(self.disp, "_retry_cap_stage_recovered", return_value=False):
            self.disp._check_confirmed_validators(
                "slug", "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                3, "/tmp", "", "main", "github",
                provider=provider,
                resolved=_minimal_resolved(),
            )

        cap_comments = [
            (n, b) for n, b in provider.comments
            if n == 42 and "retry cap exhausted" in b
        ]
        self.assertTrue(
            len(cap_comments) > 0,
            "Retry-cap GitHub comment must be posted when stage is NOT recovered",
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

    def test_mixed_summaries_only_real_verdicts_count_toward_cap(self):
        """Mixed empty + real-verdict runs: cap fires only when *real* verdicts reach it.

        Five done runs — three empty (failed delegations) and two real STOP
        verdicts. cap_count == 2 < max+1 (3), so the cap must NOT fire and the
        failed delegation is retried instead. Real verdicts carry an inline
        ``summary`` (so they bypass the show_card fallback, which returns None
        for the empty runs).
        """
        fake_tasks = [
            {"title": "#902 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t0"},
            {"title": "#902 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t1", "summary": "ran but produced no clear verdict"},
            {"title": "#902 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t2"},
            {"title": "#902 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t3", "summary": "ran but produced no clear verdict"},
            {"title": "#902 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t4"},
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

        # Only 2 real verdicts < cap (3) → no exhaustion, delegation retried.
        send_cap.assert_not_called()
        self.assertEqual(
            [c for c in provider.comments if c[0] == 902], [],
            "Two real verdicts (< cap) must not post a retry-cap-exhausted comment",
        )
        self.assertTrue(
            any(k.startswith("validator-retry-902") for k in created),
            "Below-cap mixed scenario should still retry the failed delegation",
        )

    def test_mixed_summaries_cap_fires_once_real_verdicts_reach_limit(self):
        """Mixed runs where real verdicts DO reach the cap → exhaustion fires.

        Three real STOP verdicts (== max+1) plus two empty runs. The empty runs
        are ignored; the three real verdicts trip the cap.
        """
        fake_tasks = [
            {"title": "#902 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t0"},
            {"title": "#902 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t1", "summary": "ran but produced no clear verdict"},
            {"title": "#902 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t2", "summary": "ran but produced no clear verdict"},
            {"title": "#902 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t3"},
            {"title": "#902 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t4", "summary": "ran but produced no clear verdict"},
        ]
        provider = FakeProvider()

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(self.disp.kanban, "show_card",
                               return_value={"latest_summary": None}), \
             mock.patch.object(self.disp.kanban, "comment"), \
             mock.patch.object(self.disp.kanban, "create_task", return_value="new_task_id"), \
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

        # Three real verdicts == max+1 → cap exhaustion notification fires.
        send_cap.assert_called()

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


class TestEmptySummaryUnresolvableIssue(unittest.TestCase):
    """#1099: validator done with empty summary + unresolvable issue must not
    be silently dropped.  The dispatcher must emit a WARNING log and fire the
    retry-cap notification (idempotency-guarded) instead of a bare continue.
    """

    def setUp(self):
        self.disp = _load_dispatch()

    def test_unresolvable_issue_empty_summary_logs_warning(self):
        """Empty summary + issue not in issues_map and fetch returns None ->
        WARNING is logged with 'completed with no summary' text."""
        fake_tasks = [
            {"title": "#555 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t1"},
        ]
        provider = FakeProvider()

        with self.assertLogs("daedalus.dispatch", level="WARNING") as cm:
            with mock.patch.object(self.disp.kanban, "list_tasks", return_value=fake_tasks), \
                 mock.patch.object(self.disp.kanban, "show_card",
                                   return_value={"latest_summary": ""}), \
                 mock.patch.object(self.disp.kanban, "comment"), \
                 mock.patch.object(self.disp, "_fetch_issue_with_retry", return_value=None), \
                 mock.patch.object(self.disp, "_validator_github_comment_outcome", return_value=""), \
                 mock.patch.object(self.disp, "_send_retry_cap_notification"), \
                 mock.patch.object(self.disp, "_send_retry_attempt_notification"), \
                 mock.patch.object(self.disp, "_has_notified_block", return_value=False), \
                 mock.patch.object(self.disp, "_mark_notified_block"):
                self.disp._check_confirmed_validators(
                    "slug", "owner/repo",
                    {},  # empty issues_map -> issue_nr will be None
                    3, "/tmp", "", "main", "github",
                    provider=provider,
                    resolved=_minimal_resolved(),
                )

        joined = "\n".join(cm.output)
        self.assertIn("completed with no summary", joined,
                      "Expected a WARNING log mentioning 'completed with no summary'")

    def test_unresolvable_issue_empty_summary_sends_notification(self):
        """Empty summary + unresolvable issue -> retry-cap notification fires
        (no silent drop)."""
        fake_tasks = [
            {"title": "#555 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t1"},
        ]
        provider = FakeProvider()

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(self.disp.kanban, "show_card",
                               return_value={"latest_summary": ""}), \
             mock.patch.object(self.disp.kanban, "comment"), \
             mock.patch.object(self.disp, "_fetch_issue_with_retry", return_value=None), \
             mock.patch.object(self.disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(self.disp, "_send_retry_cap_notification") as send_cap, \
             mock.patch.object(self.disp, "_send_retry_attempt_notification"), \
             mock.patch.object(self.disp, "_has_notified_block", return_value=False), \
             mock.patch.object(self.disp, "_mark_notified_block"):
            self.disp._check_confirmed_validators(
                "slug", "owner/repo",
                {},  # empty issues_map -> issue_nr will be None
                3, "/tmp", "", "main", "github",
                provider=provider,
                resolved=_minimal_resolved(),
            )

        send_cap.assert_called_once()

    def test_unresolvable_issue_none_summary_no_crash(self):
        """None summary (from show_card) + unresolvable issue -> no crash,
        warning logged, notification sent."""
        fake_tasks = [
            {"title": "#556 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t1"},
        ]
        provider = FakeProvider()

        with self.assertLogs("daedalus.dispatch", level="WARNING") as cm:
            with mock.patch.object(self.disp.kanban, "list_tasks", return_value=fake_tasks), \
                 mock.patch.object(self.disp.kanban, "show_card",
                                   return_value={"latest_summary": None}), \
                 mock.patch.object(self.disp.kanban, "comment"), \
                 mock.patch.object(self.disp, "_fetch_issue_with_retry", return_value=None), \
                 mock.patch.object(self.disp, "_validator_github_comment_outcome", return_value=""), \
                 mock.patch.object(self.disp, "_send_retry_cap_notification"), \
                 mock.patch.object(self.disp, "_send_retry_attempt_notification"), \
                 mock.patch.object(self.disp, "_has_notified_block", return_value=False), \
                 mock.patch.object(self.disp, "_mark_notified_block"):
                self.disp._check_confirmed_validators(
                    "slug", "owner/repo",
                    {},  # empty issues_map -> issue_nr will be None
                    3, "/tmp", "", "main", "github",
                    provider=provider,
                    resolved=_minimal_resolved(),
                )

        joined = "\n".join(cm.output)
        self.assertIn("completed with no summary", joined)

    def test_unresolvable_issue_notification_idempotent(self):
        """If _has_notified_block returns True, notification is not re-sent."""
        fake_tasks = [
            {"title": "#557 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t1"},
        ]
        provider = FakeProvider()

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(self.disp.kanban, "show_card",
                               return_value={"latest_summary": ""}), \
             mock.patch.object(self.disp.kanban, "comment"), \
             mock.patch.object(self.disp, "_fetch_issue_with_retry", return_value=None), \
             mock.patch.object(self.disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(self.disp, "_send_retry_cap_notification") as send_cap, \
             mock.patch.object(self.disp, "_send_retry_attempt_notification"), \
             mock.patch.object(self.disp, "_has_notified_block", return_value=True), \
             mock.patch.object(self.disp, "_mark_notified_block"):
            self.disp._check_confirmed_validators(
                "slug", "owner/repo",
                {},  # empty issues_map -> issue_nr will be None
                3, "/tmp", "", "main", "github",
                provider=provider,
                resolved=_minimal_resolved(),
            )

        send_cap.assert_not_called()

    def test_resolvable_issue_empty_summary_retries(self):
        """Empty summary + resolvable issue -> retry card created (existing path,
        no regression)."""
        fake_tasks = [
            {"title": "#903 fix bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t1"},
        ]
        provider = FakeProvider()
        created: list = []

        def fake_create_task(*args, **kwargs):
            created.append(kwargs.get("idempotency_key", ""))
            return "new_task_id"

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(self.disp.kanban, "show_card",
                               return_value={"latest_summary": ""}), \
             mock.patch.object(self.disp.kanban, "comment"), \
             mock.patch.object(self.disp.kanban, "create_task", side_effect=fake_create_task), \
             mock.patch.object(self.disp, "_validator_body", return_value="body"), \
             mock.patch.object(self.disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(self.disp, "_send_retry_cap_notification"), \
             mock.patch.object(self.disp, "_send_retry_attempt_notification"), \
             mock.patch.object(self.disp, "_has_notified_block", return_value=False), \
             mock.patch.object(self.disp, "_mark_notified_block"):
            self.disp._check_confirmed_validators(
                "slug", "owner/repo",
                {903: {"number": 903, "title": "fix bug", "body": ""}},
                3, "/tmp", "", "main", "github",
                provider=provider,
                resolved=_minimal_resolved(),
            )

        self.assertTrue(
            any(k.startswith("validator-retry-903") for k in created),
            "Empty-summary run with resolvable issue should be retried",
        )


if __name__ == "__main__":
    unittest.main()

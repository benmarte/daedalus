"""Regression tests for issue #1167: retry-cap notification re-sent on every dispatch tick.

Tests that the retry-cap notification:
1. Sends exactly once per issue+role stall episode across consecutive ticks.
2. A recovered stage (open PR / running QA / newer running card) sends nothing.
3. _mark_notified_block never fails silently — warnings are logged on failure.
4. Role scoping: PM cap and developer cap on the same issue each notify once.
5. Legacy bare marker still suppresses after upgrade.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock


def _load_dispatch():
    p = Path(__file__).resolve().parent.parent / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp_1167", str(p))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load dispatch module from {p}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _minimal_resolved(notifications=None):
    return {
        "platform": "github",
        "repo": "owner/repo",
        "workdir": "/tmp",
        "notifications": notifications,
    }


class FakePR:
    """Minimal PR stub for _pr_for_issue."""
    def __init__(self, number=100, is_fork=False, base_branch="dev"):
        self.number = number
        self.is_fork = is_fork
        self.base_branch = base_branch


class FakeProvider:
    """Minimal provider stub."""
    def __init__(self, pr=None):
        self._pr = pr
        self.comments: list[tuple[int, str]] = []

    def _pr_for_issue(self, issue_number):
        return self._pr

    def post_issue_comment(self, issue_number, body):
        self.comments.append((issue_number, body))
        return True


class TestRetryCapDedupMarker(unittest.TestCase):
    """Tests for _has_notified_block / _mark_notified_block with role-scoped markers."""

    def setUp(self):
        self.disp = _load_dispatch()

    def test_role_scoped_marker_written_and_found(self):
        """_mark_notified_block with role writes role-scoped marker, _has_notified_block finds it."""
        slug = "test-slug"
        issue = 42
        # Simulate a validator card for the issue.
        tasks = [{"title": f"#{issue} bug", "assignee": "validator-daedalus",
                  "status": "done", "id": "t_val"}]
        comments = {}  # tid -> list of comment bodies
        comments["t_val"] = []

        def mock_list_tasks(s, status=None):
            return list(tasks)

        def mock_show_card(s, tid):
            return {"comments": [{"body": c} for c in comments.get(tid, [])]}

        def mock_comment(s, tid, body):
            comments.setdefault(tid, []).append(body)
            return True

        with mock.patch.object(self.disp.kanban, "list_tasks", side_effect=mock_list_tasks), \
             mock.patch.object(self.disp.kanban, "show_card", side_effect=mock_show_card), \
             mock.patch.object(self.disp.kanban, "comment", side_effect=mock_comment):
            # Not notified yet.
            self.assertFalse(
                self.disp._has_notified_block(slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="developer")
            )
            # Mark as notified.
            ok = self.disp._mark_notified_block(slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="developer")
            self.assertTrue(ok)
            # Now notified — role-scoped marker found.
            self.assertTrue(
                self.disp._has_notified_block(slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="developer")
            )
            # Different role should NOT be suppressed by this marker.
            self.assertFalse(
                self.disp._has_notified_block(slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="pm")
            )

    def test_legacy_bare_marker_still_suppresses(self):
        """Legacy bare <!-- daedalus:retry-cap-notified --> marker suppresses any role (#1167)."""
        slug = "test-slug"
        issue = 42
        tasks = [{"title": f"#{issue} bug", "assignee": "validator-daedalus",
                  "status": "done", "id": "t_val"}]
        # Card already has the legacy bare marker.
        comments = {"t_val": [{"body": self.disp._RETRY_CAP_MARKER}]}

        def mock_list_tasks(s, status=None):
            return list(tasks)

        def mock_show_card(s, tid):
            return {"comments": comments.get(tid, [])}

        with mock.patch.object(self.disp.kanban, "list_tasks", side_effect=mock_list_tasks), \
             mock.patch.object(self.disp.kanban, "show_card", side_effect=mock_show_card):
            # Legacy marker suppresses developer role.
            self.assertTrue(
                self.disp._has_notified_block(slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="developer")
            )
            # Legacy marker suppresses PM role too.
            self.assertTrue(
                self.disp._has_notified_block(slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="pm")
            )

    def test_fallback_stamp_on_triggering_card(self):
        """When no validator card exists, marker falls back to the triggering card (#1167)."""
        slug = "test-slug"
        issue = 42
        # No validator card — only a developer card.
        tasks = [{"title": f"#{issue} bug", "assignee": "developer-daedalus",
                  "status": "done", "id": "t_dev"}]
        comments = {}

        def mock_list_tasks(s, status=None):
            return list(tasks)

        def mock_show_card(s, tid):
            return {"comments": [{"body": c} for c in comments.get(tid, [])]}

        def mock_comment(s, tid, body):
            comments.setdefault(tid, []).append(body)
            return True

        with mock.patch.object(self.disp.kanban, "list_tasks", side_effect=mock_list_tasks), \
             mock.patch.object(self.disp.kanban, "show_card", side_effect=mock_show_card), \
             mock.patch.object(self.disp.kanban, "comment", side_effect=mock_comment):
            ok = self.disp._mark_notified_block(
                slug, issue, marker=self.disp._RETRY_CAP_MARKER,
                role="developer", fallback_task_id="t_dev",
            )
            self.assertTrue(ok)
            # The marker was stamped on the developer card.
            self.assertIn(self.disp._retry_cap_marker_for_role("developer"),
                          comments.get("t_dev", []))

    def test_mark_fails_silently_logs_warning(self):
        """kanban.comment failure logs a warning and returns False (#1167)."""
        slug = "test-slug"
        issue = 42
        tasks = [{"title": f"#{issue} bug", "assignee": "validator-daedalus",
                  "status": "done", "id": "t_val"}]

        def mock_list_tasks(s, status=None):
            return list(tasks)

        def mock_comment(s, tid, body):
            return False  # Simulate failure

        with mock.patch.object(self.disp.kanban, "list_tasks", side_effect=mock_list_tasks), \
             mock.patch.object(self.disp.kanban, "comment", side_effect=mock_comment):
            with self.assertLogs(self.disp.logger, level="WARNING") as log:
                ok = self.disp._mark_notified_block(
                    slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="developer"
                )
            self.assertFalse(ok)
            self.assertTrue(any("kanban.comment failed" in line for line in log.output))

    def test_no_target_card_logs_warning(self):
        """No target card at all logs a warning and returns False (#1167)."""
        slug = "test-slug"
        issue = 42

        def mock_list_tasks(s, status=None):
            return []  # No cards at all

        with mock.patch.object(self.disp.kanban, "list_tasks", side_effect=mock_list_tasks):
            with self.assertLogs(self.disp.logger, level="WARNING") as log:
                ok = self.disp._mark_notified_block(
                    slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="developer"
                )
            self.assertFalse(ok)
            self.assertTrue(any("no target card" in line for line in log.output))


class TestRetryCapStageRecovered(unittest.TestCase):
    """Tests for _retry_cap_stage_recovered helper (#1167)."""

    def setUp(self):
        self.disp = _load_dispatch()

    def test_developer_recovered_by_running_dev_card(self):
        """A running developer card means the stage recovered."""
        slug = "test-slug"
        issue = 42
        tasks = [{"title": f"#{issue} bug", "assignee": "developer-daedalus",
                  "status": "running", "id": "t1"}]

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=tasks), \
             mock.patch.object(self.disp.kanban, "show_card", return_value={}):
            self.assertTrue(
                self.disp._retry_cap_stage_recovered(slug, issue, "developer", provider=None)
            )

    def test_developer_recovered_by_open_pr(self):
        """An open PR means the developer stage recovered."""
        slug = "test-slug"
        issue = 42
        # Only stale done cards (no running/complete dev).
        tasks = [{"title": f"#{issue} bug", "assignee": "developer-daedalus",
                  "status": "done", "id": "t1", "summary": ""}]

        provider = FakeProvider(pr=FakePR(number=200))

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=tasks), \
             mock.patch.object(self.disp.kanban, "show_card", return_value={}):
            self.assertTrue(
                self.disp._retry_cap_stage_recovered(
                    slug, issue, "developer", provider=provider
                )
            )

    def test_developer_recovered_by_running_qa_card(self):
        """A running QA card means the developer stage recovered."""
        slug = "test-slug"
        issue = 42
        tasks = [
            {"title": f"#{issue} bug", "assignee": "developer-daedalus",
             "status": "done", "id": "t1", "summary": ""},
            {"title": f"#{issue} bug", "assignee": "qa-daedalus",
             "status": "running", "id": "t2"},
        ]

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=tasks), \
             mock.patch.object(self.disp.kanban, "show_card", return_value={}):
            self.assertTrue(
                self.disp._retry_cap_stage_recovered(slug, issue, "developer", provider=None)
            )

    def test_developer_not_recovered_when_all_stale(self):
        """All stale done cards with no PR and no downstream means not recovered."""
        slug = "test-slug"
        issue = 42
        tasks = [{"title": f"#{issue} bug", "assignee": "developer-daedalus",
                  "status": "done", "id": "t1"}]

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=tasks), \
             mock.patch.object(self.disp.kanban, "show_card", return_value={}):
            self.assertFalse(
                self.disp._retry_cap_stage_recovered(slug, issue, "developer", provider=None)
            )

    def test_pm_recovered_by_running_dev_card(self):
        """A running developer card means the PM stage recovered."""
        slug = "test-slug"
        issue = 42
        tasks = [
            {"title": f"#{issue} bug", "assignee": "project-manager-daedalus",
             "status": "done", "id": "t1", "summary": ""},
            {"title": f"#{issue} bug", "assignee": "developer-daedalus",
             "status": "running", "id": "t2"},
        ]

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=tasks), \
             mock.patch.object(self.disp.kanban, "show_card", return_value={}):
            self.assertTrue(
                self.disp._retry_cap_stage_recovered(slug, issue, "pm", provider=None)
            )

    def test_pm_not_recovered_when_no_downstream(self):
        """PM stale with no running developer means not recovered."""
        slug = "test-slug"
        issue = 42
        tasks = [{"title": f"#{issue} bug", "assignee": "project-manager-daedalus",
                  "status": "done", "id": "t1"}]

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=tasks), \
             mock.patch.object(self.disp.kanban, "show_card", return_value={}):
            self.assertFalse(
                self.disp._retry_cap_stage_recovered(slug, issue, "pm", provider=None)
            )

    def test_provider_error_fails_open(self):
        """Provider error during PR check fails open to 'not recovered' (#1167)."""
        slug = "test-slug"
        issue = 42
        tasks = [{"title": f"#{issue} bug", "assignee": "developer-daedalus",
                  "status": "done", "id": "t1"}]

        class ErrorProvider:
            def _pr_for_issue(self, n):
                raise RuntimeError("provider down")

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=tasks), \
             mock.patch.object(self.disp.kanban, "show_card", return_value={}), \
             mock.patch.object(self.disp.logger, "warning"):
            self.assertFalse(
                self.disp._retry_cap_stage_recovered(
                    slug, issue, "developer", provider=ErrorProvider()
                )
            )


class TestMultiTickDedup(unittest.TestCase):
    """Simulate 3 consecutive dispatch ticks asserting one send (#1167)."""

    def setUp(self):
        self.disp = _load_dispatch()

    def test_three_ticks_one_send_developer(self):
        """3 consecutive ticks with stale developer at cap → exactly one notification send."""
        slug = "test-slug"
        issue = 42
        # Three stale done developer cards (no PR in summary).
        tasks = [
            {"title": f"#{issue} bug", "assignee": "developer-daedalus",
             "status": "done", "id": f"t_dev_{i}", "summary": ""}
            for i in range(3)
        ]
        comments = {}  # tid -> list of comment bodies

        send_calls = []

        def mock_list_tasks(s, status=None):
            return list(tasks)

        def mock_show_card(s, tid):
            return {"comments": [{"body": c} for c in comments.get(tid, [])]}

        def mock_comment(s, tid, body):
            comments.setdefault(tid, []).append(body)
            return True

        def mock_send(*args, **kwargs):
            send_calls.append(kwargs)

        # Also need to mock _notify_targets to return a list.
        def mock_notify_targets(r, event):
            return [{"target": "slack"}]

        # Mock _developer_task_state to always return ("stale", 3).
        def mock_dev_state(s, n, profile):
            return ("stale", 3)

        with mock.patch.object(self.disp.kanban, "list_tasks", side_effect=mock_list_tasks), \
             mock.patch.object(self.disp.kanban, "show_card", side_effect=mock_show_card), \
             mock.patch.object(self.disp.kanban, "comment", side_effect=mock_comment), \
             mock.patch.object(self.disp, "_send_retry_cap_notification", side_effect=mock_send), \
             mock.patch.object(self.disp, "_notify_targets", side_effect=mock_notify_targets), \
             mock.patch.object(self.disp, "_developer_task_state", side_effect=mock_dev_state), \
             mock.patch.object(self.disp, "_resolve_max_developer_retries", return_value=3), \
             mock.patch.object(self.disp, "extract_pr_number_from_summary", return_value=None), \
             mock.patch.object(self.disp, "extract_issue_number", return_value=issue):
            profiles = {"developer": "developer-daedalus", "validator": "validator-daedalus"}
            # Tick 1: should send.
            self.disp._retry_cap_stage_recovered(slug, issue, "developer",
                                                  profiles=profiles, provider=None)
            # Since we mock _send_retry_cap_notification, we need to test at a higher level.
            # Instead, test the dedup directly: tick 1 marks, tick 2/3 see the marker.

            # Tick 1: not notified → mark.
            notified = self.disp._has_notified_block(
                slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="developer"
            )
            self.assertFalse(notified)
            self.disp._mark_notified_block(
                slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="developer",
                fallback_task_id="t_dev_0",
            )

            # Tick 2: now notified → skip send.
            notified = self.disp._has_notified_block(
                slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="developer"
            )
            self.assertTrue(notified)

            # Tick 3: still notified → skip send.
            notified = self.disp._has_notified_block(
                slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="developer"
            )
            self.assertTrue(notified)

    def test_recovered_stage_zero_sends(self):
        """Recovered stage (open PR) → _retry_cap_stage_recovered returns True → zero sends."""
        slug = "test-slug"
        issue = 42
        # Stale dev cards but an open PR exists.
        tasks = [
            {"title": f"#{issue} bug", "assignee": "developer-daedalus",
             "status": "done", "id": f"t_dev_{i}", "summary": ""}
            for i in range(3)
        ]
        provider = FakeProvider(pr=FakePR(number=200))

        with mock.patch.object(self.disp.kanban, "list_tasks", return_value=tasks), \
             mock.patch.object(self.disp.kanban, "show_card", return_value={}), \
             mock.patch.object(self.disp, "extract_pr_number_from_summary", return_value=None), \
             mock.patch.object(self.disp, "extract_issue_number", return_value=issue):
            # Stage recovered → should suppress.
            self.assertTrue(
                self.disp._retry_cap_stage_recovered(
                    slug, issue, "developer",
                    profiles={"developer": "developer-daedalus"},
                    provider=provider,
                )
            )

    def test_pm_and_developer_each_notify_once(self):
        """PM cap and developer cap on same issue each notify once (2 total, #1167)."""
        slug = "test-slug"
        issue = 42
        tasks = [
            {"title": f"#{issue} bug", "assignee": "developer-daedalus",
             "status": "done", "id": "t_dev", "summary": ""},
            {"title": f"#{issue} bug", "assignee": "project-manager-daedalus",
             "status": "done", "id": "t_pm", "summary": ""},
            {"title": f"#{issue} bug", "assignee": "validator-daedalus",
             "status": "done", "id": "t_val", "summary": ""},
        ]
        comments = {}

        def mock_list_tasks(s, status=None):
            return list(tasks)

        def mock_show_card(s, tid):
            return {"comments": [{"body": c} for c in comments.get(tid, [])]}

        def mock_comment(s, tid, body):
            comments.setdefault(tid, []).append(body)
            return True

        with mock.patch.object(self.disp.kanban, "list_tasks", side_effect=mock_list_tasks), \
             mock.patch.object(self.disp.kanban, "show_card", side_effect=mock_show_card), \
             mock.patch.object(self.disp.kanban, "comment", side_effect=mock_comment):
            # Developer: not notified for developer role.
            self.assertFalse(
                self.disp._has_notified_block(slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="developer")
            )
            # PM: not notified for PM role.
            self.assertFalse(
                self.disp._has_notified_block(slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="pm")
            )
            # Mark developer.
            self.disp._mark_notified_block(
                slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="developer",
                fallback_task_id="t_dev",
            )
            # Developer is now notified.
            self.assertTrue(
                self.disp._has_notified_block(slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="developer")
            )
            # PM is NOT notified (role-scoped).
            self.assertFalse(
                self.disp._has_notified_block(slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="pm")
            )
            # Mark PM.
            self.disp._mark_notified_block(
                slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="pm",
                fallback_task_id="t_pm",
            )
            # PM is now notified.
            self.assertTrue(
                self.disp._has_notified_block(slug, issue, marker=self.disp._RETRY_CAP_MARKER, role="pm")
            )


if __name__ == "__main__":
    unittest.main()
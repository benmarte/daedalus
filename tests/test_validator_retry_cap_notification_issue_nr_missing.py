"""
Tests for issue #378: Validator retry-cap notification when issue_nr is missing.

Scenario: A validator task has completed (status="done") with a non-CONFIRMED
verdict. The dispatcher tries to fetch the GitHub issue but fails (issue_nr
remains None). Previously, the `if not issue_nr: continue` guard would skip the
retry-cap check entirely, so retry-cap exhaustion notifications would never fire.

Fix: Move the retry-cap check BEFORE the `if not issue_nr: continue` guard.

Note (#916): the validator runs here carry a real non-CONFIRMED verdict summary
so they legitimately burn the cap. Empty/None summaries are failed delegations
and no longer count toward the cap — see test_retry_cap_github_comment.py.
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import _load_dispatch  # noqa: E402

disp = _load_dispatch()


def _minimal_resolved(*, notifications=None):
    cron = {}
    if notifications is not None:
        cron["notifications"] = notifications
    return {"cron": cron}


def test_retry_cap_notification_fires_when_issue_nr_missing():
    """
    When issue_nr is None (issue missing and can't be fetched), the retry-cap
    check should still run and emit the notification if retries are exhausted.

    Before fix: notification never fires because `if not issue_nr: continue` skips cap check.
    After fix: notification fires even when issue_nr is None.
    """
    # Simulate validator tasks with a real non-CONFIRMED verdict — cap reached (3 done tasks)
    fake_tasks = [
        {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "summary": "ran but produced no clear verdict"},
        {"id": "t2", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "summary": "ran but produced no clear verdict"},
        {"id": "t3", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "summary": "ran but produced no clear verdict"},
    ]

    # Empty issues_map — issue_nr will be None
    # provider=None — no fallback fetch, so issue_nr stays None
    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"latest_summary": None}), \
         mock.patch.object(disp.kanban, "comment"), \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify:

        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {},  # issues_map empty → issue_nr = None
            3, "/tmp", "", "main", "github",  # iterations, workdir, notify_target, base_branch, provider_name
            provider=None,
            resolved=_minimal_resolved(
                notifications=[{"platform": "Slack", "target": "slack:ops", "events": ["retry-cap-exhausted"]}]
            ),
        )

        # Verify: notification fired even though issue_nr is None
        assert mock_notify.called, "Notification must fire even when issue_nr is None (#378)"

        # Verify the call arguments
        call_args = mock_notify.call_args
        assert call_args[1]["role"] == "validator"
        assert call_args[1]["issue_number"] == 42
        assert call_args[1]["retry_count"] >= 3


def test_dedup_marker_stamped_when_issue_nr_missing():
    """
    The retry-cap marker must be stamped on the validator task even when
    issue_nr is None, so the next dispatcher tick doesn't re-fire the alert.
    """
    fake_tasks = [
        {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "summary": "ran but produced no clear verdict"},
        {"id": "t2", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "summary": "ran but produced no clear verdict"},
        {"id": "t3", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "summary": "ran but produced no clear verdict"},
    ]

    comments_store = []

    def fake_comment(slug, tid, body):
        comments_store.append({"body": body})

    def run_tick():
        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {},  # issue_nr = None
            3, "/tmp", "", "main", "github",
            provider=None,
            resolved=_minimal_resolved(
                notifications=[{"platform": "Slack", "target": "slack:ops", "events": ["retry-cap-exhausted"]}]
            ),
        )

    # First tick
    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": comments_store}), \
         mock.patch.object(disp.kanban, "comment", side_effect=fake_comment), \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify:
        run_tick()

        # Notification fired
        assert mock_notify.call_count == 1
        # Dedicated marker stamped
        assert any(disp._RETRY_CAP_MARKER in c["body"] for c in comments_store)

    # Second tick - with marker present
    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": comments_store}), \
         mock.patch.object(disp.kanban, "comment", side_effect=fake_comment), \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify:
        run_tick()

        # Notification does NOT fire again (dedup works)
        assert mock_notify.call_count == 0


def test_no_duplicate_notifications_with_concurrent_retries():
    """
    When multiple validator tasks for the same issue are done with empty summaries,
    the notification should fire exactly once (dedup marker prevents duplicates).
    """
    # Simulate multiple validator tasks for the same issue, all with empty summaries
    fake_tasks = [
        {"id": "t1", "title": "#42 fix bug attempt 1", "assignee": "validator-daedalus", "status": "done", "summary": "ran but produced no clear verdict"},
        {"id": "t2", "title": "#42 fix bug attempt 2", "assignee": "validator-daedalus", "status": "done", "summary": "ran but produced no clear verdict"},
        {"id": "t3", "title": "#42 fix bug attempt 3", "assignee": "validator-daedalus", "status": "done", "summary": "ran but produced no clear verdict"},
    ]

    comments_store = []

    def fake_comment(slug, tid, body):
        comments_store.append({"body": body})

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": comments_store}), \
         mock.patch.object(disp.kanban, "comment", side_effect=fake_comment), \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify:

        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {},  # issue_nr = None
            3, "/tmp", "", "main", "github",
            provider=None,
            resolved=_minimal_resolved(
                notifications=[{"platform": "Slack", "target": "slack:ops", "events": ["retry-cap-exhausted"]}]
            ),
        )

        # Notification fires exactly once (not once per task)
        assert mock_notify.call_count == 1

        # Marker stamped (prevents future ticks from re-firing)
        assert any(disp._RETRY_CAP_MARKER in c["body"] for c in comments_store)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])

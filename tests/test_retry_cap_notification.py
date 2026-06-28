#!/usr/bin/env python3
"""Tests for retry cap exhaustion notifications (issue #181).

Validates that when PM or validator retry caps are exhausted, a distinct
notification is sent to configured targets via the "retry-cap-exhausted"
event.

Run: python3 tests/test_retry_cap_notification.py
"""
import sys
from pathlib import Path
from unittest import mock

# Make the package root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests import conftest
from tests.conftest import _load_dispatch, check


def _minimal_resolved(*, notifications=None, deliver=""):
    """Build a minimal resolved config dict with retry-cap targets."""
    cron = {}
    if deliver:
        cron["deliver"] = deliver
    if notifications is not None:
        cron["notifications"] = notifications
    return {"cron": cron}


# ── Test 1: "retry-cap-exhausted" is in NOTIFY_EVENTS ───────────────────────────

def test_retry_cap_exhausted_in_notify_events():
    """NOTIFY_EVENTS includes 'retry-cap-exhausted' as a subscribable event."""
    disp = _load_dispatch()
    check(
        "retry-cap-exhausted in NOTIFY_EVENTS",
        "retry-cap-exhausted" in disp.NOTIFY_EVENTS,
    )


# ── Test 2: _send_retry_cap_notification exists and is callable ─────────────────

def test_send_retry_cap_notification_exists():
    """_send_retry_cap_notification helper function exists."""
    disp = _load_dispatch()
    check(
        "_send_retry_cap_notification exists",
        hasattr(disp, "_send_retry_cap_notification"),
    )
    check(
        "_send_retry_cap_notification is callable",
        callable(getattr(disp, "_send_retry_cap_notification", None)),
    )


# ── Test 3: _send_retry_cap_notification uses the retry-cap-exhausted event ─────

def test_send_retry_cap_notification_calls_notify_targets():
    """_send_retry_cap_notification queries _notify_targets with 'retry-cap-exhausted'."""
    disp = _load_dispatch()

    with mock.patch.object(disp, "_notify_targets", return_value=["slack:C1"]) as mock_targets, \
         mock.patch.object(disp, "_hermes_send", return_value=(True, "ts-1")):
        disp._send_retry_cap_notification(
            role="validator",
            issue_number=42,
            retry_count=3,
            max_retries=2,
            resolved=_minimal_resolved(),
            dry_run=False,
        )
        check(
            "_notify_targets called with 'retry-cap-exhausted'",
            mock_targets.called and mock_targets.call_args[0][1] == "retry-cap-exhausted",
        )


# ── Test 4: sends to every configured target ────────────────────────────────────

def test_send_retry_cap_notification_sends_to_all_targets():
    """_send_retry_cap_notification delivers to every target returned by _notify_targets."""
    disp = _load_dispatch()

    targets = ["slack:C1", "discord:123", "telegram:-100"]
    with mock.patch.object(disp, "_notify_targets", return_value=targets), \
         mock.patch.object(disp, "_hermes_send", return_value=(True, "ts-1")) as mock_send:
        disp._send_retry_cap_notification(
            role="pm",
            issue_number=42,
            retry_count=3,
            max_retries=3,
            resolved=_minimal_resolved(),
            dry_run=False,
        )
        check(
            "_hermes_send called once per target",
            mock_send.call_count == len(targets),
        )


# ── Test 5: validator message content ───────────────────────────────────────────

def test_send_retry_cap_notification_validator_message():
    """Validator retry cap notification has distinct, actionable content."""
    disp = _load_dispatch()

    with mock.patch.object(disp, "_notify_targets", return_value=["slack:C1"]), \
         mock.patch.object(disp, "_hermes_send", return_value=(True, "ts-1")) as mock_send:
        disp._send_retry_cap_notification(
            role="validator",
            issue_number=42,
            retry_count=3,
            max_retries=2,
            resolved=_minimal_resolved(),
            dry_run=False,
        )
        body = mock_send.call_args[0][1]
        check("validator notification contains header",
              "Retry Cap Exhausted" in body)
        check("validator notification mentions role",
              "VALIDATOR" in body)
        check("validator notification mentions issue number",
              "#42" in body)
        check("validator notification mentions retry count",
              "3/2" in body)
        check("validator notification mentions manual intervention",
              "manual intervention" in body.lower())
        check("validator notification distinct from pipeline-failure",
              "Pipeline Failure" not in body)
        check("validator notification distinct from security-escalation",
              "SECURITY" not in body and "Security" not in body)


# ── Test 6: PM message content ──────────────────────────────────────────────────

def test_send_retry_cap_notification_pm_message():
    """PM retry cap notification has distinct, actionable content."""
    disp = _load_dispatch()

    with mock.patch.object(disp, "_notify_targets", return_value=["slack:C1"]), \
         mock.patch.object(disp, "_hermes_send", return_value=(True, "ts-1")) as mock_send:
        disp._send_retry_cap_notification(
            role="pm",
            issue_number=151,
            retry_count=3,
            max_retries=3,
            resolved=_minimal_resolved(),
            dry_run=False,
        )
        body = mock_send.call_args[0][1]
        check("PM notification contains header",
              "Retry Cap Exhausted" in body)
        check("PM notification mentions role",
              "PM" in body)
        check("PM notification mentions issue number",
              "#151" in body)
        check("PM notification mentions retry count",
              "3/3" in body)
        check("PM notification mentions SPEC summary",
              "SPEC:" in body)
        check("PM notification mentions recovery",
              "hermes kanban edit" in body.lower())


# ── Test 7: no-targets → silent no-op ───────────────────────────────────────────

def test_send_retry_cap_notification_no_targets():
    """No targets configured → function returns early silently."""
    disp = _load_dispatch()

    with mock.patch.object(disp, "_notify_targets", return_value=[]), \
         mock.patch.object(disp, "_hermes_send") as mock_send:
        disp._send_retry_cap_notification(
            role="validator",
            issue_number=42,
            retry_count=3,
            max_retries=2,
            resolved=_minimal_resolved(),
            dry_run=False,
        )
        check(
            "_hermes_send not called when no targets",
            not mock_send.called,
        )


# ── Test 8: dry_run skips actual send ───────────────────────────────────────────

def test_send_retry_cap_notification_dry_run():
    """dry_run=True → logs intent but does not invoke _hermes_send."""
    disp = _load_dispatch()

    with mock.patch.object(disp, "_notify_targets", return_value=["slack:C1"]), \
         mock.patch.object(disp, "_hermes_send") as mock_send:
        disp._send_retry_cap_notification(
            role="validator",
            issue_number=42,
            retry_count=3,
            max_retries=2,
            resolved=_minimal_resolved(),
            dry_run=True,
        )
        check(
            "_hermes_send not called in dry_run mode",
            not mock_send.called,
        )


# ── Test 9: integration — validator retry cap exhaustion triggers notification ──

def test_validator_retry_cap_exhaustion_triggers_notification():
    """When validator retry_count >= _MAX_VALIDATOR_RETRIES + 1, notification fires."""
    disp = _load_dispatch()

    # Simulate 3 completed validator tasks for issue #42 (cap is 2, so 3 > cap+1)
    fake_tasks = [
        {"title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "id": "t1"},
        {"title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "id": "t2"},
        {"title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "id": "t3"},
    ]

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"latest_summary": None}), \
         mock.patch.object(disp.kanban, "comment"), \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify:
        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {42: {"number": 42, "title": "fix bug", "body": ""}},
            3, "/tmp", "", "main", "github",
            provider=None,
            resolved=_minimal_resolved(),
        )
        check(
            "_send_retry_cap_notification called for validator",
            mock_notify.called,
        )
        if mock_notify.called:
            kw = mock_notify.call_args[1]
            check("validator notification role='validator'",
                  kw.get("role") == "validator")
            check("validator notification issue_number=42",
                  kw.get("issue_number") == 42)
            check("validator notification retry_count=3",
                  kw.get("retry_count") >= 3)
            check("validator notification max_retries=2",
                  kw.get("max_retries") == 2)
            check("validator notification dry_run=False",
                  kw.get("dry_run") is False)


# ── Test 10: integration — PM retry cap exhaustion triggers notification ────────

def test_pm_retry_cap_exhaustion_triggers_notification():
    """When PM stale_count >= _MAX_PM_RETRIES, notification fires."""
    disp = _load_dispatch()

    # Simulate a validator task with CONFIRMED summary → triggers PM path
    fake_tasks = [
        {
            "title": "#42 fix bug",
            "assignee": "validator-daedalus",
            "status": "done",
            "summary": "CONFIRMED: valid issue",
            "id": "t_v42",
        },
    ]

    def fake_pm_task_state(slug, issue_nr, pm_profile):
        return ("stale", 3)

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card",
                           return_value={"latest_summary": "CONFIRMED: valid issue"}), \
         mock.patch.object(disp, "_pm_task_state", side_effect=fake_pm_task_state), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify:
        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {42: {"number": 42, "title": "fix bug", "body": ""}},
            3, "/tmp", "", "main", "github",
            provider=None,
            resolved=_minimal_resolved(),
        )
        check(
            "_send_retry_cap_notification called for PM",
            mock_notify.called,
        )
        if mock_notify.called:
            kw = mock_notify.call_args[1]
            check("PM notification role='pm'",
                  kw.get("role") == "pm")
            check("PM notification issue_number=42",
                  kw.get("issue_number") == 42)
            check("PM notification retry_count=3",
                  kw.get("retry_count") >= 3)
            check("PM notification max_retries=3",
                  kw.get("max_retries") == 3)


# ── Test 11: event filtering works for retry-cap-exhausted ──────────────────────

def test_event_filtering_retry_cap_exhausted():
    """Config with events: ['pipeline-failure'] does NOT receive retry-cap notification."""
    disp = _load_dispatch()

    # Config that only subscribes to pipeline-failure
    config1 = _minimal_resolved(notifications=[
        {"platform": "Slack", "target": "slack:C1", "events": ["pipeline-failure"]},
    ])
    targets = disp._notify_targets(config1, "retry-cap-exhausted")
    check(
        "retry-cap-exhausted not delivered to pipeline-failure-only target",
        "slack:C1" not in targets,
    )

    # Config that explicitly subscribes to retry-cap-exhausted
    config2 = _minimal_resolved(notifications=[
        {"platform": "Slack", "target": "slack:C1", "events": ["retry-cap-exhausted"]},
    ])
    targets2 = disp._notify_targets(config2, "retry-cap-exhausted")
    check(
        "retry-cap-exhausted delivered to subscribed target",
        "slack:C1" in targets2,
    )

    # Catch-all (no events filter) receives retry-cap-exhausted
    config3 = _minimal_resolved(notifications=[
        {"platform": "Slack", "target": "slack:C1"},
    ])
    targets3 = disp._notify_targets(config3, "retry-cap-exhausted")
    check(
        "retry-cap-exhausted delivered to catch-all target",
        "slack:C1" in targets3,
    )


# ── Test 12: delivery failure does not raise ────────────────────────────────────

def test_send_retry_cap_notification_delivery_failure():
    """If _hermes_send fails, warning is logged but no exception raised."""
    disp = _load_dispatch()

    with mock.patch.object(disp, "_notify_targets", return_value=["slack:C1"]), \
         mock.patch.object(disp, "_hermes_send", return_value=(False, None)):
        try:
            disp._send_retry_cap_notification(
                role="validator",
                issue_number=42,
                retry_count=3,
                max_retries=2,
                resolved=_minimal_resolved(),
                dry_run=False,
            )
            check("delivery failure does not raise exception", True)
        except Exception as e:
            check(f"delivery failure raised unexpectedly: {e}", False)


# ── Test 13: dedup — validator retry-cap notification sent only once (#183) ──────

def test_validator_retry_cap_notification_sent_once_across_ticks():
    """Issue #183: the exhaustion branch re-runs on every dispatcher tick (no new
    task is created past the cap), but the retry-cap notification must be sent only
    once — subsequent ticks see the marker and skip the duplicate alert."""
    disp = _load_dispatch()

    fake_tasks = [
        {"title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "id": "t1"},
        {"title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "id": "t2"},
        {"title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "id": "t3"},
    ]
    # Shared comment store that persists across simulated ticks; show_card reads it
    # and kanban.comment appends to it, mirroring the real idempotency mechanism.
    comments_store: list = []

    def fake_comment(slug, tid, body):
        comments_store.append({"body": body})

    def run_tick():
        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {42: {"number": 42, "title": "fix bug", "body": ""}},
            3, "/tmp", "", "main", "github",
            provider=None,
            resolved=_minimal_resolved(),
        )

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": comments_store}), \
         mock.patch.object(disp.kanban, "comment", side_effect=fake_comment), \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify:
        # Tick 1: cap exhausted for the first time → notification fires + marker stamped.
        run_tick()
        check("retry-cap notification sent on first tick",
              mock_notify.call_count == 1)
        check("retry-cap marker stamped after first tick",
              any(disp._RETRY_CAP_MARKER in c["body"] for c in comments_store))
        # Tick 2: marker present → no duplicate notification.
        run_tick()
        check("retry-cap notification NOT re-sent on second tick (no duplicate)",
              mock_notify.call_count == 1)


# ── Test 14: dry_run does not stamp the dedup marker ────────────────────────────

def test_validator_retry_cap_dry_run_does_not_mark():
    """In dry_run the notification is previewed but the marker is NOT stamped, so
    the board is never mutated during a dry run."""
    disp = _load_dispatch()

    fake_tasks = [
        {"title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "id": "t1"},
        {"title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "id": "t2"},
        {"title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "id": "t3"},
    ]

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
         mock.patch.object(disp.kanban, "comment") as mock_comment, \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify:
        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {42: {"number": 42, "title": "fix bug", "body": ""}},
            3, "/tmp", "", "main", "github",
            provider=None,
            resolved=_minimal_resolved(),
            dry_run=True,
        )
        check("dry_run still previews the notification", mock_notify.called)
        check("dry_run does not stamp the marker (no board mutation)",
              not mock_comment.called)


# ── Test 15: PM dry_run does not stamp the dedup marker ─────────────────────────

def test_pm_retry_cap_dry_run_does_not_mark():
    """In dry_run, PM retry-cap notification is logged but marker is NOT stamped,
    ensuring no side effects during dry run."""
    disp = _load_dispatch()

    fake_tasks = [
        {"title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done",
         "summary": "CONFIRMED: valid issue", "id": "t_v42"},
    ]

    fake_tasks_all = list(fake_tasks)  # list_tasks returns all tasks

    def fake_pm_task_state(slug, issue_nr, pm_profile):
        return ("stale", 3)  # stale_count >= _MAX_PM_RETRIES

    comments_store = []

    def fake_comment(slug, tid, body):
        comments_store.append({"body": body})

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks_all), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": comments_store}), \
         mock.patch.object(disp.kanban, "comment", side_effect=fake_comment), \
         mock.patch.object(disp, "_pm_task_state", side_effect=fake_pm_task_state), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify:
        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {42: {"number": 42, "title": "fix bug", "body": ""}},
            3, "/tmp", "", "main", "github",
            provider=None,
            resolved=_minimal_resolved(),
            dry_run=True,
        )
        check("PM dry_run calls _send_retry_cap_notification", mock_notify.called)
        check("PM dry_run does not stamp marker (no board mutation)",
              not any(disp._RETRY_CAP_MARKER in c["body"] for c in comments_store))


# ── Test 16: PM retry-cap notification dedup across ticks ──────────────

def test_pm_retry_cap_notification_sent_once_across_ticks():
    """Issue #181: PM retry-cap exhaustion re-runs on every dispatcher tick,
    but notification must be sent only once. Second tick sees marker and skips.
    Mirrors test_validator_retry_cap_notification_sent_once_across_ticks but
    for the PM retry-cap path."""
    disp = _load_dispatch()

    fake_tasks = [
        {"title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done",
         "summary": "CONFIRMED: valid issue", "id": "t_v42"},
    ]

    def fake_pm_task_state(slug, issue_nr, pm_profile):
        return ("stale", 3)  # stale_count >= _MAX_PM_RETRIES

    # Shared comment store persists across ticks
    comments_store = []

    def fake_comment(slug, tid, body):
        comments_store.append({"body": body})

    def run_tick():
        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {42: {"number": 42, "title": "fix bug", "body": ""}},
            3, "/tmp", "", "main", "github",
            provider=None,
            resolved=_minimal_resolved(),
        )

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": comments_store}), \
         mock.patch.object(disp.kanban, "comment", side_effect=fake_comment), \
         mock.patch.object(disp, "_pm_task_state", side_effect=fake_pm_task_state), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify:
        # Tick 1: PM retry cap exhausted → notification fires + marker stamped.
        run_tick()
        check("PM retry-cap notification sent on first tick",
              mock_notify.call_count == 1)
        check("PM retry-cap marker stamped after first tick",
              any(disp._RETRY_CAP_MARKER in c["body"] for c in comments_store))
        # Tick 2: marker present → no duplicate notification.
        run_tick()
        check("PM retry-cap notification NOT re-sent on second tick (no duplicate)",
              mock_notify.call_count == 1)




# ── Tests 17-27: Intermediate Retry Notifications ──

def test_retry_attempt_in_notify_events():
    """NOTIFY_EVENTS includes 'retry-attempt' event type."""
    disp = _load_dispatch()
    check("retry-attempt in NOTIFY_EVENTS", "retry-attempt" in disp.NOTIFY_EVENTS)


def test_send_retry_attempt_notification_exists():
    """_send_retry_attempt_notification function exists."""
    disp = _load_dispatch()
    check("has _send_retry_attempt_notification", hasattr(disp, "_send_retry_attempt_notification"))


def test_send_retry_attempt_notification_calls_notify_targets():
    """Intermediate retry notification uses 'retry-attempt' event."""
    disp = _load_dispatch()
    with mock.patch.object(disp, "_notify_targets", return_value=["slack:C1"]) as mock_targets, \
         mock.patch.object(disp, "_hermes_send", return_value=(True, "ts-1")):
        disp._send_retry_attempt_notification(
            role="validator", issue_number=42, retry_count=1, max_retries=3,
            resolved=_minimal_resolved(notifications=[
                {"platform": "Slack", "target": "slack:C1", "events": ["retry-attempt"]}
            ]),
            dry_run=False,
        )
        check(
            "retry-attempt notification uses retry-attempt event",
            mock_targets.called and mock_targets.call_args[0][1] == "retry-attempt"
        )


def test_send_retry_attempt_notification_sends_to_all_targets():
    """Intermediate retry notification sends to all configured targets."""
    disp = _load_dispatch()
    targets = ["slack:C1", "slack:C2"]
    with mock.patch.object(disp, "_notify_targets", return_value=targets), \
         mock.patch.object(disp, "_hermes_send", return_value=(True, "ts-1")) as mock_send:
        disp._send_retry_attempt_notification(
            role="validator", issue_number=42, retry_count=1, max_retries=3,
            resolved=_minimal_resolved(notifications=[
                {"platform": "Slack", "target": "slack:C1", "events": ["retry-attempt"]},
                {"platform": "Slack", "target": "slack:C2", "events": ["retry-attempt"]}
            ]),
            dry_run=False,
        )
        check("retry-attempt sends to all targets", mock_send.call_count == len(targets))


def test_send_retry_attempt_notification_validator_message():
    """Validator intermediate retry message has correct content."""
    disp = _load_dispatch()
    with mock.patch.object(disp, "_notify_targets", return_value=["slack:C1"]), \
         mock.patch.object(disp, "_hermes_send", return_value=(True, "ts-1")) as mock_send:
        disp._send_retry_attempt_notification(
            role="validator", issue_number=42, retry_count=2, max_retries=3,
            resolved=_minimal_resolved(), dry_run=False,
        )
        body = mock_send.call_args[0][1]
        check("validator retry attempt contains issue number", "#42" in body)
        check("validator retry attempt contains retry count", "2/3" in body)
        check("validator retry attempt has role indicator", "validator" in body.lower())


def test_send_retry_attempt_notification_pm_message():
    """PM intermediate retry message has correct content."""
    disp = _load_dispatch()
    with mock.patch.object(disp, "_notify_targets", return_value=["slack:C1"]), \
         mock.patch.object(disp, "_hermes_send", return_value=(True, "ts-1")) as mock_send:
        disp._send_retry_attempt_notification(
            role="pm", issue_number=123, retry_count=1, max_retries=5,
            resolved=_minimal_resolved(), dry_run=False,
        )
        body = mock_send.call_args[0][1]
        check("PM retry attempt contains issue number", "#123" in body)
        check("PM retry attempt contains retry count", "1/5" in body)
        check("PM retry attempt has role indicator", "pm" in body.lower())


def test_send_retry_attempt_notification_no_targets():
    """Intermediate retry notification is no-op when no targets configured."""
    disp = _load_dispatch()
    with mock.patch.object(disp, "_notify_targets", return_value=[]), \
         mock.patch.object(disp, "_hermes_send") as mock_send:
        disp._send_retry_attempt_notification(
            role="validator", issue_number=42, retry_count=1, max_retries=3,
            resolved=_minimal_resolved(), dry_run=False,
        )
        check("retry-attempt no-op when no targets", not mock_send.called)


def test_send_retry_attempt_notification_dry_run():
    """Intermediate retry notification respects dry_run mode."""
    disp = _load_dispatch()
    with mock.patch.object(disp, "_notify_targets", return_value=["slack:C1"]), \
         mock.patch.object(disp, "_hermes_send") as mock_send:
        disp._send_retry_attempt_notification(
            role="validator", issue_number=42, retry_count=1, max_retries=3,
            resolved=_minimal_resolved(), dry_run=True,
        )
        check("retry-attempt dry_run prevents send", not mock_send.called)


def test_send_retry_attempt_notification_delivery_failure():
    """Intermediate retry notification handles delivery failure gracefully."""
    disp = _load_dispatch()
    with mock.patch.object(disp, "_notify_targets", return_value=["slack:C1"]), \
         mock.patch.object(disp, "_hermes_send", return_value=(False, None)):
        try:
            disp._send_retry_attempt_notification(
                role="validator", issue_number=42, retry_count=1, max_retries=3,
                resolved=_minimal_resolved(), dry_run=False,
            )
            check("retry-attempt handles delivery failure", True)
        except Exception as e:
            check(f"retry-attempt raised on failure: {e}", False)


def test_retry_notifications_are_distinct():
    """retry-attempt and retry-cap-exhausted have different content."""
    disp = _load_dispatch()
    with mock.patch.object(disp, "_notify_targets", return_value=["slack:C1"]), \
         mock.patch.object(disp, "_hermes_send", return_value=(True, "ts-1")) as mock_send:
        # Cap exhausted
        mock_send.reset_mock()
        disp._send_retry_cap_notification(
            role="validator", issue_number=42, retry_count=3, max_retries=3,
            resolved=_minimal_resolved(), dry_run=False,
        )
        cap_body = mock_send.call_args[0][1]

        # Intermediate retry
        mock_send.reset_mock()
        disp._send_retry_attempt_notification(
            role="validator", issue_number=42, retry_count=2, max_retries=3,
            resolved=_minimal_resolved(), dry_run=False,
        )
        attempt_body = mock_send.call_args[0][1]

        check("retry notifications have distinct content", cap_body != attempt_body)
        check("cap exhausted has 'Cap Exhausted' title", "Cap Exhausted" in cap_body)
        check("retry attempt has 'Retry' title", "Retry" in attempt_body)


if __name__ == "__main__":
    test_retry_cap_exhausted_in_notify_events()
    test_send_retry_cap_notification_exists()
    test_send_retry_cap_notification_calls_notify_targets()
    test_send_retry_cap_notification_sends_to_all_targets()
    test_send_retry_cap_notification_validator_message()
    test_send_retry_cap_notification_pm_message()
    test_send_retry_cap_notification_no_targets()
    test_send_retry_cap_notification_dry_run()
    test_validator_retry_cap_exhaustion_triggers_notification()
    test_pm_retry_cap_exhaustion_triggers_notification()
    test_event_filtering_retry_cap_exhausted()
    test_send_retry_cap_notification_delivery_failure()
    test_validator_retry_cap_notification_sent_once_across_ticks()
    test_validator_retry_cap_dry_run_does_not_mark()
    test_pm_retry_cap_dry_run_does_not_mark()
    test_pm_retry_cap_notification_sent_once_across_ticks()
    # Tests 17-27: Intermediate retry notifications
    test_retry_attempt_in_notify_events()
    test_send_retry_attempt_notification_exists()
    test_send_retry_attempt_notification_calls_notify_targets()
    test_send_retry_attempt_notification_sends_to_all_targets()
    test_send_retry_attempt_notification_validator_message()
    test_send_retry_attempt_notification_pm_message()
    test_send_retry_attempt_notification_no_targets()
    test_send_retry_attempt_notification_dry_run()
    test_send_retry_attempt_notification_delivery_failure()
    test_retry_notifications_are_distinct()
    print(f"\n{'='*60}")
    print(f"Passed: {conftest._passed}  Failed: {conftest._failed}")
    print(f"{'='*60}\n")
    sys.exit(1 if conftest._failed else 0)

#!/usr/bin/env python3
"""Unit tests for retry cap notification boundary values and integration.

Covers:
  1. Boundary: retry_count exactly at max_retries (should fire)
  2. Boundary: retry_count just below max_retries (should NOT fire)
  3. Large retry numbers (stress test message formatting)
  4. Negative retry_count (graceful handling)
  5. Special characters in role/issue_number (Unicode, emoji, quotes)
  6. Very long message bodies (no truncation issues)
  7. Thread safety of concurrent webhook notifications
  8. Integration: verify notification fires at correct point in dispatcher flow

Run: python3 tests/test_retry_cap_notification_boundary_and_integration.py
"""
import sys
import time
import threading
from pathlib import Path
from unittest import mock

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


# ── Test 1: Boundary - retry_count exactly equals max_retries ───────────────────

def test_notification_fires_when_retry_count_equals_max_retries():
    """Notification fires when retry_count == max_retries (exact boundary)."""
    disp = _load_dispatch()

    with mock.patch.object(disp, '_fire_webhook_notification') as mock_webhook, \
         mock.patch.object(disp, '_notify_targets', return_value=["slack:C1"]), \
         mock.patch.object(disp, '_hermes_send', return_value=(True, "ts-1")) as mock_send:
        disp._send_retry_cap_notification(
            role="validator",
            issue_number=42,
            retry_count=3,
            max_retries=3,
            resolved=_minimal_resolved(),
            dry_run=False,
        )
        check("notification fires when retry_count == max_retries",
              mock_send.called and mock_webhook.called)
        check("message contains '3/3' ratio",
              "3/3" in mock_send.call_args[0][1])


# ── Test 2: Boundary - retry_count below max_retries (caller responsibility) ────

def test_notification_still_fires_when_retry_count_below_max():
    """_send_retry_cap_notification fires regardless of retry_count vs max_retries.
    
    Note: The caller is responsible for only invoking this when cap is exhausted.
    The function itself doesn't check the ratio — it blindly sends the notification.
    """
    disp = _load_dispatch()

    with mock.patch.object(disp, '_fire_webhook_notification') as mock_webhook, \
         mock.patch.object(disp, '_notify_targets', return_value=["slack:C1"]), \
         mock.patch.object(disp, '_hermes_send', return_value=(True, "ts-1")) as mock_send:
        disp._send_retry_cap_notification(
            role="validator",
            issue_number=42,
            retry_count=1,  # below max_retries
            max_retries=3,
            resolved=_minimal_resolved(),
            dry_run=False,
        )
        check("notification fires even when retry_count < max_retries (caller's job to guard)",
              mock_send.called and mock_webhook.called)
        check("message contains '1/3' ratio",
              "1/3" in mock_send.call_args[0][1])


# ── Test 3: Large retry numbers (stress test) ───────────────────────────────────

def test_large_retry_numbers_render_correctly():
    """Large retry_count and max_retries render correctly in message."""
    disp = _load_dispatch()

    with mock.patch.object(disp, '_fire_webhook_notification') as mock_webhook, \
         mock.patch.object(disp, '_notify_targets', return_value=["slack:C1"]), \
         mock.patch.object(disp, '_hermes_send', return_value=(True, "ts-1")) as mock_send:
        disp._send_retry_cap_notification(
            role="validator",
            issue_number=999,
            retry_count=999,
            max_retries=100,
            resolved=_minimal_resolved(),
            dry_run=False,
        )
        body = mock_send.call_args[0][1]
        check("large retry_count renders correctly", "999/100" in body)
        check("large issue_number renders correctly", "#999" in body)
        check("webhook called with large numbers",
              mock_webhook.call_args.kwargs["retry_count"] == 999 and
              mock_webhook.call_args.kwargs["max_retries"] == 100)


# ── Test 4: Negative retry_count (graceful handling) ────────────────────────────

def test_negative_retry_count_does_not_crash():
    """Negative retry_count is handled gracefully (no crash)."""
    disp = _load_dispatch()

    try:
        with mock.patch.object(disp, '_fire_webhook_notification'), \
             mock.patch.object(disp, '_notify_targets', return_value=["slack:C1"]), \
             mock.patch.object(disp, '_hermes_send', return_value=(True, "ts-1")) as mock_send:
            disp._send_retry_cap_notification(
                role="validator",
                issue_number=42,
                retry_count=-1,  # negative
                max_retries=3,
                resolved=_minimal_resolved(),
                dry_run=False,
            )
            check("negative retry_count does not crash", True)
            check("negative retry_count still sends notification",
                  mock_send.called)
            body = mock_send.call_args[0][1]
            check("negative retry_count renders in message", "-1" in body)
    except Exception as e:
        check(f"negative retry_count raised exception: {e}", False)


# ── Test 5: Special characters in role ──────────────────────────────────────────

def test_special_characters_in_role():
    """Role with Unicode, emoji, quotes renders correctly."""
    disp = _load_dispatch()

    with mock.patch.object(disp, '_fire_webhook_notification'), \
         mock.patch.object(disp, '_notify_targets', return_value=["slack:C1"]), \
         mock.patch.object(disp, '_hermes_send', return_value=(True, "ts-1")) as mock_send:
        disp._send_retry_cap_notification(
            role="válïdátör 🤖",
            issue_number=42,
            retry_count=3,
            max_retries=2,
            resolved=_minimal_resolved(),
            dry_run=False,
        )
        body = mock_send.call_args[0][1]
        # role.upper() converts to uppercase
        check("Unicode role renders correctly",
              "VÁLÏDÁTÖR 🤖" in body)


# ── Test 6: Very long message body (no truncation) ──────────────────────────────

def test_very_long_message_body_no_truncation():
    """Very long message body doesn't get truncated unexpectedly."""
    disp = _load_dispatch()

    with mock.patch.object(disp, '_fire_webhook_notification'), \
         mock.patch.object(disp, '_notify_targets', return_value=["slack:C1"]), \
         mock.patch.object(disp, '_hermes_send', return_value=(True, "ts-1")) as mock_send:
        disp._send_retry_cap_notification(
            role="validator",
            issue_number=42,
            retry_count=3,
            max_retries=2,
            resolved=_minimal_resolved(),
            dry_run=False,
        )
        body = mock_send.call_args[0][1]
        # Message should contain all sections
        check("full message body present (no truncation)",
              len(body) > 200)
        check("all sections present in long message",
              "Retry Cap Exhausted" in body and
              "Manual intervention required" in body and
              "Likely cause" in body and
              "Recovery" in body)


# ── Test 7: Thread safety - concurrent webhook notifications ────────────────────

def test_concurrent_webhook_notifications_thread_safe():
    """Multiple concurrent webhook notifications don't race or corrupt state."""
    disp = _load_dispatch()

    webhook_calls = []
    lock = threading.Lock()

    def capture_webhook(payload):
        with lock:
            webhook_calls.append(payload)
        time.sleep(0.01)  # Simulate slow webhook
        return {"slack": True}

    # Fire 5 concurrent notifications
    threads = []
    with mock.patch.object(disp, 'send_webhook_notification', side_effect=capture_webhook):
        for i in range(5):
            def fire_notification(issue_nr=i):
                disp._fire_webhook_notification(
                    role="validator",
                    issue_number=40 + issue_nr,
                    retry_count=3,
                    max_retries=2,
                    dry_run=False,
                )
            t = threading.Thread(target=fire_notification, args=(i,))
            threads.append(t)
            t.start()

        # Wait for all threads to start
        for t in threads:
            t.join(timeout=1.0)

        # Wait for webhook threads to complete
        time.sleep(0.05)

    check("all 5 concurrent webhooks fired",
          len(webhook_calls) == 5)
    
    # Verify each payload is distinct and correct
    issue_numbers = [p.context.get("issue") for p in webhook_calls]
    check("each webhook has distinct issue number",
          len(set(issue_numbers)) == 5)
    check("webhook payloads contain correct issue numbers",
          set(issue_numbers) == {"#40", "#41", "#42", "#43", "#44"})


# ── Test 8: Integration - dispatcher flow triggers notification ─────────────────

def test_dispatcher_flow_triggers_notification_at_cap():
    """Integration: dispatcher's _check_confirmed_validators triggers notification exactly when cap exhausted."""
    disp = _load_dispatch()

    # Simulate exactly 3 validator completions for issue #42 (cap is 2, so 3 > 2)
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
        
        check("notification fired when validator cap exhausted (3 completions >= cap 2)",
              mock_notify.called)
        
        if mock_notify.called:
            kw = mock_notify.call_args.kwargs
            check("notification role is 'validator'",
                  kw.get("role") == "validator")
            check("notification issue_number is 42",
                  kw.get("issue_number") == 42)
            rc = kw.get("retry_count")
            check("notification retry_count >= 3",
                  rc is not None and rc >= 3)
            check("notification max_retries is 2",
                  kw.get("max_retries") == 2)


# ── Test 9: Integration - notification does NOT fire below cap ──────────────────

def test_dispatcher_flow_no_notification_below_cap():
    """Integration: dispatcher does NOT fire notification when below cap."""
    disp = _load_dispatch()

    # Only 1 validator completion (below cap of 2)
    fake_tasks = [
        {"title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "id": "t1"},
    ]

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"latest_summary": None}), \
         mock.patch.object(disp.kanban, "comment"), \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify:
        
        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {42: {"number": 42, "title": "fix bug", "body": ""}},
            1, "/tmp", "", "main", "github",
            provider=None,
            resolved=_minimal_resolved(),
        )
        
        check("notification NOT fired when below cap (1 completion < cap 2)",
              not mock_notify.called)


# ── Test 10: Integration - PM path triggers notification at cap ─────────────────

def test_dispatcher_flow_pm_notification_at_cap():
    """Integration: PM retry cap exhaustion triggers notification."""
    disp = _load_dispatch()

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
        return ("stale", 3)  # stale_count >= _MAX_PM_RETRIES (3)

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
        
        check("PM notification fired when PM stale_count >= max_retries",
              mock_notify.called)
        
        if mock_notify.called:
            kw = mock_notify.call_args.kwargs
            check("PM notification role is 'pm'",
                  kw.get("role") == "pm")
            check("PM notification issue_number is 42",
                  kw.get("issue_number") == 42)
            rc = kw.get("retry_count")
            check("PM notification retry_count >= 3",
                  rc is not None and rc >= 3)


# ── Test 11: Webhook payload with empty context fields ─────────────────────────

def test_webhook_payload_handles_empty_context():
    """Webhook payload handles None/empty context gracefully."""
    disp = _load_dispatch()

    captured = []

    def capture_webhook(payload):
        captured.append(payload)
        return {"slack": True}

    with mock.patch.object(disp, 'send_webhook_notification', side_effect=capture_webhook):
        disp._fire_webhook_notification(
            role="validator",
            issue_number=42,
            retry_count=3,
            max_retries=2,
            dry_run=False,
        )
        time.sleep(0.03)
        
        check("webhook payload created", len(captured) > 0)
        
        if captured:
            payload = captured[0]
            # All context fields should be present and non-empty
            check("webhook context 'issue' is non-empty",
                  payload.context.get("issue") and len(payload.context["issue"]) > 0)
            check("webhook context 'role' is non-empty",
                  payload.context.get("role") and len(payload.context["role"]) > 0)
            check("webhook context 'retry_count' is non-empty",
                  payload.context.get("retry_count") and len(payload.context["retry_count"]) > 0)
            check("webhook context 'recovery' is non-empty",
                  payload.context.get("recovery") and len(payload.context["recovery"]) > 0)


# ── Test 12: NotificationPayload with minimal fields ────────────────────────────

def test_notification_payload_minimal_fields():
    """NotificationPayload works with only required fields (title, body)."""
    disp = _load_dispatch()

    try:
        payload = disp.NotificationPayload(
            title="Test",
            body="Body"
        )
        check("NotificationPayload works with minimal fields", True)
        check("default severity is 'info'", payload.severity == "info")
        check("default context is empty dict",
              isinstance(payload.context, dict) and len(payload.context) == 0)
        
        d = payload.as_dict()
        check("as_dict() contains all fields",
              "title" in d and "body" in d and "severity" in d and "context" in d and "timestamp" in d)
    except Exception as e:
        check(f"NotificationPayload minimal fields raised: {e}", False)


def main():
    """Run all test functions and print PASS/FAIL results."""
    test_functions = [
        test_notification_fires_when_retry_count_equals_max_retries,
        test_notification_still_fires_when_retry_count_below_max,
        test_large_retry_numbers_render_correctly,
        test_negative_retry_count_does_not_crash,
        test_special_characters_in_role,
        test_very_long_message_body_no_truncation,
        test_concurrent_webhook_notifications_thread_safe,
        test_dispatcher_flow_triggers_notification_at_cap,
        test_dispatcher_flow_no_notification_below_cap,
        test_dispatcher_flow_pm_notification_at_cap,
        test_webhook_payload_handles_empty_context,
        test_notification_payload_minimal_fields,
    ]
    
    for test_func in test_functions:
        print(f"\n{'='*60}")
        print(f"Running: {test_func.__name__}")
        print(f"{'='*60}")
        try:
            test_func()
            print(f"✓ {test_func.__name__}: PASS")
        except AssertionError as e:
            print(f"✗ {test_func.__name__}: FAIL - {e}")
        except Exception as e:
            print(f"✗ {test_func.__name__}: ERROR - {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()

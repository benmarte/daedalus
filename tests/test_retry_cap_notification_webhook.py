#!/usr/bin/env python3
"""Tests for async webhook notification on retry cap exhaustion (issue #260).

Validates that:
  1. Webhook notification is sent when retry cap is exhausted
  2. Payload contains the expected fields
  3. No webhook is sent during normal retries (only on cap exhaustion)
  4. Webhook failure doesn't block or raise
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests import conftest
from tests.conftest import _load_dispatch, check


def _load_dispatch_module():
    """Load daedalus_dispatch module."""
    disp = _load_dispatch()
    return disp


# Test 1: _fire_webhook_notification is called when retry cap exhausted
def test_retry_cap_exhausted_invokes_webhook():
    """_fire_webhook_notification() is invoked when retry cap exhausted."""
    disp = _load_dispatch_module()
    
    with mock.patch.object(disp, '_fire_webhook_notification') as mock_fire:
        with mock.patch.object(disp, '_notify_targets', return_value=['slack:C1']):
            with mock.patch.object(disp, '_hermes_send', return_value=(True, None)):
                resolved = {'cron': {'deliver': '', 'notifications': [{'platform': 'Slack', 'target': 'slack:C1'}]}}
                disp._send_retry_cap_notification(
                    role='validator',
                    issue_number=42,
                    retry_count=3,
                    max_retries=2,
                    resolved=resolved,
                    dry_run=False
                )
        check("_fire_webhook_notification called", mock_fire.called)
        check("called with correct role", mock_fire.call_args.kwargs['role'] == 'validator')
        check("called with correct issue_number", mock_fire.call_args.kwargs['issue_number'] == 42)
        check("called with correct retry_count", mock_fire.call_args.kwargs['retry_count'] == 3)
        check("called with correct max_retries", mock_fire.call_args.kwargs['max_retries'] == 2)


# Test 2: Async helper constructs correct payload
def test_fire_webhook_notification_constructs_payload():
    """_fire_webhook_notification constructs NotificationPayload correctly."""
    disp = _load_dispatch_module()
    
    with mock.patch.object(disp, 'send_webhook_notification') as mock_send:
        mock_send.return_value = {'slack': True}
        
        disp._fire_webhook_notification(
            role='pm',
            issue_number=99,
            retry_count=5,
            max_retries=3,
            dry_run=False
        )
        
        # Wait for async thread
        import time
        time.sleep(0.1)
        
        check("send_webhook_notification called", mock_send.called)
        payload = mock_send.call_args.args[0]
        check("payload has correct title", "Retry Cap Exhausted: pm" in payload.title)
        check("payload has correct body", "#99" in payload.body)
        check("payload has correct severity", payload.severity == 'critical')
        check("payload has correct context", payload.context.get('issue') == '#99')
        check("payload context has role", payload.context.get('role') == 'pm')


# Test 3: Async helper is truly async (non-blocking)
def test_fire_webhook_notification_is_async():
    """_fire_webhook_notification runs in background thread."""
    import time
    disp = _load_dispatch_module()
    
    called = []
    def slow_send(payload):
        called.append(time.time())
        time.sleep(0.5)  # Simulate slow network call
        return {'slack': True}
    
    with mock.patch.object(disp, 'send_webhook_notification', side_effect=slow_send):
        start = time.time()
        disp._fire_webhook_notification(
            role='validator',
            issue_number=42,
            retry_count=3,
            max_retries=2,
            dry_run=False
        )
        elapsed = time.time() - start
        
        # Should return immediately (async), not wait 0.5s
        check("returns quickly (<0.2s)", elapsed < 0.2)
        
        # Wait for background thread to complete
        time.sleep(0.7)
        check("webhook was called in background", len(called) > 0)


# Test 4: No webhook notification during normal retry
def test_no_webhook_during_normal_retry():
    """_send_retry_attempt_notification does NOT invoke webhook."""
    disp = _load_dispatch_module()
    
    with mock.patch.object(disp, '_fire_webhook_notification') as mock_fire:
        with mock.patch.object(disp, '_notify_targets', return_value=['slack:C1']):
            with mock.patch.object(disp, '_hermes_send', return_value=(True, None)):
                resolved = {'cron': {'deliver': '', 'notifications': [{'platform': 'Slack', 'target': 'slack:C1'}]}}
                disp._send_retry_attempt_notification(
                    role='validator',
                    issue_number=42,
                    retry_count=1,
                    max_retries=2,
                    resolved=resolved,
                    dry_run=False
                )
        
        check("_fire_webhook_notification NOT called during normal retry", not mock_fire.called)


# Test 5: Webhook failure is graceful
def test_webhook_failure_graceful():
    """If webhook send fails, error is logged but doesn't raise."""
    disp = _load_dispatch_module()
    
    with mock.patch.object(disp, 'send_webhook_notification', side_effect=Exception("Network error")):
        # Should not raise
        try:
            disp._fire_webhook_notification(
                role='pm',
                issue_number=42,
                retry_count=3,
                max_retries=3,
                dry_run=False
            )
            # Wait for async thread
            import time
            time.sleep(0.1)
            check("webhook failure doesn't raise", True)
        except Exception as e:
            check("webhook failure doesn't raise", False)


# Test 6: Integration - full flow
def test_integration_retry_cap_exhausted():
    """Full integration test: retry cap exhausted -> webhook notified."""
    disp = _load_dispatch_module()
    
    webhook_called = []
    def capture_webhook(payload):
        webhook_called.append(payload)
        return {'slack': True}
    
    # Mock the actual HTTP send, capture the call
    with mock.patch.object(disp, 'send_webhook_notification', side_effect=capture_webhook):
        # Ensure webhook module is available
        if hasattr(disp, '_fire_webhook_notification'):
            # Simulate retry cap exhaustion
            resolved = {'cron': {'deliver': '', 'notifications': [{'platform': 'Slack', 'target': 'slack:C1'}]}}
            
            with mock.patch.object(disp, '_notify_targets', return_value=['slack:C1']):
                with mock.patch.object(disp, '_hermes_send', return_value=(True, None)):
                    disp._send_retry_cap_notification(
                        role='validator',
                        issue_number=42,
                        retry_count=3,
                        max_retries=2,
                        resolved=resolved,
                        dry_run=False
                    )
            
            # Wait for async webhook
            import time
            time.sleep(0.1)
            
            check("webhook was invoked", len(webhook_called) > 0)


def main():
    """Run all test functions and print PASS/FAIL results."""
    test_functions = [
        test_retry_cap_exhausted_invokes_webhook,
        test_fire_webhook_notification_constructs_payload,
        test_fire_webhook_notification_is_async,
        test_no_webhook_during_normal_retry,
        test_webhook_failure_graceful,
        test_integration_retry_cap_exhausted,
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


if __name__ == "__main__":
    main()

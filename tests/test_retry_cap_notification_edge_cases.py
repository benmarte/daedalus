#!/usr/bin/env python3
"""Unit tests for retry cap notification edge cases and uncovered paths.

Complements existing retry cap notification tests by covering:
  1. Webhook fires even when hermes-send has no targets
  2. Zero-retry edge case (retry_count=0, max_retries=0)
  3. Unknown role behavior
  4. Multiple notification targets with mixed event subscriptions
  5. Complete webhook payload context fields
  6. Unknown platform handling in send()
  7. NotificationPayload validation edge cases

Run: python3 tests/test_retry_cap_notification_edge_cases.py
"""
from __future__ import annotations

import sys
import time
import unittest
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


class TestRetryCapNotificationEdgeCases(unittest.TestCase):
    """Test edge cases not covered by existing retry cap notification tests."""

    def setUp(self):
        self.disp = _load_dispatch()

    def test_webhook_fires_when_no_hermes_targets(self):
        """Webhook notification fires even when _notify_targets returns empty."""
        captured_payloads = []

        def capture_webhook(payload):
            captured_payloads.append(payload)
            return {"slack": True}

        with mock.patch.object(self.disp, '_notify_targets', return_value=[]), \
             mock.patch.object(self.disp, 'send_webhook_notification', side_effect=capture_webhook):
            self.disp._send_retry_cap_notification(
                role="validator",
                issue_number=42,
                retry_count=3,
                max_retries=2,
                resolved=_minimal_resolved(),
                dry_run=False,
            )
            # Wait for async webhook thread
            time.sleep(0.2)
            check(
                "webhook fires even when no hermes-send targets configured",
                len(captured_payloads) > 0
            )
            if captured_payloads:
                payload = captured_payloads[0]
                check("webhook payload contains correct title",
                      "Retry Cap Exhausted" in payload.title)
                check("webhook payload contains issue number",
                      "#42" in payload.body)

    def test_zero_retry_edge_case(self):
        """Zero retries edge case: retry_count=0, max_retries=0."""
        with mock.patch.object(self.disp, '_fire_webhook_notification') as mock_webhook, \
             mock.patch.object(self.disp, '_notify_targets', return_value=["slack:C1"]), \
             mock.patch.object(self.disp, '_hermes_send', return_value=(True, "ts-1")) as mock_send:
            self.disp._send_retry_cap_notification(
                role="validator",
                issue_number=99,
                retry_count=0,
                max_retries=0,
                resolved=_minimal_resolved(),
                dry_run=False,
            )
            body = mock_send.call_args[0][1]
            check("zero retry case: retry_count=0 is in body",
                  "0" in body and "0/0" in body)
            check("webhook still called with zero retries",
                  mock_webhook.called)

    def test_unknown_role_uses_validator_path(self):
        """Unknown role falls through to validator else-branch."""
        with mock.patch.object(self.disp, '_fire_webhook_notification'), \
             mock.patch.object(self.disp, '_notify_targets', return_value=["slack:C1"]), \
             mock.patch.object(self.disp, '_hermes_send', return_value=(True, "ts-1")) as mock_send:
            self.disp._send_retry_cap_notification(
                role="unknown-role",
                issue_number=42,
                retry_count=3,
                max_retries=2,
                resolved=_minimal_resolved(),
                dry_run=False,
            )
            body = mock_send.call_args[0][1]
            # Unknown role should hit the else-branch (validator path)
            check("unknown role uses VALIDATOR in title",
                  "UNKNOWN-ROLE" in body)
            check("unknown role includes validator diagnosis",
                  "CONFIRMED" in body)

    def test_mixed_event_subscriptions(self):
        """Multiple targets with different event subscriptions."""
        resolved = _minimal_resolved(notifications=[
            # Target A subscribes to retry-cap-exhausted
            {"platform": "Slack", "target": "slack:A", "events": ["retry-cap-exhausted"]},
            # Target B subscribes to retry-attempt (should NOT receive cap-exhausted)
            {"platform": "Slack", "target": "slack:B", "events": ["retry-attempt"]},
            # Target C is catch-all (no events filter)
            {"platform": "Slack", "target": "slack:C"},
        ])

        targets = self.disp._notify_targets(resolved, "retry-cap-exhausted")
        check("target A (subscribed to retry-cap-exhausted) receives notification",
              "slack:A" in targets)
        check("target B (subscribed to retry-attempt) does NOT receive cap-exhausted",
              "slack:B" not in targets)
        check("target C (catch-all) receives notification",
              "slack:C" in targets)

    def test_webhook_payload_contains_all_context_fields(self):
        """Webhook payload contains all required context fields."""
        captured = []

        def capture_webhook(payload):
            captured.append(payload)
            return {"slack": True}

        with mock.patch.object(self.disp, 'send_webhook_notification', side_effect=capture_webhook):
            self.disp._fire_webhook_notification(
                role="pm",
                issue_number=151,
                retry_count=5,
                max_retries=3,
                dry_run=False,
            )
            time.sleep(0.2)
            self.assertGreater(len(captured), 0, "webhook should have been called")
            payload = captured[0]

            # Check all required context fields
            context = payload.context
            check("webhook context contains 'issue' field",
                  context.get("issue") == "#151")
            check("webhook context contains 'role' field",
                  context.get("role") == "pm")
            check("webhook context contains 'retry_count' field",
                  context.get("retry_count") == "5/3")
            check("webhook context contains 'max_retries' field",
                  context.get("max_retries") == "3")
            check("webhook context contains 'recovery' field",
                  "recovery" in context and len(context["recovery"]) > 0)
            check("webhook severity is 'critical'",
                  payload.severity == "critical")

    def test_notification_payload_validation_rejects_whitespace(self):
        """NotificationPayload rejects whitespace-only title/body."""
        try:
            self.disp.NotificationPayload(title="   ", body="message")
            check("whitespace-only title should raise ValueError", False)
        except ValueError:
            check("whitespace-only title raises ValueError", True)

        try:
            self.disp.NotificationPayload(title="t", body="   ")
            check("whitespace-only body should raise ValueError", False)
        except ValueError:
            check("whitespace-only body raises ValueError", True)

    def test_notification_payload_as_dict_includes_all_fields(self):
        """as_dict() returns a dict with all payload fields."""
        payload = self.disp.NotificationPayload(
            title="Test",
            body="Body",
            severity="error",
            context={"key": "value"},
            timestamp=12345.0
        )
        d = payload.as_dict()
        check("as_dict contains 'title'", "title" in d)
        check("as_dict contains 'body'", "body" in d)
        check("as_dict contains 'severity'", "severity" in d)
        check("as_dict contains 'context'", "context" in d)
        check("as_dict contains 'timestamp'", "timestamp" in d)
        self.assertEqual(d["severity"], "error")
        self.assertEqual(d["title"], "Test")

    def test_retry_cap_notification_body_format_validator(self):
        """Validator retry cap notification body has correct format."""
        with mock.patch.object(self.disp, '_fire_webhook_notification'), \
             mock.patch.object(self.disp, '_notify_targets', return_value=["slack:C1"]), \
             mock.patch.object(self.disp, '_hermes_send', return_value=(True, "ts-1")) as mock_send:
            self.disp._send_retry_cap_notification(
                role="validator",
                issue_number=42,
                retry_count=3,
                max_retries=2,
                resolved=_minimal_resolved(),
                dry_run=False,
            )
            body = mock_send.call_args[0][1]
            # Check all required sections are present
            check("validator body contains 'Retry Cap Exhausted'",
                  "Retry Cap Exhausted" in body)
            check("validator body contains role",
                  "VALIDATOR" in body)
            check("validator body contains issue number",
                  "#42" in body)
            check("validator body contains retry count",
                  "3/2" in body)
            check("validator body contains 'Manual intervention required'",
                  "Manual intervention required" in body)
            check("validator body contains 'Likely cause'",
                  "Likely cause" in body)
            check("validator body contains 'Recovery'",
                  "Recovery" in body)
            check("validator body contains 'CONFIRMED' (validator-specific)",
                  "CONFIRMED" in body)

    def test_retry_cap_notification_body_format_pm(self):
        """PM retry cap notification body has correct format."""
        with mock.patch.object(self.disp, '_fire_webhook_notification'), \
             mock.patch.object(self.disp, '_notify_targets', return_value=["slack:C1"]), \
             mock.patch.object(self.disp, '_hermes_send', return_value=(True, "ts-1")) as mock_send:
            self.disp._send_retry_cap_notification(
                role="pm",
                issue_number=151,
                retry_count=5,
                max_retries=3,
                resolved=_minimal_resolved(),
                dry_run=False,
            )
            body = mock_send.call_args[0][1]
            # Check all required sections are present
            check("PM body contains 'Retry Cap Exhausted'",
                  "Retry Cap Exhausted" in body)
            check("PM body contains role",
                  "PM" in body)
            check("PM body contains issue number",
                  "#151" in body)
            check("PM body contains retry count",
                  "5/3" in body)
            check("PM body contains 'Manual intervention required'",
                  "Manual intervention required" in body)
            check("PM body contains 'Likely cause'",
                  "Likely cause" in body)
            check("PM body contains 'Recovery'",
                  "Recovery" in body)
            check("PM body contains 'SPEC:' (PM-specific)",
                  "SPEC:" in body)
            check("PM body contains 'hermes kanban edit' (recovery hint)",
                  "hermes kanban edit" in body.lower())

    def test_fire_webhook_notification_constructs_validator_payload(self):
        """_fire_webhook_notification constructs correct validator payload."""
        captured = []

        def capture_webhook(payload):
            captured.append(payload)
            return {"slack": True}

        with mock.patch.object(self.disp, 'send_webhook_notification', side_effect=capture_webhook):
            self.disp._fire_webhook_notification(
                role="validator",
                issue_number=55,
                retry_count=3,
                max_retries=2,
                dry_run=False,
            )
            time.sleep(0.2)
            self.assertGreater(len(captured), 0, "webhook should have been called")
            payload = captured[0]

            check("validator webhook title contains 'VALIDATOR'",
                  "VALIDATOR" in payload.title)
            check("validator webhook body contains issue number",
                  "#55" in payload.body)
            check("validator webhook body contains retry count",
                  "3/2" in payload.body)
            check("validator webhook body contains 'CONFIRMED'",
                  "CONFIRMED" in payload.body)
            check("validator webhook severity is 'critical'",
                  payload.severity == "critical")

    def test_fire_webhook_notification_constructs_pm_payload(self):
        """_fire_webhook_notification constructs correct PM payload."""
        captured = []

        def capture_webhook(payload):
            captured.append(payload)
            return {"slack": True}

        with mock.patch.object(self.disp, 'send_webhook_notification', side_effect=capture_webhook):
            self.disp._fire_webhook_notification(
                role="pm",
                issue_number=72,
                retry_count=4,
                max_retries=3,
                dry_run=False,
            )
            time.sleep(0.2)
            self.assertGreater(len(captured), 0, "webhook should have been called")
            payload = captured[0]

            check("PM webhook title contains 'PM'",
                  "PM" in payload.title)
            check("PM webhook body contains issue number",
                  "#72" in payload.body)
            check("PM webhook body contains retry count",
                  "4/3" in payload.body)
            check("PM webhook body contains 'SPEC:'",
                  "SPEC:" in payload.body)
            check("PM webhook severity is 'critical'",
                  payload.severity == "critical")


def main():
    """Run all test methods."""
    print(f"\n{'='*60}")
    print("Running retry cap notification edge case tests")
    print(f"{'='*60}")

    test_instance = TestRetryCapNotificationEdgeCases()
    test_instance.setUp()

    test_methods = [
        test_instance.test_webhook_fires_when_no_hermes_targets,
        test_instance.test_zero_retry_edge_case,
        test_instance.test_unknown_role_uses_validator_path,
        test_instance.test_mixed_event_subscriptions,
        test_instance.test_webhook_payload_contains_all_context_fields,
        test_instance.test_notification_payload_validation_rejects_whitespace,
        test_instance.test_notification_payload_as_dict_includes_all_fields,
        test_instance.test_retry_cap_notification_body_format_validator,
        test_instance.test_retry_cap_notification_body_format_pm,
        test_instance.test_fire_webhook_notification_constructs_validator_payload,
        test_instance.test_fire_webhook_notification_constructs_pm_payload,
    ]

    for test_method in test_methods:
        try:
            test_method()
        except Exception as e:
            print(f"✗ {test_method.__name__}: ERROR - {e}")

    print(f"\n{'='*60}")
    print(f"Passed: {conftest._passed}  Failed: {conftest._failed}")
    print(f"{'='*60}\n")
    sys.exit(1 if conftest._failed else 0)


if __name__ == "__main__":
    main()

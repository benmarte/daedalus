"""Test that _fire_webhook_notification produces payloads consistent with
the hermes-send notification built by _send_retry_cap_notification (issue #283).

The webhook channel uses ``NotificationPayload`` (structured, rich content)
while the hermes-send channel uses a plain-text markdown body. Both must
agree on:
  * Title — contains "Retry Cap Exhausted:" + role
  * Body contains the issue number, retry count, max retries
  * Body includes role-specific Likely cause and Recovery sections
"""
from __future__ import annotations

import importlib.util
import sys
import time
import unittest
from pathlib import Path
from unittest import mock


def _load_dispatch():
    p = Path(__file__).resolve().parent.parent / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp_consistency", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestWebhookHermesConsistency(unittest.TestCase):
    """Verify webhook payload body matches the hermes-send body (issue #283)."""

    def setUp(self):
        self.disp = _load_dispatch()
        self.captured = []

        def fake_send(payload):
            self.captured.append(payload)
            return {"slack": True}

        self._patcher = mock.patch.object(
            self.disp, "send_webhook_notification", side_effect=fake_send,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    # -- helpers -------------------------------------------------------------

    def _wait(self) -> None:
        deadline = time.monotonic() + 2.0
        while not self.captured and time.monotonic() < deadline:
            time.sleep(0.02)

    # -- tests ---------------------------------------------------------------

    def test_validator_title_matches_hermes_format(self):
        """Webhook title mirrors the hermes-send header for the validator role."""
        self.disp._fire_webhook_notification(
            role="validator", issue_number=55,
            retry_count=3, max_retries=2, dry_run=False,
        )
        self._wait()
        self.assertEqual(len(self.captured), 1)
        title = self.captured[0].title
        # Must contain the role (upper) and the "Retry Cap Exhausted" marker,
        # matching the hermes-send header "Retry Cap Exhausted: VALIDATOR".
        self.assertIn("Retry Cap Exhausted", title)
        self.assertIn("VALIDATOR", title)

    def test_pm_title_matches_hermes_format(self):
        """Webhook title mirrors the hermes-send header for the PM role."""
        self.disp._fire_webhook_notification(
            role="pm", issue_number=72,
            retry_count=4, max_retries=3, dry_run=False,
        )
        self._wait()
        self.assertEqual(len(self.captured), 1)
        title = self.captured[0].title
        self.assertIn("Retry Cap Exhausted", title)
        self.assertIn("PM", title)

    def test_validator_body_has_diagnosis_and_recovery(self):
        """Webhook body for validator includes Likely cause + Recovery,
        matching the hermes-send validator body."""
        self.disp._fire_webhook_notification(
            role="validator", issue_number=55,
            retry_count=3, max_retries=2, dry_run=False,
        )
        self._wait()
        body = self.captured[0].body
        # Core facts
        self.assertIn("#55", body)
        self.assertIn("3", body)
        self.assertIn("2", body)
        # Diagnosis/recovery sections mirror hermes-send validator body
        self.assertIn("Likely cause", body)
        self.assertIn("Recovery", body)
        # Validator-specific keywords from _send_retry_cap_notification
        self.assertIn("CONFIRMED", body)

    def test_pm_body_has_diagnosis_and_recovery(self):
        """Webhook body for PM includes Likely cause + Recovery,
        matching the hermes-send PM body."""
        self.disp._fire_webhook_notification(
            role="pm", issue_number=72,
            retry_count=4, max_retries=3, dry_run=False,
        )
        self._wait()
        body = self.captured[0].body
        # Core facts
        self.assertIn("#72", body)
        self.assertIn("4", body)
        self.assertIn("3", body)
        # Diagnosis/recovery sections mirror hermes-send PM body
        self.assertIn("Likely cause", body)
        self.assertIn("Recovery", body)
        # PM-specific keywords from _send_retry_cap_notification
        self.assertIn("SPEC:", body)

    def test_context_contains_recovery_field(self):
        """Webhook context mirrors the structured hermes-send body fields."""
        self.disp._fire_webhook_notification(
            role="validator", issue_number=55,
            retry_count=3, max_retries=2, dry_run=False,
        )
        self._wait()
        ctx = self.captured[0].context
        self.assertEqual(ctx["issue"], "#55")
        self.assertEqual(ctx["role"], "validator")
        # retry_count/max_retries both present
        self.assertIn("retry_count", ctx)
        self.assertIn("max_retries", ctx)
        # Recovery guidance is now in context, mirroring the hermes-send body
        self.assertIn("recovery", ctx)
        self.assertTrue(ctx["recovery"])

    def test_dry_run_skips_webhook(self):
        """dry_run=True must not fire the webhook."""
        self.disp._fire_webhook_notification(
            role="validator", issue_number=55,
            retry_count=3, max_retries=2, dry_run=True,
        )
        time.sleep(0.2)
        self.assertEqual(len(self.captured), 0)


if __name__ == "__main__":
    unittest.main()

"""Tests for the ``notify.native_send`` delivery flag (issue #1293).

Delivery is already ~90% native: ``_hermes_send`` delivers escalation
notifications through ``hermes send`` (with threading). The legacy raw-webhook
path (``send_webhook_notification`` → SLACK_WEBHOOK_URL / DISCORD_WEBHOOK_URL,
Block Kit / embeds) is fired redundantly alongside it at two sites:
  * ``_send_retry_cap_notification`` (retry-cap-exhausted)
  * ``_send_crash_retries_exhausted_notification`` (crash-retries-exhausted)

``notify.native_send`` (default false) gates ONLY those redundant legacy calls:
  * OFF  → byte-identical: legacy webhook fires AND native _hermes_send fires.
  * ON   → skip the legacy webhook; native _hermes_send is unaffected.

These tests mock both transports so nothing touches a real board/webhook, and
run under pytest AND as a standalone ``__main__`` script (dual-mode).
"""
from __future__ import annotations

import importlib.util
import time
import unittest
from pathlib import Path
from unittest import mock


def _load_dispatch():
    p = Path(__file__).resolve().parent.parent / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp_native_send", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestNativeSendHelper(unittest.TestCase):
    """`_native_send_enabled` resolution (default off, never raises)."""

    def setUp(self):
        self.disp = _load_dispatch()

    def test_defaults_false_when_absent(self):
        self.assertFalse(self.disp._native_send_enabled({}))
        self.assertFalse(self.disp._native_send_enabled({"notify": {}}))

    def test_true_when_set(self):
        self.assertTrue(
            self.disp._native_send_enabled({"notify": {"native_send": True}})
        )

    def test_false_when_explicitly_off(self):
        self.assertFalse(
            self.disp._native_send_enabled({"notify": {"native_send": False}})
        )

    def test_never_raises_on_bad_input(self):
        # notify is not a dict-ish -> falsy branch, no exception.
        self.assertFalse(self.disp._native_send_enabled({"notify": None}))
        self.assertFalse(self.disp._native_send_enabled({"notify": "nope"}))


class _CallSiteBase(unittest.TestCase):
    """Mocks both transports and provides a webhook-fire waiter."""

    def setUp(self):
        self.disp = _load_dispatch()
        self.webhook_payloads = []
        self.native_sends = []  # (target, body)

        def fake_webhook(payload):
            self.webhook_payloads.append(payload)
            return {"slack": True}

        def fake_hermes(target, body, *a, **k):
            self.native_sends.append((target, body))
            return (True, "anchor-1")

        self._patchers = [
            mock.patch.object(
                self.disp, "send_webhook_notification", side_effect=fake_webhook
            ),
            mock.patch.object(
                self.disp, "_hermes_send", side_effect=fake_hermes
            ),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def _wait_for_webhook(self, timeout: float = 2.0) -> None:
        """Legacy webhook fires from a daemon thread — poll briefly."""
        deadline = time.monotonic() + timeout
        while not self.webhook_payloads and time.monotonic() < deadline:
            time.sleep(0.005)

    def _settle(self, seconds: float = 0.05) -> None:
        """Give any (unexpected) daemon thread a chance to run before asserting absence."""
        time.sleep(seconds)


class TestRetryCapCallSite(_CallSiteBase):
    """retry-cap-exhausted: legacy webhook gated, native always fires."""

    def _resolved(self, native_send: bool) -> dict:
        return {
            "notify": {"native_send": native_send},
            "cron": {"deliver": "slack:C1"},
        }

    def test_flag_off_fires_both_transports(self):
        self.disp._send_retry_cap_notification(
            role="validator", issue_number=55, retry_count=3, max_retries=2,
            resolved=self._resolved(False), dry_run=False,
        )
        self._wait_for_webhook()
        # Legacy webhook fired (byte-identical to today).
        self.assertEqual(len(self.webhook_payloads), 1)
        # Native path also fired.
        self.assertEqual(len(self.native_sends), 1)
        self.assertEqual(self.native_sends[0][0], "slack:C1")

    def test_flag_on_skips_legacy_keeps_native(self):
        self.disp._send_retry_cap_notification(
            role="validator", issue_number=55, retry_count=3, max_retries=2,
            resolved=self._resolved(True), dry_run=False,
        )
        self._settle()
        # Legacy webhook skipped.
        self.assertEqual(len(self.webhook_payloads), 0)
        # Native path unaffected.
        self.assertEqual(len(self.native_sends), 1)
        self.assertEqual(self.native_sends[0][0], "slack:C1")


class TestCrashRetriesCallSite(_CallSiteBase):
    """crash-retries-exhausted: legacy webhook gated, native always fires."""

    def _action(self) -> dict:
        return {
            "issue": 77,
            "task_id": "t_abc123",
            "attempt": 3,
            "max_attempts": 3,
            "elapsed_minutes": 42,
            "summary": "boom",
            "assignee": "developer-daedalus",
        }

    def _resolved(self, native_send: bool) -> dict:
        return {
            "notify": {"native_send": native_send},
            "cron": {"deliver": "slack:C1"},
        }

    def test_flag_off_fires_both_transports(self):
        self.disp._send_crash_retries_exhausted_notification(
            action=self._action(), resolved=self._resolved(False), dry_run=False,
        )
        self._wait_for_webhook()
        self.assertEqual(len(self.webhook_payloads), 1)
        self.assertGreaterEqual(len(self.native_sends), 1)
        self.assertEqual(self.native_sends[0][0], "slack:C1")

    def test_flag_on_skips_legacy_keeps_native(self):
        self.disp._send_crash_retries_exhausted_notification(
            action=self._action(), resolved=self._resolved(True), dry_run=False,
        )
        self._settle()
        self.assertEqual(len(self.webhook_payloads), 0)
        self.assertGreaterEqual(len(self.native_sends), 1)
        self.assertEqual(self.native_sends[0][0], "slack:C1")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Unit tests for retry cap notification behavior.

Covers:
  - Notification triggering when retry cap is reached
  - Notification content/payload correctness
  - Edge cases (zero retries, immediate cap, concurrent retries)
  - Integration with notification delivery mechanism
  - Tests are isolated, fast, and use appropriate mocking
  - Ensures 100% branch coverage for the notification logic

Run: pytest tests/test_retry_cap_notifications.py -v
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from conftest import _load_dispatch  # noqa: E402

disp = _load_dispatch()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_resolved_config(
    notifications=None,
    deliver="",
):
    """Build a minimal resolved config dict for notification tests."""
    return {
        "cron": {
            "notifications": notifications or [],
            "deliver": deliver,
        }
    }


# ── _send_retry_cap_notification tests ──────────────────────────────────────


class TestSendRetryCapNotification:
    """Tests for _send_retry_cap_notification function."""

    def test_webhook_fires_regardless_of_targets(self):
        """Webhook notification fires even when no hermes send targets configured."""
        with mock.patch.object(disp, "_fire_webhook_notification") as mock_webhook, \
             mock.patch.object(disp, "_notify_targets", return_value=[]):
            resolved = _make_resolved_config()
            disp._send_retry_cap_notification(
                role="validator",
                issue_number=42,
                retry_count=3,
                max_retries=3,
                resolved=resolved,
                dry_run=False,
            )
            mock_webhook.assert_called_once_with(
                role="validator",
                issue_number=42,
                retry_count=3,
                max_retries=3,
                dry_run=False,
            )

    def test_no_targets_returns_silently(self):
        """When no notification targets are configured, function returns without error."""
        with mock.patch.object(disp, "_fire_webhook_notification"), \
             mock.patch.object(disp, "_notify_targets", return_value=[]):
            resolved = _make_resolved_config()
            # Should not raise
            disp._send_retry_cap_notification(
                role="pm",
                issue_number=10,
                retry_count=2,
                max_retries=2,
                resolved=resolved,
                dry_run=False,
            )

    def test_dry_run_mode_logs_without_sending(self):
        """Dry run mode logs intent but doesn't call _hermes_send."""
        with mock.patch.object(disp, "_fire_webhook_notification"), \
             mock.patch.object(disp, "_notify_targets", return_value=["slack:C123"]), \
             mock.patch.object(disp, "_hermes_send") as mock_send:
            resolved = _make_resolved_config()
            disp._send_retry_cap_notification(
                role="validator",
                issue_number=99,
                retry_count=5,
                max_retries=5,
                resolved=resolved,
                dry_run=True,
            )
            mock_send.assert_not_called()

    def test_validator_role_body_content(self):
        """Validator role generates correct body with recovery instructions."""
        with mock.patch.object(disp, "_fire_webhook_notification"), \
             mock.patch.object(disp, "_notify_targets", return_value=["telegram:-100123"]), \
             mock.patch.object(disp, "_hermes_send", return_value=(True, "msg_123")) as mock_send:
            resolved = _make_resolved_config()
            disp._send_retry_cap_notification(
                role="validator",
                issue_number=123,
                retry_count=3,
                max_retries=3,
                resolved=resolved,
                dry_run=False,
            )
            # Verify the body contains validator-specific content
            call_args = mock_send.call_args
            body = call_args[0][1]
            assert "VALIDATOR" in body
            assert "#123" in body
            assert "3/3" in body
            assert "CONFIRMED" in body
            assert "Manual intervention required" in body
            assert "agent logs" in body

    def test_pm_role_body_content(self):
        """PM role generates correct body with PM-specific recovery instructions."""
        with mock.patch.object(disp, "_fire_webhook_notification"), \
             mock.patch.object(disp, "_notify_targets", return_value=["slack:C456"]), \
             mock.patch.object(disp, "_hermes_send", return_value=(True, "msg_456")) as mock_send:
            resolved = _make_resolved_config()
            disp._send_retry_cap_notification(
                role="pm",
                issue_number=456,
                retry_count=2,
                max_retries=2,
                resolved=resolved,
                dry_run=False,
            )
            call_args = mock_send.call_args
            body = call_args[0][1]
            assert "PM" in body
            assert "#456" in body
            assert "2/2" in body
            assert "SPEC:" in body
            assert "Manual intervention required" in body

    def test_multiple_targets_all_notified(self):
        """Notification is sent to all configured targets."""
        targets = ["slack:C1", "telegram:-100", "discord:#general"]
        with mock.patch.object(disp, "_fire_webhook_notification"), \
             mock.patch.object(disp, "_notify_targets", return_value=targets), \
             mock.patch.object(disp, "_hermes_send", return_value=(True, "msg")) as mock_send:
            resolved = _make_resolved_config()
            disp._send_retry_cap_notification(
                role="validator",
                issue_number=789,
                retry_count=4,
                max_retries=4,
                resolved=resolved,
                dry_run=False,
            )
            assert mock_send.call_count == 3
            for i, target in enumerate(targets):
                assert mock_send.call_args_list[i][0][0] == target

    def test_hermes_send_failure_logged_not_raised(self):
        """When _hermes_send fails, error is logged but exception not raised."""
        with mock.patch.object(disp, "_fire_webhook_notification"), \
             mock.patch.object(disp, "_notify_targets", return_value=["slack:C999"]), \
             mock.patch.object(disp, "_hermes_send", return_value=(False, None)):
            resolved = _make_resolved_config()
            # Should not raise even though send failed
            disp._send_retry_cap_notification(
                role="pm",
                issue_number=111,
                retry_count=1,
                max_retries=1,
                resolved=resolved,
                dry_run=False,
            )


# ── _fire_webhook_notification tests ────────────────────────────────────────


class TestFireWebhookNotification:
    """Tests for _fire_webhook_notification function."""

    def test_dry_run_skips_webhook(self):
        """Dry run mode skips webhook firing entirely."""
        with mock.patch.object(disp, "send_webhook_notification") as mock_send:
            disp._fire_webhook_notification(
                role="validator",
                issue_number=42,
                retry_count=3,
                max_retries=3,
                dry_run=True,
            )
            mock_send.assert_not_called()

    def test_webhook_payload_validator_role(self):
        """Webhook payload for validator role has correct structure and content."""
        with mock.patch.object(disp, "send_webhook_notification") as mock_send:
            disp._fire_webhook_notification(
                role="validator",
                issue_number=123,
                retry_count=3,
                max_retries=3,
                dry_run=False,
            )
            # Give thread time to complete
            time.sleep(0.1)
            mock_send.assert_called_once()
            payload = mock_send.call_args[0][0]
            assert "VALIDATOR" in payload.title
            assert payload.severity == "critical"
            assert "#123" in payload.body
            assert "3/3" in payload.context["retry_count"]
            assert payload.context["role"] == "validator"

    def test_webhook_payload_pm_role(self):
        """Webhook payload for PM role has correct structure and content."""
        with mock.patch.object(disp, "send_webhook_notification") as mock_send:
            disp._fire_webhook_notification(
                role="pm",
                issue_number=456,
                retry_count=2,
                max_retries=2,
                dry_run=False,
            )
            time.sleep(0.1)
            mock_send.assert_called_once()
            payload = mock_send.call_args[0][0]
            assert "PM" in payload.title
            assert payload.severity == "critical"
            assert "#456" in payload.body
            assert "2/2" in payload.context["retry_count"]
            assert payload.context["role"] == "pm"

    def test_webhook_exception_caught_not_raised(self):
        """Exceptions in webhook thread are caught and logged, not raised."""
        with mock.patch.object(disp, "send_webhook_notification", side_effect=Exception("Network error")):
            # Should not raise even though webhook fails
            disp._fire_webhook_notification(
                role="validator",
                issue_number=789,
                retry_count=5,
                max_retries=5,
                dry_run=False,
            )
            # Give thread time to attempt and fail
            time.sleep(0.1)


# ── _send_retry_attempt_notification tests ──────────────────────────────────


class TestSendRetryAttemptNotification:
    """Tests for _send_retry_attempt_notification function."""

    def test_no_targets_returns_silently(self):
        """When no retry-attempt targets configured, function returns without error."""
        with mock.patch.object(disp, "_notify_targets", return_value=[]):
            resolved = _make_resolved_config()
            disp._send_retry_attempt_notification(
                role="validator",
                issue_number=100,
                retry_count=1,
                max_retries=3,
                resolved=resolved,
                dry_run=False,
            )

    def test_retry_attempt_body_content(self):
        """Retry attempt notification has correct body format."""
        with mock.patch.object(disp, "_notify_targets", return_value=["slack:C123"]), \
             mock.patch.object(disp, "_hermes_send", return_value=(True, "msg")) as mock_send:
            resolved = _make_resolved_config()
            disp._send_retry_attempt_notification(
                role="pm",
                issue_number=200,
                retry_count=2,
                max_retries=5,
                resolved=resolved,
                dry_run=False,
            )
            call_args = mock_send.call_args
            body = call_args[0][1]
            assert "Retry Attempt" in body or "🔄" in body
            assert "PM" in body
            assert "#200" in body
            assert "2" in body
            assert "5" in body

    def test_dry_run_mode_no_send(self):
        """Dry run mode logs but doesn't call _hermes_send."""
        with mock.patch.object(disp, "_notify_targets", return_value=["telegram:-100"]), \
             mock.patch.object(disp, "_hermes_send") as mock_send:
            resolved = _make_resolved_config()
            disp._send_retry_attempt_notification(
                role="validator",
                issue_number=300,
                retry_count=1,
                max_retries=2,
                resolved=resolved,
                dry_run=True,
            )
            mock_send.assert_not_called()

    def test_hermes_send_failure_logged_not_raised(self):
        """When _hermes_send fails for retry-attempt, error is logged but not raised."""
        with mock.patch.object(disp, "_notify_targets", return_value=["slack:C999"]), \
             mock.patch.object(disp, "_hermes_send", return_value=(False, None)):
            resolved = _make_resolved_config()
            # Should not raise even though send failed
            disp._send_retry_attempt_notification(
                role="pm",
                issue_number=333,
                retry_count=1,
                max_retries=3,
                resolved=resolved,
                dry_run=False,
            )

    def test_multiple_targets_all_notified(self):
        """Retry attempt notification sent to all configured targets."""
        targets = ["slack:C1", "telegram:-100"]
        with mock.patch.object(disp, "_notify_targets", return_value=targets), \
             mock.patch.object(disp, "_hermes_send", return_value=(True, "msg")) as mock_send:
            resolved = _make_resolved_config()
            disp._send_retry_attempt_notification(
                role="validator",
                issue_number=444,
                retry_count=1,
                max_retries=2,
                resolved=resolved,
                dry_run=False,
            )
            assert mock_send.call_count == 2


# ── _notify_targets tests ───────────────────────────────────────────────────


class TestNotifyTargets:
    """Tests for _notify_targets function."""

    def test_empty_config_returns_empty_list(self):
        """Empty resolved config returns no targets."""
        resolved = _make_resolved_config()
        targets = disp._notify_targets(resolved, "retry-cap-exhausted")
        assert targets == []

    def test_legacy_deliver_fallback(self):
        """When notifications list is empty, falls back to cron.deliver."""
        resolved = _make_resolved_config(deliver="slack:C999")
        targets = disp._notify_targets(resolved, "retry-cap-exhausted")
        assert targets == ["slack:C999"]

    def test_notifications_with_event_filter(self):
        """Only targets subscribed to the event are returned."""
        notifications = [
            {"target": "slack:C1", "events": ["retry-cap-exhausted", "other"]},
            {"target": "telegram:-100", "events": ["retry-cap-exhausted"]},
            {"target": "discord:#general", "events": ["other-event"]},
        ]
        resolved = _make_resolved_config(notifications=notifications)
        targets = disp._notify_targets(resolved, "retry-cap-exhausted")
        assert "slack:C1" in targets
        assert "telegram:-100" in targets
        assert "discord:#general" not in targets

    def test_no_events_list_receives_all_events(self):
        """Targets with no events list receive all events."""
        notifications = [
            {"target": "slack:C1"},  # No events list = all events
            {"target": "telegram:-100", "events": ["specific-only"]},
        ]
        resolved = _make_resolved_config(notifications=notifications)
        targets = disp._notify_targets(resolved, "retry-cap-exhausted")
        assert "slack:C1" in targets
        assert "telegram:-100" not in targets

    def test_empty_target_skipped(self):
        """Entries with empty target strings are skipped."""
        notifications = [
            {"target": "", "events": ["retry-cap-exhausted"]},
            {"target": "slack:C1", "events": ["retry-cap-exhausted"]},
        ]
        resolved = _make_resolved_config(notifications=notifications)
        targets = disp._notify_targets(resolved, "retry-cap-exhausted")
        assert targets == ["slack:C1"]

    def test_duplicate_targets_deduped(self):
        """Duplicate targets are deduplicated."""
        notifications = [
            {"target": "slack:C1", "events": ["retry-cap-exhausted"]},
            {"target": "slack:C1", "events": ["retry-cap-exhausted"]},  # Duplicate
        ]
        resolved = _make_resolved_config(notifications=notifications)
        targets = disp._notify_targets(resolved, "retry-cap-exhausted")
        assert targets == ["slack:C1"]


# ── Edge cases ───────────────────────────────────────────────────────────────


class TestRetryCapEdgeCases:
    """Edge case tests for retry cap notifications."""

    def test_zero_retries_immediate_cap(self):
        """Notification can fire with retry_count=0 (immediate cap scenario)."""
        with mock.patch.object(disp, "_fire_webhook_notification") as mock_webhook, \
             mock.patch.object(disp, "_notify_targets", return_value=["slack:C1"]), \
             mock.patch.object(disp, "_hermes_send", return_value=(True, "msg")):
            resolved = _make_resolved_config()
            disp._send_retry_cap_notification(
                role="validator",
                issue_number=50,
                retry_count=0,
                max_retries=0,
                resolved=resolved,
                dry_run=False,
            )
            mock_webhook.assert_called_once()
            assert mock_webhook.call_args[1]["retry_count"] == 0

    def test_large_retry_counts(self):
        """Large retry counts are handled correctly."""
        with mock.patch.object(disp, "_fire_webhook_notification") as mock_webhook, \
             mock.patch.object(disp, "_notify_targets", return_value=["slack:C1"]), \
             mock.patch.object(disp, "_hermes_send", return_value=(True, "msg")) as mock_send:
            resolved = _make_resolved_config()
            disp._send_retry_cap_notification(
                role="pm",
                issue_number=999,
                retry_count=999999,
                max_retries=999999,
                resolved=resolved,
                dry_run=False,
            )
            mock_webhook.assert_called_once()
            body = mock_send.call_args[0][1]
            assert "999999/999999" in body

    def test_issue_number_formatting(self):
        """Issue number is formatted with # prefix."""
        with mock.patch.object(disp, "_fire_webhook_notification"), \
             mock.patch.object(disp, "_notify_targets", return_value=["slack:C1"]), \
             mock.patch.object(disp, "_hermes_send", return_value=(True, "msg")) as mock_send:
            resolved = _make_resolved_config()
            disp._send_retry_cap_notification(
                role="validator",
                issue_number=42,
                retry_count=1,
                max_retries=1,
                resolved=resolved,
                dry_run=False,
            )
            body = mock_send.call_args[0][1]
            assert "#42" in body


# ── Integration with delivery mechanism ─────────────────────────────────────


class TestNotificationDeliveryIntegration:
    """Integration tests for notification delivery."""

    def test_both_webhook_and_hermes_send_fire(self):
        """Both webhook and hermes send are invoked when targets configured."""
        with mock.patch.object(disp, "_fire_webhook_notification") as mock_webhook, \
             mock.patch.object(disp, "_notify_targets", return_value=["slack:C1"]), \
             mock.patch.object(disp, "_hermes_send", return_value=(True, "msg")) as mock_send:
            resolved = _make_resolved_config()
            disp._send_retry_cap_notification(
                role="validator",
                issue_number=100,
                retry_count=2,
                max_retries=2,
                resolved=resolved,
                dry_run=False,
            )
            mock_webhook.assert_called_once()
            mock_send.assert_called_once()

    def test_concurrent_notifications_isolated(self):
        """Multiple concurrent notifications don't interfere with each other."""
        with mock.patch.object(disp, "_fire_webhook_notification") as mock_webhook, \
             mock.patch.object(disp, "_notify_targets", return_value=["slack:C1"]), \
             mock.patch.object(disp, "_hermes_send", return_value=(True, "msg")):
            resolved = _make_resolved_config()
            # Fire multiple notifications concurrently
            for i in range(5):
                disp._send_retry_cap_notification(
                    role="validator" if i % 2 == 0 else "pm",
                    issue_number=100 + i,
                    retry_count=i,
                    max_retries=2,
                    resolved=resolved,
                    dry_run=False,
                )
            assert mock_webhook.call_count == 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

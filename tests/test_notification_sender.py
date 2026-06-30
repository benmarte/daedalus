#!/usr/bin/env python3
"""Unit tests for core.notification_sender (issue #260).

Covers:
  - NotificationPayload validation and default timestamp
  - Slack Block Kit formatting (header, section, context, severity)
  - Discord embed formatting (title, description, color, fields, timestamp)
  - Severity normalisation (unknown values fall back to "info")
  - Webhook URL resolution: env vars, overrides, comma-separated values
  - HTTP POST: success, non-2xx, timeout, URL error, JSON serialisation failure
  - send_slack / send_discord / send orchestration
  - No external HTTP calls — all urllib.request.urlopen calls are mocked

Run: python3 -m unittest tests.test_notification_sender -v
  or: python3 tests/test_notification_sender.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

# Make repo root importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.notification_sender import (  # noqa: E402
    DEFAULT_TIMEOUT,
    DISCORD_ENV_VAR,
    SLACK_ENV_VAR,
    NotificationPayload,
    _as_list,
    _post_json,
    format_discord,
    format_slack,
    load_webhook_urls,
    send,
    send_discord,
    send_slack,
)


# ── helpers ─────────────────────────────────────────────────────────────────


class _FakeHTTPResponse:
    """Stand-in for ``urllib.request.urlopen`` return value."""

    def __init__(self, status: int = 200, body: bytes = b"ok"):
        self.status = status
        self.code = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


# ── NotificationPayload tests ─────────────────────────────────────────────


class TestNotificationPayload(unittest.TestCase):
    def test_requires_title(self):
        with self.assertRaises(ValueError):
            NotificationPayload(title="", body="message")

    def test_requires_body(self):
        with self.assertRaises(ValueError):
            NotificationPayload(title="t", body="")

    def test_defaults_severity_to_info(self):
        p = NotificationPayload(title="x", body="y")
        self.assertEqual(p.severity, "info")

    def test_auto_sets_timestamp(self):
        before = time.time()
        p = NotificationPayload(title="t", body="b")
        after = time.time()
        self.assertIsNotNone(p.timestamp)
        assert p.timestamp is not None  # type narrowing
        self.assertGreaterEqual(p.timestamp, before)
        self.assertLessEqual(p.timestamp, after)

    def test_preserves_explicit_timestamp(self):
        p = NotificationPayload(title="t", body="b", timestamp=1234.5)
        self.assertEqual(p.timestamp, 1234.5)

    def test_default_context_is_empty_dict(self):
        p = NotificationPayload(title="t", body="b")
        self.assertEqual(p.context, {})

    def test_as_dict_contains_all_fields(self):
        p = NotificationPayload(
            title="t", body="b", severity="error",
            context={"k": "v"}, timestamp=1.0,
        )
        d = p.as_dict()
        for key in ("title", "body", "severity", "context", "timestamp"):
            self.assertIn(key, d)
        self.assertEqual(d["severity"], "error")


# ── Slack formatter ───────────────────────────────────────────────────────


class TestFormatSlack(unittest.TestCase):
    def test_header_block_present(self):
        payload = NotificationPayload(title="Deploy", body="v1.2.3 shipped")
        result = format_slack(payload)
        headers = [b for b in result["blocks"] if b["type"] == "header"]
        self.assertEqual(len(headers), 1)
        self.assertEqual(headers[0]["text"]["text"], "Deploy")

    def test_section_contains_body(self):
        payload = NotificationPayload(title="t", body="hello *world*")
        result = format_slack(payload)
        sections = [b for b in result["blocks"] if b["type"] == "section"]
        self.assertEqual(len(sections), 1)
        self.assertIn("hello *world*", sections[0]["text"]["text"])

    def test_context_block_when_context_present(self):
        payload = NotificationPayload(
            title="t", body="b", context={"env": "prod", "region": "us-east"},
        )
        result = format_slack(payload)
        context_blocks = [b for b in result["blocks"] if b["type"] == "context"]
        # At least one context block for the user-supplied context, plus one
        # mandatory severity context block.
        self.assertGreaterEqual(len(context_blocks), 2)
        combined = "\n".join(
            el["text"] for cb in context_blocks for el in cb.get("elements", [])
        )
        self.assertIn("*env*: prod", combined)
        self.assertIn("*region*: us-east", combined)

    def test_no_context_block_when_context_empty(self):
        payload = NotificationPayload(title="t", body="b")
        result = format_slack(payload)
        context_blocks = [b for b in result["blocks"] if b["type"] == "context"]
        # Only the severity context block remains.
        self.assertEqual(len(context_blocks), 1)

    def test_severity_in_context(self):
        payload = NotificationPayload(title="t", body="b", severity="error")
        result = format_slack(payload)
        severity_block = next(
            b for b in result["blocks"] if b["type"] == "context"
        )
        self.assertIn("Severity: *error*", severity_block["elements"][0]["text"])

    def test_attachment_color_matches_severity(self):
        payload = NotificationPayload(title="t", body="b", severity="critical")
        result = format_slack(payload)
        self.assertEqual(result["attachments"][0]["color"], "#8e44ad")

    def test_unknown_severity_normalizes_to_info(self):
        payload = NotificationPayload(title="t", body="b", severity="weird")
        result = format_slack(payload)
        # Attachment color should fall back to the info colour.
        self.assertEqual(result["attachments"][0]["color"], "#3498db")

    def test_top_level_text_is_plain_fallback(self):
        payload = NotificationPayload(title="title", body="body text")
        result = format_slack(payload)
        self.assertIn("title", result["text"])
        self.assertIn("body text", result["text"])


# ── Discord formatter ─────────────────────────────────────────────────────


class TestFormatDiscord(unittest.TestCase):
    def test_embed_title_and_description(self):
        payload = NotificationPayload(title="t", body="b")
        result = format_discord(payload)
        self.assertEqual(len(result["embeds"]), 1)
        embed = result["embeds"][0]
        self.assertEqual(embed["title"], "t")
        self.assertEqual(embed["description"], "b")

    def test_embed_timestamp_iso_utc(self):
        payload = NotificationPayload(title="t", body="b", timestamp=0)
        result = format_discord(payload)
        self.assertEqual(result["embeds"][0]["timestamp"], "1970-01-01T00:00:00Z")

    def test_severity_color_for_error(self):
        payload = NotificationPayload(title="t", body="b", severity="error")
        result = format_discord(payload)
        self.assertEqual(result["embeds"][0]["color"], 0xE74C3C)

    def test_severity_color_for_success(self):
        payload = NotificationPayload(title="t", body="b", severity="success")
        result = format_discord(payload)
        self.assertEqual(result["embeds"][0]["color"], 0x2ECC71)

    def test_unknown_severity_uses_info_color(self):
        payload = NotificationPayload(title="t", body="b", severity="???")
        result = format_discord(payload)
        self.assertEqual(result["embeds"][0]["color"], 0x3498DB)  # info blue

    def test_context_rendered_as_inline_fields(self):
        payload = NotificationPayload(
            title="t", body="b", context={"env": "prod", "region": "us"},
        )
        result = format_discord(payload)
        fields = result["embeds"][0]["fields"]
        names = {f["name"] for f in fields}
        self.assertIn("env", names)
        self.assertIn("region", names)
        # Severity field is always appended.
        self.assertIn("Severity", names)

    def test_severity_field_present(self):
        payload = NotificationPayload(title="t", body="b")
        result = format_discord(payload)
        severity_field = next(
            f for f in result["embeds"][0]["fields"] if f["name"] == "Severity"
        )
        self.assertEqual(severity_field["value"], "info")
        self.assertTrue(severity_field["inline"])

    def test_content_uses_first_body_line(self):
        payload = NotificationPayload(title="t", body="line one\nline two")
        result = format_discord(payload)
        # Discord's preview text should contain the title and first body line,
        # NOT the second line.
        self.assertIn("**t**", result["content"])
        self.assertIn("line one", result["content"])
        self.assertNotIn("line two", result["content"])


# ── Configuration loading ─────────────────────────────────────────────────


class TestLoadWebhookUrls(unittest.TestCase):
    def setUp(self):
        # Clear env vars between tests to avoid leakage.
        self._patcher_slack = mock.patch.dict(os.environ, {}, clear=False)
        self._patcher_discord = mock.patch.dict(os.environ, {}, clear=False)
        self._patcher_slack.start()
        self._patcher_discord.start()
        os.environ.pop(SLACK_ENV_VAR, None)
        os.environ.pop(DISCORD_ENV_VAR, None)

    def tearDown(self):
        self._patcher_slack.stop()
        self._patcher_discord.stop()

    def test_reads_slack_env_var(self):
        os.environ[SLACK_ENV_VAR] = "https://hooks.slack.com/services/abc"
        self.assertEqual(
            load_webhook_urls(),
            {"slack": ["https://hooks.slack.com/services/abc"]},
        )

    def test_reads_discord_env_var(self):
        os.environ[DISCORD_ENV_VAR] = "https://discord.com/api/webhooks/x/y"
        self.assertEqual(
            load_webhook_urls(),
            {"discord": ["https://discord.com/api/webhooks/x/y"]},
        )

    def test_comma_separated_env_expands(self):
        os.environ[SLACK_ENV_VAR] = "  https://a , https://b , https://c  "
        self.assertEqual(
            load_webhook_urls()["slack"],
            ["https://a", "https://b", "https://c"],
        )

    def test_empty_env_var_omits_platform(self):
        os.environ[SLACK_ENV_VAR] = "   "
        self.assertNotIn("slack", load_webhook_urls())

    def test_overrides_take_precedence_over_env(self):
        os.environ[SLACK_ENV_VAR] = "https://env-url"
        result = load_webhook_urls(overrides={"slack": ["https://override-url"]})
        self.assertEqual(result["slack"], ["https://override-url"])

    def test_override_string_is_split(self):
        result = load_webhook_urls(overrides={"slack": "https://a,https://b"})
        self.assertEqual(result["slack"], ["https://a", "https://b"])

    def test_override_iterable_accepted(self):
        result = load_webhook_urls(overrides={"discord": ["https://x", "https://y"]})
        self.assertEqual(result["discord"], ["https://x", "https://y"])

    def test_empty_override_falls_back_to_env(self):
        os.environ[SLACK_ENV_VAR] = "https://env"
        result = load_webhook_urls(overrides={"slack": []})
        self.assertEqual(result["slack"], ["https://env"])

    def test_no_config_returns_empty_dict(self):
        self.assertEqual(load_webhook_urls(), {})


class TestAsList(unittest.TestCase):
    def test_string_splits_on_comma(self):
        self.assertEqual(_as_list("a,b,c"), ["a", "b", "c"])

    def test_list_passthrough(self):
        self.assertEqual(_as_list(["a", "b"]), ["a", "b"])

    def test_filters_empties(self):
        self.assertEqual(_as_list("a,,b, ,c"), ["a", "b", "c"])

    def test_non_iterable_returns_empty(self):
        self.assertEqual(_as_list(42), [])


# ── HTTP sender ────────────────────────────────────────────────────────────


class TestPostJson(unittest.TestCase):
    @mock.patch("urllib.request.urlopen")
    def test_success_returns_true(self, mock_urlopen):
        mock_urlopen.return_value = _FakeHTTPResponse(status=200)
        self.assertTrue(_post_json("https://example.com", {"a": 1}))
        # Verify the request body was JSON-encoded with correct content-type.
        (request,), kwargs = mock_urlopen.call_args
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), {"a": 1})
        self.assertEqual(request.method, "POST")
        self.assertEqual(kwargs.get("timeout"), DEFAULT_TIMEOUT)

    @mock.patch("urllib.request.urlopen")
    def test_non_2xx_returns_false(self, mock_urlopen):
        mock_urlopen.return_value = _FakeHTTPResponse(status=500)
        self.assertFalse(_post_json("https://example.com", {"a": 1}))

    @mock.patch("urllib.request.urlopen")
    def test_http_error_returns_false(self, mock_urlopen):
        from email.message import Message
        hdrs = Message()
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://example.com", code=404, msg="Not Found",
            hdrs=hdrs, fp=None,
        )
        self.assertFalse(_post_json("https://example.com", {"a": 1}))

    @mock.patch("urllib.request.urlopen")
    def test_url_error_returns_false(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("dns boom")
        self.assertFalse(_post_json("https://example.com", {"a": 1}))

    @mock.patch("urllib.request.urlopen")
    def test_timeout_returns_false(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("took too long")
        self.assertFalse(_post_json("https://example.com", {"a": 1}))

    @mock.patch("urllib.request.urlopen")
    def test_os_error_returns_false(self, mock_urlopen):
        mock_urlopen.side_effect = OSError("network down")
        self.assertFalse(_post_json("https://example.com", {"a": 1}))

    def test_unserializable_payload_returns_false(self):
        # Lambdas aren't JSON-serialisable — should fail gracefully, no POST.
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            self.assertFalse(_post_json("https://example.com", {"fn": lambda: None}))
            mock_urlopen.assert_not_called()

    @mock.patch("urllib.request.urlopen")
    def test_custom_timeout_propagated(self, mock_urlopen):
        mock_urlopen.return_value = _FakeHTTPResponse(200)
        _post_json("https://example.com", {"a": 1}, timeout=42.5)
        _, kwargs = mock_urlopen.call_args
        self.assertEqual(kwargs["timeout"], 42.5)


# ── send_slack / send_discord / send ──────────────────────────────────────


class TestSendSlack(unittest.TestCase):
    def setUp(self):
        self._patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._patcher.start()
        os.environ.pop(SLACK_ENV_VAR, None)
        os.environ.pop(DISCORD_ENV_VAR, None)

    def tearDown(self):
        self._patcher.stop()

    @mock.patch("core.notification_sender._post_json", return_value=True)
    def test_sends_to_explicit_urls(self, mock_post):
        payload = NotificationPayload(title="t", body="b")
        self.assertTrue(send_slack(payload, ["https://slack-hook-1", "https://slack-hook-2"]))
        self.assertEqual(mock_post.call_count, 2)

    @mock.patch("core.notification_sender._post_json", return_value=True)
    def test_falls_back_to_env_when_no_urls(self, mock_post):
        os.environ[SLACK_ENV_VAR] = "https://env-slack"
        payload = NotificationPayload(title="t", body="b")
        self.assertTrue(send_slack(payload))
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(mock_post.call_args[0][0], "https://env-slack")

    def test_no_urls_returns_false(self):
        payload = NotificationPayload(title="t", body="b")
        self.assertFalse(send_slack(payload))

    @mock.patch("core.notification_sender._post_json", return_value=False)
    def test_any_failure_returns_false(self, mock_post):
        payload = NotificationPayload(title="t", body="b")
        self.assertFalse(send_slack(payload, ["https://ok", "https://fail"]))
        self.assertEqual(mock_post.call_count, 2)

    @mock.patch("core.notification_sender._post_json", return_value=True)
    def test_post_body_is_slack_formatted(self, mock_post):
        payload = NotificationPayload(title="Deploy", body="v1 live", severity="success")
        send_slack(payload, ["https://hook"])
        posted_body = mock_post.call_args[0][1]
        # Slack format: must include a "blocks" key with at least a header.
        self.assertIn("blocks", posted_body)
        self.assertIn("attachments", posted_body)


class TestSendDiscord(unittest.TestCase):
    def setUp(self):
        self._patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._patcher.start()
        os.environ.pop(SLACK_ENV_VAR, None)
        os.environ.pop(DISCORD_ENV_VAR, None)

    def tearDown(self):
        self._patcher.stop()

    @mock.patch("core.notification_sender._post_json", return_value=True)
    def test_sends_to_explicit_urls(self, mock_post):
        payload = NotificationPayload(title="t", body="b")
        self.assertTrue(send_discord(payload, ["https://discord-hook"]))
        self.assertEqual(mock_post.call_count, 1)

    @mock.patch("core.notification_sender._post_json", return_value=True)
    def test_falls_back_to_env_when_no_urls(self, mock_post):
        os.environ[DISCORD_ENV_VAR] = "https://env-discord"
        payload = NotificationPayload(title="t", body="b")
        self.assertTrue(send_discord(payload))
        self.assertEqual(mock_post.call_args[0][0], "https://env-discord")

    def test_no_urls_returns_false(self):
        payload = NotificationPayload(title="t", body="b")
        self.assertFalse(send_discord(payload))

    @mock.patch("core.notification_sender._post_json", return_value=True)
    def test_post_body_is_discord_formatted(self, mock_post):
        payload = NotificationPayload(title="t", body="b", severity="warning")
        send_discord(payload, ["https://hook"])
        posted_body = mock_post.call_args[0][1]
        self.assertIn("embeds", posted_body)
        self.assertEqual(posted_body["embeds"][0]["color"], 0xF1C40F)


class TestSend(unittest.TestCase):
    def setUp(self):
        self._patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._patcher.start()
        os.environ.pop(SLACK_ENV_VAR, None)
        os.environ.pop(DISCORD_ENV_VAR, None)

    def tearDown(self):
        self._patcher.stop()

    @mock.patch("core.notification_sender._post_json", return_value=True)
    def test_sends_to_all_configured_platforms(self, mock_post):
        payload = NotificationPayload(title="t", body="b")
        result = send(payload, webhook_urls={
            "slack": ["https://s"],
            "discord": ["https://d"],
        })
        self.assertEqual(result, {"slack": True, "discord": True})
        self.assertEqual(mock_post.call_count, 2)

    @mock.patch("core.notification_sender._post_json", return_value=True)
    def test_explicit_platforms_subset(self, mock_post):
        payload = NotificationPayload(title="t", body="b")
        # Only ask for slack even though both are configured.
        result = send(payload, platforms=["slack"], webhook_urls={
            "slack": ["https://s"],
            "discord": ["https://d"],
        })
        self.assertEqual(result, {"slack": True})
        self.assertEqual(mock_post.call_count, 1)

    def test_unsupported_platform_records_false(self):
        payload = NotificationPayload(title="t", body="b")
        result = send(payload, platforms=["teams"], webhook_urls={"teams": ["https://t"]})
        self.assertFalse(result["teams"])

    def test_no_configured_platforms_returns_empty(self):
        payload = NotificationPayload(title="t", body="b")
        self.assertEqual(send(payload), {})


if __name__ == "__main__":
    unittest.main()

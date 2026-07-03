"""Notification sender module for retry-cap-exhausted alerts (Issue #181).

This module provides structured notification delivery to Slack and Discord via
webhooks when the PM or validator retry cap is exhausted. It formats notifications
appropriately for each platform and handles webhook delivery asynchronously.

Exports:
    NotificationPayload: Structured notification data
    send: Send notifications to configured webhooks
    format_slack: Format notification for Slack Block Kit
    format_discord: Format notification for Discord embeds
    load_webhook_urls: Load webhook URLs from environment
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Environment variable names
SLACK_ENV_VAR = "SLACK_WEBHOOK_URL"
DISCORD_ENV_VAR = "DISCORD_WEBHOOK_URL"

# Default HTTP timeout for webhook requests
DEFAULT_TIMEOUT = 10.0


@dataclass
class NotificationPayload:
    """Structured notification payload.

    Attributes:
        title: Notification title (required, non-empty)
        body: Notification body text (required, non-empty)
        severity: Severity level (info, success, warning, error, critical)
        context: Additional context fields (dict)
        timestamp: Unix timestamp (auto-set if None)
    """
    title: str
    body: str
    severity: str = "info"
    context: dict[str, Any] = field(default_factory=dict)
    timestamp: float | None = None

    def __post_init__(self):
        """Validate required fields and set defaults."""
        if not self.title or not self.title.strip():
            raise ValueError("title must be non-empty")
        if not self.body or not self.body.strip():
            raise ValueError("body must be non-empty")
        if self.timestamp is None:
            self.timestamp = time.time()
        # Normalize severity
        valid_severities = {"info", "success", "warning", "error", "critical"}
        if self.severity not in valid_severities:
            self.severity = "info"

    def as_dict(self) -> dict[str, Any]:
        """Return a dictionary representation of the payload."""
        from dataclasses import asdict
        return asdict(self)


def _normalize_severity(severity: str) -> str:
    """Normalize severity to a known value."""
    valid = {"info", "success", "warning", "error", "critical"}
    return severity if severity in valid else "info"


def _format_context_lines(context: dict[str, Any]) -> str:
    """Format context dict into readable lines."""
    if not context:
        return ""
    lines = [f"*{k}*: {v}" for k, v in context.items()]
    return "\n".join(lines)


def format_slack(payload: NotificationPayload) -> dict[str, Any]:
    """Format notification for Slack Block Kit.

    Args:
        payload: Notification payload

    Returns:
        Slack message dict with blocks and attachments
    """
    severity = _normalize_severity(payload.severity)
    context_text = _format_context_lines(payload.context)

    # Build main text
    main_text = f"*{payload.title}*\n{payload.body}"
    if context_text:
        main_text += f"\n\n{context_text}"

    # Build blocks
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": payload.title,
                "emoji": True
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": payload.body
            }
        }
    ]

    # Add context section if present
    if context_text:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": context_text
                }
            ]
        })

    # Add severity indicator
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": f"Severity: *{severity}*"
            }
        ]
    })

    # Build attachment fallback
    fallback_text = f"*{payload.title}*\n{payload.body}"
    if context_text:
        fallback_text += f"\n\n{context_text}"
    fallback_text += f"\nSeverity: {severity}"

    # Determine color based on severity
    color = _severity_to_color(severity)

    return {
        "text": main_text,
        "blocks": blocks,
        "attachments": [
            {
                "color": color,
                "fallback": fallback_text
            }
        ]
    }


def format_discord(payload: NotificationPayload) -> dict[str, Any]:
    """Format notification for Discord webhook.

    Args:
        payload: Notification payload

    Returns:
        Discord webhook payload dict
    """
    severity = _normalize_severity(payload.severity)

    # Build main content - only first line of body
    first_body_line = payload.body.splitlines()[0] if payload.body else ""
    content = f"**{payload.title}**\n{first_body_line}"

    # Build embed
    embed = {
        "title": payload.title,
        "description": payload.body,
        "color": _severity_to_color_hex(severity),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(payload.timestamp))
    }

    # Add context fields
    fields = []
    if payload.context:
        for k, v in payload.context.items():
            fields.append({
                "name": k,
                "value": str(v),
                "inline": True
            })

    # Add severity field
    fields.append({
        "name": "Severity",
        "value": severity,
        "inline": True
    })
    embed["fields"] = fields

    return {
        "content": content,
        "embeds": [embed]
    }


def _severity_to_color(severity: str) -> str:
    """Convert severity to Slack color code."""
    color_map = {
        "info": "#3498db",      # Blue
        "success": "#2ecc71",   # Green
        "warning": "#f1c40f",   # Yellow
        "error": "#e74c3c",     # Red
        "critical": "#8e44ad"   # Purple
    }
    return color_map.get(severity, "#95a5a6")  # Default: grey


def _severity_to_color_hex(severity: str) -> int:
    """Convert severity to Discord color hex code."""
    color_map = {
        "info": 0x3498DB,
        "success": 0x2ECC71,
        "warning": 0xF1C40F,
        "error": 0xE74C3C,
        "critical": 0x8E44AF
    }
    return color_map.get(severity, 0x95A5A6)


def load_webhook_urls(overrides: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """Load webhook URLs from environment variables.

    Args:
        overrides: Optional dict of platform -> [urls] to override env vars.
            Values can be strings (split on comma), lists, or other iterables.
            Empty overrides fall back to env vars.

    Returns:
        Dict mapping platform names to lists of webhook URLs
    """
    result = {}

    # Determine which env vars and overrides to check
    env_map = {
        "slack": SLACK_ENV_VAR,
        "discord": DISCORD_ENV_VAR
    }

    for platform, env_var in env_map.items():
        # Check override first if provided and non-empty
        if overrides and overrides.get(platform):
            override_val = overrides[platform]
            urls = _as_list(override_val)
            if urls:
                result[platform] = urls
                continue

        # Fall back to env var
        env_val = os.environ.get(env_var, "").strip()
        if env_val:
            result[platform] = _as_list(env_val)

    return result


def _as_list(value: Any) -> list[str]:
    """Convert value to list of strings.
    
    Handles strings (split on comma), lists/iterables (flatten), and non-iterables (return []).
    """
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    
    if isinstance(value, (list, tuple)):
        # Flatten and convert to strings
        result = []
        for item in value:
            s = str(item).strip()
            if s:
                result.append(s)
        return result
    
    if hasattr(value, '__iter__') and not isinstance(value, (dict, str)):
        # Generic iterable
        result = []
        for item in value:
            s = str(item).strip()
            if s:
                result.append(s)
        return result
    
    # Non-iterable (int, None, etc.)
    return []


def _post_json(url: str, payload: dict[str, Any], timeout: float = DEFAULT_TIMEOUT) -> bool:
    """Post JSON payload to webhook URL.

    Args:
        url: Webhook URL
        payload: JSON-serializable payload
        timeout: HTTP timeout in seconds

    Returns:
        True if successful (2xx response), False otherwise
    """
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.code
            return 200 <= status < 300

    except urllib.error.HTTPError as e:
        logger.warning("Webhook HTTP error %s: %s", e.code, e.reason)
        return False
    except urllib.error.URLError as e:
        logger.warning("Webhook URL error: %s", e.reason)
        return False
    except TimeoutError:
        logger.warning("Webhook timeout after %s seconds", timeout)
        return False
    except Exception as e:
        logger.error("Unexpected webhook error: %s", e)
        return False


def send_slack(
    payload: NotificationPayload,
    webhook_urls: list[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT
) -> bool:
    """Send notification to Slack.

    Args:
        payload: Notification payload
        webhook_urls: Optional list of webhook URLs (loaded from env if None)
        timeout: HTTP timeout in seconds

    Returns:
        True if all webhooks successful, False if any failed
    """
    if webhook_urls is None:
        webhook_urls = load_webhook_urls().get("slack", [])

    if not webhook_urls:
        logger.info("No Slack webhook URLs configured")
        return False

    slack_payload = format_slack(payload)
    results = [_post_json(url, slack_payload, timeout) for url in webhook_urls]

    return all(results)


def send_discord(
    payload: NotificationPayload,
    webhook_urls: list[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT
) -> bool:
    """Send notification to Discord.

    Args:
        payload: Notification payload
        webhook_urls: Optional list of webhook URLs (loaded from env if None)
        timeout: HTTP timeout in seconds

    Returns:
        True if all webhooks successful, False if any failed
    """
    if webhook_urls is None:
        webhook_urls = load_webhook_urls().get("discord", [])

    if not webhook_urls:
        logger.info("No Discord webhook URLs configured")
        return False

    discord_payload = format_discord(payload)
    results = [_post_json(url, discord_payload, timeout) for url in webhook_urls]

    return all(results)


def send(
    payload: NotificationPayload,
    platforms: list[str] | None = None,
    webhook_urls: dict[str, list[str]] | None = None,
    timeout: float = DEFAULT_TIMEOUT
) -> dict[str, bool]:
    """Send notification to multiple platforms.

    Args:
        payload: Notification payload
        platforms: List of platform names to send to (default: all configured)
        webhook_urls: Optional dict of platform -> [urls] overrides
        timeout: HTTP timeout in seconds

    Returns:
        Dict mapping platform names to success status
    """
    urls = load_webhook_urls(webhook_urls)

    if platforms is None:
        platforms = list(urls.keys())

    results = {}

    if "slack" in platforms:
        results["slack"] = send_slack(payload, urls.get("slack", []), timeout)

    if "discord" in platforms:
        results["discord"] = send_discord(payload, urls.get("discord", []), timeout)

    # Handle unsupported platforms
    for platform in platforms:
        if platform not in ["slack", "discord"]:
            logger.warning("Unsupported platform: %s", platform)
            results[platform] = False

    return results

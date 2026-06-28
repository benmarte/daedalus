"""Reusable Slack/Discord incoming-webhook notification sender (issue #260).

This module is intentionally small and stdlib-only so it can be imported from
anywhere in the Daedalus pipeline (dispatcher scripts, watchdog cron jobs,
CLI helpers) without pulling in additional dependencies.

Responsibilities:
  1. Define a structured, platform-agnostic ``NotificationPayload`` dataclass:
     ``title``, ``body``, optional ``severity`` / ``context``, and ``timestamp``.
  2. Render that payload into each platform's preferred wire format:
     - Slack → Block Kit blocks (rich header + markdown section + context).
     - Discord → webhook ``embeds`` (color-coded by severity).
     - Plain-text fallbacks for either platform when blocks/embeds are not
       wanted.
  3. POST the formatted payload to one or more configured webhook URLs with
     timeouts, non-2xx handling, and structured ``logging``.
  4. Resolve webhook URLs from the environment (``SLACK_WEBHOOK_URL``,
     ``DISCORD_WEBHOOK_URL``) or from an explicit ``webhook_urls`` mapping
     passed to the send helpers.

All HTTP work uses ``urllib.request`` so unit tests never need third-party
libraries — tests simply mock ``urllib.request.urlopen``.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

logger = logging.getLogger("daedalus.notification_sender")

# Env vars read by ``load_webhook_urls``. Comma-separated values are expanded
# so a single env var can fan out to multiple webhooks if needed.
SLACK_ENV_VAR = "SLACK_WEBHOOK_URL"
DISCORD_ENV_VAR = "DISCORD_WEBHOOK_URL"

# Discord embed colors (decimal) by severity. Matches common status semantics.
_SEVERITY_COLORS: Dict[str, int] = {
    "info": 0x3498DB,     # blue
    "success": 0x2ECC71,  # green
    "warning": 0xF1C40F,  # yellow
    "error": 0xE74C3C,    # red
    "critical": 0x8E44AD, # purple
}

# Slack color strip on the attachment fallback (hex; Block Kit itself is
# uncolored, but the fallback attachment keeps the old layout working).
_SEVERITY_SLACK_COLORS: Dict[str, str] = {
    "info": "#3498db",
    "success": "#2ecc71",
    "warning": "#f1c40f",
    "error": "#e74c3c",
    "critical": "#8e44ad",
}

# Default HTTP timeout (seconds) for webhook POSTs.
DEFAULT_TIMEOUT = 10.0


# ── payload ──────────────────────────────────────────────────────────────────


@dataclass
class NotificationPayload:
    """Structured notification body — platform-agnostic.

    ``title`` / ``body`` are the only required fields. ``severity`` is free-form
    text but is normalised to one of the known buckets (info/success/warning/
    error/critical) when formatting so the colour is predictable. ``context``
    is an unstructured dict that the formatters render as "context" lines.
    """

    title: str
    body: str
    severity: str = "info"
    context: Dict[str, Any] = field(default_factory=dict)
    timestamp: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.title or not self.title.strip():
            raise ValueError("NotificationPayload.title must not be empty")
        if not self.body or not self.body.strip():
            raise ValueError("NotificationPayload.body must not be empty")
        if self.timestamp is None:
            self.timestamp = time.time()

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── formatters ───────────────────────────────────────────────────────────────


def _normalize_severity(severity: str) -> str:
    key = (severity or "info").lower().strip()
    return key if key in _SEVERITY_COLORS else "info"


def _format_context_lines(context: Mapping[str, Any]) -> List[str]:
    # Slack mrkdwn bold is *text* (asterisks wrap the text, not trailing).
    return [f"*{k}*: {v}" for k, v in context.items()] if context else []


def format_slack(payload: NotificationPayload) -> Dict[str, Any]:
    """Render a Slack Block Kit payload.

    Returns the full top-level body to POST to a Slack incoming webhook.
    Includes a fallback ``attachments`` array so clients that don't support
    Block Kit still render something readable.
    """
    severity = _normalize_severity(payload.severity)
    context_lines = _format_context_lines(payload.context)

    sections = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": payload.title,
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": payload.body},
        },
    ]
    if context_lines:
        sections.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "\n".join(context_lines)}
                ],
            }
        )
    sections.append(
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"Severity: *{severity}*"}
            ],
        }
    )

    # Legacy attachment fallback — same content, plain markdown.
    fallback_text = "\n".join(
        [f"*{payload.title}*", "", payload.body]
        + (["", *context_lines] if context_lines else [])
        + [f"\nSeverity: {severity}"]
    )

    return {
        "text": fallback_text,
        "blocks": sections,
        "attachments": [
            {
                "color": _SEVERITY_SLACK_COLORS.get(severity, "#cccccc"),
                "fallback": fallback_text,
            }
        ],
    }


def format_discord(payload: NotificationPayload) -> Dict[str, Any]:
    """Render a Discord webhook payload using embeds.

    Returns a top-level ``{"content": ..., "embeds": [...]}`` body.
    """
    severity = _normalize_severity(payload.severity)
    fields = [
        {"name": str(k), "value": str(v), "inline": True}
        for k, v in payload.context.items()
    ]
    fields.append({"name": "Severity", "value": severity, "inline": True})

    embed = {
        "title": payload.title,
        "description": payload.body,
        "color": _SEVERITY_COLORS.get(severity, _SEVERITY_COLORS["info"]),
        "fields": fields,
        "timestamp": _iso_timestamp(payload.timestamp),
    }

    return {
        "content": f"**{payload.title}** — {payload.body.splitlines()[0] if payload.body else ''}",
        "embeds": [embed],
    }


def _iso_timestamp(timestamp: Optional[float]) -> str:
    if timestamp is None:
        timestamp = time.time()
    # `time.strftime` is UTC-compatible with `time.gmtime`.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


# ── configuration ────────────────────────────────────────────────────────────


def load_webhook_urls(
    overrides: Optional[Mapping[str, Any]] = None,
) -> Dict[str, List[str]]:
    """Resolve Slack/Discord webhook URLs.

    Resolution order (per platform):
      1. ``overrides`` mapping (``{"slack": [...]}`` or ``{"discord": [...]}``)
         if present and non-empty.
      2. The matching environment variable (``SLACK_WEBHOOK_URL`` /
         ``DISCORD_WEBHOOK_URL``). Comma-separated values are split and
         stripped; empty entries are dropped.

    Returns a dict with keys ``slack`` and/or ``discord`` mapping to lists
    of URLs. Platforms with no configured webhook are omitted.
    """
    overrides = overrides or {}
    result: Dict[str, List[str]] = {}

    env_map = {"slack": SLACK_ENV_VAR, "discord": DISCORD_ENV_VAR}
    for platform, env_var in env_map.items():
        urls: Iterable[str] = []
        if platform in overrides and overrides[platform]:
            urls = _as_list(overrides[platform])
        else:
            raw = os.environ.get(env_var, "") or ""
            urls = [u.strip() for u in raw.split(",") if u and u.strip()]
        urls = [u for u in urls if u]
        if urls:
            result[platform] = list(urls)
    return result


def _as_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, Iterable):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


# ── HTTP sender ──────────────────────────────────────────────────────────────


def _post_json(
    url: str,
    body: Dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """POST ``body`` as JSON to ``url``. Returns True on 2xx, False otherwise.

    Never raises — every failure mode (timeout, DNS, HTTP 4xx/5xx, JSON
    serialisation) is logged at WARNING and converted to False.
    """
    try:
        data = json.dumps(body).encode("utf-8")
    except (TypeError, ValueError) as exc:
        logger.warning("notification_sender: failed to serialise payload: %s", exc)
        return False

    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                # urllib on Python 3 reports HTTPStatus via the code attribute.
                status = getattr(response, "code", 0)
            ok = 200 <= int(status) < 300
            if not ok:
                logger.warning(
                    "notification_sender: non-2xx response from %s: %s",
                    url,
                    status,
                )
            return ok
    except urllib.error.HTTPError as exc:
        logger.warning(
            "notification_sender: HTTP %s from %s: %s",
            exc.code,
            url,
            exc.reason,
        )
        return False
    except urllib.error.URLError as exc:
        logger.warning(
            "notification_sender: URL error posting to %s: %s", url, exc.reason
        )
        return False
    except TimeoutError:
        logger.warning(
            "notification_sender: timeout posting to %s (timeout=%.1fs)",
            url,
            timeout,
        )
        return False
    except OSError as exc:
        logger.warning("notification_sender: OS error posting to %s: %s", url, exc)
        return False


def send_slack(
    payload: NotificationPayload,
    webhook_urls: Optional[Iterable[str]] = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Send ``payload`` to one or more Slack incoming webhooks.

    If ``webhook_urls`` is omitted/empty, falls back to
    ``load_webhook_urls()["slack"]``. Returns True only when ALL targeted URLs
    succeed; returns False if no URLs are configured.
    """
    urls = list(webhook_urls) if webhook_urls else load_webhook_urls().get("slack", [])
    if not urls:
        logger.info("notification_sender: no Slack webhooks configured — skipping")
        return False

    body = format_slack(payload)
    success = True
    for url in urls:
        ok = _post_json(url, body, timeout=timeout)
        if ok:
            logger.info("notification_sender: slack webhook delivered to %s", url)
        else:
            success = False
    return success


def send_discord(
    payload: NotificationPayload,
    webhook_urls: Optional[Iterable[str]] = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Send ``payload`` to one or more Discord incoming webhooks.

    Mirrors ``send_slack`` semantics — returns True only when ALL URLs succeed;
    False if no URLs are configured or any delivery fails.
    """
    urls = (
        list(webhook_urls) if webhook_urls else load_webhook_urls().get("discord", [])
    )
    if not urls:
        logger.info("notification_sender: no Discord webhooks configured — skipping")
        return False

    body = format_discord(payload)
    success = True
    for url in urls:
        ok = _post_json(url, body, timeout=timeout)
        if ok:
            logger.info("notification_sender: discord webhook delivered to %s", url)
        else:
            success = False
    return success


def send(
    payload: NotificationPayload,
    platforms: Optional[Iterable[str]] = None,
    *,
    webhook_urls: Optional[Mapping[str, Iterable[str]]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, bool]:
    """Send ``payload`` to multiple platforms in one call.

    ``platforms`` defaults to every platform with a configured webhook. The
    return value maps each attempted platform to its success boolean.
    """
    resolved_urls = load_webhook_urls(webhook_urls)
    targets = list(platforms) if platforms else list(resolved_urls.keys())
    results: Dict[str, bool] = {}
    for platform in targets:
        urls = (webhook_urls or {}).get(platform) or resolved_urls.get(platform, [])
        if platform == "slack":
            results["slack"] = send_slack(payload, urls, timeout=timeout)
        elif platform == "discord":
            results["discord"] = send_discord(payload, urls, timeout=timeout)
        else:
            logger.warning(
                "notification_sender: unsupported platform %r — skipping", platform
            )
            results[platform] = False
    return results

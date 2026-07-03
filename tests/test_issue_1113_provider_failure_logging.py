"""Regression tests for issue #1113 — provider instantiation failure in
``_notify_project_summary`` silently drops the tick-summary notification.

When ``providers.get_provider(resolved)`` raises, the code falls back to
``provider = None`` and ``render_dispatch_summary`` then produces a degraded /
empty message — dropping the summary for the whole tick with no trace. These
tests assert that:

  * a warning is emitted (with the exception detail and project identity), and
  * the rest of the dispatch continues normally (the summary is still sent), and
  * the happy path is unchanged (no warning when the provider builds fine).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest import mock

# Make the package root importable (config/, core/) and the tests dir (conftest).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _load_dispatch  # noqa: E402

_RESOLVED = {
    # No "workdir" -> the simple plain-send fallback path, so we can assert the
    # summary is still delivered after the provider failure.
    "cron": {
        "notifications": [
            {
                "platform": "Slack",
                "target": "slack:C1",
                "events": ["dispatch-summary"],
            },
        ]
    },
}

_SUMMARY = {
    "board": "b",
    "mode": "github",
    "created": [1],
    "reconciled": [],
    "completed": [],
    "advance_prs": [],
    "routed_actions": {},
    "issues_seen": 1,
    "spec_created": [],
    "slack_delivered": [],
}


def test_provider_failure_logs_and_dispatch_continues(caplog):
    """get_provider raising must log a warning AND still deliver the summary."""
    disp = _load_dispatch()
    sent = []
    with (
        mock.patch.object(
            disp.providers, "get_provider", side_effect=RuntimeError("boom")
        ),
        mock.patch.object(
            disp.notify_templates,
            "render_dispatch_summary",
            return_value="degraded summary",
        ),
        mock.patch.object(
            disp, "_send_via_hermes", side_effect=lambda t, m: sent.append(t) or True
        ),
    ):
        with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
            handled = disp._notify_project_summary("proj", _SUMMARY, _RESOLVED)

    # Warning surfaced with the exception detail and project identity.
    assert any("boom" in r.message and "proj" in r.message for r in caplog.records), (
        "expected a warning naming the exception and project"
    )
    # Dispatch continued: render was still called and the summary delivered.
    assert handled is True
    assert sent == ["slack:C1"], "summary should still be sent despite provider failure"


def test_happy_path_emits_no_warning(caplog):
    """No behavior change on the happy path: a working provider logs nothing."""
    disp = _load_dispatch()
    sent = []
    fake_provider = object()
    with (
        mock.patch.object(disp.providers, "get_provider", return_value=fake_provider),
        mock.patch.object(
            disp.notify_templates,
            "render_dispatch_summary",
            return_value="summary",
        ),
        mock.patch.object(
            disp, "_send_via_hermes", side_effect=lambda t, m: sent.append(t) or True
        ),
    ):
        with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
            handled = disp._notify_project_summary("proj", _SUMMARY, _RESOLVED)

    assert handled is True
    assert sent == ["slack:C1"]
    assert not any(
        "provider instantiation failed" in r.message for r in caplog.records
    ), "happy path must not emit the provider-failure warning"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))

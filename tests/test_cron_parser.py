"""Tests for core/cron_parser.py — the shared ``hermes cron list`` parser (issue #1148).

Covers the canonical parser extracted from ``dashboard/plugin_api._parse_cron_jobs``
and consumed by both the dashboard API and the ``__init__.py`` cron self-heal loop.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.cron_parser import parse_cron_jobs  # noqa: E402


_MULTI_ENTRY = """\
┌─────────────────────────────────────────────────────────────────────────┐
│                         Scheduled Jobs                                  │
└─────────────────────────────────────────────────────────────────────────┘

  99f7d116a95b [active]
    Name:      alpha-daedalus
    Schedule:  */15 * * * *
    Last run:  2026-07-01T10:00:00 ok
    Script:    daedalus-cron.sh

  4be2c9d001ff [completed]
    Name:      beta-daedalus
    Schedule:  once in 15m

  ⚠  Gateway is not running — jobs won't fire automatically.
"""


def test_multi_entry_output_parsed():
    jobs = parse_cron_jobs(_MULTI_ENTRY)
    assert len(jobs) == 2
    first, second = jobs
    assert first == {
        "job_id": "99f7d116a95b",
        "state": "active",
        "name": "alpha-daedalus",
        "schedule": "*/15 * * * *",
        "last_run": "2026-07-01T10:00:00",
        "last_status": "ok",
        "script": "daedalus-cron.sh",
    }
    assert second["job_id"] == "4be2c9d001ff"
    assert second["state"] == "completed"
    assert second["name"] == "beta-daedalus"
    assert second["schedule"] == "once in 15m"
    assert second["last_run"] is None
    assert second["script"] is None


def test_empty_output_returns_empty_list():
    assert parse_cron_jobs("") == []


def test_noise_only_output_returns_empty_list():
    noise = "┌───┐\n│ Scheduled Jobs │\n└───┘\n\n  ⚠  Gateway is not running\n"
    assert parse_cron_jobs(noise) == []


def test_malformed_lines_ignored():
    output = (
        "garbage line without header\n"
        "  zzzz [active]\n"  # not hex — not a header
        "    Name:      orphan-fields-before-any-header\n"
        "  99f7d116a95b [active]\n"
        "    Name:      good-job\n"
    )
    jobs = parse_cron_jobs(output)
    assert len(jobs) == 1
    assert jobs[0]["name"] == "good-job"


def test_entry_without_name_dropped():
    output = "  99f7d116a95b [active]\n    Schedule:  */5 * * * *\n"
    assert parse_cron_jobs(output) == []

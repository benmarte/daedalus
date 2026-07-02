"""Regression tests for issue #1133 — silent exception swallowing hides failures.

Handlers that degrade gracefully (return None/[]/{} on error) must log the
exception detail so a malformed config, missing token, or CLI failure is
diagnosable instead of invisible. These tests assert the fallback value is
still returned (no behavior change) AND that a warning is emitted.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest import mock

# Make the package root importable (dashboard/, core/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import iterate  # noqa: E402
from dashboard import plugin_api  # noqa: E402


def test_fetch_project_tasks_logs_on_failure(caplog):
    """list_tasks raising must log a warning and still return None."""
    with mock.patch.object(plugin_api, "list_tasks", side_effect=RuntimeError("boom")):
        with caplog.at_level(logging.WARNING, logger="daedalus.dashboard.plugin_api"):
            result = plugin_api._fetch_project_tasks("some-board")
    assert result is None
    assert any("boom" in r.message and "list_tasks" in r.message for r in caplog.records)


def test_project_provider_logs_on_failure(caplog):
    """get_provider raising must log a warning and still return None."""
    with mock.patch.object(plugin_api, "get_provider", side_effect=RuntimeError("no token")):
        with caplog.at_level(logging.WARNING, logger="daedalus.dashboard.plugin_api"):
            result = plugin_api._project_provider({"name": "demo", "repo": "o/r"})
    assert result is None
    assert any("no token" in r.message for r in caplog.records)


def test_read_fix_attempts_logs_on_corrupt_file(tmp_path, caplog):
    """A corrupt fix-attempts JSON file must log a warning and return {}."""
    hermes = tmp_path / ".hermes"
    hermes.mkdir(parents=True)
    (hermes / "daedalus-fix-attempts.json").write_text("{not valid json")
    with caplog.at_level(logging.WARNING, logger="daedalus.iterate"):
        result = iterate._read_fix_attempts(str(tmp_path))
    assert result == {}
    assert any("fix-attempts" in r.message for r in caplog.records)


def test_read_fix_attempts_still_reads_valid_file(tmp_path):
    """Sanity: valid file still parses (no behavior change beyond logging)."""
    hermes = tmp_path / ".hermes"
    hermes.mkdir(parents=True)
    (hermes / "daedalus-fix-attempts.json").write_text(json.dumps({"t_1": 2}))
    assert iterate._read_fix_attempts(str(tmp_path)) == {"t_1": 2}

"""Integration tests for config fingerprint computation on each dispatcher tick.

Verifies that:
- The dispatcher stores a config fingerprint on a tick
- A subsequent tick with changed config produces a different fingerprint
- A subsequent tick with identical config produces the same fingerprint

Run: pytest tests/test_config_fingerprint_integration.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import daedalus_dispatch as disp  # noqa: E402
from core import dispatch_state  # noqa: E402


def _make_resolved(tmp_path: str, coding_agent: str = "hermes",
                   model_default: str = "anthropic/claude-sonnet-4") -> dict:
    """Build a minimal resolved config dict for run()."""
    return {
        "repo": "test/repo",
        "workdir": str(tmp_path),
        "execution": {
            "coding_agent": coding_agent,
            "coding_agent_cmd": "echo hello",
            "max_lifecycle_iterations": 0,
        },
        "issues": {"filters": {}},
        "vcs": {"target_branch": "dev"},
        "name": "test-repo",
        "cron": {"deliver": ""},
    }


def _write_hermes_config(tmp_path: Path, model_default: str):
    """Write a Hermes config.yaml with the given model.default."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(exist_ok=True)
    (hermes_home / "config.yaml").write_text(
        f"model:\n  default: {model_default}\n  provider: anthropic\n"
    )


class TestDispatcherConfigFingerprintIntegration:
    """Integration tests: dispatcher stores and updates config fingerprint per tick."""

    def test_tick_stores_fingerprint(self, tmp_path):
        """A single dispatch tick stores a config fingerprint in dispatch_state."""
        workdir = str(tmp_path)
        _write_hermes_config(tmp_path, "anthropic/claude-sonnet-4")
        resolved = _make_resolved(workdir, coding_agent="claude-code")

        with mock.patch.dict(os.environ, {"HERMES_HOME": str(tmp_path / "hermes")}):
            disp.run(resolved, dry_run=False, provider=disp.providers.get_provider(resolved))

        fp = dispatch_state.get_config_fingerprint(workdir)
        assert fp is not None
        assert len(fp) == 64  # SHA-256 hex

    def test_same_config_produces_same_fingerprint_across_ticks(self, tmp_path):
        """Two ticks with identical config produce the same fingerprint."""
        workdir = str(tmp_path)
        _write_hermes_config(tmp_path, "anthropic/claude-sonnet-4")
        resolved = _make_resolved(workdir, coding_agent="claude-code")

        with mock.patch.dict(os.environ, {"HERMES_HOME": str(tmp_path / "hermes")}):
            disp.run(resolved, dry_run=False, provider=disp.providers.get_provider(resolved))
            fp1 = dispatch_state.get_config_fingerprint(workdir)
            disp.run(resolved, dry_run=False, provider=disp.providers.get_provider(resolved))
            fp2 = dispatch_state.get_config_fingerprint(workdir)

        assert fp1 is not None
        assert fp2 is not None
        assert fp1 == fp2

    def test_changed_coding_agent_produces_different_fingerprint(self, tmp_path):
        """Changing coding_agent between ticks produces a different fingerprint."""
        workdir = str(tmp_path)
        _write_hermes_config(tmp_path, "anthropic/claude-sonnet-4")

        with mock.patch.dict(os.environ, {"HERMES_HOME": str(tmp_path / "hermes")}):
            resolved1 = _make_resolved(workdir, coding_agent="claude-code")
            disp.run(resolved1, dry_run=False, provider=disp.providers.get_provider(resolved1))
            fp1 = dispatch_state.get_config_fingerprint(workdir)

            resolved2 = _make_resolved(workdir, coding_agent="codex")
            disp.run(resolved2, dry_run=False, provider=disp.providers.get_provider(resolved2))
            fp2 = dispatch_state.get_config_fingerprint(workdir)

        assert fp1 is not None
        assert fp2 is not None
        assert fp1 != fp2

    def test_changed_model_produces_different_fingerprint(self, tmp_path):
        """Changing model.default between ticks produces a different fingerprint."""
        workdir = str(tmp_path)

        _write_hermes_config(tmp_path, "anthropic/claude-sonnet-4")
        resolved = _make_resolved(workdir, coding_agent="claude-code")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(tmp_path / "hermes")}):
            disp.run(resolved, dry_run=False, provider=disp.providers.get_provider(resolved))
            fp1 = dispatch_state.get_config_fingerprint(workdir)

        _write_hermes_config(tmp_path, "openrouter/qwen3")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(tmp_path / "hermes")}):
            disp.run(resolved, dry_run=False, provider=disp.providers.get_provider(resolved))
            fp2 = dispatch_state.get_config_fingerprint(workdir)

        assert fp1 is not None
        assert fp2 is not None
        assert fp1 != fp2

    def test_dry_run_does_not_store_fingerprint(self, tmp_path):
        """dry_run=True does not persist a fingerprint (no side effects)."""
        workdir = str(tmp_path)
        _write_hermes_config(tmp_path, "anthropic/claude-sonnet-4")
        resolved = _make_resolved(workdir, coding_agent="claude-code")

        with mock.patch.dict(os.environ, {"HERMES_HOME": str(tmp_path / "hermes")}):
            disp.run(resolved, dry_run=True, provider=disp.providers.get_provider(resolved))

        assert dispatch_state.get_config_fingerprint(workdir) is None

    def test_fingerprint_matches_compute_config_fingerprint(self, tmp_path):
        """The stored fingerprint matches what compute_config_fingerprint produces."""
        workdir = str(tmp_path)
        _write_hermes_config(tmp_path, "anthropic/claude-sonnet-4")
        resolved = _make_resolved(workdir, coding_agent="claude-code")

        with mock.patch.dict(os.environ, {"HERMES_HOME": str(tmp_path / "hermes")}):
            disp.run(resolved, dry_run=False, provider=disp.providers.get_provider(resolved))

        stored_fp = dispatch_state.get_config_fingerprint(workdir)
        expected_fp = dispatch_state.compute_config_fingerprint("claude-code", "anthropic/claude-sonnet-4")
        assert stored_fp == expected_fp


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
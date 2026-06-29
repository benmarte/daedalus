"""Unit tests for the profile resync log message format (issue #1055).

Verifies the exact log output for:
- coding_agent-change scenario
- global-model-change scenario
- both-changed scenario
- zero profiles resynced edge case

Run: pytest tests/test_resync_log.py -v
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import daedalus_dispatch as disp  # noqa: E402
from core import dispatch_state  # noqa: E402


class TestLogResync:
    """Unit tests for _log_resync — the INFO-level resync log line."""

    def test_coding_agent_change_only(self, caplog):
        """Log: 'Resynced 8 profiles to model X (coding_agent changed from A to B)'."""
        with caplog.at_level(logging.INFO, logger="daedalus.dispatch"):
            disp._log_resync(
                count=8,
                new_model="anthropic/claude-sonnet-4",
                old_coding_agent="claude-code",
                new_coding_agent="codex",
                old_model="anthropic/claude-sonnet-4",
                new_model_for_log="anthropic/claude-sonnet-4",
            )
        resync_logs = [r for r in caplog.records if "Resynced" in r.getMessage()]
        assert len(resync_logs) == 1
        msg = resync_logs[0].getMessage()
        assert msg == (
            "Resynced 8 profiles to model anthropic/claude-sonnet-4 "
            "(coding_agent changed from claude-code to codex)"
        )
        assert resync_logs[0].levelno == logging.INFO

    def test_global_model_change_only(self, caplog):
        """Log: 'Resynced 8 profiles to model X (global model changed from A to B)'."""
        with caplog.at_level(logging.INFO, logger="daedalus.dispatch"):
            disp._log_resync(
                count=8,
                new_model="openrouter/qwen3",
                old_coding_agent="claude-code",
                new_coding_agent="claude-code",
                old_model="anthropic/claude-sonnet-4",
                new_model_for_log="openrouter/qwen3",
            )
        resync_logs = [r for r in caplog.records if "Resynced" in r.getMessage()]
        assert len(resync_logs) == 1
        msg = resync_logs[0].getMessage()
        assert msg == (
            "Resynced 8 profiles to model openrouter/qwen3 "
            "(global model changed from anthropic/claude-sonnet-4 to openrouter/qwen3)"
        )
        assert resync_logs[0].levelno == logging.INFO

    def test_both_changed(self, caplog):
        """Log: 'Resynced 8 profiles to model X (coding_agent changed from A to B, global model changed from C to D)'."""
        with caplog.at_level(logging.INFO, logger="daedalus.dispatch"):
            disp._log_resync(
                count=8,
                new_model="openrouter/qwen3",
                old_coding_agent="claude-code",
                new_coding_agent="codex",
                old_model="anthropic/claude-sonnet-4",
                new_model_for_log="openrouter/qwen3",
            )
        resync_logs = [r for r in caplog.records if "Resynced" in r.getMessage()]
        assert len(resync_logs) == 1
        msg = resync_logs[0].getMessage()
        assert msg == (
            "Resynced 8 profiles to model openrouter/qwen3 "
            "(coding_agent changed from claude-code to codex, "
            "global model changed from anthropic/claude-sonnet-4 to openrouter/qwen3)"
        )

    def test_zero_profiles_coding_agent_change(self, caplog):
        """Edge case: zero profiles resynced, coding_agent changed."""
        with caplog.at_level(logging.INFO, logger="daedalus.dispatch"):
            disp._log_resync(
                count=0,
                new_model="anthropic/claude-sonnet-4",
                old_coding_agent="claude-code",
                new_coding_agent="codex",
                old_model="anthropic/claude-sonnet-4",
                new_model_for_log="anthropic/claude-sonnet-4",
            )
        resync_logs = [r for r in caplog.records if "Resynced" in r.getMessage()]
        assert len(resync_logs) == 1
        msg = resync_logs[0].getMessage()
        assert msg == (
            "Resynced 0 profiles to model anthropic/claude-sonnet-4 "
            "(coding_agent changed from claude-code to codex)"
        )

    def test_zero_profiles_global_model_change(self, caplog):
        """Edge case: zero profiles resynced, global model changed."""
        with caplog.at_level(logging.INFO, logger="daedalus.dispatch"):
            disp._log_resync(
                count=0,
                new_model="openrouter/qwen3",
                old_coding_agent="claude-code",
                new_coding_agent="claude-code",
                old_model="anthropic/claude-sonnet-4",
                new_model_for_log="openrouter/qwen3",
            )
        resync_logs = [r for r in caplog.records if "Resynced" in r.getMessage()]
        assert len(resync_logs) == 1
        msg = resync_logs[0].getMessage()
        assert msg == (
            "Resynced 0 profiles to model openrouter/qwen3 "
            "(global model changed from anthropic/claude-sonnet-4 to openrouter/qwen3)"
        )

    def test_no_change_no_parenthetical(self, caplog):
        """When nothing actually changed (fingerprint changed but values are same), no parenthetical."""
        with caplog.at_level(logging.INFO, logger="daedalus.dispatch"):
            disp._log_resync(
                count=3,
                new_model="anthropic/claude-sonnet-4",
                old_coding_agent="claude-code",
                new_coding_agent="claude-code",
                old_model="anthropic/claude-sonnet-4",
                new_model_for_log="anthropic/claude-sonnet-4",
            )
        resync_logs = [r for r in caplog.records if "Resynced" in r.getMessage()]
        assert len(resync_logs) == 1
        msg = resync_logs[0].getMessage()
        assert msg == "Resynced 3 profiles to model anthropic/claude-sonnet-4"

    def test_none_model_shown_as_none(self, caplog):
        """When model is None/empty, it's shown as 'none' in the log."""
        with caplog.at_level(logging.INFO, logger="daedalus.dispatch"):
            disp._log_resync(
                count=5,
                new_model="",
                old_coding_agent="claude-code",
                new_coding_agent="hermes",
                old_model="anthropic/claude-sonnet-4",
                new_model_for_log="",
            )
        resync_logs = [r for r in caplog.records if "Resynced" in r.getMessage()]
        assert len(resync_logs) == 1
        msg = resync_logs[0].getMessage()
        assert "Resynced 5 profiles to model none" in msg
        assert "coding_agent changed from claude-code to hermes" in msg
        assert "global model changed from anthropic/claude-sonnet-4 to none" in msg

    def test_empty_coding_agent_shown_as_none(self, caplog):
        """When old coding_agent is empty, it's shown as 'none' in the log."""
        with caplog.at_level(logging.INFO, logger="daedalus.dispatch"):
            disp._log_resync(
                count=2,
                new_model="anthropic/claude-sonnet-4",
                old_coding_agent="",
                new_coding_agent="claude-code",
                old_model="anthropic/claude-sonnet-4",
                new_model_for_log="anthropic/claude-sonnet-4",
            )
        resync_logs = [r for r in caplog.records if "Resynced" in r.getMessage()]
        assert len(resync_logs) == 1
        msg = resync_logs[0].getMessage()
        assert "coding_agent changed from none to claude-code" in msg


class TestResyncProfilesToModel:
    """Unit tests for _resync_profiles_to_model — using current keyword-argument API."""

    def _make_profiles(self, hermes_home: Path, names: list[str], model: str = "old-model"):
        profiles_dir = hermes_home / "profiles"
        profiles_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            pdir = profiles_dir / name
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "config.yaml").write_text(
                f"model:\n  default: {model}\n  provider: anthropic\n"
            )

    def test_resyncs_matching_profiles(self, tmp_path):
        hh = tmp_path / "hermes"
        self._make_profiles(hh, ["developer-daedalus", "validator-daedalus"], model="old-model")

        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count = disp._resync_profiles_to_model(
                workdir=str(tmp_path),
                new_model="new-model",
                new_provider="openrouter",
                old_values={"coding_agent": "claude-code", "model_default": "old-model"},
            )
        assert count == 2
        for name in ("developer-daedalus", "validator-daedalus"):
            cfg = yaml.safe_load((hh / "profiles" / name / "config.yaml").read_text())
            assert cfg["model"]["default"] == "new-model"
            assert cfg["model"]["provider"] == "openrouter"

    def test_skips_explicit_override_profiles(self, tmp_path):
        """Profiles whose model differs from old global default are treated as intentional overrides."""
        hh = tmp_path / "hermes"
        self._make_profiles(hh, ["developer-daedalus"], model="old-model")
        self._make_profiles(hh, ["locked-daedalus"], model="custom-model")

        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count = disp._resync_profiles_to_model(
                workdir=str(tmp_path),
                new_model="new-model",
                new_provider="openrouter",
                old_values={"coding_agent": "claude-code", "model_default": "old-model"},
            )
        assert count == 1
        dev_cfg = yaml.safe_load((hh / "profiles" / "developer-daedalus" / "config.yaml").read_text())
        assert dev_cfg["model"]["default"] == "new-model"
        locked_cfg = yaml.safe_load((hh / "profiles" / "locked-daedalus" / "config.yaml").read_text())
        assert locked_cfg["model"]["default"] == "custom-model"

    def test_zero_profiles_when_none_exist(self, tmp_path):
        hh = tmp_path / "hermes"
        (hh / "profiles").mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count = disp._resync_profiles_to_model(
                workdir=str(tmp_path),
                new_model="new-model",
                new_provider="openrouter",
                old_values={"coding_agent": "claude-code", "model_default": "old-model"},
            )
        assert count == 0

    def test_zero_profiles_when_already_in_sync(self, tmp_path):
        hh = tmp_path / "hermes"
        self._make_profiles(hh, ["developer-daedalus"], model="new-model")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count = disp._resync_profiles_to_model(
                workdir=str(tmp_path),
                new_model="new-model",
                new_provider="openrouter",
                old_values={"coding_agent": "claude-code", "model_default": "old-model"},
            )
        assert count == 0

    def test_skips_non_daedalus_profiles(self, tmp_path):
        hh = tmp_path / "hermes"
        profiles_dir = hh / "profiles"
        profiles_dir.mkdir(parents=True, exist_ok=True)
        (profiles_dir / "default").mkdir(parents=True, exist_ok=True)
        (profiles_dir / "default" / "config.yaml").write_text(
            "model:\n  default: should-not-change\n  provider: anthropic\n"
        )
        self._make_profiles(hh, ["developer-daedalus"], model="old-model")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count = disp._resync_profiles_to_model(
                workdir=str(tmp_path),
                new_model="new-model",
                new_provider="openrouter",
                old_values={"coding_agent": "claude-code", "model_default": "old-model"},
            )
        assert count == 1
        default_cfg = yaml.safe_load((hh / "profiles" / "default" / "config.yaml").read_text())
        assert default_cfg["model"]["default"] == "should-not-change"

    def test_no_profiles_dir_returns_zero(self, tmp_path):
        hh = tmp_path / "hermes"
        hh.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count = disp._resync_profiles_to_model(
                workdir=str(tmp_path),
                new_model="new-model",
                new_provider="openrouter",
                old_values={"coding_agent": "claude-code", "model_default": "old-model"},
            )
        assert count == 0


class TestConfigValuesPersistence:
    """Tests for dispatch_state.set_config_values / get_config_values."""

    def test_set_and_get_config_values(self, tmp_path):
        """Config values round-trip through the state file."""
        wd = str(tmp_path)
        dispatch_state.set_config_values(wd, "claude-code", "anthropic/claude-sonnet-4")
        vals = dispatch_state.get_config_values(wd)
        assert vals is not None
        assert vals["coding_agent"] == "claude-code"
        assert vals["model_default"] == "anthropic/claude-sonnet-4"

    def test_get_config_values_returns_none_when_not_set(self, tmp_path):
        """No config values stored → None."""
        wd = str(tmp_path)
        assert dispatch_state.get_config_values(wd) is None

    def test_set_config_values_overwrites_previous(self, tmp_path):
        """Second set overwrites the first."""
        wd = str(tmp_path)
        dispatch_state.set_config_values(wd, "claude-code", "model-a")
        dispatch_state.set_config_values(wd, "codex", "model-b")
        vals = dispatch_state.get_config_values(wd)
        assert vals is not None
        assert vals["coding_agent"] == "codex"
        assert vals["model_default"] == "model-b"

    def test_set_config_values_preserves_other_state(self, tmp_path):
        """Setting config values doesn't wipe other dispatch_state keys."""
        wd = str(tmp_path)
        dispatch_state.record_dispatch(wd, 42)
        dispatch_state.set_config_values(wd, "hermes", "model-x")
        assert dispatch_state.get_dispatch_age_hours(wd, 42) is not None
        vals = dispatch_state.get_config_values(wd)
        assert vals is not None
        assert vals["coding_agent"] == "hermes"
        assert vals["model_default"] == "model-x"

    def test_set_config_values_handles_none(self, tmp_path):
        """None values are stored as empty strings."""
        wd = str(tmp_path)
        dispatch_state.set_config_values(wd, None, None)
        vals = dispatch_state.get_config_values(wd)
        assert vals is not None
        assert vals["coding_agent"] == ""
        assert vals["model_default"] == ""

    def test_config_values_survive_reload(self, tmp_path):
        """Config values persist across state reloads (simulates next tick)."""
        wd = str(tmp_path)
        dispatch_state.set_config_values(wd, "claude-code", "anthropic/claude-sonnet-4")
        # Simulate a new tick by reading fresh from disk
        vals = dispatch_state.get_config_values(wd)
        assert vals is not None
        assert vals["coding_agent"] == "claude-code"
        assert vals["model_default"] == "anthropic/claude-sonnet-4"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
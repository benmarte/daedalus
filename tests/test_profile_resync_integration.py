"""Integration tests for dispatcher-triggered profile resync on fingerprint change.

Verifies end-to-end: fingerprint change -> dispatcher run() -> profile resync invocation.

Run: pytest tests/test_profile_resync_integration.py -v
"""
from __future__ import annotations

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


def _write_hermes_config(hermes_home: Path, model_default: str, provider: str = "anthropic"):
    """Write a Hermes config.yaml with the given model.default."""
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "config.yaml").write_text(
        f"model:\n  default: {model_default}\n  provider: {provider}\n"
    )


def _write_profile(hermes_home: Path, profile_name: str, model_default: str,
                    provider: str = "anthropic"):
    """Write a *-daedalus profile config.yaml."""
    pdir = hermes_home / "profiles" / profile_name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "config.yaml").write_text(
        f"model:\n  default: {model_default}\n  provider: {provider}\n"
    )


def _read_profile(hermes_home: Path, profile_name: str) -> dict:
    p = hermes_home / "profiles" / profile_name / "config.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


class TestDispatcherProfileResyncIntegration:
    """Integration tests: fingerprint change triggers profile resync via run()."""

    def test_fingerprint_change_triggers_resync(self, tmp_path):
        """Tick 1 stores fingerprint A; tick 2 with fingerprint B triggers resync."""
        workdir = str(tmp_path)
        hh = tmp_path / "hermes"

        # Tick 1: initial config -> seeds resync fingerprint (no resync)
        _write_hermes_config(hh, "anthropic/claude-sonnet-4")
        _write_profile(hh, "developer-daedalus", "anthropic/claude-sonnet-4")
        _write_profile(hh, "validator-daedalus", "anthropic/claude-sonnet-4")

        resolved = _make_resolved(workdir, coding_agent="claude-code")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            disp.run(resolved, dry_run=False, provider=disp.providers.get_provider(resolved))

        # After tick 1: resync fingerprint seeded, no resync happened
        fp_after_tick1 = dispatch_state.get_config_fingerprint(workdir)
        resync_fp_after_tick1 = dispatch_state.get_resync_fingerprint(workdir)
        assert fp_after_tick1 is not None
        assert resync_fp_after_tick1 is not None
        assert resync_fp_after_tick1 == fp_after_tick1

        # Tick 2: change the global model -> fingerprint changes -> resync fires
        _write_hermes_config(hh, "openrouter/qwen3", "openrouter")
        # Profiles still have the old model, so resync will actually update them
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            disp.run(resolved, dry_run=False, provider=disp.providers.get_provider(resolved))

        # After tick 2: resync fingerprint updated, profiles synced
        fp_after_tick2 = dispatch_state.get_config_fingerprint(workdir)
        resync_fp_after_tick2 = dispatch_state.get_resync_fingerprint(workdir)
        assert fp_after_tick2 != fp_after_tick1  # fingerprint changed
        assert resync_fp_after_tick2 == fp_after_tick2  # resync ran

        # Profiles should now match the new global config
        dc = _read_profile(hh, "developer-daedalus")
        assert dc["model"]["default"] == "openrouter/qwen3"
        vc = _read_profile(hh, "validator-daedalus")
        assert vc["model"]["default"] == "openrouter/qwen3"

    def test_same_fingerprint_does_not_trigger_resync(self, tmp_path):
        """Two ticks with the same config do not trigger a second resync."""
        workdir = str(tmp_path)
        hh = tmp_path / "hermes"
        _write_hermes_config(hh, "anthropic/claude-sonnet-4")
        _write_profile(hh, "developer-daedalus", "anthropic/claude-sonnet-4")

        resolved = _make_resolved(workdir, coding_agent="claude-code")

        # Tick 1 — seeds resync fingerprint
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            disp.run(resolved, dry_run=False, provider=disp.providers.get_provider(resolved))
        resync_fp_1 = dispatch_state.get_resync_fingerprint(workdir)

        # Tick 2 — same config, no change
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            with mock.patch.object(disp, "_resync_profiles_to_model") as mock_resync:
                disp.run(resolved, dry_run=False, provider=disp.providers.get_provider(resolved))

        # _resync_profiles_to_model should NOT have been called (fingerprint unchanged)
        mock_resync.assert_not_called()
        resync_fp_2 = dispatch_state.get_resync_fingerprint(workdir)
        assert resync_fp_1 == resync_fp_2

    def test_dry_run_does_not_trigger_resync(self, tmp_path):
        """dry_run=True does not trigger resync (no side effects)."""
        workdir = str(tmp_path)
        hh = tmp_path / "hermes"
        _write_hermes_config(hh, "anthropic/claude-sonnet-4")
        _write_profile(hh, "developer-daedalus", "old-model")

        resolved = _make_resolved(workdir, coding_agent="claude-code")

        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            disp.run(resolved, dry_run=True, provider=disp.providers.get_provider(resolved))

        # No fingerprint stored in dry_run
        assert dispatch_state.get_config_fingerprint(workdir) is None
        assert dispatch_state.get_resync_fingerprint(workdir) is None

    def test_first_tick_seeds_resync_fingerprint_without_resync(self, tmp_path):
        """The very first tick seeds the resync fingerprint but doesn't resync."""
        workdir = str(tmp_path)
        hh = tmp_path / "hermes"
        _write_hermes_config(hh, "anthropic/claude-sonnet-4")
        _write_profile(hh, "developer-daedalus", "old-model")

        resolved = _make_resolved(workdir, coding_agent="claude-code")

        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            with mock.patch.object(disp, "_resync_profiles_to_model") as mock_resync:
                disp.run(resolved, dry_run=False, provider=disp.providers.get_provider(resolved))

        # First tick: resync fingerprint seeded, but _resync_profiles_to_model not called
        mock_resync.assert_not_called()
        fp = dispatch_state.get_config_fingerprint(workdir)
        assert dispatch_state.get_resync_fingerprint(workdir) == fp

        # Profile should still have the old model (no resync ran)
        dc = _read_profile(hh, "developer-daedalus")
        assert dc["model"]["default"] == "old-model"

    def test_rapid_successive_changes_deduped(self, tmp_path):
        """Simulate rapid ticks: change -> resync -> same config tick -> no resync."""
        workdir = str(tmp_path)
        hh = tmp_path / "hermes"

        # Initial state
        _write_hermes_config(hh, "anthropic/claude-sonnet-4")
        _write_profile(hh, "developer-daedalus", "anthropic/claude-sonnet-4")
        resolved = _make_resolved(workdir, coding_agent="claude-code")

        # Tick 1: seed
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            disp.run(resolved, dry_run=False, provider=disp.providers.get_provider(resolved))

        # Tick 2: change config -> triggers resync
        _write_hermes_config(hh, "openrouter/qwen3", "openrouter")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            disp.run(resolved, dry_run=False, provider=disp.providers.get_provider(resolved))

        # Tick 3: same config (rapid successive) -> NO resync
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            with mock.patch.object(disp, "_resync_profiles_to_model") as mock_resync:
                disp.run(resolved, dry_run=False, provider=disp.providers.get_provider(resolved))

        mock_resync.assert_not_called()

    def test_unrelated_project_not_affected(self, tmp_path):
        """Resync triggered for project A does not affect project B's state."""
        wd_a = str(tmp_path / "projectA")
        wd_b = str(tmp_path / "projectB")
        hh = tmp_path / "hermes"

        # Shared Hermes home with initial config
        _write_hermes_config(hh, "anthropic/claude-sonnet-4")
        _write_profile(hh, "developer-daedalus", "anthropic/claude-sonnet-4")

        os.makedirs(wd_a, exist_ok=True)
        os.makedirs(wd_b, exist_ok=True)

        resolved_a = _make_resolved(wd_a, coding_agent="claude-code")
        resolved_b = _make_resolved(wd_b, coding_agent="codex")

        # Tick 1 for both projects — seeds resync fingerprints
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            disp.run(resolved_a, dry_run=False, provider=disp.providers.get_provider(resolved_a))
            disp.run(resolved_b, dry_run=False, provider=disp.providers.get_provider(resolved_b))

        fp_a_1 = dispatch_state.get_config_fingerprint(wd_a)
        fp_b_1 = dispatch_state.get_config_fingerprint(wd_b)
        # They have different fingerprints (different coding_agent)
        assert fp_a_1 != fp_b_1

        # Tick 2: change model -> fingerprint changes for both
        _write_hermes_config(hh, "openrouter/qwen3", "openrouter")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            disp.run(resolved_a, dry_run=False, provider=disp.providers.get_provider(resolved_a))

        # Project A resynced
        assert dispatch_state.get_resync_fingerprint(wd_a) != fp_a_1

        # Project B's resync fingerprint is still from tick 1 (not yet ticked)
        assert dispatch_state.get_resync_fingerprint(wd_b) == fp_b_1

        # When B ticks, it also resyncs (fingerprint changed for B too)
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            disp.run(resolved_b, dry_run=False, provider=disp.providers.get_provider(resolved_b))

        assert dispatch_state.get_resync_fingerprint(wd_b) != fp_b_1

    def test_resync_logs_fingerprint_change(self, tmp_path, caplog):
        """The dispatcher logs the fingerprint change event with old/new values."""
        workdir = str(tmp_path)
        hh = tmp_path / "hermes"
        _write_hermes_config(hh, "anthropic/claude-sonnet-4")
        _write_profile(hh, "developer-daedalus", "anthropic/claude-sonnet-4")

        resolved = _make_resolved(workdir, coding_agent="claude-code")

        # Tick 1: seed
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            disp.run(resolved, dry_run=False, provider=disp.providers.get_provider(resolved))

        # Tick 2: change config
        _write_hermes_config(hh, "openrouter/qwen3", "openrouter")
        import logging
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            with caplog.at_level(logging.INFO, logger="daedalus.dispatch"):
                disp.run(resolved, dry_run=False, provider=disp.providers.get_provider(resolved))

        # Check that the fingerprint change was logged
        fp_change_logs = [r for r in caplog.records if "fingerprint changed" in r.getMessage()]
        assert len(fp_change_logs) >= 1
        # The log should mention the project name and "resync"
        msg = fp_change_logs[0].getMessage()
        assert "test-repo" in msg or "test/repo" in msg
        assert "resync" in msg.lower() or "triggering" in msg.lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
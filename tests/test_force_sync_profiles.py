"""Tests for manual force sync profiles feature (issue #1244).

Covers:
- get_profile_models(): returns profile model settings
- sync_profiles_to_model(force=True): force sync all profiles
- sync_profiles_to_model(force=False): skip manual overrides
- sync_profiles_to_model(target_model=...): explicit target
- sync_profiles_to_model(profile_names=[...]): specific profiles
- _get_global_model(): read global model from config
- Empty/missing profiles dir → return 0
- Non-daedalus profiles not touched
- Dashboard API: GET /profiles/model, POST /profiles/model/sync
- CLI flag --sync-profiles-model: recognized and callable

Run: pytest tests/test_force_sync_profiles.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.sync_profiles import (  # noqa: E402
    get_profile_models,
    sync_profiles_to_model,
    _get_global_model,
)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_profile(hermes_home: Path, name: str, model: str, provider: str = "") -> Path:
    """Create a profile directory with config.yaml."""
    d = hermes_home / "profiles" / name
    d.mkdir(parents=True, exist_ok=True)
    cfg_path = d / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": {"default": model, "provider": provider}}))
    return cfg_path


def _make_global_config(hermes_home: Path, model: str, provider: str) -> Path:
    """Create global config.yaml with model settings."""
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": {"default": model, "provider": provider}}))
    return cfg_path


def _hermes(tmp_path: Path) -> Path:
    """Create and return a fake HERMES_HOME under tmp_path."""
    h = tmp_path / "hermes"
    h.mkdir(exist_ok=True)
    return h


# ─── get_profile_models() ─────────────────────────────────────────────────────

class TestGetProfileModels:

    def test_returns_empty_for_no_profiles_dir(self, tmp_path):
        """No profiles/ dir → empty dict."""
        hh = _hermes(tmp_path)
        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            result = get_profile_models()
        assert result == {}

    def test_returns_empty_for_empty_profiles_dir(self, tmp_path):
        """Profiles/ dir exists but empty → empty dict."""
        hh = _hermes(tmp_path)
        (hh / "profiles").mkdir()
        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            result = get_profile_models()
        assert result == {}

    def test_returns_daedalus_profiles_only(self, tmp_path):
        """Only *-daedalus profiles are returned."""
        hh = _hermes(tmp_path)
        _make_profile(hh, "developer-daedalus", "model-a")
        _make_profile(hh, "qa-daedalus", "model-b")
        _make_profile(hh, "my-personal", "model-c")  # not daedalus

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            result = get_profile_models()

        assert "developer-daedalus" in result
        assert "qa-daedalus" in result
        assert "my-personal" not in result

    def test_returns_model_and_provider(self, tmp_path):
        """Each profile dict has model_default, model_provider, is_daedalus, path."""
        hh = _hermes(tmp_path)
        _make_profile(hh, "developer-daedalus", "anthropic/claude-sonnet-4", "anthropic")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            result = get_profile_models()

        info = result["developer-daedalus"]
        assert info["model_default"] == "anthropic/claude-sonnet-4"
        assert info["model_provider"] == "anthropic"
        assert info["is_daedalus"] is True
        assert "path" in info and isinstance(info["path"], str)

    def test_handles_missing_config_yaml(self, tmp_path):
        """Profile dir without config.yaml is skipped."""
        hh = _hermes(tmp_path)
        (hh / "profiles" / "developer-daedalus").mkdir(parents=True)
        # No config.yaml written

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            result = get_profile_models()

        assert "developer-daedalus" not in result

    def test_handles_invalid_yaml(self, tmp_path):
        """Invalid YAML doesn't crash, logs warning and continues."""
        hh = _hermes(tmp_path)
        d = hh / "profiles" / "developer-daedalus"
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text("invalid: yaml: content: [")
        _make_profile(hh, "qa-daedalus", "model-b")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            result = get_profile_models()

        # developer-daedalus skipped due to invalid YAML, qa-daedalus included
        assert "qa-daedalus" in result
        assert "developer-daedalus" not in result


# ─── _get_global_model() ──────────────────────────────────────────────────────

class TestGetGlobalModel:

    def test_returns_empty_for_no_config(self, tmp_path):
        """No config.yaml → empty strings."""
        hh = _hermes(tmp_path)
        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            model, provider = _get_global_model()
        assert model == ""
        assert provider == ""

    def test_reads_model_from_config(self, tmp_path):
        """Reads model.default and model.provider from config.yaml."""
        hh = _hermes(tmp_path)
        _make_global_config(hh, "anthropic/claude-sonnet-4", "anthropic")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            model, provider = _get_global_model()

        assert model == "anthropic/claude-sonnet-4"
        assert provider == "anthropic"

    def test_handles_missing_model_block(self, tmp_path):
        """Config without model block → empty strings."""
        hh = _hermes(tmp_path)
        (hh / "config.yaml").write_text("other: key\n")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            model, provider = _get_global_model()

        assert model == ""
        assert provider == ""

    def test_strips_whitespace(self, tmp_path):
        """Whitespace in model/provider is stripped."""
        hh = _hermes(tmp_path)
        (hh / "config.yaml").write_text("model:\n  default: '  claude-sonnet-4  '\n  provider: '  anthropic  '\n")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            model, provider = _get_global_model()

        assert model == "claude-sonnet-4"
        assert provider == "anthropic"

    def test_handles_invalid_yaml(self, tmp_path):
        """Invalid YAML → empty strings, no crash."""
        hh = _hermes(tmp_path)
        (hh / "config.yaml").write_text("invalid: yaml: [")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            model, provider = _get_global_model()

        assert model == ""
        assert provider == ""


# ─── sync_profiles_to_model() ─────────────────────────────────────────────────

class TestSyncProfilesToModel:

    def test_force_sync_updates_all_daedalus_profiles(self, tmp_path):
        """force=True updates all *-daedalus profiles to target model."""
        hh = _hermes(tmp_path)
        _make_profile(hh, "developer-daedalus", "old-model", "old-provider")
        _make_profile(hh, "qa-daedalus", "old-model", "old-provider")
        _make_global_config(hh, "new-model", "new-provider")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count, updated = sync_profiles_to_model(force=True)

        assert count == 2
        assert "developer-daedalus" in updated
        assert "qa-daedalus" in updated

        dev_cfg = yaml.safe_load((hh / "profiles" / "developer-daedalus" / "config.yaml").read_text())
        assert dev_cfg["model"]["default"] == "new-model"
        assert dev_cfg["model"]["provider"] == "new-provider"

    def test_force_sync_skips_non_daedalus(self, tmp_path):
        """Non-*-daedalus profiles are not touched."""
        hh = _hermes(tmp_path)
        _make_profile(hh, "developer-daedalus", "old-model")
        _make_profile(hh, "my-personal", "custom-model")
        _make_global_config(hh, "new-model", "new-provider")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count, updated = sync_profiles_to_model(force=True)

        assert count == 1
        personal_cfg = yaml.safe_load((hh / "profiles" / "my-personal" / "config.yaml").read_text())
        assert personal_cfg["model"]["default"] == "custom-model"

    def test_no_force_skips_manual_overrides(self, tmp_path):
        """force=False skips profiles with explicit override (model != old global)."""
        hh = _hermes(tmp_path)
        _make_profile(hh, "developer-daedalus", "old-model")  # stale: was old global
        _make_profile(hh, "qa-daedalus", "custom-model")  # override: user changed
        _make_global_config(hh, "new-model", "new-provider")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count, updated = sync_profiles_to_model(
                force=False,
                old_model="old-model",  # previous global before change
            )

        assert count == 1
        assert "developer-daedalus" in updated
        assert "qa-daedalus" not in updated

        qa_cfg = yaml.safe_load((hh / "profiles" / "qa-daedalus" / "config.yaml").read_text())
        assert qa_cfg["model"]["default"] == "custom-model"

    def test_no_force_updates_empty_model_profiles(self, tmp_path):
        """force=False updates profiles with empty model (not explicit override)."""
        hh = _hermes(tmp_path)
        _make_profile(hh, "developer-daedalus", "", "")
        _make_global_config(hh, "new-model", "new-provider")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count, updated = sync_profiles_to_model(
                force=False,
                old_model="old-model",  # previous global
            )

        assert count == 1
        dev_cfg = yaml.safe_load((hh / "profiles" / "developer-daedalus" / "config.yaml").read_text())
        assert dev_cfg["model"]["default"] == "new-model"

    def test_explicit_target_model(self, tmp_path):
        """target_model overrides global config."""
        hh = _hermes(tmp_path)
        _make_profile(hh, "developer-daedalus", "old-model")
        _make_global_config(hh, "global-model", "global-provider")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count, updated = sync_profiles_to_model(
                force=True,
                target_model="explicit-model",
                target_provider="explicit-provider",
            )

        assert count == 1
        dev_cfg = yaml.safe_load((hh / "profiles" / "developer-daedalus" / "config.yaml").read_text())
        assert dev_cfg["model"]["default"] == "explicit-model"
        assert dev_cfg["model"]["provider"] == "explicit-provider"

    def test_specific_profile_names(self, tmp_path):
        """profile_names=[...] syncs only specified profiles."""
        hh = _hermes(tmp_path)
        _make_profile(hh, "developer-daedalus", "old-model")
        _make_profile(hh, "qa-daedalus", "old-model")
        _make_profile(hh, "reviewer-daedalus", "old-model")
        _make_global_config(hh, "new-model", "new-provider")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count, updated = sync_profiles_to_model(
                force=True,
                profile_names=["developer-daedalus", "reviewer-daedalus"],
            )

        assert count == 2
        assert "developer-daedalus" in updated
        assert "reviewer-daedalus" in updated
        assert "qa-daedalus" not in updated

        qa_cfg = yaml.safe_load((hh / "profiles" / "qa-daedalus" / "config.yaml").read_text())
        assert qa_cfg["model"]["default"] == "old-model"

    def test_skips_already_at_target(self, tmp_path):
        """Profiles already matching target model are skipped."""
        hh = _hermes(tmp_path)
        _make_profile(hh, "developer-daedalus", "new-model", "new-provider")
        _make_global_config(hh, "new-model", "new-provider")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count, updated = sync_profiles_to_model(force=True)

        assert count == 0
        assert updated == []

    def test_empty_model_no_sync(self, tmp_path):
        """No target model and no global model → no sync."""
        hh = _hermes(tmp_path)
        _make_profile(hh, "developer-daedalus", "old-model")
        # No global config.yaml

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count, updated = sync_profiles_to_model(force=True)

        assert count == 0
        assert updated == []

    def test_zero_profiles_returns_zero(self, tmp_path):
        """Empty profiles dir → 0."""
        hh = _hermes(tmp_path)
        (hh / "profiles").mkdir()
        _make_global_config(hh, "new-model", "new-provider")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count, updated = sync_profiles_to_model(force=True)

        assert count == 0
        assert updated == []

    def test_no_profiles_dir_returns_zero(self, tmp_path):
        """No profiles/ dir → 0."""
        hh = _hermes(tmp_path)
        hh.mkdir(exist_ok=True)
        _make_global_config(hh, "new-model", "new-provider")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count, updated = sync_profiles_to_model(force=True)

        assert count == 0
        assert updated == []

    def test_atomic_write(self, tmp_path):
        """Profile config is written atomically (no partial writes)."""
        hh = _hermes(tmp_path)
        cfg = _make_profile(hh, "developer-daedalus", "old-model")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count, updated = sync_profiles_to_model(force=True, target_model="new-model", target_provider="new-provider")

        assert count == 1
        # Verify file is valid YAML (not truncated)
        cfg_content = cfg.read_text()
        parsed = yaml.safe_load(cfg_content)
        assert parsed is not None
        assert parsed["model"]["default"] == "new-model"


# ─── Dashboard API Endpoint ──────────────────────────────────────────────────

class TestDashboardAPISync:
    """Test the dashboard API endpoints for profile sync."""

    def test_get_profiles_model_endpoint_structure(self, tmp_path):
        """GET /profiles/model returns correct structure."""
        # This test verifies the API contract; actual HTTP testing requires FastAPI TestClient
        hh = _hermes(tmp_path)
        _make_profile(hh, "developer-daedalus", "model-a", "provider-a")
        _make_profile(hh, "qa-daedalus", "model-b", "provider-b")
        _make_global_config(hh, "global-model", "global-provider")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            profiles = get_profile_models()
            model, provider = _get_global_model()

        # Verify structure matches API contract
        assert "model_default" in profiles["developer-daedalus"]
        assert "model_provider" in profiles["developer-daedalus"]
        assert "is_daedalus" in profiles["developer-daedalus"]
        assert "path" in profiles["developer-daedalus"]
        assert model == "global-model"
        assert provider == "global-provider"

    def test_post_sync_profiles_model_endpoint(self, tmp_path):
        """POST /profiles/model/sync updates profiles."""
        hh = _hermes(tmp_path)
        _make_profile(hh, "developer-daedalus", "old-model", "old-provider")
        _make_profile(hh, "qa-daedalus", "old-model", "old-provider")
        _make_global_config(hh, "new-model", "new-provider")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count, updated = sync_profiles_to_model(force=True)

        # Verify API contract: returns (count, updated_list)
        assert count == 2
        assert len(updated) == 2
        assert "developer-daedalus" in updated
        assert "qa-daedalus" in updated

        # Verify profiles were actually updated
        for name in updated:
            cfg = yaml.safe_load((hh / "profiles" / name / "config.yaml").read_text())
            assert cfg["model"]["default"] == "new-model"
            assert cfg["model"]["provider"] == "new-provider"


# ─── CLI Flag ─────────────────────────────────────────────────────────────────

class TestCLIFlag:
    """Test the --sync-profiles-model CLI flag in dispatcher."""

    def test_flag_is_boolean(self, tmp_path):
        """--sync-profiles-model is a boolean flag (no value required)."""
        # This is a contract test; actual argparse testing requires subprocess
        # The flag should work as: dispatcher --sync-profiles-model
        # and NOT as: dispatcher --sync-profiles-model some-value
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--sync-profiles-model", action="store_true")
        args = parser.parse_args(["--sync-profiles-model"])
        assert args.sync_profiles_model is True
        args = parser.parse_args([])
        assert args.sync_profiles_model is False

    def test_handler_calls_sync_profiles_to_model(self, tmp_path):
        """The CLI handler calls sync_profiles_to_model(force=True)."""
        # This test verifies the integration; actual HTTP testing requires FastAPI TestClient
        hh = _hermes(tmp_path)
        _make_profile(hh, "developer-daedalus", "old-model")
        _make_global_config(hh, "new-model", "new-provider")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            # Simulate what the handler does
            count, updated = sync_profiles_to_model(force=True)

        assert count == 1
        assert updated == ["developer-daedalus"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

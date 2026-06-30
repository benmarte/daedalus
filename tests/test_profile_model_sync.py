"""Unit tests for _on_kanban_task_claimed — agent profile model sync.

Covers three scenarios per the task spec:
1. model change in global config (coding_agent or model.default) triggers resync
2. explicit user override (_daedalus_model_override: true) is preserved and not overwritten
3. no-op when the model remains unchanged

Run: pytest tests/test_profile_model_sync.py -v
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_package():
    """Load the daedalus package by path (dir has a hyphen so normal import fails)."""
    spec = importlib.util.spec_from_file_location(
        "daedalus_plugin_sync", str(ROOT / "__init__.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def _write_config(path: Path, cfg: dict) -> None:
    """Write a YAML config dict to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False))


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Create a fake HERMES_HOME with global config and profile dirs."""
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def mod():
    """Load the plugin module fresh."""
    return _load_package()


def _setup_profile(home: Path, assignee: str, profile_cfg: dict | None = None) -> Path:
    """Create a profile dir and write its config.yaml; return the config path."""
    profile_dir = home / "profiles" / assignee
    profile_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = profile_dir / "config.yaml"
    if profile_cfg is not None:
        _write_config(cfg_path, profile_cfg)
    return cfg_path


def _setup_global(home: Path, global_cfg: dict) -> Path:
    """Write the global config.yaml; return its path."""
    cfg_path = home / "config.yaml"
    _write_config(cfg_path, global_cfg)
    return cfg_path


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 1: model change triggers resync
# ─────────────────────────────────────────────────────────────────────────────


class TestModelChangeTriggersResync:
    """When the global model/providers change, the profile must be updated."""

    def test_model_default_change_triggers_resync(self, mod, hermes_home):
        """Changing model.default in global config triggers a resync into profile."""
        _setup_global(hermes_home, {
            "model": {"default": "anthropic/claude-sonnet-4", "provider": "anthropic"},
            "providers": {"anthropic": {"api_key": "key1"}},
        })
        profile_cfg = {
            "model": {"default": "old-model", "provider": "old-provider"},
            "providers": {"old-provider": {"api_key": "old-key"}},
        }
        cfg_path = _setup_profile(hermes_home, "developer-daedalus", profile_cfg)

        mod._on_kanban_task_claimed(
            task_id="t_test", board="default", assignee="developer-daedalus", run_id=1
        )

        result = yaml.safe_load(cfg_path.read_text())
        assert result["model"]["default"] == "anthropic/claude-sonnet-4"
        assert result["model"]["provider"] == "anthropic"
        assert result["providers"] == {"anthropic": {"api_key": "key1"}}

    def test_coding_agent_change_triggers_resync(self, mod, hermes_home):
        """Changing providers/fallback_providers in global config triggers resync."""
        _setup_global(hermes_home, {
            "model": {"default": "anthropic/claude-sonnet-4", "provider": "anthropic"},
            "providers": {"anthropic": {"api_key": "new-key"}},
            "fallback_providers": ["fallback-1"],
            "custom_providers": {"custom-1": {"api_key": "ck1"}},
        })
        # Profile has old provider config — should be fully overwritten
        profile_cfg = {
            "model": {"default": "anthropic/claude-sonnet-4", "provider": "anthropic"},
            "providers": {"anthropic": {"api_key": "old-key"}},
        }
        cfg_path = _setup_profile(hermes_home, "developer-daedalus", profile_cfg)

        mod._on_kanban_task_claimed(
            task_id="t_test", board="default", assignee="developer-daedalus", run_id=1
        )

        result = yaml.safe_load(cfg_path.read_text())
        assert result["providers"] == {"anthropic": {"api_key": "new-key"}}
        assert result["fallback_providers"] == ["fallback-1"]
        assert result["custom_providers"] == {"custom-1": {"api_key": "ck1"}}

    def test_new_key_added_to_global_is_synced(self, mod, hermes_home):
        """A sync key present in global but absent from profile is added."""
        _setup_global(hermes_home, {
            "model": {"default": "anthropic/claude-sonnet-4", "provider": "anthropic"},
            "fallback_providers": ["fb1", "fb2"],
        })
        # Profile has no fallback_providers key at all
        profile_cfg = {
            "model": {"default": "anthropic/claude-sonnet-4", "provider": "anthropic"},
        }
        cfg_path = _setup_profile(hermes_home, "developer-daedalus", profile_cfg)

        mod._on_kanban_task_claimed(
            task_id="t_test", board="default", assignee="developer-daedalus", run_id=1
        )

        result = yaml.safe_load(cfg_path.read_text())
        assert result["fallback_providers"] == ["fb1", "fb2"]

    def test_removed_global_key_is_removed_from_profile(self, mod, hermes_home):
        """A sync key absent from global (None) is removed from profile."""
        # Global has no fallback_providers
        _setup_global(hermes_home, {
            "model": {"default": "anthropic/claude-sonnet-4", "provider": "anthropic"},
            "providers": {"anthropic": {"api_key": "k1"}},
        })
        # Profile has fallback_providers that should be removed
        profile_cfg = {
            "model": {"default": "anthropic/claude-sonnet-4", "provider": "anthropic"},
            "providers": {"anthropic": {"api_key": "k1"}},
            "fallback_providers": ["should-be-removed"],
            "custom_providers": {"should-remove": {"api_key": "x"}},
        }
        cfg_path = _setup_profile(hermes_home, "developer-daedalus", profile_cfg)

        mod._on_kanban_task_claimed(
            task_id="t_test", board="default", assignee="developer-daedalus", run_id=1
        )

        result = yaml.safe_load(cfg_path.read_text())
        assert "fallback_providers" not in result
        assert "custom_providers" not in result

    def test_skips_non_daedalus_assignee(self, mod, hermes_home):
        """A non-daedalus assignee is ignored — no files touched."""
        _setup_global(hermes_home, {
            "model": {"default": "anthropic/claude-sonnet-4", "provider": "anthropic"},
        })
        profile_cfg = {
            "model": {"default": "old-model", "provider": "old-provider"},
        }
        cfg_path = _setup_profile(hermes_home, "some-other-profile", profile_cfg)

        mod._on_kanban_task_claimed(
            task_id="t_test", board="default", assignee="some-other-profile", run_id=1
        )

        # Profile config must be unchanged
        result = yaml.safe_load(cfg_path.read_text())
        assert result["model"]["default"] == "old-model"

    def test_missing_global_config_is_noop(self, mod, hermes_home):
        """No global config.yaml — hook silently returns, profile untouched."""
        # No global config written
        profile_cfg = {
            "model": {"default": "keep-me", "provider": "keep"},
        }
        cfg_path = _setup_profile(hermes_home, "developer-daedalus", profile_cfg)

        # Must not raise
        mod._on_kanban_task_claimed(
            task_id="t_test", board="default", assignee="developer-daedalus", run_id=1
        )

        result = yaml.safe_load(cfg_path.read_text())
        assert result["model"]["default"] == "keep-me"

    def test_missing_profile_config_is_noop(self, mod, hermes_home):
        """No profile config.yaml — hook silently returns, no file created."""
        _setup_global(hermes_home, {
            "model": {"default": "anthropic/claude-sonnet-4", "provider": "anthropic"},
        })
        # Create profile dir but no config.yaml
        profile_dir = hermes_home / "profiles" / "developer-daedalus"
        profile_dir.mkdir(parents=True)

        mod._on_kanban_task_claimed(
            task_id="t_test", board="default", assignee="developer-daedalus", run_id=1
        )

        # No config.yaml should have been created
        assert not (profile_dir / "config.yaml").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 2: explicit user override is preserved
# ─────────────────────────────────────────────────────────────────────────────


class TestUserOverridePreserved:
    """When _daedalus_model_override: true, the profile keeps its own model."""

    def test_override_prevents_overwrite(self, mod, hermes_home):
        """Profile with _daedalus_model_override: true is not synced."""
        _setup_global(hermes_home, {
            "model": {"default": "anthropic/claude-sonnet-4", "provider": "anthropic"},
            "providers": {"anthropic": {"api_key": "global-key"}},
            "fallback_providers": ["global-fb"],
            "custom_providers": {"global-custom": {"api_key": "gc"}},
        })
        # Profile has a completely different model and the override flag
        profile_cfg = {
            "_daedalus_model_override": True,
            "model": {"default": "local-model", "provider": "local-provider"},
            "providers": {"local-provider": {"api_key": "local-key"}},
            "fallback_providers": ["local-fb"],
            "custom_providers": {"local-custom": {"api_key": "lc"}},
        }
        cfg_path = _setup_profile(hermes_home, "developer-daedalus", profile_cfg)

        mod._on_kanban_task_claimed(
            task_id="t_test", board="default", assignee="developer-daedalus", run_id=1
        )

        result = yaml.safe_load(cfg_path.read_text())
        # Everything must be unchanged
        assert result["model"]["default"] == "local-model"
        assert result["model"]["provider"] == "local-provider"
        assert result["providers"] == {"local-provider": {"api_key": "local-key"}}
        assert result["fallback_providers"] == ["local-fb"]
        assert result["custom_providers"] == {"local-custom": {"api_key": "lc"}}
        assert result["_daedalus_model_override"] is True

    def test_override_false_allows_sync(self, mod, hermes_home):
        """_daedalus_model_override: false (or absent) allows normal sync."""
        _setup_global(hermes_home, {
            "model": {"default": "new-model", "provider": "new-provider"},
        })
        profile_cfg = {
            "_daedalus_model_override": False,
            "model": {"default": "old-model", "provider": "old-provider"},
        }
        cfg_path = _setup_profile(hermes_home, "developer-daedalus", profile_cfg)

        mod._on_kanban_task_claimed(
            task_id="t_test", board="default", assignee="developer-daedalus", run_id=1
        )

        result = yaml.safe_load(cfg_path.read_text())
        assert result["model"]["default"] == "new-model"
        assert result["model"]["provider"] == "new-provider"
        # Override flag remains False (not touched)
        assert result["_daedalus_model_override"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 3: no-op when model is unchanged
# ─────────────────────────────────────────────────────────────────────────────


class TestNoOpWhenUnchanged:
    """When the model config is already in sync, no write should occur."""

    def test_no_write_when_already_in_sync(self, mod, hermes_home):
        """Identical global and profile configs → no file modification."""
        global_cfg = {
            "model": {"default": "anthropic/claude-sonnet-4", "provider": "anthropic"},
            "providers": {"anthropic": {"api_key": "k1"}},
            "fallback_providers": ["fb1"],
            "custom_providers": {"c1": {"api_key": "ck1"}},
        }
        _setup_global(hermes_home, global_cfg)
        # Profile has identical sync keys
        profile_cfg = dict(global_cfg)
        cfg_path = _setup_profile(hermes_home, "developer-daedalus", profile_cfg)

        # Record mtime before
        mtime_before = cfg_path.stat().st_mtime_ns

        mod._on_kanban_task_claimed(
            task_id="t_test", board="default", assignee="developer-daedalus", run_id=1
        )

        # mtime must not change (no write)
        mtime_after = cfg_path.stat().st_mtime_ns
        assert mtime_after == mtime_before, "file was rewritten despite no change"

    def test_no_write_when_only_non_sync_keys_differ(self, mod, hermes_home):
        """Non-sync keys (outside sync_keys) differ — file should not be rewritten."""
        _setup_global(hermes_home, {
            "model": {"default": "anthropic/claude-sonnet-4", "provider": "anthropic"},
            "providers": {"anthropic": {"api_key": "k1"}},
        })
        # Profile has same sync keys but a different non-sync key
        profile_cfg = {
            "model": {"default": "anthropic/claude-sonnet-4", "provider": "anthropic"},
            "providers": {"anthropic": {"api_key": "k1"}},
            "other_key": "profile-specific-value",
        }
        cfg_path = _setup_profile(hermes_home, "developer-daedalus", profile_cfg)

        mtime_before = cfg_path.stat().st_mtime_ns

        mod._on_kanban_task_claimed(
            task_id="t_test", board="default", assignee="developer-daedalus", run_id=1
        )

        mtime_after = cfg_path.stat().st_mtime_ns
        assert mtime_after == mtime_before, "file rewritten despite only non-sync keys differing"

        # Non-sync key must be preserved
        result = yaml.safe_load(cfg_path.read_text())
        assert result["other_key"] == "profile-specific-value"

    def test_preserves_non_sync_keys_on_resync(self, mod, hermes_home):
        """When sync keys change, non-sync keys in the profile must be preserved."""
        _setup_global(hermes_home, {
            "model": {"default": "new-model", "provider": "new-provider"},
        })
        profile_cfg = {
            "model": {"default": "old-model", "provider": "old-provider"},
            "other_key": "preserved-value",
            "another_key": 42,
        }
        cfg_path = _setup_profile(hermes_home, "developer-daedalus", profile_cfg)

        mod._on_kanban_task_claimed(
            task_id="t_test", board="default", assignee="developer-daedalus", run_id=1
        )

        result = yaml.safe_load(cfg_path.read_text())
        assert result["model"]["default"] == "new-model"
        assert result["other_key"] == "preserved-value"
        assert result["another_key"] == 42


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Edge cases for robustness."""

    def test_none_assignee_is_noop(self, mod, hermes_home):
        """None assignee must not raise."""
        _setup_global(hermes_home, {
            "model": {"default": "m", "provider": "p"},
        })
        # Must not raise
        mod._on_kanban_task_claimed(
            task_id="t_test", board="default", assignee=None, run_id=1
        )

    def test_empty_assignee_is_noop(self, mod, hermes_home):
        """Empty string assignee must not raise."""
        _setup_global(hermes_home, {
            "model": {"default": "m", "provider": "p"},
        })
        mod._on_kanban_task_claimed(
            task_id="t_test", board="default", assignee="", run_id=1
        )

    def test_never_raises_on_corrupt_profile_config(self, mod, hermes_home):
        """Corrupt profile config.yaml must not crash the hook."""
        _setup_global(hermes_home, {
            "model": {"default": "m", "provider": "p"},
        })
        profile_dir = hermes_home / "profiles" / "developer-daedalus"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("{{{{invalid yaml")

        # Must not raise
        mod._on_kanban_task_claimed(
            task_id="t_test", board="default", assignee="developer-daedalus", run_id=1
        )

    def test_never_raises_on_corrupt_global_config(self, mod, hermes_home):
        """Corrupt global config.yaml must not crash the hook."""
        (hermes_home / "config.yaml").write_text("{{{{invalid yaml")
        _setup_profile(hermes_home, "developer-daedalus", {
            "model": {"default": "keep", "provider": "keep"},
        })

        # Must not raise
        mod._on_kanban_task_claimed(
            task_id="t_test", board="default", assignee="developer-daedalus", run_id=1
        )
"""Unit tests for the on_model_change hook consumer (issue #1368, ADR-007).

Covers the eager, all-profile model sync that Hermes fires the instant the
operator switches models:
1. every *-daedalus profile is resynced on fire (not just the one about to run)
2. per-profile _daedalus_model_override locks are respected
3. non-daedalus profiles are left untouched
4. the hook is registered forward-compatibly and never raises

Run: pytest tests/test_on_model_change_hook.py -v
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
        "daedalus_plugin_model_change", str(ROOT / "__init__.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False))


@pytest.fixture
def mod():
    return _load_package()


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _setup_profile(home: Path, name: str, cfg: dict | None = None) -> Path:
    cfg_path = home / "profiles" / name / "config.yaml"
    if cfg is not None:
        _write_config(cfg_path, cfg)
    else:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
    return cfg_path


def _setup_global(home: Path, cfg: dict) -> Path:
    cfg_path = home / "config.yaml"
    _write_config(cfg_path, cfg)
    return cfg_path


def _read_model(cfg_path: Path) -> str:
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    return (cfg.get("model") or {}).get("default", "")


# ─────────────────────────────────────────────────────────────────────────────
# _iter_daedalus_profiles
# ─────────────────────────────────────────────────────────────────────────────


class TestIterDaedalusProfiles:
    def test_lists_only_daedalus_profiles_sorted(self, mod, hermes_home):
        _setup_profile(hermes_home, "developer-daedalus", {})
        _setup_profile(hermes_home, "qa-daedalus", {})
        _setup_profile(hermes_home, "some-other-profile", {})
        names = mod._iter_daedalus_profiles(str(hermes_home))
        assert names == ["developer-daedalus", "qa-daedalus"]

    def test_missing_profiles_dir_returns_empty(self, mod, hermes_home):
        assert mod._iter_daedalus_profiles(str(hermes_home)) == []


# ─────────────────────────────────────────────────────────────────────────────
# _on_model_change — eager all-profile resync
# ─────────────────────────────────────────────────────────────────────────────


class TestOnModelChangeResyncsAllProfiles:
    def test_resyncs_every_daedalus_profile(self, mod, hermes_home):
        """The hook updates ALL profiles, including ones that never get claimed."""
        _setup_global(hermes_home, {"model": {"default": "claude-opus-4-8", "provider": "anthropic"}})
        dev = _setup_profile(hermes_home, "developer-daedalus", {"model": {"default": "old-model"}})
        qa = _setup_profile(hermes_home, "qa-daedalus", {"model": {"default": "old-model"}})

        mod._on_model_change(
            old_model="old-model",
            new_model="claude-opus-4-8",
            old_provider="anthropic",
            new_provider="anthropic",
            source="cli",
        )

        assert _read_model(dev) == "claude-opus-4-8"
        assert _read_model(qa) == "claude-opus-4-8"

    def test_respects_per_profile_override(self, mod, hermes_home):
        """A locked profile (_daedalus_model_override) is left untouched."""
        _setup_global(hermes_home, {"model": {"default": "new-model"}})
        locked = _setup_profile(
            hermes_home, "reviewer-daedalus",
            {"_daedalus_model_override": True, "model": {"default": "pinned-model"}},
        )
        free = _setup_profile(hermes_home, "developer-daedalus", {"model": {"default": "old"}})

        mod._on_model_change(old_model="old", new_model="new-model", source="web")

        assert _read_model(locked) == "pinned-model"
        assert _read_model(free) == "new-model"

    def test_leaves_non_daedalus_profiles_untouched(self, mod, hermes_home):
        _setup_global(hermes_home, {"model": {"default": "new-model"}})
        other = _setup_profile(hermes_home, "not-ours", {"model": {"default": "keep-me"}})

        mod._on_model_change(old_model="old", new_model="new-model", source="config_set")

        assert _read_model(other) == "keep-me"

    def test_no_profiles_is_noop(self, mod, hermes_home):
        _setup_global(hermes_home, {"model": {"default": "new-model"}})
        # No profiles dir at all — must not raise.
        mod._on_model_change(old_model="old", new_model="new-model", source="cli")

    def test_never_raises_on_corrupt_profile(self, mod, hermes_home):
        _setup_global(hermes_home, {"model": {"default": "new-model"}})
        bad = _setup_profile(hermes_home, "developer-daedalus")
        bad.write_text("{{ not: valid: yaml")
        # Must not propagate the parse error.
        mod._on_model_change(old_model="old", new_model="new-model", source="cli")

    def test_accepts_only_kwargs_and_extra_kwargs(self, mod, hermes_home):
        """Hook is observer-only and tolerates unexpected/extra kwargs."""
        _setup_global(hermes_home, {"model": {"default": "m"}})
        _setup_profile(hermes_home, "developer-daedalus", {"model": {"default": "old"}})
        # Extra kwargs (e.g. telemetry_schema_version) must be swallowed.
        ret = mod._on_model_change(
            old_model="old", new_model="m", old_provider=None, new_provider=None,
            source="cli", telemetry_schema_version=3,
        )
        assert ret is None  # observer-only


# ─────────────────────────────────────────────────────────────────────────────
# registration
# ─────────────────────────────────────────────────────────────────────────────


class TestRegistration:
    def test_registers_on_model_change_hook(self, mod, hermes_home):
        registered: dict[str, object] = {}

        class Ctx:
            def register_auxiliary_task(self, **kwargs):
                pass

            def register_hook(self, name, cb):
                registered[name] = cb

        mod.register(Ctx())
        assert "on_model_change" in registered
        assert registered["on_model_change"] is mod._on_model_change
        # The pre-existing hooks are still wired.
        assert "kanban_task_claimed" in registered
        assert "on_session_end" in registered

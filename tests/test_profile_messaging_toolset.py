"""Tests for issue #1123 AC4 — messaging toolset removal from daedalus profile configs.

Hermes emits "Warning: Unknown toolsets: messaging" because the messaging toolset is
listed in platform_toolsets (claude_ai, slack) of auto-generated profile config.yaml
files. Daedalus agents don't need it. The _on_kanban_task_claimed hook must strip it.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_package():
    spec = importlib.util.spec_from_file_location(
        "daedalus_plugin_msg", str(ROOT / "__init__.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def mod(hermes_home):
    return _load_package()


def _write_cfg(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False))


def _setup(home: Path, assignee: str, profile_cfg: dict) -> Path:
    global_path = home / "config.yaml"
    _write_cfg(global_path, {"model": {"default": "m", "provider": "p"}})
    profile_path = home / "profiles" / assignee / "config.yaml"
    _write_cfg(profile_path, profile_cfg)
    return profile_path


# ── messaging in platform_toolsets ───────────────────────────────────────────


def test_messaging_removed_from_platform_toolsets_claude_ai(mod, hermes_home):
    """messaging must be stripped from platform_toolsets.claude_ai on task claim."""
    profile_cfg = {
        "model": {"default": "old", "provider": "p"},
        "platform_toolsets": {
            "claude_ai": ["browser", "messaging", "file"],
            "slack": ["browser", "messaging"],
        },
    }
    cfg_path = _setup(hermes_home, "developer-daedalus", profile_cfg)

    mod._on_kanban_task_claimed(
        task_id="t1", board="default", assignee="developer-daedalus", run_id=1
    )

    result = yaml.safe_load(cfg_path.read_text())
    assert "messaging" not in result["platform_toolsets"]["claude_ai"]
    assert "messaging" not in result["platform_toolsets"]["slack"]
    assert "browser" in result["platform_toolsets"]["claude_ai"]
    assert "file" in result["platform_toolsets"]["claude_ai"]


def test_messaging_removed_from_top_level_toolsets(mod, hermes_home):
    """messaging must be stripped from the top-level toolsets list if present."""
    profile_cfg = {
        "model": {"default": "old", "provider": "p"},
        "toolsets": ["hermes-cli", "messaging", "skills"],
    }
    cfg_path = _setup(hermes_home, "reviewer-daedalus", profile_cfg)

    mod._on_kanban_task_claimed(
        task_id="t2", board="default", assignee="reviewer-daedalus", run_id=1
    )

    result = yaml.safe_load(cfg_path.read_text())
    assert "messaging" not in result["toolsets"]
    assert "hermes-cli" in result["toolsets"]
    assert "skills" in result["toolsets"]


def test_no_messaging_profile_unchanged_except_model(mod, hermes_home):
    """Profile without messaging is not written if only model is in sync."""
    profile_cfg = {
        "model": {"default": "m", "provider": "p"},
        "platform_toolsets": {
            "claude_ai": ["browser", "file"],
        },
    }
    cfg_path = _setup(hermes_home, "qa-daedalus", profile_cfg)

    mod._on_kanban_task_claimed(
        task_id="t3", board="default", assignee="qa-daedalus", run_id=1
    )

    result = yaml.safe_load(cfg_path.read_text())
    # messaging was never there — toolsets should be unchanged
    assert "messaging" not in result["platform_toolsets"].get("claude_ai", [])


def test_non_daedalus_profile_not_touched(mod, hermes_home):
    """_on_kanban_task_claimed must skip profiles that don't end in -daedalus."""
    profile_cfg = {
        "model": {"default": "x", "provider": "p"},
        "toolsets": ["messaging"],
    }
    cfg_path = _setup(hermes_home, "my-custom-profile", profile_cfg)

    mod._on_kanban_task_claimed(
        task_id="t4", board="default", assignee="my-custom-profile", run_id=1
    )

    # Must not have been modified
    result = yaml.safe_load(cfg_path.read_text())
    assert "messaging" in result["toolsets"]


def test_messaging_removed_from_all_platform_toolset_entries(mod, hermes_home):
    """messaging must be removed from every platform entry, not just claude_ai."""
    profile_cfg = {
        "model": {"default": "old", "provider": "p"},
        "platform_toolsets": {
            "claude_ai": ["messaging", "browser"],
            "slack": ["messaging", "file"],
            "discord": ["messaging"],
            "signal": ["hermes-signal"],
        },
    }
    cfg_path = _setup(hermes_home, "validator-daedalus", profile_cfg)

    mod._on_kanban_task_claimed(
        task_id="t5", board="default", assignee="validator-daedalus", run_id=1
    )

    result = yaml.safe_load(cfg_path.read_text())
    pts = result["platform_toolsets"]
    for platform, ts_list in pts.items():
        if isinstance(ts_list, list):
            assert "messaging" not in ts_list, (
                f"messaging not removed from platform_toolsets.{platform}"
            )

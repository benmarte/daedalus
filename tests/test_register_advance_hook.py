"""Regression tests for scripts/register_advance_hook.py (issue #962).

The daedalus pipeline only advances in near-real-time when each Hermes profile's
config.yaml lists daedalus-advance.sh under hooks.on_session_end. provision_roster.sh
never wrote that block, so planner-daedalus (and other roles) silently never
self-advanced — the pipeline stalled up to 60 min until the hourly cron tick.

These tests pin the mutation helper's contract: it adds the hook when absent, is
idempotent (exactly one entry on re-runs, matched by basename), and is
non-destructive (existing hooks/config survive).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    """Load scripts/register_advance_hook.py as a standalone module."""
    path = _REPO_ROOT / "scripts" / "register_advance_hook.py"
    spec = importlib.util.spec_from_file_location("register_advance_hook", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["register_advance_hook"] = mod
    spec.loader.exec_module(mod)
    return mod


rah = _load_module()

_HOOK = "/Users/dev/.hermes/agent-hooks/daedalus-advance.sh"


class TestRegisterAdvanceHook:
    def test_added_when_absent(self):
        """A profile with no hooks section gains the advance hook entry."""
        cfg: dict = {}
        rah.register_advance_hook(cfg, _HOOK)
        entries = cfg["hooks"]["on_session_end"]
        assert entries == [{"command": _HOOK, "timeout": 90}]
        assert cfg["hooks_auto_accept"] is True

    def test_idempotent_no_duplicates(self):
        """Re-applying never duplicates the daedalus-advance.sh command entry."""
        cfg: dict = {}
        rah.register_advance_hook(cfg, _HOOK)
        rah.register_advance_hook(cfg, _HOOK)
        rah.register_advance_hook(cfg, _HOOK)
        advance_entries = [
            e
            for e in cfg["hooks"]["on_session_end"]
            if str(e.get("command", "")).endswith("daedalus-advance.sh")
        ]
        assert len(advance_entries) == 1

    def test_idempotent_across_differing_home_prefix(self):
        """An existing entry under a different home prefix is matched (no duplicate)."""
        cfg = {
            "hooks": {
                "on_session_end": [
                    {
                        "command": "/other/home/.hermes/agent-hooks/daedalus-advance.sh",
                        "timeout": 90,
                    }
                ]
            }
        }
        rah.register_advance_hook(cfg, _HOOK)
        advance_entries = [
            e
            for e in cfg["hooks"]["on_session_end"]
            if str(e.get("command", "")).endswith("daedalus-advance.sh")
        ]
        assert len(advance_entries) == 1, "matched by basename — must not re-add"

    def test_preserves_unrelated_config(self):
        """Unrelated top-level config keys survive the mutation."""
        cfg = {
            "model": "claude-opus",
            "terminal": {"env_passthrough": ["GITHUB_TOKEN"]},
        }
        rah.register_advance_hook(cfg, _HOOK)
        assert cfg["model"] == "claude-opus"
        assert cfg["terminal"] == {"env_passthrough": ["GITHUB_TOKEN"]}

    def test_preserves_existing_hooks_keys(self):
        """Other hooks.* keys and other on_session_end commands are not clobbered."""
        cfg = {
            "hooks": {
                "on_session_start": [{"command": "/x/start.sh", "timeout": 10}],
                "on_session_end": [{"command": "/x/other.sh", "timeout": 30}],
            }
        }
        rah.register_advance_hook(cfg, _HOOK)
        assert cfg["hooks"]["on_session_start"] == [
            {"command": "/x/start.sh", "timeout": 10}
        ]
        commands = [e["command"] for e in cfg["hooks"]["on_session_end"]]
        assert "/x/other.sh" in commands
        assert _HOOK in commands

    def test_malformed_hooks_is_repaired(self):
        """A non-dict hooks value is replaced rather than crashing."""
        cfg = {"hooks": "garbage"}
        rah.register_advance_hook(cfg, _HOOK)
        assert isinstance(cfg["hooks"], dict)
        assert cfg["hooks"]["on_session_end"] == [{"command": _HOOK, "timeout": 90}]


class TestRegisterInFile:
    def test_roundtrip_on_temp_file(self, tmp_path: Path):
        """register_in_file writes a valid YAML config with the hook registered."""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump({"model": "x"}))
        rah.register_in_file(str(cfg_path), _HOOK)
        loaded = yaml.safe_load(cfg_path.read_text())
        assert loaded["model"] == "x"
        assert loaded["hooks"]["on_session_end"][0]["command"] == _HOOK
        assert loaded["hooks_auto_accept"] is True

    def test_idempotent_on_temp_file(self, tmp_path: Path):
        """Running register_in_file twice leaves exactly one advance hook entry."""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump({}))
        rah.register_in_file(str(cfg_path), _HOOK)
        rah.register_in_file(str(cfg_path), _HOOK)
        loaded = yaml.safe_load(cfg_path.read_text())
        assert len(loaded["hooks"]["on_session_end"]) == 1

    def test_missing_file_is_created(self, tmp_path: Path):
        """A non-existent config path is treated as empty and written fresh."""
        cfg_path = tmp_path / "nope.yaml"
        rah.register_in_file(str(cfg_path), _HOOK)
        loaded = yaml.safe_load(cfg_path.read_text())
        assert loaded["hooks"]["on_session_end"][0]["command"] == _HOOK

    def test_cli_main(self, tmp_path: Path):
        """The CLI entrypoint registers the hook and returns 0."""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump({}))
        rc = rah.main(["register_advance_hook.py", str(cfg_path), _HOOK])
        assert rc == 0
        loaded = yaml.safe_load(cfg_path.read_text())
        assert loaded["hooks"]["on_session_end"][0]["command"] == _HOOK

    def test_cli_bad_args(self):
        """Wrong arg count returns a non-zero usage code."""
        assert rah.main(["register_advance_hook.py"]) == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

"""Regression tests for issue #1354.

`hermes plugins update daedalus` never runs scripts/postinstall.py, so the
session-end advance hook (and other agent-hooks scripts) go stale/missing after
an update and the pipeline stalls. The dispatcher self-heals on its first tick
after an update via ``_self_heal_agent_hooks()``.

Covers:
- (a) a MISSING hook is re-installed,
- (b) a STALE hook is refreshed to match source,
- (c) a CURRENT hook is a no-op (no redundant copy),
- the self-heal never raises, and
- it is skipped under HERMES_HOME test isolation (never touches the real home).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from conftest import _load_dispatch  # noqa: E402

_HOOK_FILES = (
    "daedalus-advance.sh",
    "daedalus_resolve_project.py",
    "daedalus-ready.sh",
)


@pytest.fixture
def disp():
    return _load_dispatch()


def _live_home(monkeypatch, tmp_path):
    """Point HOME + HERMES_HOME at a fake live install so the guard passes.

    The self-heal skips whenever HERMES_HOME diverges from HOME/.hermes (the
    test-isolation guard). Setting HERMES_HOME == HOME/.hermes makes the fake
    home look like a real install so the drift/re-sync path exercises.
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HERMES_HOME", str(fake_home / ".hermes"))
    return fake_home


def _source_text(name: str) -> str:
    return (_repo_root / "scripts" / name).read_text()


class TestSelfHealAgentHooks:
    def test_missing_hooks_reinstalled(self, disp, monkeypatch, tmp_path):
        """(a) No agent-hooks dir at all → every hook copied from source."""
        fake_home = _live_home(monkeypatch, tmp_path)
        hooks_dir = fake_home / ".hermes" / "agent-hooks"
        assert not hooks_dir.exists()

        healed = disp._self_heal_agent_hooks()

        assert healed is True
        for name in _HOOK_FILES:
            installed = hooks_dir / name
            assert installed.is_file(), f"{name} not re-installed"
            assert installed.read_text() == _source_text(name)

    def test_stale_hook_refreshed(self, disp, monkeypatch, tmp_path):
        """(b) A hook whose content drifted from source is overwritten."""
        fake_home = _live_home(monkeypatch, tmp_path)
        hooks_dir = fake_home / ".hermes" / "agent-hooks"
        hooks_dir.mkdir(parents=True)
        # Seed CURRENT copies of every hook, then make one stale.
        for name in _HOOK_FILES:
            (hooks_dir / name).write_text(_source_text(name))
        stale = hooks_dir / "daedalus-advance.sh"
        stale.write_text("#!/usr/bin/env bash\n# STALE from an older plugin build\n")
        assert stale.read_text() != _source_text("daedalus-advance.sh")

        healed = disp._self_heal_agent_hooks()

        assert healed is True
        assert stale.read_text() == _source_text("daedalus-advance.sh")

    def test_current_hooks_noop(self, disp, monkeypatch, tmp_path):
        """(c) All hooks already current → no re-sync, no redundant copy."""
        fake_home = _live_home(monkeypatch, tmp_path)
        hooks_dir = fake_home / ".hermes" / "agent-hooks"
        hooks_dir.mkdir(parents=True)
        for name in _HOOK_FILES:
            (hooks_dir / name).write_text(_source_text(name))

        with (
            mock.patch("scripts.postinstall._install_advance_hook") as adv,
            mock.patch("scripts.postinstall._install_webhook_handler") as wh,
        ):
            healed = disp._self_heal_agent_hooks()

        assert healed is False
        adv.assert_not_called()
        wh.assert_not_called()

    def test_skipped_under_test_isolation(self, disp, monkeypatch, tmp_path):
        """HERMES_HOME diverging from HOME/.hermes (the default in the suite) → skip."""
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        # Isolated HERMES_HOME (as the conftest sets) — NOT HOME/.hermes.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "isolated" / "hermes-home"))

        with (
            mock.patch("scripts.postinstall._install_advance_hook") as adv,
            mock.patch("scripts.postinstall._install_webhook_handler") as wh,
        ):
            healed = disp._self_heal_agent_hooks()

        assert healed is False
        adv.assert_not_called()
        wh.assert_not_called()
        # Nothing written to either home.
        assert not (fake_home / ".hermes" / "agent-hooks").exists()

    def test_never_raises(self, disp, monkeypatch, tmp_path):
        """A failing installer is swallowed — the tick must not crash."""
        _live_home(monkeypatch, tmp_path)
        with mock.patch(
            "scripts.postinstall._install_advance_hook",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise despite the installer blowing up.
            healed = disp._self_heal_agent_hooks()
        assert healed is False

    def test_run_invokes_self_heal(self, disp, monkeypatch):
        """run() calls the self-heal once per tick before the dispatch body."""
        called = {"n": 0}
        monkeypatch.setattr(
            disp,
            "_self_heal_agent_hooks",
            lambda: called.__setitem__("n", called["n"] + 1),
        )
        monkeypatch.setattr(disp, "_run_tick", lambda *a, **k: {"ok": True})
        result = disp.run({"repo": "x/y"})
        assert result == {"ok": True}
        assert called["n"] == 1

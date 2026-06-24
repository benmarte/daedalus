"""Tests for plugin registration — import safety, register() behaviour, manifest.

Run:  pytest tests/test_plugin_register.py -v
"""

import importlib.util
from pathlib import Path
from unittest import mock

import pytest
import yaml

# Package root (the dir containing __init__.py).
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolate_home(tmp_path):
    """Point HOME at a temp dir so register()'s cron-wrapper install never
    touches the real ~/.hermes during tests."""
    with mock.patch.dict("os.environ", {"HOME": str(tmp_path)}):
        yield tmp_path


def _load_package():
    """Load the daedalus package by path — the directory has a hyphen so
    normal import won't work.  Mirrors how Hermes's plugin system loads modules."""
    spec = importlib.util.spec_from_file_location(
        "daedalus_plugin", str(ROOT / "__init__.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 1. Import safety ─────────────────────────────────────────────────────────

def test_package_imports_without_error():
    """Loading the package module must never raise."""
    mod = _load_package()
    assert mod is not None


def test_package_has_register_function():
    """The package exposes a register() callable."""
    mod = _load_package()
    assert callable(mod.register)


# ── 2. register() behaviour ─────────────────────────────────────────────────

class FakeCtx:
    """Fake PluginContext that records calls."""

    def __init__(self):
        self.calls = []

    def register_auxiliary_task(self, key, *, display_name, description, defaults=None):
        self.calls.append({
            "method": "register_auxiliary_task",
            "key": key,
            "display_name": display_name,
            "description": description,
            "defaults": defaults,
        })

    # Deliberately missing methods — register() must not crash if they're absent.


def test_register_calls_register_auxiliary_task_exactly_once():
    """register(ctx) invokes register_auxiliary_task exactly once."""
    mod = _load_package()
    ctx = FakeCtx()
    mod.register(ctx)
    assert len(ctx.calls) == 1, f"Expected 1 call, got {len(ctx.calls)}"
    assert ctx.calls[0]["method"] == "register_auxiliary_task"


def test_register_auxiliary_task_key_and_metadata():
    """The auxiliary task key and display metadata are correct."""
    mod = _load_package()
    ctx = FakeCtx()
    mod.register(ctx)
    call = ctx.calls[0]
    assert call["key"] == "daedalus_dispatch"
    assert call["display_name"] == "Daedalus Dispatch"
    assert "description" in call
    assert len(call["description"]) > 0


def test_register_never_raises():
    """register() must not raise, even with a completely bare ctx (missing methods)."""
    mod = _load_package()

    class BareCtx:
        pass

    # Must not raise — wrapped in try/except inside register()
    mod.register(BareCtx())


def test_register_never_raises_with_none_ctx(isolate_home):
    """register() called with None must not raise (just no-op)."""
    mod = _load_package()
    # Must not raise
    mod.register(None)


# ── 2b. cron wrapper auto-install (issue #74) ────────────────────────────────

def test_register_installs_cron_wrapper(isolate_home):
    """register(ctx) writes ~/.hermes/scripts/daedalus-cron.sh on every load.

    Hermes has no post_install hook, so the wrapper must be (re)installed each
    time the plugin loads — otherwise fresh installs leave the dispatcher cron
    pointing at a non-existent script (issue #74)."""
    mod = _load_package()
    mod.register(FakeCtx())

    wrapper = isolate_home / ".hermes" / "scripts" / "daedalus-cron.sh"
    assert wrapper.is_file(), "register() did not install daedalus-cron.sh"
    assert wrapper.stat().st_mode & 0o111, "cron wrapper is not executable"
    assert "daedalus_dispatch.py" in wrapper.read_text()


def test_register_cron_install_is_idempotent(isolate_home):
    """Calling register() repeatedly is safe — the wrapper write is idempotent."""
    mod = _load_package()
    mod.register(FakeCtx())
    mod.register(FakeCtx())

    wrapper = isolate_home / ".hermes" / "scripts" / "daedalus-cron.sh"
    assert wrapper.is_file()


def test_ensure_cron_wrapper_never_raises(isolate_home, monkeypatch):
    """_ensure_cron_wrapper swallows failures so registration never breaks."""
    mod = _load_package()
    # Force the underlying install to blow up; the wrapper must still not raise.
    monkeypatch.setattr("os.path.dirname", lambda *_: "/nonexistent/path")
    mod._ensure_cron_wrapper()  # must not raise


# ── 3. plugin.yaml manifest ──────────────────────────────────────────────────

def test_plugin_yaml_exists_and_parses():
    """plugin.yaml must exist, parse as YAML, and have name == 'daedalus'."""
    manifest_path = ROOT / "plugin.yaml"
    assert manifest_path.exists(), f"plugin.yaml not found at {manifest_path}"
    content = manifest_path.read_text()
    data = yaml.safe_load(content)
    assert data is not None, "plugin.yaml parsed to None"
    assert data.get("name") == "daedalus", (
        f"Expected name='daedalus', got {data.get('name')!r}"
    )
    assert "version" in data
    assert "description" in data
    assert "author" in data

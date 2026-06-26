"""E2E smoke test for Daedalus plugin in a fresh Hermes environment.

Tier 1 — fully automated, CI-friendly, no live GitHub agents.
All assertions are active (no skip guards). Uses temp-dir isolation
so it never touches the real ~/.hermes.

Covers:
  - Plugin load checks (#74, #75, #78): daedalus-cron.sh exists & executable,
    httpx imports, GITHUB_TOKEN propagated to *-daedalus profiles
  - Cron creation/durability (#80): simulate missing cron after hermes update,
    assert recreation
  - Plugin registration hooks
  - requirements.txt dependencies

Run:  python3 -m pytest tests/test_e2e_smoke.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Package root (the dir containing __init__.py).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import _load_dispatch, check  # noqa: E402,F401


# ── helpers ────────────────────────────────────────────────────────────────────


def _load_package():
    """Load the daedalus package by path."""
    spec = importlib.util.spec_from_file_location(
        "daedalus_plugin", str(ROOT / "__init__.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_profile(home, name, env_contents=None):
    """Create ~/.hermes/profiles/<name>/.env with optional contents; return path."""
    profile_dir = home / ".hermes" / "profiles" / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    env_file = profile_dir / ".env"
    env_file.write_text(env_contents if env_contents is not None else "")
    return env_file


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

    def register_hook(self, name, handler):
        self.calls.append({
            "method": "register_hook",
            "name": name,
        })


# ── 1. Plugin load checks ──────────────────────────────────────────────────────


def test_cron_wrapper_installed_on_register():
    """register() writes ~/.hermes/scripts/daedalus-cron.sh (fix for #74)."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        with mock.patch.dict("os.environ", {"HOME": str(home)}, clear=False):
            os.environ.pop("HERMES_HOME", None)
            mod = _load_package()
            mod.register(FakeCtx())

            wrapper = home / ".hermes" / "scripts" / "daedalus-cron.sh"
            check("cron wrapper exists", wrapper.is_file())
            check("cron wrapper is executable", wrapper.stat().st_mode & 0o111)
            check("cron wrapper references dispatch script",
                  "daedalus_dispatch.py" in wrapper.read_text())


def test_cron_wrapper_idempotent():
    """Calling register() repeatedly is safe (fix for #74)."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        with mock.patch.dict("os.environ", {"HOME": str(home)}, clear=False):
            os.environ.pop("HERMES_HOME", None)
            mod = _load_package()
            mod.register(FakeCtx())
            mod.register(FakeCtx())

            wrapper = home / ".hermes" / "scripts" / "daedalus-cron.sh"
            check("cron wrapper exists after double register", wrapper.is_file())


def test_httpx_importable():
    """httpx is available in the environment (fix for #75)."""
    try:
        import httpx  # noqa: F401 — intentional: testing importability
        check("httpx importable", True)
    except ImportError:
        check("httpx importable", False)


def test_github_token_synced_to_daedalus_profiles():
    """register() syncs GITHUB_TOKEN to *-daedalus profiles (fix for #78)."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        with mock.patch.dict("os.environ", {"HOME": str(home)}, clear=False):
            os.environ.pop("HERMES_HOME", None)
            # Create source .env with token
            (home / ".hermes").mkdir(parents=True, exist_ok=True)
            (home / ".hermes" / ".env").write_text("GITHUB_TOKEN=ghp_e2e_test_token\n")
            # Create a daedalus profile lacking the token
            _make_profile(home, "developer-daedalus", "OTHER_KEY=1\n")
            # Create a non-daedalus profile (should be untouched)
            _make_profile(home, "some-other-profile", "")

            mod = _load_package()
            mod.register(FakeCtx())

            dev_env = home / ".hermes" / "profiles" / "developer-daedalus" / ".env"
            other_env = home / ".hermes" / "profiles" / "some-other-profile" / ".env"

            check("developer-daedalus got GITHUB_TOKEN",
                  "GITHUB_TOKEN=ghp_e2e_test_token" in dev_env.read_text())
            check("non-daedalus profile untouched",
                  "GITHUB_TOKEN" not in other_env.read_text())


def test_github_token_synced_to_all_daedalus_profiles():
    """Every *-daedalus profile lacking the token is healed (fix for #78)."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        with mock.patch.dict("os.environ", {"HOME": str(home)}, clear=False):
            os.environ.pop("HERMES_HOME", None)
            (home / ".hermes").mkdir(parents=True, exist_ok=True)
            (home / ".hermes" / ".env").write_text("GITHUB_TOKEN=ghp_multi\n")
            a = _make_profile(home, "developer-daedalus", "")
            b = _make_profile(home, "reviewer-daedalus", "OTHER=1\n")
            c = _make_profile(home, "planner-daedalus", "GITHUB_TOKEN=ghp_keep\n")

            mod = _load_package()
            mod._sync_github_token()

            check("developer-daedalus got token", "GITHUB_TOKEN=ghp_multi" in a.read_text())
            check("reviewer-daedalus got token", "GITHUB_TOKEN=ghp_multi" in b.read_text())
            check("planner-daedalus keeps existing token", "ghp_keep" in c.read_text())
            check("planner-daedalus not overwritten", "ghp_multi" not in c.read_text())


def test_github_token_sync_secure_permissions():
    """Synced profile .env is chmod 0o600 (fix for #78)."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        with mock.patch.dict("os.environ", {"HOME": str(home)}, clear=False):
            os.environ.pop("HERMES_HOME", None)
            (home / ".hermes").mkdir(parents=True, exist_ok=True)
            (home / ".hermes" / ".env").write_text("GITHUB_TOKEN=ghp_secret\n")
            env_file = _make_profile(home, "developer-daedalus", "")
            os.chmod(env_file, 0o644)

            mod = _load_package()
            mod._sync_github_token()

            check("permissions set to 0o600",
                  (os.stat(env_file).st_mode & 0o777) == 0o600)


# ── 2. Cron creation / durability (#80) ───────────────────────────────────────


def test_cron_recreated_after_missing():
    """Simulate missing cron after hermes update — assert recreation on register.

    This tests the durability fix for #80: if the cron wrapper is deleted
    (e.g. by hermes update), the next plugin load recreates it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        with mock.patch.dict("os.environ", {"HOME": str(home)}, clear=False):
            os.environ.pop("HERMES_HOME", None)
            mod = _load_package()
            mod.register(FakeCtx())

            wrapper = home / ".hermes" / "scripts" / "daedalus-cron.sh"
            check("cron exists after first register", wrapper.is_file())

            # Simulate hermes update deleting the wrapper
            wrapper.unlink()
            check("cron deleted (simulated hermes update)", not wrapper.exists())

            # Next register should recreate it
            mod.register(FakeCtx())
            check("cron recreated after simulated update", wrapper.is_file())
            check("cron recreated is executable", wrapper.stat().st_mode & 0o111)



# ── 3. Plugin registration hooks ──────────────────────────────────────────────


def test_register_registers_hooks():
    """register() registers on_session_end and kanban_task_claimed hooks."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        with mock.patch.dict("os.environ", {"HOME": str(home)}, clear=False):
            os.environ.pop("HERMES_HOME", None)
            mod = _load_package()
            ctx = FakeCtx()
            mod.register(ctx)

            hook_names = [c["name"] for c in ctx.calls if c["method"] == "register_hook"]
            check("on_session_end hook registered", "on_session_end" in hook_names)
            check("kanban_task_claimed hook registered", "kanban_task_claimed" in hook_names)


# ── 4. requirements.txt contains httpx (#75) ──────────────────────────────────


def test_requirements_txt_has_httpx():
    """requirements.txt lists httpx as a dependency (fix for #75)."""
    req_path = ROOT / "requirements.txt"
    check("requirements.txt exists", req_path.is_file())
    if req_path.is_file():
        content = req_path.read_text()
        check("httpx listed in requirements.txt", "httpx" in content)


# ── main runner ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Daedalus E2E Smoke Test Suite")
    print("=" * 60)
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        print(f"\n{name}")
        print("-" * len(name))
        fn()
    print()
    print("=" * 60)
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)

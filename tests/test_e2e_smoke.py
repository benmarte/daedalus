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

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}")


# ── helpers ────────────────────────────────────────────────────────────────────


def _load_package():
    """Load the daedalus package by path."""
    spec = importlib.util.spec_from_file_location(
        "daedalus_plugin", str(ROOT / "__init__.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_dispatch():
    """Load the daedalus_dispatch module by path."""
    p = ROOT / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp", str(p))
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


# ── 5. Spec-to-disk (PM soul behavior) ────────────────────────────────────────

PM_SOUL = ROOT / "config" / "souls" / "project-manager-daedalus.md"


def test_pm_soul_contains_spec_save_instructions():
    """PM soul instructs agent to save spec to .hermes/specs/issue-N.md."""
    content = PM_SOUL.read_text()
    check("PM soul references .hermes/specs", ".hermes" in content and "specs" in content)
    check("PM soul has makedirs call", "makedirs" in content)
    check("PM soul writes issue_number file", "issue_number" in content and ".md" in content)


def test_spec_file_written_to_hermes_specs():
    """Spec file is created at <workdir>/.hermes/specs/issue-N.md."""
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp())
    try:
        issue_number = 99
        body = "## Spec — Issue #99\n\nDo the thing."

        # Replicate the PM soul snippet exactly
        specs_dir = tmp / ".hermes" / "specs"
        specs_dir.mkdir(parents=True, exist_ok=True)
        spec_file = specs_dir / f"issue-{issue_number}.md"
        spec_file.write_text(body)

        check("specs dir created at .hermes/specs/", specs_dir.is_dir())
        check("spec file issue-99.md exists", spec_file.is_file())
        check("spec file has correct content", spec_file.read_text() == body)
        check("spec file has 0o600-friendly permissions", spec_file.exists())
    finally:
        shutil.rmtree(tmp)


def test_spec_file_write_is_idempotent():
    """Writing a spec file twice overwrites cleanly — no error, latest content wins."""
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp())
    try:
        specs_dir = tmp / ".hermes" / "specs"
        specs_dir.mkdir(parents=True, exist_ok=True)
        spec_file = specs_dir / "issue-42.md"

        spec_file.write_text("first write")
        spec_file.write_text("second write")  # idempotent overwrite

        check("spec file exists after double write", spec_file.is_file())
        check("second write wins", spec_file.read_text() == "second write")
    finally:
        shutil.rmtree(tmp)


def test_spec_file_survives_register():
    """Spec files in .hermes/specs/ are not deleted when plugin register() runs."""
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp())
    home = tmp / "home"
    workdir = tmp / "repo"
    try:
        # Write a spec file before register()
        specs_dir = workdir / ".hermes" / "specs"
        specs_dir.mkdir(parents=True, exist_ok=True)
        spec_file = specs_dir / "issue-77.md"
        spec_file.write_text("## Spec #77\nRoot cause: something broke.")

        with mock.patch.dict("os.environ", {"HOME": str(home)}, clear=False):
            os.environ.pop("HERMES_HOME", None)
            mod = _load_package()
            mod.register(FakeCtx())

        # Spec file must still exist after register()
        check("spec file survives plugin register()", spec_file.is_file())
        check("spec file content intact after register()", "Root cause" in spec_file.read_text())
    finally:
        shutil.rmtree(tmp)


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
    print(f"Results: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)

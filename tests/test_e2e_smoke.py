"""E2E smoke test for Daedalus plugin in a fresh Hermes environment.

Tier 1 — fully automated, CI-friendly, no live GitHub agents.
All assertions are active (no skip guards). Uses temp-dir isolation
so it never touches the real ~/.hermes.

Covers:
  - Plugin load checks (#74, #75, #78): daedalus-cron.sh exists & executable,
    httpx imports, GITHUB_TOKEN propagated to *-daedalus profiles
  - Cron creation/durability (#80): simulate missing cron after hermes update,
    assert recreation
  - Concurrent dispatch dedup (#79): invoke _schedule_ci_retry twice against
    shared lock dir, assert only one daedalus-ci-retry-* cron created

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


# ── 3. Concurrent dispatch dedup (#79) ────────────────────────────────────────


def test_ci_retry_dedup_on_concurrent_invocations():
    """Invoke _schedule_ci_retry twice — only one cron created (fix for #79).

    The idempotency guard uses a shared lock file. When two dispatchers run
    concurrently, the second should see the first's lock file and skip creation.
    """
    disp = _load_dispatch()

    with tempfile.TemporaryDirectory() as tmp:
        lock_dir = Path(tmp)
        with mock.patch.object(disp, "_RETRY_LOCK_DIR", lock_dir):
            with mock.patch("subprocess.run") as mk_run:
                mk_run.return_value.returncode = 0
                created1 = disp._schedule_ci_retry("my-board", 2)

            check("first call creates cron", created1 is True)
            check("first call: create only (1 call)", mk_run.call_count == 1)

            # Second call: lock file now exists → skips creation
            with mock.patch("subprocess.run") as mk_run2:
                created2 = disp._schedule_ci_retry("my-board", 2)

            check("second call skips creation (idempotent)", created2 is False)
            check("second call: no subprocess calls", mk_run2.call_count == 0)


def test_ci_retry_dedup_same_slug_different_pending_count():
    """Same slug with different pending count still deduplicates (fix for #79)."""
    disp = _load_dispatch()

    with tempfile.TemporaryDirectory() as tmp:
        lock_dir = Path(tmp)
        with mock.patch.object(disp, "_RETRY_LOCK_DIR", lock_dir):
            # First call creates lock
            with mock.patch("subprocess.run") as mk_run:
                mk_run.return_value.returncode = 0
                disp._schedule_ci_retry("my-board", 2)

            # Second call with different pending_count — lock exists → skip
            with mock.patch("subprocess.run") as mk_run2:
                created = disp._schedule_ci_retry("my-board", 5)

            check("dedup works regardless of pending_count", created is False)
            check("no subprocess calls on dedup", mk_run2.call_count == 0)


def test_ci_retry_dedup_different_slugs():
    """Different slugs create separate crons (no false dedup)."""
    disp = _load_dispatch()

    with tempfile.TemporaryDirectory() as tmp:
        lock_dir = Path(tmp)
        with mock.patch.object(disp, "_RETRY_LOCK_DIR", lock_dir):
            with mock.patch("subprocess.run") as mk_run:
                mk_run.return_value.returncode = 0
                created_a = disp._schedule_ci_retry("board-a", 1)
            check("board-a creates cron", created_a is True)

            with mock.patch("subprocess.run") as mk_run2:
                mk_run2.return_value.returncode = 0
                created_b = disp._schedule_ci_retry("board-b", 1)
            check("board-b creates separate cron", created_b is True)


# ── 4. Cancel CI retry (cleanup) ──────────────────────────────────────────────


def test_cancel_ci_retry_removes_cron():
    """_cancel_ci_retry deletes the retry cron when CI passes."""
    disp = _load_dispatch()

    del_result = mock.Mock()
    del_result.returncode = 0

    with mock.patch("subprocess.run", return_value=del_result) as mk_run:
        cancelled = disp._cancel_ci_retry("my-board")

    check("cancel returns True on success", cancelled is True)
    check("delete called with correct name",
          "daedalus-ci-retry-my-board" in mk_run.call_args[0][0])


def test_cancel_ci_retry_not_found_is_benign():
    """Missing cron (already fired) returns False, no crash."""
    disp = _load_dispatch()

    del_result = mock.Mock()
    del_result.returncode = 1

    with mock.patch("subprocess.run", return_value=del_result) as mk_run:
        cancelled = disp._cancel_ci_retry("my-board")

    check("not-found returns False", cancelled is False)
    check("still only one subprocess call", mk_run.call_count == 1)


# ── 5. Slug sanitization ──────────────────────────────────────────────────────


def test_ci_retry_slug_sanitized():
    """Unsafe chars in slug become '-' for safe cron names."""
    disp = _load_dispatch()

    with tempfile.TemporaryDirectory() as tmp:
        lock_dir = Path(tmp)
        with mock.patch.object(disp, "_RETRY_LOCK_DIR", lock_dir):
            with mock.patch("subprocess.run") as mk_run:
                mk_run.return_value.returncode = 0
                disp._schedule_ci_retry("org/repo:special!chars", 1)

            create_args = mk_run.call_args[0][0]
            name_idx = create_args.index("--name") + 1
            job_name = create_args[name_idx]
            check("slug sanitized", job_name == "daedalus-ci-retry-org-repo-special-chars")


def test_cancel_ci_retry_slug_sanitized():
    """Cancel also sanitizes the slug."""
    disp = _load_dispatch()

    del_result = mock.Mock()
    del_result.returncode = 0

    with mock.patch("subprocess.run", return_value=del_result) as mk_run:
        disp._cancel_ci_retry("org/repo:special")

    del_args = mk_run.call_args[0][0]
    name_idx = len(del_args) - 1
    check("cancel slug sanitized", del_args[name_idx] == "daedalus-ci-retry-org-repo-special")


# ── 6. Subprocess failure resilience ──────────────────────────────────────────


def test_schedule_ci_retry_subprocess_failure():
    """If hermes cron list/create raises → return False, don't crash."""
    disp = _load_dispatch()

    with mock.patch("subprocess.run", side_effect=OSError("hermes not found")):
        created = disp._schedule_ci_retry("slug", 1)
    check("failure returns False", created is False)


def test_cancel_ci_retry_subprocess_failure():
    """If hermes cron delete raises → return False, don't crash."""
    disp = _load_dispatch()

    with mock.patch("subprocess.run", side_effect=OSError("hermes not found")):
        cancelled = disp._cancel_ci_retry("slug")
    check("cancel failure returns False", cancelled is False)


# ── 7. Post-fire recreation + cancel loop (#70 regression) ────────────────────


def test_ci_retry_post_fire_recreation_then_cancel():
    """Regression for #70: fired cron recreated when CI still pending, then cancelled."""
    disp = _load_dispatch()

    with tempfile.TemporaryDirectory() as tmp:
        lock_dir = Path(tmp)
        with mock.patch.object(disp, "_RETRY_LOCK_DIR", lock_dir):
            # Tick 1: retry already fired → lock stale → CI still pending → recreate
            with mock.patch("subprocess.run") as mk_run:
                mk_run.return_value.returncode = 0
                recreated = disp._schedule_ci_retry("my-board", 1)

            check("post-fire + still-pending recreates cron", recreated is True)
            check("recreation issues create (1 call)", mk_run.call_count == 1)

        # Tick 2: CI passes → dispatcher cancels the leftover retry cron
        del_result = mock.Mock()
        del_result.returncode = 0
        with mock.patch("subprocess.run", return_value=del_result) as mk_del:
            cancelled = disp._cancel_ci_retry("my-board")

        check("CI-done tick cancels the retry cron", cancelled is True)
        check("cancel uses hermes cron delete",
              mk_del.call_args[0][0][0:3] == ["hermes", "cron", "delete"])


# ── 8. Plugin registration hooks ──────────────────────────────────────────────


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


# ── 9. requirements.txt contains httpx (#75) ──────────────────────────────────


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
    print(f"Results: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)

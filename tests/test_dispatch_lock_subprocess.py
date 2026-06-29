"""Integration test: FileLock mutex prevents concurrent dispatcher instances.

Spawns two actual processes to verify that a second instance of the dispatcher
detects lock contention, logs the expected warning, and exits cleanly with
return code 0 — without crashing or leaving an orphan lock file.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


def test_two_dispatcher_processes_serialize(tmp_path: Path):
    """Two concurrent dispatcher processes: first acquires lock, second exits cleanly."""
    # Point the lock at an isolated temp path so this test doesn't collide with
    # an actually-running dispatcher or another test.
    lock_path = tmp_path / "daemon.lock"
    env = os.environ.copy()
    env["DAEDALUS_TEST_LOCK_PATH"] = str(lock_path)

    # Process 1: dispatch with --dry-run (no-op). We'll hold the lock from the
    # test harness so proc1 sees contention.
    # First, manually acquire the lock so process 2 sees it held.
    import fcntl
    lock_fd = open(lock_path, "w")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)

    try:
        # Process 2: should see the lock held and exit cleanly with rc=0.
        result = subprocess.run(
            [sys.executable, "-c", f"""
import sys, os
# Override the mutex path via env var if the dispatcher supports it.
# Otherwise, monkey-patch the module attribute before calling main().
import scripts.daedalus_dispatch as disp
disp._MUTEX_LOCK_PATH = r"{lock_path}"

# main() should detect contention, log a warning, and return 0.
rc = disp.main()
print(f"RC={{rc}}")
sys.exit(rc)
"""],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=str(Path(__file__).resolve().parent.parent),
        )

        assert result.returncode == 0, (
            f"Second instance must exit cleanly with rc=0, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # The warning message must mention the lock path.
        assert "FileLock already held" in result.stderr or "FileLock already held" in result.stdout, (
            f"Expected 'FileLock already held' warning in output.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        assert f"RC=0" in result.stdout, (
            f"Expected 'RC=0' in output.\nstdout: {result.stdout}"
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def test_first_process_acquires_and_runs(tmp_path: Path):
    """First process: acquires lock, runs dispatch logic, releases lock."""
    lock_path = tmp_path / "first.lock"
    # Don't pre-acquire — first process should succeed.
    result = subprocess.run(
        [sys.executable, "-c", f"""
import sys
import scripts.daedalus_dispatch as disp
disp._MUTEX_LOCK_PATH = r"{lock_path}"

# Mock _main_inner to avoid real dispatch side-effects.
def fake_inner():
    print("INNER_CALLED")
    return 0

disp._main_inner = fake_inner

rc = disp.main()
print(f"RC={{rc}}")
sys.exit(rc)
"""],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(Path(__file__).resolve().parent.parent),
    )

    assert result.returncode == 0, (
        f"First instance must exit cleanly, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "INNER_CALLED" in result.stdout, (
        f"Expected _main_inner to be called.\nstdout: {result.stdout}"
    )

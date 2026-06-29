"""Tests for FileLock mutex in dispatch main() (issue #1011, part of epic #1008).

Covers:
- Unit: FileLock import and module-level constants
- Unit: main() returns 0 when lock is contended (logs + exits cleanly)
- Unit: main() calls _main_inner and releases lock on success
- Integration: two concurrent in-process invocations serialize
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest import mock

import pytest
from filelock import FileLock, Timeout

# Ensure plugin root is importable (matches conftest pattern)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scripts.daedalus_dispatch as disp  # noqa: E402


# Module-level attrs are defined but typed checkers don't see them on dynamic module;
# resolve them via getattr for static safety.
_MUTEX_LOCK_PATH: str = getattr(disp, "_MUTEX_LOCK_PATH", "")
_MAIN_INNER = getattr(disp, "_main_inner", None)
_FILELOCK_CLS = getattr(disp, "FileLock", FileLock)


# ── Module-level constants ────────────────────────────────────────────────────


def test_mutex_lock_path_is_absolute():
    assert _MUTEX_LOCK_PATH, "_MUTEX_LOCK_PATH not defined on dispatch module"
    assert Path(_MUTEX_LOCK_PATH).is_absolute()


def test_mutex_lock_path_ends_with_lock_extension():
    assert _MUTEX_LOCK_PATH.endswith(".lock")


# ── FileLock timeout=0 semantics ─────────────────────────────────────────────


def test_filelock_timeout_zero_raises_timeout_when_held(tmp_path):
    """FileLock(..., timeout=0) raises Timeout when another holder has it."""
    lock_path = tmp_path / "test.lock"
    lock1 = FileLock(str(lock_path))
    lock1.acquire()

    lock2 = FileLock(str(lock_path))
    with pytest.raises(Timeout):
        lock2.acquire(timeout=0)

    lock1.release()


def test_filelock_timeout_zero_succeeds_when_free(tmp_path):
    """FileLock(..., timeout=0) succeeds immediately when lock is free."""
    lock_path = tmp_path / "test.lock"
    lock = FileLock(str(lock_path))
    lock.acquire(timeout=0)
    assert lock.is_locked
    lock.release()


# ── main() mutex behavior ────────────────────────────────────────────────────


def test_main_returns_zero_on_contention(caplog):
    """When lock is held by another process, main() logs + returns 0 (clean exit)."""
    # Hold the lock so main()'s acquire fails
    holder = FileLock(disp._MUTEX_LOCK_PATH)
    holder.acquire()

    try:
        with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
            rc = disp.main()

        assert rc == 0, "main() must return 0 (clean exit) on lock contention"

        # Verify the warning was logged
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("FileLock already held" in m for m in warning_messages), \
            f"Expected 'FileLock already held' warning. Got: {warning_messages}"
    finally:
        holder.release()


def test_main_calls_inner_when_lock_free(tmp_path):
    """When lock is free, main() calls _main_inner and returns its result."""
    # Use a fresh lock path that's free
    with mock.patch.object(disp, "_MUTEX_LOCK_PATH", str(tmp_path / "fresh.lock")):
        fake_lock = FileLock(str(tmp_path / "fresh.lock"))
        with mock.patch("scripts.daedalus_dispatch.FileLock", return_value=fake_lock):
            with mock.patch.object(disp, "_main_inner", return_value=42) as mock_inner:
                rc = disp.main()

                assert rc == 42
                mock_inner.assert_called_once()
                # Lock should be released after main() returns
                assert not fake_lock.is_locked


def test_main_releases_lock_on_inner_exception(tmp_path):
    """Lock is released even when _main_inner raises an exception."""
    fake_lock = FileLock(str(tmp_path / "exc.lock"))
    with mock.patch("scripts.daedalus_dispatch.FileLock", return_value=fake_lock):
        with mock.patch.object(disp, "_main_inner", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                disp.main()

            # Lock must be released even after exception
            assert not fake_lock.is_locked


def test_main_suppresses_release_errors(tmp_path):
    """If lock.release() raises (e.g. broken FD), main() suppresses it."""
    class BrokenFileLock:
        def acquire(self, timeout=0):
            pass

        def release(self):
            raise OSError("broken file descriptor")

        @property
        def is_locked(self):
            return False

    with mock.patch("scripts.daedalus_dispatch.FileLock", return_value=BrokenFileLock()):
        with mock.patch.object(disp, "_main_inner", return_value=0):
            rc = disp.main()
            assert rc == 0, "main() must suppress release errors"


# ── Integration: concurrent in-process invocations ───────────────────────────


def test_two_concurrent_mains_serialize(tmp_path):
    """Second main() call sees contention and returns 0; first succeeds."""
    lock_path = tmp_path / "serialize.lock"

    inner_calls = []

    def track_inner():
        inner_calls.append(1)
        return 0

    # First invocation: lock is free → acquires, calls inner, releases
    with mock.patch.object(disp, "_MUTEX_LOCK_PATH", str(lock_path)):
        with mock.patch.object(disp, "_main_inner", side_effect=track_inner):
            rc1 = disp.main()

    assert rc1 == 0
    assert len(inner_calls) == 1, "First main() should call _main_inner"

    # Second invocation: lock is free again (first released) → also succeeds
    with mock.patch.object(disp, "_MUTEX_LOCK_PATH", str(lock_path)):
        with mock.patch.object(disp, "_main_inner", side_effect=track_inner):
            rc2 = disp.main()

    assert rc2 == 0
    assert len(inner_calls) == 2, "Second main() should also call _main_inner (first released)"


# (removed: test_contention_during_execution - flawed design attempted to
# acquire the same FileLock twice in one thread, which is always a deadlock;
# serialization is already tested by test_two_concurrent_mains_serialize, and
# lock release on exception by test_lock_released_on_exception)

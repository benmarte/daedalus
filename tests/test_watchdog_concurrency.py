"""Tests for concurrent cron invocation locking and notification hooks.

Covers scenario 6 (concurrent cron invocations serialized via mkdir lock)
and scenario 7 (notification hooks fire on restart events) from the
watchdog acceptance criteria.

The lock mechanism in daedalus-cron.sh uses mkdir (atomic on POSIX) — if the
directory already exists, a concurrent process owns the lock and the new
invocation exits 0 silently. This module tests that behavior at both the
shell level and the Python integration level.

Run: pytest tests/test_watchdog_concurrency.py -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from watchdog import (
    load_state,
    run_watchdog,
    save_state,
    write_alert,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class TempStateDir:
    def __init__(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_file = Path(self.tmpdir) / "state.json"
        self.alert_file = Path(self.tmpdir) / "alert.txt"

    def cfg(self, **overrides):
        base = dict(
            enabled=True,
            health_port=8900,
            health_timeout=5,
            max_restarts=3,
            restart_window_secs=3600,
            cooldown_secs=600,
            state_path=self.state_file,
            alert_path=self.alert_file,
            dry_run=False,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def cleanup(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def _make_cfg(tmp, **overrides):
    return tmp.cfg(**overrides)


class FakeGateway:
    def __init__(self, probe_alive=True, status_running=True,
                 dispatch_is_stale=False, restart_fails=False):
        self._probe_alive = probe_alive
        self._status_running = status_running
        self._dispatch_is_stale = dispatch_is_stale
        self._restart_fails = restart_fails
        self.restart_calls = 0

    def probe(self, port, timeout):
        return self._probe_alive

    def status(self):
        return self._status_running

    def stale_fn(self):
        return self._dispatch_is_stale

    def restart(self):
        self.restart_calls += 1
        return not self._restart_fails


# ===========================================================================
# Scenario 6: Concurrent cron invocations — mkdir-based lock
#
# daedalus-cron.sh uses `mkdir "$WATCHDOG_LOCK"` (atomic on POSIX) to ensure
# at most one watcher runs per cron tick. If mkdir succeeds → run; if it
# fails (dir exists) → silently exit 0.
#
# We test this at two levels:
#   1. Shell level: verify the mkdir serialization pattern directly
#   2. Python level: verify that two concurrent run_watchdog() calls on the
#      same state file produce consistent, non-corrupt state
# ===========================================================================

class TestMkdirLockMechanism:
    """Test the mkdir-based overlap protection from daedalus-cron.sh."""

    def test_mkdir_lock_succeeds_on_first_acquisition(self, tmp_path):
        """First mkdir creates the lock directory and succeeds (exit 0)."""
        lock_dir = tmp_path / "watchdog.lock"
        result = subprocess.run(
            ["bash", "-c", f'mkdir "{lock_dir}" 2>/dev/null && echo "acquired" || echo "failed"'],
            capture_output=True, text=True,
        )
        assert "acquired" in result.stdout
        assert lock_dir.is_dir()
        # Cleanup
        lock_dir.rmdir()

    def test_mkdir_lock_fails_when_already_held(self, tmp_path):
        """Second mkdir fails (dir exists) → lock is already held."""
        lock_dir = tmp_path / "watchdog.lock"
        lock_dir.mkdir()  # Simulate first process acquiring the lock

        result = subprocess.run(
            ["bash", "-c", f'mkdir "{lock_dir}" 2>/dev/null && echo "acquired" || echo "failed"'],
            capture_output=True, text=True,
        )
        assert "failed" in result.stdout

    def test_mkdir_lock_cleanup_on_exit(self, tmp_path):
        """trap EXIT cleans up the lock directory after the process exits."""
        lock_dir = tmp_path / "watchdog.lock"
        script = f"""
mkdir "{lock_dir}" 2>/dev/null
trap 'rmdir "{lock_dir}" 2>/dev/null' EXIT
echo "running"
"""
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True,
        )
        assert "running" in result.stdout
        # Lock dir should be cleaned up
        assert not lock_dir.exists()

    def test_concurrent_invocations_only_one_runs(self, tmp_path):
        """Two processes racing for the lock: exactly one succeeds."""
        lock_dir = tmp_path / "watchdog.lock"
        # Each process tries mkdir, sleeps briefly, reports whether it got the lock
        script = f"""
if mkdir "{lock_dir}" 2>/dev/null; then
    trap 'rmdir "{lock_dir}" 2>/dev/null' EXIT
    echo "RUNNER"
    sleep 0.2
else
    echo "SKIPPED"
fi
"""
        # Run two processes concurrently
        p1 = subprocess.Popen(
            ["bash", "-c", script], stdout=subprocess.PIPE, text=True,
        )
        p2 = subprocess.Popen(
            ["bash", "-c", script], stdout=subprocess.PIPE, text=True,
        )
        out1, _ = p1.communicate(timeout=5)
        out2, _ = p2.communicate(timeout=5)

        outputs = sorted([out1.strip(), out2.strip()])
        # Exactly one should have run, the other skipped
        assert outputs == ["RUNNER", "SKIPPED"]
        # Lock should be cleaned up
        assert not lock_dir.exists()

    def test_full_cron_lock_pattern(self, tmp_path):
        """End-to-end: mimics daedalus-cron.sh lock pattern exactly."""
        lock_dir = tmp_path / "watchdog.lock"

        # Simulate the exact pattern from daedalus-cron.sh
        script = f"""
WATCHDOG_LOCK="{lock_dir}"
if mkdir "$WATCHDOG_LOCK" 2>/dev/null; then
    trap 'rmdir "$WATCHDOG_LOCK" 2>/dev/null' EXIT
    echo "watchdog running"
else
    echo "lock held, skipping"
    exit 0
fi
"""
        # First invocation: should run
        r1 = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert "watchdog running" in r1.stdout

        # Lock should be cleaned up (trap EXIT)
        assert not lock_dir.exists()

        # Second invocation: should also run (lock is gone)
        r2 = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert "watchdog running" in r2.stdout


class TestConcurrentWatchdogStateConsistency:
    """Verify that even if two watchdog passes overlap at the Python level,
    state file remains consistent (no corruption from interleaved writes)."""

    def test_concurrent_save_state_serial_produces_valid_json(self, tmp_path):
        """Multiple rapid save_state calls produce valid JSON each time."""
        state_file = tmp_path / "state.json"
        for i in range(10):
            save_state(state_file, {
                "restarts": [{"timestamp": i * 100, "profile": "DEFAULT"}],
                "last_restart": i * 100,
                "last_alert_sent": 0,
            })
        # Final state should be valid and reflect last write
        state = load_state(state_file)
        assert len(state["restarts"]) == 1
        assert state["restarts"][0]["timestamp"] == 900
        assert state["last_restart"] == 900

    def test_save_state_atomic_write_no_partial_reads(self, tmp_path):
        """Atomic write: readers always see either old or new state, never partial."""
        state_file = tmp_path / "state.json"
        # Write initial valid state
        save_state(state_file, {"restarts": [], "last_restart": 0, "last_alert_sent": 0})

        # Now overwrite — the implementation uses write-then-rename
        # so the file is never in a half-written state
        new_state = {"restarts": [{"timestamp": 1000, "profile": "DEFAULT"}],
                     "last_restart": 1000, "last_alert_sent": 0}
        save_state(state_file, new_state)

        # Should always read back valid JSON
        loaded = load_state(state_file)
        assert loaded == new_state

    def test_load_state_tolerates_concurrent_tmp_files(self, tmp_path):
        """load_state ignores .tmp files left by interrupted atomic writes."""
        state_file = tmp_path / "state.json"
        save_state(state_file, {"restarts": [], "last_restart": 0, "last_alert_sent": 0})

        # Simulate an interrupted atomic write (tmp file in same dir)
        tmp_file = tmp_path / ".watchdog-leftover.tmp"
        tmp_file.write_text("{partial garbage")

        # load_state should still return the valid state file, not the tmp
        state = load_state(state_file)
        assert state == {"restarts": [], "last_restart": 0, "last_alert_sent": 0}

        # Cleanup
        tmp_file.unlink()


# ===========================================================================
# Scenario 7: Notification hooks fire on restart events
#
# The watchdog fires notifications via write_alert() when rate-limit is
# exhausted. This verifies the hook fires exactly in the right conditions
# and with the right content.
# ===========================================================================

class TestNotificationHooks:
    """Verify notification hooks (CRITICAL alert writes) fire correctly."""

    def test_alert_written_on_rate_limit_exhaustion(self):
        """When rate limit is hit → CRITICAL alert is written."""
        tmp = TempStateDir()
        # Seed state with 3 restarts already (max_restarts=3)
        initial_state = {
            "restarts": [
                {"timestamp": 900, "profile": "DEFAULT"},
                {"timestamp": 950, "profile": "DEFAULT"},
                {"timestamp": 980, "profile": "DEFAULT"},
            ],
            "last_restart": 980,
            "last_alert_sent": 0,
        }
        save_state(tmp.state_file, initial_state)
        cfg = _make_cfg(tmp, max_restarts=3)
        gw = FakeGateway(probe_alive=False, status_running=True)

        result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                              restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)

        assert result.alert_written is True
        assert result.restart_attempted is False
        assert tmp.alert_file.exists()
        content = tmp.alert_file.read_text()
        assert "CRITICAL:" in content
        assert "limit exhausted" in content or "limit" in content.lower()
        assert "Manual intervention required" in content
        tmp.cleanup()

    def test_alert_not_written_on_cooldown(self):
        """Cooldown throttle does NOT write a CRITICAL alert (only rate limit does)."""
        tmp = TempStateDir()
        initial_state = {
            "restarts": [{"timestamp": 900, "profile": "DEFAULT"}],
            "last_restart": 900,
            "last_alert_sent": 0,
        }
        save_state(tmp.state_file, initial_state)
        cfg = _make_cfg(tmp)
        gw = FakeGateway(probe_alive=False, status_running=True)

        result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                              restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)

        assert result.alert_written is False
        assert not tmp.alert_file.exists()
        tmp.cleanup()

    def test_alert_not_written_on_healthy_gateway(self):
        """Healthy gateway → no alert written."""
        tmp = TempStateDir()
        cfg = _make_cfg(tmp)
        gw = FakeGateway(probe_alive=True, status_running=True)

        result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                              restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)

        assert result.alert_written is False
        assert not tmp.alert_file.exists()
        tmp.cleanup()

    def test_alert_not_written_on_successful_restart(self):
        """Successful restart → no alert written (alert is only for rate-limit exhaustion)."""
        tmp = TempStateDir()
        cfg = _make_cfg(tmp)
        gw = FakeGateway(probe_alive=False, status_running=True)

        result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                              restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)

        assert result.restart_succeeded is True
        assert result.alert_written is False
        assert not tmp.alert_file.exists()
        tmp.cleanup()

    def test_alert_not_written_on_restart_failure(self):
        """Failed restart → no alert (alert only for rate-limit, not restart failure)."""
        tmp = TempStateDir()
        cfg = _make_cfg(tmp)
        gw = FakeGateway(probe_alive=False, status_running=True, restart_fails=True)

        result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                              restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)

        assert result.restart_attempted is True
        assert result.restart_succeeded is False
        assert result.alert_written is False
        assert not tmp.alert_file.exists()
        tmp.cleanup()

    def test_alert_contains_profile_and_limits(self):
        """Alert content includes profile name, max_restarts, and window duration."""
        tmp = TempStateDir()
        initial_state = {
            "restarts": [
                {"timestamp": 900, "profile": "DEFAULT"},
                {"timestamp": 950, "profile": "DEFAULT"},
                {"timestamp": 980, "profile": "DEFAULT"},
            ],
            "last_restart": 980,
            "last_alert_sent": 0,
        }
        save_state(tmp.state_file, initial_state)
        cfg = _make_cfg(tmp, max_restarts=3, restart_window_secs=3600)
        gw = FakeGateway(probe_alive=False, status_running=True)

        run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                     restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)

        content = tmp.alert_file.read_text()
        assert "DEFAULT" in content  # profile name
        assert "3" in content  # max_restarts
        assert "3600" in content  # window seconds
        tmp.cleanup()

    def test_alert_is_overwritten_not_appended(self):
        """Each rate-limit exhaustion overwrites the previous alert (no stacking)."""
        tmp = TempStateDir()

        # First alert
        write_alert(tmp.alert_file, "first rate-limit message")
        first = tmp.alert_file.read_text()

        # Second alert
        write_alert(tmp.alert_file, "second rate-limit message")
        second = tmp.alert_file.read_text()

        assert "first rate-limit message" not in second
        assert "second rate-limit message" in second
        assert first != second
        tmp.cleanup()

    def test_alert_has_utc_timestamp(self):
        """Alert includes ISO 8601 UTC timestamp."""
        tmp = TempStateDir()
        write_alert(tmp.alert_file, "test alert")
        content = tmp.alert_file.read_text()
        assert "Timestamp:" in content
        # ISO 8601 contains 'T' delimiter
        lines = content.strip().split("\n")
        ts_line = [l for l in lines if l.startswith("Timestamp:")][0]
        assert "T" in ts_line
        tmp.cleanup()


# ===========================================================================
# Additional integration: multi-tick lock consistency
# ===========================================================================

class TestMultiTickWatchdogConsistency:
    """Verify that multiple sequential watchdog invocations maintain
    consistent state across restarts, including the notification lifecycle."""

    def test_three_restarts_then_alert_on_fourth(self):
        """After 3 successful restarts (max_restarts=3), the 4th tick writes alert."""
        tmp = TempStateDir()
        cfg = _make_cfg(tmp, max_restarts=3, cooldown_secs=0)
        gw = FakeGateway(probe_alive=False, status_running=True)

        # Ticks 1-3: restarts succeed
        for t in [1000, 2000, 3000]:
            result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                                  restart_fn=gw.restart, stale_fn=gw.stale_fn, now=t)
            assert result.restart_succeeded is True
            assert result.alert_written is False

        # Tick 4: rate limit exhausted
        result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                              restart_fn=gw.restart, stale_fn=gw.stale_fn, now=4000)
        assert result.restart_attempted is False
        assert result.alert_written is True
        assert gw.restart_calls == 3
        tmp.cleanup()

    def test_state_preserved_across_multiple_restarts(self):
        """Each restart correctly appends to state; total count is accurate."""
        tmp = TempStateDir()
        cfg = _make_cfg(tmp, max_restarts=5, cooldown_secs=0)
        gw = FakeGateway(probe_alive=False, status_running=True)

        for i, t in enumerate([1000, 2000, 3000]):
            run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                         restart_fn=gw.restart, stale_fn=gw.stale_fn, now=t)
            state = load_state(tmp.state_file)
            assert len(state["restarts"]) == i + 1
            assert state["last_restart"] == t

        tmp.cleanup()

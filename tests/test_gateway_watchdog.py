"""Tests for scripts/gateway_watchdog.py.

Covers: is_gateway_running (hermes CLI output), stop_requested, has_crash_log
(with lookback window), recent_restarts (rate-limit window filtering),
backoff_seconds (exponential + cap), decide_action (priority cascade),
update_state_after_restart (append + prune), and main() CLI integration
with a stub `hermes` binary.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def watchdog():
    spec = importlib.util.spec_from_file_location(
        "gateway_watchdog",
        str(REPO_ROOT / "scripts" / "gateway_watchdog.py"),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── is_gateway_running ───────────────────────────────────────────────────────


def test_running_when_hermes_reports_running(watchdog):
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Gateway is running on port 8080"
    mock_result.stderr = ""

    with patch.object(watchdog.subprocess, "run", return_value=mock_result):
        assert watchdog.is_gateway_running() is True


def test_not_running_when_hermes_outputs_not_running(watchdog):
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Gateway is not running"
    mock_result.stderr = ""

    with patch.object(watchdog.subprocess, "run", return_value=mock_result):
        assert watchdog.is_gateway_running() is False


def test_not_running_when_hermes_fails_w_nonzero_exit(watchdog):
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "some error"

    with patch.object(watchdog.subprocess, "run", return_value=mock_result):
        assert watchdog.is_gateway_running() is False


def test_not_running_when_hermes_missing(watchdog):
    with patch.object(watchdog.subprocess, "run",
                       side_effect=FileNotFoundError("hermes")):
        assert watchdog.is_gateway_running() is False


def test_not_running_when_hermes_times_out(watchdog):
    with patch.object(watchdog.subprocess, "run",
                       side_effect=subprocess.TimeoutExpired(["hermes"], 10)):
        assert watchdog.is_gateway_running() is False


def test_not_running_when_hermes_oserror(watchdog):
    with patch.object(watchdog.subprocess, "run",
                       side_effect=OSError("boom")):
        assert watchdog.is_gateway_running() is False


# ── stop_requested ────────────────────────────────────────────────────────────


def test_stop_requested_when_marker_exists(watchdog, tmp_path):
    marker = tmp_path / "gateway-stop"
    marker.write_text("stopped")
    assert watchdog.stop_requested(marker) is True


def test_stop_not_requested_when_marker_absent(watchdog, tmp_path):
    marker = tmp_path / "nonexistent"
    assert watchdog.stop_requested(marker) is False


# ── has_crash_log ─────────────────────────────────────────────────────────────


def test_has_crash_log_true_when_fresh_gateway_log(watchdog, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "gateway-server.log").write_text("stack trace")
    assert watchdog.has_crash_log(logs_dir, lookback_seconds=300) is True


def test_has_crash_log_true_when_fresh_hermes_log(watchdog, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "hermes.agent.log").write_text("crash")
    assert watchdog.has_crash_log(logs_dir, lookback_seconds=300) is True


def test_has_crash_log_false_when_no_files(watchdog, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    assert watchdog.has_crash_log(logs_dir, lookback_seconds=300) is False


def test_has_crash_log_false_when_dir_missing(watchdog, tmp_path):
    assert watchdog.has_crash_log(tmp_path / "nope", lookback_seconds=300) is False


def test_has_crash_log_false_when_non_gateway_files(watchdog, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "random-other.log").write_text("not related")
    assert watchdog.has_crash_log(logs_dir, lookback_seconds=300) is False


def test_has_crash_log_false_when_stale(watchdog, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    crash = logs_dir / "gateway.crash.log"
    crash.write_text("old crash")
    old = time.time() - 3600
    os.utime(crash, (old, old))

    # 60s lookback — should miss the stale file
    assert watchdog.has_crash_log(logs_dir, lookback_seconds=60) is False
    # But 7200s lookback should find it
    assert watchdog.has_crash_log(logs_dir, lookback_seconds=7200) is True


# ── recent_restarts ──────────────────────────────────────────────────────────


def test_recent_restarts_empty_when_no_state(watchdog, tmp_path):
    assert watchdog.recent_restarts(tmp_path / "missing.json",
                                     window_seconds=3600) == []


def test_recent_restarts_filters_by_window(watchdog, tmp_path):
    state_file = tmp_path / "state.json"
    now = time.time()
    state_file.write_text(json.dumps({"restarts": [now - 60, now - 1800, now - 7200]}))

    recent = watchdog.recent_restarts(state_file, window_seconds=3600)
    # 60s ago (in) + 1800s ago (in) = 2; 7200s ago (out)
    assert len(recent) == 2
    assert recent == sorted(recent)


def test_recent_restarts_handles_corrupt_state(watchdog, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("not json at all")
    assert watchdog.recent_restarts(state_file, window_seconds=3600) == []


def test_recent_restarts_handles_restarts_not_list(watchdog, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"restarts": "garbage"}))
    assert watchdog.recent_restarts(state_file, window_seconds=3600) == []


def test_recent_restarts_filters_non_numeric_entries(watchdog, tmp_path):
    state_file = tmp_path / "state.json"
    now = time.time()
    state_file.write_text(json.dumps(
        {"restarts": [now - 60, "corrupt", None, now - 10]}
    ))
    recent = watchdog.recent_restarts(state_file, window_seconds=3600)
    assert len(recent) == 2


# ── backoff_seconds ──────────────────────────────────────────────────────────


def test_backoff_0_when_never_restarted(watchdog):
    assert watchdog.backoff_seconds(0, base=10, cap=300) == 0


def test_backoff_base_for_first_restart(watchdog):
    assert watchdog.backoff_seconds(1, base=10, cap=300) == 10


def test_backoff_doubles_each_time(watchdog):
    assert watchdog.backoff_seconds(2, base=10, cap=300) == 20
    assert watchdog.backoff_seconds(3, base=10, cap=300) == 40
    assert watchdog.backoff_seconds(4, base=10, cap=300) == 80


def test_backoff_capped_at_max(watchdog):
    # 10 * 2^10 = 10240, capped at 300
    assert watchdog.backoff_seconds(20, base=10, cap=300) == 300
    assert watchdog.backoff_seconds(100, base=10, cap=300) == 300


def test_backoff_respects_custom_base(watchdog):
    # 5 * 2^2 = 20
    assert watchdog.backoff_seconds(3, base=5, cap=300) == 20


# ── decide_action ────────────────────────────────────────────────────────────


def test_decide_noop_when_running(watchdog):
    assert watchdog.decide_action(True, False, 0, 3) == "noop"


def test_decide_respects_stop_marker(watchdog):
    assert watchdog.decide_action(False, True, 0, 3) == "respect_stop"


def test_decide_rate_limited_when_over_cap(watchdog):
    assert watchdog.decide_action(False, False, 3, 3) == "rate_limited"
    assert watchdog.decide_action(False, False, 4, 3) == "rate_limited"


def test_decide_restart_when_under_cap_and_not_running(watchdog):
    assert watchdog.decide_action(False, False, 0, 3) == "restart"


def test_rate_limit_wins_over_restart(watchdog):
    assert watchdog.decide_action(False, False, 5, 3) == "rate_limited"


def test_running_wins_over_stop_marker(watchdog):
    # A running gateway doesn't need a restart regardless of stop marker
    assert watchdog.decide_action(True, True, 0, 3) == "noop"


# ── update_state_after_restart ───────────────────────────────────────────────


def test_update_state_creates_file_when_missing(watchdog, tmp_path):
    state_file = tmp_path / "subdir" / "state.json"
    watchdog.update_state_after_restart(state_file, max_window=3600)

    data = json.loads(state_file.read_text())
    assert len(data["restarts"]) == 1
    assert time.time() - data["restarts"][0] < 1.0


def test_update_state_appends_timestamp(watchdog, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"restarts": [time.time() - 100]}))

    watchdog.update_state_after_restart(state_file, max_window=3600)
    data = json.loads(state_file.read_text())
    assert len(data["restarts"]) == 2


def test_update_state_prunes_old_restarts(watchdog, tmp_path):
    state_file = tmp_path / "state.json"
    now = time.time()
    # One far in the past (> 1h), one recent
    state_file.write_text(json.dumps(
        {"restarts": [now - 7200, now - 60]}
    ))
    watchdog.update_state_after_restart(state_file, max_window=3600)
    data = json.loads(state_file.read_text())
    # The 2h-old entry is pruned; the 1m-old entry plus new one remain
    assert len(data["restarts"]) == 2
    # Newest should be within 1s of now
    assert data["restarts"][-1] > now - 1


def test_update_state_handles_corrupt_file(watchdog, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("{garbage")
    watchdog.update_state_after_restart(state_file, max_window=3600)
    data = json.loads(state_file.read_text())
    assert len(data["restarts"]) == 1


def test_update_state_handles_restarts_not_list(watchdog, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"restarts": "bad value"}))
    watchdog.update_state_after_restart(state_file, max_window=3600)
    data = json.loads(state_file.read_text())
    assert len(data["restarts"]) == 1


# ── main() integration with stub `hermes` ─────────────────────────────────────


def _make_hermes_stub(tmp_path: Path, status: str, restart_rc: int = 0) -> tuple[Path, Path]:
    """Build a stub `hermes` bash script.

    status: 'running' | 'not running' — what `hermes gateway status` prints
    restart_rc: exit code for `hermes gateway restart`

    Returns (bin_dir, calls_log).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "hermes-calls.log"
    script = bin_dir / "hermes"
    script.write_text(
        f'#!/bin/bash\n'
        f'echo "$@" >> {str(calls)!r}\n'
        f'if [ "$1" = "gateway" ] && [ "$2" = "status" ]; then\n'
        f'  echo "Gateway is {status}"\n'
        f'  exit 0\n'
        f'fi\n'
        f'if [ "$1" = "gateway" ] && [ "$2" = "restart" ]; then\n'
        f'  exit {restart_rc}\n'
        f'fi\n'
    )
    script.chmod(0o755)
    return bin_dir, calls


def test_main_restarts_dead_gateway(watchdog, tmp_path, monkeypatch):
    """When gateway is down, no stop marker, no rate limit → restart happens."""
    stub_dir, calls_log = _make_hermes_stub(tmp_path, "not running", restart_rc=0)
    monkeypatch.setenv("PATH", f"{stub_dir}:{os.environ.get('PATH', '')}")

    state_file = tmp_path / "state.json"
    stop_marker = tmp_path / "stop"
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    rc = watchdog.main([
        "--state-file", str(state_file),
        "--stop-marker", str(stop_marker),
        "--logs-dir", str(logs_dir),
        "--no-dispatch",
    ])
    assert rc == 0
    assert calls_log.exists()
    text = calls_log.read_text()
    assert any("gateway restart" in line for line in text.splitlines())
    # State file should contain the new restart record
    data = json.loads(state_file.read_text())
    assert len(data["restarts"]) == 1


def test_main_skips_restart_when_running(watchdog, tmp_path, monkeypatch):
    """When gateway is already running, main() exits cleanly without restart."""
    stub_dir, calls_log = _make_hermes_stub(tmp_path, "running")
    monkeypatch.setenv("PATH", f"{stub_dir}:{os.environ.get('PATH', '')}")

    state_file = tmp_path / "state.json"
    stop_marker = tmp_path / "stop"
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    rc = watchdog.main([
        "--state-file", str(state_file),
        "--stop-marker", str(stop_marker),
        "--logs-dir", str(logs_dir),
        "--no-dispatch",
    ])
    assert rc == 0
    # Either no calls at all, or no 'gateway restart' call
    if calls_log.exists():
        text = calls_log.read_text()
        assert not any("gateway restart" in line for line in text.splitlines())
    # No state file created (no restart happened)
    assert not state_file.exists()


def test_main_respects_stop_marker(watchdog, tmp_path, monkeypatch):
    """STOP marker → no restart attempted regardless of gateway state."""
    stub_dir, calls_log = _make_hermes_stub(tmp_path, "not running")
    monkeypatch.setenv("PATH", f"{stub_dir}:{os.environ.get('PATH', '')}")

    state_file = tmp_path / "state.json"
    stop_marker = tmp_path / "stop"
    stop_marker.write_text("yes")  # marker exists
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    rc = watchdog.main([
        "--state-file", str(state_file),
        "--stop-marker", str(stop_marker),
        "--logs-dir", str(logs_dir),
        "--no-dispatch",
    ])
    assert rc == 0
    if calls_log.exists():
        text = calls_log.read_text()
        assert not any("gateway restart" in line for line in text.splitlines())


def test_main_rate_limits_after_max_restarts(watchdog, tmp_path, monkeypatch):
    """After 3 restarts in the window (default max), no more restarts."""
    stub_dir, calls_log = _make_hermes_stub(tmp_path, "not running")
    monkeypatch.setenv("PATH", f"{stub_dir}:{os.environ.get('PATH', '')}")

    state_file = tmp_path / "state.json"
    # Seed state with 3 restarts all inside the 3600s window
    now = time.time()
    state_file.write_text(json.dumps({
        "restarts": [now - 60, now - 30, now - 10]
    }))
    stop_marker = tmp_path / "stop"
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    rc = watchdog.main([
        "--state-file", str(state_file),
        "--stop-marker", str(stop_marker),
        "--logs-dir", str(logs_dir),
        "--no-dispatch",
    ])
    assert rc == 0
    # No additional restart should have been issued
    if calls_log.exists():
        text = calls_log.read_text()
        assert not any("gateway restart" in line for line in text.splitlines())
    # State unchanged
    data = json.loads(state_file.read_text())
    assert len(data["restarts"]) == 3


def test_main_does_not_record_state_on_failed_restart(watchdog, tmp_path, monkeypatch):
    """Failed restart (exit code 1) must not count against the rate limit."""
    stub_dir, calls_log = _make_hermes_stub(tmp_path, "not running", restart_rc=1)
    monkeypatch.setenv("PATH", f"{stub_dir}:{os.environ.get('PATH', '')}")

    state_file = tmp_path / "state.json"
    stop_marker = tmp_path / "stop"
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    rc = watchdog.main([
        "--state-file", str(state_file),
        "--stop-marker", str(stop_marker),
        "--logs-dir", str(logs_dir),
        "--no-dispatch",
    ])
    assert rc == 1
    # No state file written (restart was not successful)
    assert not state_file.exists()

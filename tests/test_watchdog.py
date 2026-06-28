"""Unit tests for watchdog module.

Run with: pytest tests/test_watchdog.py -v
"""

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure scripts/ is importable
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from watchdog import (
    check_gateway,
    decide_restart,
    load_config,
    load_state,
    prune_restarts,
    record_restart,
    run_watchdog,
    save_state,
    write_alert,
)


# ---------------------------------------------------------------------------
# FakeGateway: stubs the probe/status/stale/restart callbacks
# ---------------------------------------------------------------------------
class FakeGateway:
    def __init__(self, probe_alive=True, status_running=True, dispatch_is_stale=False, restart_fails=False):
        self._probe_alive = probe_alive
        self._status_running = status_running  # True=running; False=not running; None=CLI missing
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


# ---------------------------------------------------------------------------
# Temp helpers
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
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def _make_cfg(tmp, **overrides):
    return tmp.cfg(**overrides)


# ---------------------------------------------------------------------------
# State file tests
# ---------------------------------------------------------------------------
def test_load_state_missing_file():
    tmp = TempStateDir()
    assert load_state(tmp.state_file) == {
        "restarts": [],
        "last_restart": 0,
        "last_alert_sent": 0,
    }
    tmp.cleanup()


def test_load_state_corrupt_file():
    tmp = TempStateDir()
    tmp.state_file.write_text("{invalid json")
    assert load_state(tmp.state_file) == {
        "restarts": [],
        "last_restart": 0,
        "last_alert_sent": 0,
    }
    tmp.cleanup()


def test_save_and_load_state_roundtrip():
    tmp = TempStateDir()
    state = {
        "restarts": [{"timestamp": 1000, "profile": "DEFAULT"}],
        "last_restart": 1000,
        "last_alert_sent": 0,
    }
    save_state(tmp.state_file, state)
    assert load_state(tmp.state_file) == state
    tmp.cleanup()


def test_save_state_no_tmp_leftover():
    tmp = TempStateDir()
    save_state(tmp.state_file, {"restarts": [], "last_restart": 0, "last_alert_sent": 0})
    assert not list(tmp.state_file.parent.glob(".watchdog-*.tmp"))
    tmp.cleanup()


# ---------------------------------------------------------------------------
# Pruning logic
# ---------------------------------------------------------------------------
def test_prune_restarts_within_window():
    restarts = [
        {"timestamp": 100, "profile": "DEFAULT"},
        {"timestamp": 200, "profile": "DEFAULT"},
        {"timestamp": 300, "profile": "DEFAULT"},
    ]
    # cutoff = 350 - 100 = 250; only entries with timestamp > 250 remain
    pruned = prune_restarts(restarts, now=350, window=100)
    assert len(pruned) == 1
    assert pruned[0]["timestamp"] == 300


def test_prune_restarts_all_expired():
    restarts = [
        {"timestamp": 100, "profile": "DEFAULT"},
        {"timestamp": 200, "profile": "DEFAULT"},
    ]
    pruned = prune_restarts(restarts, now=1000, window=50)
    assert pruned == []


def test_prune_restarts_empty_input():
    assert prune_restarts([], now=1000, window=3600) == []


# ---------------------------------------------------------------------------
# Rate-limit decision
# ---------------------------------------------------------------------------
def test_decide_restart_allowed_on_empty_state():
    state = {"restarts": [], "last_restart": 0, "last_alert_sent": 0}
    allowed, _new, reason = decide_restart(state, now=1000, max_n=3, window=3600, cooldown=600)
    assert allowed is True
    assert reason == ""


def test_decide_restart_rate_limit_exhausted():
    state = {
        "restarts": [
            {"timestamp": 900, "profile": "DEFAULT"},
            {"timestamp": 950, "profile": "DEFAULT"},
            {"timestamp": 980, "profile": "DEFAULT"},
        ],
        "last_restart": 980,
        "last_alert_sent": 0,
    }
    allowed, _new, reason = decide_restart(state, now=1000, max_n=3, window=3600, cooldown=600)
    assert allowed is False
    assert reason == "rate_limit"
    # All 3 restarts at t=900/950/980 are within the 3600s window of now=1000
    # (cutoff = 1000-3600 = -2600), so all are retained in state.
    assert len(_new["restarts"]) == 3


def test_decide_restart_cooldown_active():
    state = {
        "restarts": [{"timestamp": 900, "profile": "DEFAULT"}],
        "last_restart": 900,
        "last_alert_sent": 0,
    }
    # last_restart (900) + cooldown (600) = 1500 > now (1000), so throttled
    allowed, _new, reason = decide_restart(state, now=1000, max_n=3, window=3600, cooldown=600)
    assert allowed is False
    assert reason == "cooldown"


def test_decide_restart_cooldown_expired():
    state = {
        "restarts": [{"timestamp": 300, "profile": "DEFAULT"}],
        "last_restart": 300,
        "last_alert_sent": 0,
    }
    # last_restart (300) + cooldown (600) = 900 <= now (1000), so allowed
    allowed, _new, reason = decide_restart(state, now=1000, max_n=3, window=3600, cooldown=600)
    assert allowed is True
    assert reason == ""


def test_record_restart_appends_entry():
    state = {"restarts": [], "last_restart": 0, "last_alert_sent": 0}
    updated = record_restart(state, now=1000, profile="DEFAULT")
    assert len(updated["restarts"]) == 1
    assert updated["restarts"][0] == {"timestamp": 1000, "profile": "DEFAULT"}
    assert updated["last_restart"] == 1000


# ---------------------------------------------------------------------------
# Gateway check
# ---------------------------------------------------------------------------
def test_check_gateway_healthy():
    gw = FakeGateway(probe_alive=True, status_running=True, dispatch_is_stale=False)
    result = check_gateway(gw.probe, gw.status, gw.stale_fn, port=8900, timeout=5)
    assert result.alive is True
    assert result.has_pid is True
    assert result._dispatch_stale is False


def test_check_gateway_dead_no_pid():
    gw = FakeGateway(probe_alive=False, status_running=False, dispatch_is_stale=False)
    result = check_gateway(gw.probe, gw.status, gw.stale_fn, port=8900, timeout=5)
    assert result.alive is False
    assert result.has_pid is False
    assert result._dispatch_stale is False


def test_check_gateway_zombie():
    gw = FakeGateway(probe_alive=False, status_running=True, dispatch_is_stale=True)
    result = check_gateway(gw.probe, gw.status, gw.stale_fn, port=8900, timeout=5)
    assert result.alive is False
    assert result.has_pid is True
    assert result._dispatch_stale is True


# ---------------------------------------------------------------------------
# Alert writing
# ---------------------------------------------------------------------------
def test_write_alert_creates_file():
    tmp = TempStateDir()
    write_alert(tmp.alert_file, "test alert message")
    content = tmp.alert_file.read_text()
    assert "CRITICAL:" in content
    assert "test alert message" in content
    assert "Timestamp:" in content
    tmp.cleanup()


def test_write_alert_appends_parent_dir_if_missing():
    tmp = TempStateDir()
    nested = Path(tmp.tmpdir) / "nested" / "alert.txt"
    write_alert(nested, "inner alert")
    assert nested.exists()
    tmp.cleanup()


# ---------------------------------------------------------------------------
# Full watchdog orchestration
# ---------------------------------------------------------------------------
def test_watchdog_disabled_when_flag_is_false():
    tmp = TempStateDir()
    cfg = _make_cfg(tmp, enabled=False)
    result = run_watchdog(cfg)
    assert result.enabled is False
    assert result.checked is False
    tmp.cleanup()


def test_watchdog_healthy_gateway_no_restart():
    tmp = TempStateDir()
    cfg = _make_cfg(tmp)
    gw = FakeGateway(probe_alive=True, status_running=True, dispatch_is_stale=False)
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)
    assert result.needed_restart is False
    assert result.restart_attempted is False
    assert gw.restart_calls == 0
    tmp.cleanup()


def test_watchdog_dead_gateway_restart_succeeds():
    tmp = TempStateDir()
    cfg = _make_cfg(tmp)
    gw = FakeGateway(probe_alive=False, status_running=True, dispatch_is_stale=False)
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)
    assert result.needed_restart is True
    assert result.restart_attempted is True
    assert result.restart_succeeded is True
    assert gw.restart_calls == 1
    # Verify state file was persisted
    state = load_state(tmp.state_file)
    assert len(state["restarts"]) == 1
    assert state["last_restart"] == 1000
    tmp.cleanup()


def test_watchdog_rate_limit_exhausted_writes_alert():
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
    cfg = _make_cfg(tmp)
    gw = FakeGateway(probe_alive=False, status_running=True, dispatch_is_stale=False)
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)
    assert result.needed_restart is True
    assert result.restart_attempted is False
    assert result.alert_written is True
    assert tmp.alert_file.exists()
    assert gw.restart_calls == 0
    tmp.cleanup()


def test_watchdog_cooldown_throttles_without_alert():
    tmp = TempStateDir()
    initial_state = {
        "restarts": [{"timestamp": 900, "profile": "DEFAULT"}],
        "last_restart": 900,
        "last_alert_sent": 0,
    }
    save_state(tmp.state_file, initial_state)
    cfg = _make_cfg(tmp)
    gw = FakeGateway(probe_alive=False, status_running=True, dispatch_is_stale=False)
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)
    assert result.needed_restart is True
    assert result.restart_attempted is False
    assert result.alert_written is False
    assert gw.restart_calls == 0
    tmp.cleanup()


def test_watchdog_zombie_detected_and_restarted():
    tmp = TempStateDir()
    cfg = _make_cfg(tmp)
    gw = FakeGateway(probe_alive=False, status_running=True, dispatch_is_stale=True)
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)
    assert result.needed_restart is True
    assert result.restart_attempted is True
    assert result.restart_succeeded is True
    tmp.cleanup()


def test_watchdog_restart_command_failure_recorded():
    tmp = TempStateDir()
    cfg = _make_cfg(tmp)
    gw = FakeGateway(probe_alive=False, status_running=True, dispatch_is_stale=False, restart_fails=True)
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)
    assert result.restart_attempted is True
    assert result.restart_succeeded is False
    # State is still updated even on failure (so the counter ticks)
    state = load_state(tmp.state_file)
    assert len(state["restarts"]) == 1
    tmp.cleanup()


def test_watchdog_no_pid_no_restart_attempt():
    """If status reports 'not running' (no PID), watchdog skips restart (not a gateway profile)."""
    tmp = TempStateDir()
    cfg = _make_cfg(tmp)
    gw = FakeGateway(probe_alive=False, status_running=False, dispatch_is_stale=False)
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)
    # status_running=False → no known PID → watchdog does NOT restart (planner profile)
    assert result.restart_attempted is False
    assert gw.restart_calls == 0
    tmp.cleanup()


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def test_load_config_defaults(monkeypatch):
    for key in [
        "DAEDALUS_GW_ENABLED", "DAEDALUS_GW_HEALTH_PORT",
        "DAEDALUS_GW_HEALTH_TIMEOUT", "DAEDALUS_GW_STALE_THRESHOLD_HOURS",
        "DAEDALUS_GW_MAX_RESTARTS", "DAEDALUS_GW_RESTART_WINDOW_SECS",
        "DAEDALUS_GW_COOLDOWN_SECS", "DAEDALUS_GW_STATE_PATH",
        "DAEDALUS_GW_ALERT_PATH", "DAEDALUS_GW_DRY_RUN",
    ]:
        monkeypatch.delenv(key, raising=False)
    cfg = load_config()
    assert cfg.enabled is True
    assert cfg.health_port == 8900
    assert cfg.health_timeout == 5
    assert cfg.stale_threshold_hours == 2
    assert cfg.max_restarts == 3
    assert cfg.restart_window_secs == 3600
    assert cfg.cooldown_secs == 600


def test_load_config_env_overrides(monkeypatch):
    monkeypatch.setenv("DAEDALUS_GW_ENABLED", "false")
    monkeypatch.setenv("DAEDALUS_GW_HEALTH_PORT", "9000")
    monkeypatch.setenv("DAEDALUS_GW_MAX_RESTARTS", "5")
    cfg = load_config()
    assert cfg.enabled is False
    assert cfg.health_port == 9000
    assert cfg.max_restarts == 5


# ---------------------------------------------------------------------------
# Multi-tick / state-recovery lifecycle
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tick_times", [
    [1000, 1010, 1020, 1030],  # 10-second intervals
    [1000, 1500, 2000, 2500],  # 500-second intervals
])
def test_watchdog_rapid_flapping_hits_rate_limit(tick_times):
    """Simulate rapid failures across 4 ticks: 3 restarts succeed, 4th is rate-limited."""
    tmp = TempStateDir()
    cfg = _make_cfg(tmp, max_restarts=3, cooldown_secs=0, restart_window_secs=3600)
    gw = FakeGateway(probe_alive=False, status_running=True, dispatch_is_stale=False)

    # Ticks 1-3: restarts succeed
    for t in tick_times[:3]:
        result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                              restart_fn=gw.restart, stale_fn=gw.stale_fn, now=t)
        assert result.restart_succeeded is True

    # Tick 4: rate limit triggered (3/3)
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=tick_times[3])
    assert result.restart_attempted is False
    assert result.alert_written is True
    assert gw.restart_calls == 3
    tmp.cleanup()


def test_watchdog_state_recovery_lifecycle():
    """Verify watchdog can restart a dead gateway, then detects recovery on next tick."""
    tmp = TempStateDir()
    cfg = _make_cfg(tmp, cooldown_secs=0)

    # Tick 1: gateway is dead — restart triggered and succeeds
    gw_dead = FakeGateway(probe_alive=False, status_running=True, dispatch_is_stale=False)
    result = run_watchdog(cfg, probe_fn=gw_dead.probe, status_fn=gw_dead.status,
                          restart_fn=gw_dead.restart, stale_fn=gw_dead.stale_fn, now=1000)
    assert result.restart_succeeded is True
    assert gw_dead.restart_calls == 1

    # Tick 2: gateway recovered — no restart needed
    gw_healthy = FakeGateway(probe_alive=True, status_running=True, dispatch_is_stale=False)
    result = run_watchdog(cfg, probe_fn=gw_healthy.probe, status_fn=gw_healthy.status,
                          restart_fn=gw_healthy.restart, stale_fn=gw_healthy.stale_fn, now=1010)
    assert result.restart_attempted is False
    assert gw_healthy.restart_calls == 0
    tmp.cleanup()


def test_watchdog_cooldown_throttle_then_allow():
    """Verify cooldown prevents restart within window, then allows after it expires."""
    tmp = TempStateDir()
    cfg = _make_cfg(tmp, cooldown_secs=600, max_restarts=10)
    gw = FakeGateway(probe_alive=False, status_running=True, dispatch_is_stale=False)

    # Tick 1: restart at t=1000
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)
    assert result.restart_succeeded is True

    # Tick 2: attempt at t=1300 (within cooldown) — blocked silently
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1300)
    assert result.restart_attempted is False
    assert result.alert_written is False  # cooldown, not rate limit

    # Tick 3: attempt at t=1700 (> cooldown from t=1000) — allowed
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1700)
    assert result.restart_attempted is True
    assert result.restart_succeeded is True
    assert gw.restart_calls == 2
    tmp.cleanup()


# ---------------------------------------------------------------------------
# Timeout boundaries and precision
# ---------------------------------------------------------------------------
def test_prune_restarts_exact_boundary_excluded():
    """Restart timestamp exactly at cutoff is excluded (cutoff uses strict >)."""
    restarts = [{"timestamp": 1000, "profile": "DEFAULT"}, {"timestamp": 1001, "profile": "DEFAULT"}]
    pruned = prune_restarts(restarts, now=2000, window=1000)
    # cutoff = 2000 - 1000 = 1000; ts=1000 NOT > 1000 (excluded), ts=1001 > 1000 (included)
    assert len(pruned) == 1
    assert pruned[0]["timestamp"] == 1001


def test_prune_restarts_non_numeric_timestamp_skipped():
    """Non-numeric / non-JSON-serializable timestamps are filtered out cleanly."""
    restarts = [
        {"timestamp": 1000, "profile": "DEFAULT"},
        {"timestamp": "invalid", "profile": "DEFAULT"},
        {"timestamp": None, "profile": "DEFAULT"},
    ]
    pruned = prune_restarts(restarts, now=2000, window=3600)
    # Only the numeric entry survives; 'invalid' and None are filtered via int() cast
    assert len(pruned) == 1
    assert pruned[0]["timestamp"] == 1000


# ---------------------------------------------------------------------------
# Rate-limit decision precision
# ---------------------------------------------------------------------------
def test_decide_restart_exactly_at_max_n():
    """Exactly at max_n → denied."""
    state = {
        "restarts": [{"timestamp": 900 * i, "profile": "DEFAULT"} for i in range(1, 4)],
        "last_restart": 2700,
        "last_alert_sent": 0,
    }
    allowed, _, reason = decide_restart(state, now=4000, max_n=3, window=5000, cooldown=0)
    assert allowed is False
    assert reason == "rate_limit"


def test_decide_restart_one_below_max_n():
    """One below max_n → allowed (if cooldown OK)."""
    state = {
        "restarts": [{"timestamp": 900, "profile": "DEFAULT"}, {"timestamp": 1000, "profile": "DEFAULT"}],
        "last_restart": 1000,
        "last_alert_sent": 0,
    }
    allowed, _, reason = decide_restart(state, now=5000, max_n=3, window=3600, cooldown=0)
    assert allowed is True
    assert reason == ""


def test_decide_restart_cooldown_boundary():
    """Exactly at cooldown boundary — one second past cooldown is allowed."""
    state = {"restarts": [{"timestamp": 1000, "profile": "DEFAULT"}],
             "last_restart": 1000, "last_alert_sent": 0}

    # Now=1599 → elapsed=599 → (599 < 600) → still in cooldown
    allowed, _, reason = decide_restart(state, now=1599, max_n=3, window=3600, cooldown=600)
    assert allowed is False
    assert reason == "cooldown"

    # Now=1600 → elapsed=600 → (600 < 600) is False → allowed
    allowed, _, reason = decide_restart(state, now=1600, max_n=3, window=3600, cooldown=600)
    assert allowed is True
    assert reason == ""


def test_decide_restart_handles_malformed_state():
    """decide_restart tolerates missing keys gracefully."""
    # No 'restarts' key
    state_no_restarts = {"last_restart": 1000}
    allowed, _, _ = decide_restart(state_no_restarts, now=2000, max_n=3, window=3600, cooldown=0)
    assert allowed is True  # no restarts = not rate-limited

    # No 'last_restart' key (defaults to 0, so cooldown never active)
    state_no_last = {"restarts": [], "last_alert_sent": 0}
    allowed, _, _ = decide_restart(state_no_last, now=2000, max_n=3, window=3600, cooldown=600)
    assert allowed is True

    # Completely empty state
    allowed, _, _ = decide_restart({}, now=2000, max_n=3, window=3600, cooldown=600)
    assert allowed is True


# ---------------------------------------------------------------------------
# Logging output
# ---------------------------------------------------------------------------
def test_watchdog_logs_healthy_message(capsys):
    """Verify 'gateway healthy' log entry on healthy path."""
    import sys
    tmp = TempStateDir()
    cfg = _make_cfg(tmp)
    gw = FakeGateway(probe_alive=True, status_running=True, dispatch_is_stale=False)
    run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                 stale_fn=gw.stale_fn, now=1000, out=sys.stderr)
    captured = capsys.readouterr()
    assert "gateway healthy" in captured.err
    assert "alive=True" in captured.err
    tmp.cleanup()


def test_watchdog_logs_down_and_restart_attempt(capsys):
    """Verify 'gateway DOWN' + 'attempting restart' logged on failure."""
    import sys
    tmp = TempStateDir()
    cfg = _make_cfg(tmp)
    gw = FakeGateway(probe_alive=False, status_running=True, dispatch_is_stale=False)
    run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                 restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000, out=sys.stderr)
    captured = capsys.readouterr()
    assert "gateway DOWN" in captured.err
    assert "attempting restart" in captured.err
    assert "restart succeeded" in captured.err
    tmp.cleanup()


def test_watchdog_logs_rate_limit_message(capsys):
    """Verify 'limit exhausted' message when rate limit triggers."""
    import sys
    tmp = TempStateDir()
    # Set timestamps WITHIN the 3600s window relative to now=5000 (cutoff=1400)
    initial_state = {
        "restarts": [
            {"timestamp": 2000, "profile": "DEFAULT"},
            {"timestamp": 3000, "profile": "DEFAULT"},
            {"timestamp": 4000, "profile": "DEFAULT"},
        ],
        "last_restart": 4000,
        "last_alert_sent": 0,
    }
    save_state(tmp.state_file, initial_state)
    cfg = _make_cfg(tmp, max_restarts=3)
    gw = FakeGateway(probe_alive=False, status_running=True, dispatch_is_stale=False)
    run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                 restart_fn=gw.restart, stale_fn=gw.stale_fn, now=5000, out=sys.stderr)
    captured = capsys.readouterr()
    assert "limit exhausted" in captured.err
    assert "Manual intervention required" in captured.err
    tmp.cleanup()


# ---------------------------------------------------------------------------
# Alert content validation
# ---------------------------------------------------------------------------
def test_alert_message_format_and_structure():
    """Verify alert file has CRITICAL: prefix and valid ISO timestamp."""
    tmp = TempStateDir()
    msg = "Gateway watchdog — restart limit exhausted (3/3600s), profile=DEFAULT. Manual intervention required."
    write_alert(tmp.alert_file, msg)
    content = tmp.alert_file.read_text()

    assert content.startswith("CRITICAL:")
    assert msg in content
    lines = content.strip().split("\n")
    assert len(lines) == 2
    # Second line is "Timestamp: <iso>"
    assert lines[1].startswith("Timestamp:")
    # ISO format contains 'T' delimiter
    assert "T" in lines[1]
    tmp.cleanup()


def test_alert_overwrites_not_appends():
    """Writing a second alert replaces the first (no stacking)."""
    tmp = TempStateDir()
    write_alert(tmp.alert_file, "first alert message")
    first = tmp.alert_file.read_text()

    write_alert(tmp.alert_file, "second alert message")
    second = tmp.alert_file.read_text()

    assert "first alert message" not in second
    assert "second alert message" in second
    assert first != second
    tmp.cleanup()


# ---------------------------------------------------------------------------
# check_gateway edge cases — stale_fn invocation logic
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("probe,status,expected_stale_call", [
    # probe OK + status OK: stale_fn is called (silent goroutine death detection)
    (True, True, True),
    # probe dead + status OK (zombie): stale_fn is called
    (False, True, True),
    # probe dead + status False (no PID): stale_fn is NOT called
    (False, False, False),
    # probe dead + status None (CLI missing): stale_fn is NOT called
    (False, None, False),
    # probe OK + status False (inconsistent): stale_fn is NOT called (probe=True + status=False has no PID)
    (True, False, False),
    # probe OK + status None: stale_fn is NOT called
    (True, None, False),
])
def test_check_gateway_stale_fn_invocation(probe, status, expected_stale_call):
    """Verify stale_fn called only when meaningful (has PID or probe OK)."""
    call_count = {"count": 0}

    def probe_fn(port, timeout):
        return probe

    def status_fn():
        return status

    def stale_fn():
        call_count["count"] += 1
        return False

    check_gateway(probe_fn, status_fn, stale_fn, port=8900, timeout=5)
    called = call_count["count"] > 0
    # The stale_fn call depends on whether the gateway has a PID or probe succeeded
    # and whether probe is failing (suggesting zombie) or succeeding (but dispatch stale)
    if expected_stale_call:
        assert called, f"stale_fn should have been called (probe={probe}, status={status})"
    else:
        assert not called, f"stale_fn should NOT have been called (probe={probe}, status={status})"


# ---------------------------------------------------------------------------
# State persistence edge cases
# ---------------------------------------------------------------------------
def test_state_atomic_write_no_partial_data_or_tmp_files():
    """Atomic save_state leaves no .tmp residuals and produces valid JSON."""
    tmp = TempStateDir()
    state = {"restarts": [{"timestamp": 1000, "profile": "DEFAULT"}],
             "last_restart": 1000, "last_alert_sent": 0}
    save_state(tmp.state_file, state)

    # No .tmp residuals
    residuals = list(tmp.state_file.parent.glob(".watchdog-*.tmp"))
    assert residuals == []

    # Final file is valid JSON and round-trips correctly
    loaded = load_state(tmp.state_file)
    assert loaded == state
    tmp.cleanup()


def test_record_restart_updates_both_fields():
    """record_restart must update both restarts list AND last_restart atomically."""
    state = {"restarts": [], "last_restart": 0, "last_alert_sent": 0}
    updated = record_restart(state, now=5000, profile="test-profile")

    assert len(updated["restarts"]) == 1
    assert updated["restarts"][0] == {"timestamp": 5000, "profile": "test-profile"}
    assert updated["last_restart"] == 5000
    assert updated["last_alert_sent"] == 0  # unchanged by this function

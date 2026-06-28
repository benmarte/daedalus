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

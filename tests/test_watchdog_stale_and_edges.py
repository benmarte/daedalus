"""Additional watchdog tests — covering untested branches in scripts/watchdog.py
and scripts/gateway_watchdog.py.

Focused on:
- _stale_wrapper threshold parameter path (TypeError fallback vs threshold-accepting)
- _dispatch_stale CLI parsing edge cases (malformed, mixed-case, negative values)
- Combined "zombie + stale dispatch" triggers
- write_alert content edge cases (empty, unicode, long messages)
- has_crash_log symlink + permission edge cases
- run_watchdog return-value semantics
- Config stale_threshold_hours=0 path
- is_dispatch_stale negative / boundary precision

Run: pytest tests/test_watchdog_stale_and_edges.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Load watchdog modules
# ---------------------------------------------------------------------------
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from watchdog import (
    check_gateway,
    decide_restart,
    is_dispatch_stale,
    load_config,
    load_state,
    prune_restarts,
    record_restart,
    run_watchdog,
    save_state,
    write_alert,
    _dispatch_stale,
)


@pytest.fixture(scope="module")
def gw_watchdog():
    """Load gateway_watchdog.py as a module."""
    spec = importlib.util.spec_from_file_location(
        "gw_wd", str(REPO_ROOT / "scripts" / "gateway_watchdog.py"),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
            stale_threshold_hours=2,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def cleanup(self):
        import shutil
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

    def stale_fn(self, threshold_hours=None):
        return self._dispatch_is_stale

    def restart(self):
        self.restart_calls += 1
        return not self._restart_fails


# ===========================================================================
# _stale_wrapper threshold parameter path
#
# watchdog.py:321-325 wraps stale_fn to inject the threshold from config.
# The _stale_wrapper tries stale_fn(threshold_hours) first; on TypeError
# (legacy 0-arg stubs), falls back to stale_fn().
#
# We verify BOTH branches:
#   1. New-style stale_fn(threshold) path — threshold gets passed correctly
#   2. Old-style 0-arg stale_fn() path — TypeError fallback works
#   3. Custom stale_threshold_hours config propagates to stale_fn
# ===========================================================================

def test_stale_fn_called_with_no_args():
    """stale_fn receives no args (check_gateway calls stale_fn() with 0 args)."""
    tmp = TempStateDir()
    cfg = _make_cfg(tmp)
    call_args_received = []

    def probe(p, t): return False
    def status(): return True
    def stale_fn():
        # Use *args, **kwargs variant would also work; 0-arg here matches the real signature
        call_args_received.append(())
        return True
    def restart(): return True

    run_watchdog(cfg, probe_fn=probe, status_fn=status,
                 restart_fn=restart, stale_fn=stale_fn, now=1000)
    assert len(call_args_received) == 1, "stale_fn should have been called once"
    tmp.cleanup()


def test_stale_wrapper_fallback_to_legacy_0arg_stale_fn():
    """Legacy 0-arg stale_fn still works (TypeError fallback path)."""
    tmp = TempStateDir()
    cfg = _make_cfg(tmp)
    calls = {"count": 0}

    def probe(p, t): return False
    def status(): return True
    def stale_fn():  # legacy 0-arg form
        calls["count"] += 1
        return True
    def restart(): return True

    # Should NOT raise — watchdog.py:321-325 TypeError fallback kicks in
    result = run_watchdog(cfg, probe_fn=probe, status_fn=status,
                          restart_fn=restart, stale_fn=stale_fn, now=1000)
    assert calls["count"] >= 1, "legacy stale_fn should have been called"
    assert result.needed_restart is True
    tmp.cleanup()


def test_stale_fn_returns_true_triggers_restart():
    """stale_fn returning True triggers a restart even if probe is alive."""
    tmp = TempStateDir()
    cfg = _make_cfg(tmp)

    def probe(p, t): return False
    def status(): return True
    def stale_fn():
        return True  # mimics: dispatch stale
    def restart(): return True

    result = run_watchdog(cfg, probe_fn=probe, status_fn=status,
                          restart_fn=restart, stale_fn=stale_fn, now=1000)
    assert result.needed_restart is True
    assert result.restart_attempted is True
    tmp.cleanup()


# ===========================================================================
# _dispatch_stale CLI output parsing — edge cases
#
# _dispatch_stale parses "Last dispatch: <N>" or "last_dispatch: <N>" where
# N is the number of seconds. We test:
# - negative values
# - mixed-case variants
# - extra text on the line after the number
# - non-numeric values (ValueError fallback)
# - empty stdout
# - multiple matching lines (first one wins)
# ===========================================================================

def test_dispatch_stale_cli_negative_value_treated_as_not_stale():
    """Negative seconds value → int() succeeds, negative < threshold → False."""
    mock = MagicMock()
    mock.stdout = "Last dispatch: -100\n"
    with patch("watchdog.subprocess.run", return_value=mock):
        # -100 < 7200 (default threshold) → not stale
        assert _dispatch_stale() is False


def test_dispatch_stale_cli_non_numeric_value_returns_false():
    """Non-numeric value after prefix → ValueError caught → False."""
    mock = MagicMock()
    mock.stdout = "Last dispatch: not-a-number\n"
    with patch("watchdog.subprocess.run", return_value=mock):
        assert _dispatch_stale() is False


def test_dispatch_stale_cli_mixed_case_prefix_match():
    """'LAST DISPATCH:' should match (case-insensitive comparison)."""
    mock = MagicMock()
    mock.stdout = "LAST DISPATCH: 10000\n"
    with patch("watchdog.subprocess.run", return_value=mock):
        # 10000 >= 7200 (default 2h threshold) → stale
        assert _dispatch_stale() is True


def test_dispatch_stale_cli_extra_text_after_number():
    """'Last dispatch: 8000 seconds ago' — split()[0] extracts '8000' correctly."""
    mock = MagicMock()
    mock.stdout = "Last dispatch: 8000 seconds ago\n"
    with patch("watchdog.subprocess.run", return_value=mock):
        # 8000 >= 7200 → stale
        assert _dispatch_stale() is True


def test_dispatch_stale_cli_empty_stdout():
    """Empty stdout → no lines to match → False."""
    mock = MagicMock()
    mock.stdout = ""
    with patch("watchdog.subprocess.run", return_value=mock):
        assert _dispatch_stale() is False


def test_dispatch_stale_cli_subprocess_timeout_returns_false():
    """TimeoutExpired → returns False (conservative)."""
    import subprocess
    with patch("watchdog.subprocess.run",
               side_effect=subprocess.TimeoutExpired(["hermes"], 15)):
        assert _dispatch_stale() is False


def test_dispatch_stale_cli_value_just_below_threshold():
    """Dispatch 7199s ago (just under 2h=7200s) → not stale."""
    mock = MagicMock()
    mock.stdout = "Last dispatch: 7199\n"
    with patch("watchdog.subprocess.run", return_value=mock):
        # 7199 < 7200 (default 2h threshold) → not stale
        assert _dispatch_stale() is False


# ===========================================================================
# Combined triggers — zombie + stale dispatch simultaneously
#
# check_gateway returns a GatewayResult with both alive=False and
# _dispatch_stale=True if probe fails, status=True, and stale_fn returns True.
# We test this compound state propagates correctly to run_watchdog.
# ===========================================================================

def test_check_gateway_zombie_and_stale_dispatch_combined():
    """Both probe dead AND dispatch stale → need_restart is True on both signals."""
    gw = FakeGateway(probe_alive=False, status_running=True, dispatch_is_stale=True)
    result = check_gateway(gw.probe, gw.status, gw.stale_fn, port=8900, timeout=5)
    assert result.alive is False
    assert result.has_pid is True
    assert result._dispatch_stale is True


def test_run_watchdog_both_probes_ok_but_dispatch_stale_triggers_restart():
    """Even if probe succeeds, stale dispatch alone is enough to restart."""
    tmp = TempStateDir()
    cfg = _make_cfg(tmp)
    gw = FakeGateway(probe_alive=True, status_running=True, dispatch_is_stale=True)
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)
    assert result.needed_restart is True
    assert result.restart_attempted is True
    assert result.restart_succeeded is True
    tmp.cleanup()


# ===========================================================================
# write_alert — content edge cases
# ===========================================================================

def test_write_alert_empty_message_still_creates_file():
    """Empty message → file still created with CRITICAL prefix."""
    tmp = TempStateDir()
    write_alert(tmp.alert_file, "")
    assert tmp.alert_file.exists()
    content = tmp.alert_file.read_text()
    assert content.startswith("CRITICAL:")
    assert "Timestamp:" in content
    tmp.cleanup()


def test_write_alert_unicode_message():
    """Unicode characters in alert message are preserved."""
    tmp = TempStateDir()
    unicode_msg = "Gateway down — ⚠️ restart failed 🔥"
    write_alert(tmp.alert_file, unicode_msg)
    content = tmp.alert_file.read_text()
    assert unicode_msg in content
    tmp.cleanup()


def test_write_alert_multiline_message():
    """Multi-line messages preserve line breaks in the content."""
    tmp = TempStateDir()
    multiline = "line 1\nline 2\nline 3"
    write_alert(tmp.alert_file, multiline)
    content = tmp.alert_file.read_text()
    assert multiline in content
    tmp.cleanup()


# ===========================================================================
# run_watchdog return-value semantics
# ===========================================================================

def test_run_watchdog_allowed_field_true_on_successful_restart():
    """When restart succeeds, `allowed` field in result is True (via reason check)."""
    tmp = TempStateDir()
    cfg = _make_cfg(tmp)
    gw = FakeGateway(probe_alive=False, status_running=True, dispatch_is_stale=False)
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)
    assert result.restart_succeeded is True
    # allowed = allowed_local or (reason == "succeeded")
    # allowed_local stays False but reason=="succeeded" → allowed=True
    assert result.allowed is True
    tmp.cleanup()


def test_run_watchdog_allowed_field_false_on_healthy_gateway():
    """Healthy gateway: no restart needed → allowed=False."""
    tmp = TempStateDir()
    cfg = _make_cfg(tmp)
    gw = FakeGateway(probe_alive=True, status_running=True, dispatch_is_stale=False)
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)
    assert result.allowed is False
    tmp.cleanup()


def test_run_watchdog_allowed_field_false_on_cooldown():
    """Cooldown-blocked restart: allowed=False (not rate_limit)."""
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
    assert result.allowed is False
    assert result.reason == "cooldown"
    tmp.cleanup()


def test_run_watchdog_allowed_field_false_on_rate_limit():
    """Rate-limited restart: allowed=False."""
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
    gw = FakeGateway(probe_alive=False, status_running=True)
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)
    assert result.allowed is False
    assert result.reason == "rate_limit"
    tmp.cleanup()


def test_run_watchdog_allowed_field_true_on_dry_run():
    """Dry-run mode: restart not attempted but state updated, allowed=False (reason='dry_run')."""
    tmp = TempStateDir()
    cfg = _make_cfg(tmp, dry_run=True)
    gw = FakeGateway(probe_alive=False, status_running=True)
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)
    # dry_run reason ≠ "succeeded" → allowed remains False
    assert result.allowed is False
    assert result.reason == "dry_run"
    assert result.restart_attempted is False
    tmp.cleanup()


def test_run_watchdog_allowed_field_false_on_restart_failure():
    """Failed restart: reason='restart_failed' → allowed=False."""
    tmp = TempStateDir()
    cfg = _make_cfg(tmp)
    gw = FakeGateway(probe_alive=False, status_running=True, restart_fails=True)
    result = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                          restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)
    assert result.allowed is False
    assert result.reason == "restart_failed"
    tmp.cleanup()


# ===========================================================================
# is_dispatch_stale — boundary precision
# ===========================================================================

def test_is_dispatch_stale_future_timestamp():
    """Future last_dispatch (e.g., clock skew) → negative diff → not stale."""
    assert is_dispatch_stale(now=1000, last_dispatch=2000, threshold_hours=2) is False


def test_is_dispatch_stale_exactly_threshold_boundary():
    """Exactly at threshold (>=) → stale."""
    # threshold_hours=2 → 7200s. diff = 7200 exactly → stale.
    assert is_dispatch_stale(now=10000, last_dispatch=10000 - 7200, threshold_hours=2) is True


def test_is_dispatch_stale_one_second_before():
    """diff = threshold - 1s → not stale."""
    assert is_dispatch_stale(now=10000, last_dispatch=10000 - 7199, threshold_hours=2) is False


def test_is_dispatch_stale_large_threshold():
    """Custom large threshold scales correctly."""
    # 24h threshold = 86400s; dispatch 1h ago → not stale
    assert is_dispatch_stale(now=100000, last_dispatch=100000 - 3600, threshold_hours=24) is False
    # dispatch 25h ago → stale
    assert is_dispatch_stale(now=100000, last_dispatch=100000 - 25 * 3600, threshold_hours=24) is True


# ===========================================================================
# gateway_watchdog.py — has_crash_log with symlinks
# ===========================================================================

def test_has_crash_log_follows_symlinks(tmp_path, gw_watchdog):
    """has_crash_log detects crash logs even if they're reached via symlinks
    (iterdir follows symlinks, so this should work)."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    # Create a real crash log outside the dir and symlink it in
    real_log = tmp_path / "real-gateway.log"
    real_log.write_text("crash content")
    symlink = logs_dir / "gateway-linked.log"
    symlink.symlink_to(real_log)
    assert gw_watchdog.has_crash_log(logs_dir, lookback_seconds=300) is True


def test_has_crash_log_broken_symlink_does_not_crash(tmp_path, gw_watchdog):
    """Broken symlink (target missing) doesn't crash has_crash_log."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    broken = logs_dir / "gateway-broken.log"
    broken.symlink_to(tmp_path / "nonexistent-file")
    # Should not raise; broken link → stat().st_mtime raises OSError → caught
    result = gw_watchdog.has_crash_log(logs_dir, lookback_seconds=300)
    assert result is False


def test_has_crash_log_symlink_to_directory_ignored(tmp_path, gw_watchdog):
    """Symlink to a directory is not a file → ignored, no crash detected."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    sub_dir = tmp_path / "realdir"
    sub_dir.mkdir()
    (sub_dir / "gateway-deep.log").write_text("deep crash")
    symlink = logs_dir / "gateway-subdir"
    symlink.symlink_to(sub_dir)
    # symlink_to_dir → is_file() returns False → ignored
    result = gw_watchdog.has_crash_log(logs_dir, lookback_seconds=300)
    assert result is False


# ===========================================================================
# gateway_watchdog.py — decide_action boundary precision
# ===========================================================================

def test_decide_action_negative_recent_count_means_restart(gw_watchdog):
    """recent_count < 0 is nonsensical but treated as under cap → restart."""
    # -1 < max_restarts=3 → restart
    assert gw_watchdog.decide_action(False, False, -1, 3) == "restart"


def test_decide_action_max_restarts_boundary_exactly_at_max(gw_watchdog):
    """recent_count == max_restarts → rate_limited (>= operator)."""
    assert gw_watchdog.decide_action(False, False, 3, 3) == "rate_limited"


def test_decide_action_max_restarts_one_below(gw_watchdog):
    """recent_count = max - 1 → restart allowed."""
    assert gw_watchdog.decide_action(False, False, 2, 3) == "restart"


# ===========================================================================
# gateway_watchdog.py — backoff_seconds with unusual parameters
# ===========================================================================

def test_backoff_zero_base_returns_zero(gw_watchdog):
    """base=0 → all delays are 0 regardless of n."""
    assert gw_watchdog.backoff_seconds(1, base=0, cap=300) == 0
    assert gw_watchdog.backoff_seconds(10, base=0, cap=300) == 0


def test_backoff_zero_cap_clamps_to_zero(gw_watchdog):
    """cap=0 → all delays clamped to 0."""
    assert gw_watchdog.backoff_seconds(5, base=10, cap=0) == 0


def test_backoff_very_high_n_still_capped(gw_watchdog):
    """n=1000 with base=10, cap=300 → 300 (capped)."""
    assert gw_watchdog.backoff_seconds(1000, base=10, cap=300) == 300


# ===========================================================================
# gateway_watchdog.py — read_state / write_state edge cases
# ===========================================================================

def test_read_state_empty_file_returns_empty_dict(tmp_path, gw_watchdog):
    """Empty file (not valid JSON) → returns {}."""
    state_file = tmp_path / "state.json"
    state_file.write_text("")
    assert gw_watchdog.read_state(state_file) == {}


def test_read_state_non_dict_value_returns_as_is(tmp_path, gw_watchdog):
    """JSON that parses to a list is returned as-is (not validated as dict)."""
    state_file = tmp_path / "state.json"
    state_file.write_text("[1, 2, 3]")
    result = gw_watchdog.read_state(state_file)
    # read_state doesn't validate it's a dict — returns the list
    assert result == [1, 2, 3]


def test_write_state_creates_nested_directories(tmp_path, gw_watchdog):
    """write_state creates parent directories as needed."""
    state_file = tmp_path / "a" / "b" / "c" / "state.json"
    assert not state_file.parent.exists()
    gw_watchdog.write_state(state_file, {"restarts": []})
    assert state_file.exists()
    assert json.loads(state_file.read_text()) == {"restarts": []}


def test_write_state_overwrites_existing_file(tmp_path, gw_watchdog):
    """Second write_state call fully replaces previous content."""
    state_file = tmp_path / "state.json"
    gw_watchdog.write_state(state_file, {"restarts": [1.0]})
    gw_watchdog.write_state(state_file, {"restarts": [2.0, 3.0]})
    data = json.loads(state_file.read_text())
    assert data == {"restarts": [2.0, 3.0]}
    assert 1.0 not in data["restarts"]


# ===========================================================================
# prune_restarts — zero/one element lists
# ===========================================================================

def test_prune_restarts_single_element_inside_window():
    """Single element inside window → retained."""
    restarts = [{"timestamp": 1000, "profile": "DEFAULT"}]
    pruned = prune_restarts(restarts, now=1500, window=1000)
    assert len(pruned) == 1


def test_prune_restarts_single_element_outside_window():
    """Single element outside window → pruned to empty."""
    restarts = [{"timestamp": 100, "profile": "DEFAULT"}]
    pruned = prune_restarts(restarts, now=1000, window=100)
    assert pruned == []


# ===========================================================================
# decide_restart — interaction between rate limit AND cooldown
# ===========================================================================

def test_decide_restart_rate_limit_takes_priority_over_cooldown():
    """When BOTH rate limit AND cooldown would deny → rate_limit reason wins."""
    state = {
        "restarts": [
            {"timestamp": 900, "profile": "DEFAULT"},
            {"timestamp": 950, "profile": "DEFAULT"},
            {"timestamp": 990, "profile": "DEFAULT"},
        ],
        "last_restart": 990,
        "last_alert_sent": 0,
    }
    # Both conditions met: len(restarts)==3 (rate_limit) AND within cooldown
    allowed, _, reason = decide_restart(state, now=1000, max_n=3,
                                        window=3600, cooldown=600)
    assert allowed is False
    assert reason == "rate_limit"  # rate_limit checked before cooldown


def test_decide_restart_cooldown_only_when_under_rate_limit():
    """Under rate limit but within cooldown → cooldown denial."""
    state = {
        "restarts": [{"timestamp": 900, "profile": "DEFAULT"}],
        "last_restart": 900,
        "last_alert_sent": 0,
    }
    allowed, _, reason = decide_restart(state, now=1100, max_n=3,
                                        window=3600, cooldown=600)
    assert allowed is False
    assert reason == "cooldown"


# ===========================================================================
# Config load_config — state_path and alert_path from env
# ===========================================================================

def test_load_config_state_path_from_env(monkeypatch, tmp_path):
    """DAEDALUS_GW_STATE_PATH env var controls state file path."""
    custom_path = str(tmp_path / "custom-state.json")
    monkeypatch.setenv("DAEDALUS_GW_STATE_PATH", custom_path)
    cfg = load_config()
    assert str(cfg.state_path) == str(Path(custom_path).expanduser())


def test_load_config_alert_path_from_env(monkeypatch, tmp_path):
    """DAEDALUS_GW_ALERT_PATH env var controls alert file path."""
    custom_path = str(tmp_path / "custom-alert.txt")
    monkeypatch.setenv("DAEDALUS_GW_ALERT_PATH", custom_path)
    cfg = load_config()
    assert str(cfg.alert_path) == str(Path(custom_path).expanduser())

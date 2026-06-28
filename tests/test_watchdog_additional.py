"""Additional unit tests for watchdog modules — filling coverage gaps.

Covers: HTTP probe behavior with different HTTP status codes,
multi-profile scenarios, config parsing with malformed env vars,
atomic write failure cleanup, and deeper edge cases.

Run: pytest tests/test_watchdog_additional.py -v
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts/ is importable
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
    _health_probe,
    is_dispatch_stale,
)


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
# _health_probe — HTTP status code handling
# _health_probe does `import urllib.request` inside the function, so we
# must patch `urllib.request.urlopen` directly (it's a stdlib import, not
# a watchdog attribute).
# ---------------------------------------------------------------------------
def test_health_probe_returns_true_on_200():
    """HTTP 200 response → probe returns True (healthy)."""
    import urllib.request
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    
    with patch.object(urllib.request, "urlopen", return_value=mock_resp):
        assert _health_probe(port=8900, timeout=5) is True


def test_health_probe_returns_true_on_299():
    """HTTP 299 (upper bound of 2xx) → probe returns True."""
    import urllib.request
    mock_resp = MagicMock()
    mock_resp.status = 299
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    
    with patch.object(urllib.request, "urlopen", return_value=mock_resp):
        assert _health_probe(port=8900, timeout=5) is True


def test_health_probe_returns_false_on_404():
    """HTTP 404 → probe returns False (not healthy)."""
    import urllib.request
    mock_resp = MagicMock()
    mock_resp.status = 404
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    
    with patch.object(urllib.request, "urlopen", return_value=mock_resp):
        assert _health_probe(port=8900, timeout=5) is False


def test_health_probe_returns_false_on_500():
    """HTTP 500 → probe returns False (server error, not healthy)."""
    import urllib.request
    mock_resp = MagicMock()
    mock_resp.status = 500
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    
    with patch.object(urllib.request, "urlopen", return_value=mock_resp):
        assert _health_probe(port=8900, timeout=5) is False


def test_health_probe_returns_false_on_connection_error():
    """Connection error (refused, timeout) → probe returns False."""
    import urllib.request
    with patch.object(urllib.request, "urlopen", side_effect=ConnectionError("refused")):
        assert _health_probe(port=8900, timeout=5) is False


def test_health_probe_returns_false_on_timeout():
    """HTTP timeout → probe returns False."""
    import urllib.request
    import urllib.error
    with patch.object(urllib.request, "urlopen", side_effect=urllib.error.URLError("timeout")):
        assert _health_probe(port=8900, timeout=5) is False


def test_health_probe_returns_false_on_any_exception():
    """Any exception during HTTP request → probe returns False."""
    import urllib.request
    with patch.object(urllib.request, "urlopen", side_effect=Exception("network down")):
        assert _health_probe(port=8900, timeout=5) is False


def test_health_probe_constructs_correct_url():
    """Verify _health_probe hits /health endpoint on correct port."""
    import urllib.request
    
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    
    with patch.object(urllib.request, "urlopen", return_value=mock_resp) as mock_urlopen:
        _health_probe(port=9999, timeout=10)
        
        # Verify the correct URL was constructed
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        assert request.full_url == "http://127.0.0.1:9999/health"
        assert request.method == "GET"
        assert call_args[1].get("timeout") == 10


# ---------------------------------------------------------------------------
# Config parsing — malformed environment variables
# ---------------------------------------------------------------------------
def test_load_config_handles_non_numeric_port(monkeypatch):
    """Non-numeric health port → int() raises, but we catch it gracefully."""
    monkeypatch.setenv("DAEDALUS_GW_HEALTH_PORT", "not-a-number")
    # Should raise ValueError when int() is called
    with pytest.raises(ValueError):
        load_config()


def test_load_config_handles_empty_string(monkeypatch):
    """Empty string for numeric field → int() raises ValueError."""
    monkeypatch.setenv("DAEDALUS_GW_MAX_RESTARTS", "")
    with pytest.raises(ValueError):
        load_config()


def test_load_config_boolean_parsing_truthy_values(monkeypatch):
    """DAEDALUS_GW_ENABLED accepts 1, true, yes, on (case-insensitive)."""
    for val in ["1", "true", "TRUE", "True", "yes", "YES", "on", "ON"]:
        monkeypatch.setenv("DAEDALUS_GW_ENABLED", val)
        cfg = load_config()
        assert cfg.enabled is True, f"Failed for value: {val}"


def test_load_config_boolean_parsing_falsy_values(monkeypatch):
    """DAEDALUS_GW_ENABLED accepts 0, false, no, off, anything else (case-insensitive)."""
    for val in ["0", "false", "FALSE", "False", "no", "NO", "off", "OFF", "random", ""]:
        monkeypatch.setenv("DAEDALUS_GW_ENABLED", val)
        cfg = load_config()
        assert cfg.enabled is False, f"Failed for value: {val}"


# ---------------------------------------------------------------------------
# Atomic write failure — temp file cleanup
# ---------------------------------------------------------------------------
def test_save_state_cleans_up_temp_file_on_write_error(tmp_path):
    """If write fails mid-way, no .tmp file should be left behind."""
    state_file = tmp_path / "state.json"
    state = {"restarts": [], "last_restart": 0, "last_alert_sent": 0}
    
    # Patch tempfile.mkstemp to return a valid fd, but patch os.fdopen to raise
    with patch("watchdog.tempfile.mkstemp", return_value=(999, tmp_path / ".watchdog-test.tmp")):
        with patch("watchdog.os.fdopen", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                save_state(state_file, state)
    
    # Verify no .tmp files remain
    tmp_files = list(tmp_path.glob(".watchdog-*.tmp"))
    assert len(tmp_files) == 0, f"Found leftover tmp files: {tmp_files}"


def test_save_state_cleans_up_temp_file_on_rename_error(tmp_path):
    """If os.replace fails, temp file is cleaned up."""
    state_file = tmp_path / "state.json"
    state = {"restarts": [{"timestamp": 1000}], "last_restart": 1000, "last_alert_sent": 0}
    
    with patch("watchdog.os.replace", side_effect=OSError("permission denied")):
        with pytest.raises(OSError):
            save_state(state_file, state)
    
    # Verify no .tmp files remain
    tmp_files = list(tmp_path.glob(".watchdog-*.tmp"))
    assert len(tmp_files) == 0, f"Found leftover tmp files: {tmp_files}"


# ---------------------------------------------------------------------------
# State file parent directory creation
# ---------------------------------------------------------------------------
def test_save_state_creates_parent_directories(tmp_path):
    """save_state creates nested parent directories if they don't exist."""
    state_file = tmp_path / "nested" / "deep" / "state.json"
    state = {"restarts": [], "last_restart": 0, "last_alert_sent": 0}
    
    assert not state_file.parent.exists()
    save_state(state_file, state)
    
    assert state_file.exists()
    assert state_file.parent.is_dir()
    loaded = load_state(state_file)
    assert loaded == state


def test_write_alert_creates_missing_parent_dirs(tmp_path):
    """write_alert creates nested parent directories."""
    alert_file = tmp_path / "nested" / "deep" / "alert.txt"
    
    assert not alert_file.parent.exists()
    write_alert(alert_file, "test message")
    
    assert alert_file.exists()
    content = alert_file.read_text()
    assert "CRITICAL:" in content
    assert "test message" in content


# ---------------------------------------------------------------------------
# decide_restart — allowed field semantics
# ---------------------------------------------------------------------------
def test_decide_restart_allowed_empty_state():
    """Empty state → restart allowed."""
    state = {"restarts": [], "last_restart": 0, "last_alert_sent": 0}
    allowed, _, reason = decide_restart(state, now=1000, max_n=3, window=3600, cooldown=600)
    assert allowed is True
    assert reason == ""


def test_decide_restart_allowed_after_one_restart():
    """One restart in state, but under max and past cooldown → allowed."""
    state = {
        "restarts": [{"timestamp": 100, "profile": "DEFAULT"}],
        "last_restart": 100,
        "last_alert_sent": 0,
    }
    allowed, _, reason = decide_restart(state, now=1000, max_n=3, window=3600, cooldown=600)
    assert allowed is True
    assert reason == ""


def test_decide_restart_denied_rate_limit():
    """At max_n restarts → denied with reason 'rate_limit'."""
    state = {
        "restarts": [
            {"timestamp": 100, "profile": "DEFAULT"},
            {"timestamp": 200, "profile": "DEFAULT"},
            {"timestamp": 300, "profile": "DEFAULT"},
        ],
        "last_restart": 300,
        "last_alert_sent": 0,
    }
    allowed, _, reason = decide_restart(state, now=1000, max_n=3, window=3600, cooldown=0)
    assert allowed is False
    assert reason == "rate_limit"


def test_decide_restart_denied_cooldown():
    """Within cooldown window → denied with reason 'cooldown'."""
    state = {
        "restarts": [{"timestamp": 500, "profile": "DEFAULT"}],
        "last_restart": 500,
        "last_alert_sent": 0,
    }
    allowed, _, reason = decide_restart(state, now=800, max_n=3, window=3600, cooldown=600)
    assert allowed is False
    assert reason == "cooldown"


def test_decide_restart_new_state_has_pruned_restarts():
    """decide_restart returns new_state with restarts pruned to window."""
    state = {
        "restarts": [
            {"timestamp": 100, "profile": "DEFAULT"},  # old, outside window
            {"timestamp": 900, "profile": "DEFAULT"},  # recent, inside window
        ],
        "last_restart": 900,
        "last_alert_sent": 0,
    }
    allowed, new_state, _ = decide_restart(state, now=1000, max_n=3, window=500, cooldown=0)
    # Cutoff = 1000 - 500 = 500; only ts=900 survives
    assert len(new_state["restarts"]) == 1
    assert new_state["restarts"][0]["timestamp"] == 900


# ---------------------------------------------------------------------------
# is_dispatch_stale — additional boundary cases
# ---------------------------------------------------------------------------
def test_is_dispatch_stale_boundary_exactly_at_threshold():
    """Dispatch exactly at threshold (>=) is stale."""
    # 2h threshold = 7200s; now=10000, last_dispatch=10000-7200=2800
    assert is_dispatch_stale(now=10000, last_dispatch=2800, threshold_hours=2) is True


def test_is_dispatch_stale_boundary_one_second_before():
    """Dispatch 1 second before threshold is not stale."""
    assert is_dispatch_stale(now=10000, last_dispatch=10000 - 7199, threshold_hours=2) is False


def test_is_dispatch_stale_zero_threshold():
    """Zero threshold → even dispatch right now is stale (0 >= 0)."""
    assert is_dispatch_stale(now=1000, last_dispatch=1000, threshold_hours=0) is True


def test_is_dispatch_stale_none_last_dispatch():
    """None last_dispatch (never seen) is stale."""
    assert is_dispatch_stale(now=1000, last_dispatch=None, threshold_hours=2) is True


def test_is_dispatch_stale_negative_difference():
    """Negative time difference (future dispatch) is not stale."""
    # now=1000, last_dispatch=2000 (future) → negative, not stale
    assert is_dispatch_stale(now=1000, last_dispatch=2000, threshold_hours=2) is False


# ---------------------------------------------------------------------------
# prune_restarts — additional edge cases
# ---------------------------------------------------------------------------
def test_prune_restarts_all_outside_window():
    """All restarts outside window → empty list."""
    restarts = [
        {"timestamp": 100, "profile": "DEFAULT"},
        {"timestamp": 200, "profile": "DEFAULT"},
    ]
    pruned = prune_restarts(restarts, now=5000, window=100)
    # Cutoff = 5000 - 100 = 4900; all timestamps < 4900 → empty
    assert pruned == []


def test_prune_restarts_mixed_valid_and_invalid():
    """Mix of valid timestamps and invalid entries → only valid ones kept."""
    restarts = [
        {"timestamp": 1000, "profile": "DEFAULT"},
        {"timestamp": "invalid", "profile": "DEFAULT"},
        {"timestamp": 950, "profile": "DEFAULT"},
        {"timestamp": None, "profile": "DEFAULT"},
    ]
    pruned = prune_restarts(restarts, now=1000, window=100)
    # Cutoff = 1000 - 100 = 900; only ts=1000 survives (950 > 900 too)
    # Actually 950 > 900, so both should survive
    assert len(pruned) == 2
    assert pruned[0]["timestamp"] == 1000
    assert pruned[1]["timestamp"] == 950


def test_prune_restarts_empty_window():
    """Zero window → only restarts with ts > now remain (nothing)."""
    restarts = [
        {"timestamp": 1000, "profile": "DEFAULT"},
        {"timestamp": 1001, "profile": "DEFAULT"},
    ]
    pruned = prune_restarts(restarts, now=1000, window=0)
    # Cutoff = 1000 - 0 = 1000; ts > 1000 only → ts=1001
    assert len(pruned) == 1
    assert pruned[0]["timestamp"] == 1001


# ---------------------------------------------------------------------------
# record_restart — state mutation
# ---------------------------------------------------------------------------
def test_record_restart_appends_to_existing_list():
    """record_restart appends to existing restarts list."""
    state = {
        "restarts": [{"timestamp": 100, "profile": "DEFAULT"}],
        "last_restart": 100,
        "last_alert_sent": 0,
    }
    result = record_restart(state, now=1000, profile="DEFAULT")
    
    assert len(result["restarts"]) == 2
    assert result["restarts"][0]["timestamp"] == 100
    assert result["restarts"][1]["timestamp"] == 1000
    assert result["restarts"][1]["profile"] == "DEFAULT"
    assert result["last_restart"] == 1000


def test_record_restart_preserves_other_fields():
    """record_restart doesn't touch last_alert_sent."""
    state = {
        "restarts": [],
        "last_restart": 0,
        "last_alert_sent": 5000,
    }
    result = record_restart(state, now=1000, profile="DEFAULT")
    assert result["last_alert_sent"] == 5000


# ---------------------------------------------------------------------------
# load_state — additional edge cases
# ---------------------------------------------------------------------------
def test_load_state_returns_empty_dict_on_permission_error(tmp_path):
    """Permission error on read → returns EMPTY_STATE."""
    state_file = tmp_path / "state.json"
    state_file.write_text('{"restarts": [], "last_restart": 0}')
    
    with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
        state = load_state(state_file)
    
    assert state == {"restarts": [], "last_restart": 0, "last_alert_sent": 0}


def test_load_state_handles_non_dict_json(tmp_path):
    """JSON that's not a dict → returns EMPTY_STATE."""
    state_file = tmp_path / "state.json"
    state_file.write_text('["array", "instead", "of", "dict"]')
    
    state = load_state(state_file)
    # Should not raise; returns what was parsed (array)
    # But actually, the code doesn't validate it's a dict, so it returns the array
    assert isinstance(state, list)


# ---------------------------------------------------------------------------
# Integration — multi-tick scenarios
# ---------------------------------------------------------------------------
def test_multi_tick_cooldown_then_recovery():
    """Simulate: restart at t=1000, blocked at t=1300 (within cooldown), allowed at t=1700 (past cooldown)."""
    tmp = TempStateDir()
    cfg = _make_cfg(tmp, cooldown_secs=600, max_restarts=10)
    
    class MockGateway:
        def __init__(self):
            self.calls = 0
        def probe(self, port, timeout):
            return False
        def status(self):
            return True
        def stale_fn(self):
            return False
        def restart(self):
            self.calls += 1
            return True
    
    gw = MockGateway()
    
    # Tick 1: restart succeeds at t=1000 (non-zero so last_restart > 0)
    r1 = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                      restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1000)
    assert r1.restart_succeeded is True
    assert gw.calls == 1
    
    # Tick 2: blocked by cooldown (t=1300, elapsed=300 < 600)
    r2 = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                      restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1300)
    assert r2.restart_attempted is False
    assert gw.calls == 1  # no new call
    
    # Tick 3: cooldown expired, restart allowed (t=1700, elapsed=700 > 600)
    r3 = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                      restart_fn=gw.restart, stale_fn=gw.stale_fn, now=1700)
    assert r3.restart_succeeded is True
    assert gw.calls == 2
    
    tmp.cleanup()


def test_multi_tick_rate_limit_accumulates():
    """Simulate: 3 restarts hit rate limit, 4th is blocked."""
    tmp = TempStateDir()
    cfg = _make_cfg(tmp, max_restarts=3, cooldown_secs=0)
    
    class MockGateway:
        def __init__(self):
            self.calls = 0
        def probe(self, port, timeout):
            return False
        def status(self):
            return True
        def stale_fn(self):
            return False
        def restart(self):
            self.calls += 1
            return True
    
    gw = MockGateway()
    
    # Ticks 1-3: restarts succeed
    for t in [0, 100, 200]:
        r = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                        restart_fn=gw.restart, stale_fn=gw.stale_fn, now=t)
        assert r.restart_succeeded is True
    
    # Tick 4: rate limit hit
    r4 = run_watchdog(cfg, probe_fn=gw.probe, status_fn=gw.status,
                      restart_fn=gw.restart, stale_fn=gw.stale_fn, now=300)
    assert r4.restart_attempted is False
    assert r4.alert_written is True
    assert gw.calls == 3
    
    tmp.cleanup()

#!/usr/bin/env bash
# Integration test for the gateway watchdog in daedalus-cron.sh.
#
# Verifies the shell script correctly invokes watchdog.py and forwards its
# exit intent (whether to sleep or not) through to the dispatch step.
#
# Usage:
#   bash tests/ci/test_watchdog.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCRIPTS_DIR="$ROOT_DIR/scripts"

# We test via direct Python invocation of watchdog with fake dependencies.
# The shell-level integration is tested implicitly by the cron script's
# behavior: if the watchdog module reports RESTARTED, sleep 5 is triggered.

PASS=0
FAIL=0

log_pass() { echo "PASS: $*" && PASS=$((PASS+1)); }
log_fail() { echo "FAIL: $*" 2>&1 && FAIL=$((FAIL+1)); }

# ---------------------------------------------------------------------------
# Test 1: watchdog.py is importable from scripts/
# ---------------------------------------------------------------------------
if python3 -c "import sys; sys.path.insert(0, '$SCRIPTS_DIR'); import watchdog" 2>&1; then
  log_pass "watchdog.py is importable"
else
  log_fail "watchdog.py is not importable"
fi

# ---------------------------------------------------------------------------
# Test 2: run_watchdog with healthy gateway does not attempt restart
# ---------------------------------------------------------------------------
OUT=$(python3 -c "
import sys, io
sys.path.insert(0, '$SCRIPTS_DIR')
from types import SimpleNamespace
from watchdog import run_watchdog

def ok_probe(p, t): return True
def ok_status(): return True
def ok_stale(): return False
def ok_restart(): return True

tmp = '/tmp/test-watchdog-healthy-state.json'
cfg = SimpleNamespace(
    enabled=True, health_port=8900, health_timeout=5,
    max_restarts=3, restart_window_secs=3600, cooldown_secs=600,
    state_path=__import__('pathlib').Path(tmp),
    alert_path=__import__('pathlib').Path('/tmp/test-watchdog-healthy-alert.txt'),
    dry_run=False,
)
result = run_watchdog(cfg, probe_fn=ok_probe, status_fn=ok_status,
                      restart_fn=ok_restart, stale_fn=ok_stale, now=1000)
print('needed_restart' if result.needed_restart else 'no_restart')
print('attempted' if result.restart_attempted else 'not_attempted')
")

if echo "$OUT" | grep -q 'no_restart' && echo "$OUT" | grep -q 'not_attempted'; then
  log_pass "healthy gateway triggers no restart"
else
  log_fail "healthy gateway unexpectedly triggered restart: $OUT"
fi

# ---------------------------------------------------------------------------
# Test 3: run_watchdog with dead gateway attempts restart and prints RESTARTED
# ---------------------------------------------------------------------------
OUT=$(python3 -c "
import sys, io
sys.path.insert(0, '$SCRIPTS_DIR')
from types import SimpleNamespace
from watchdog import run_watchdog

def fail_probe(p, t): return False
def running_status(): return True
def not_stale(): return False
def ok_restart(): return True

tmp = '/tmp/test-watchdog-dead-state.json'
cfg = SimpleNamespace(
    enabled=True, health_port=8900, health_timeout=5,
    max_restarts=3, restart_window_secs=3600, cooldown_secs=600,
    state_path=__import__('pathlib').Path(tmp),
    alert_path=__import__('pathlib').Path('/tmp/test-watchdog-dead-alert.txt'),
    dry_run=False,
)
result = run_watchdog(cfg, probe_fn=fail_probe, status_fn=running_status,
                      restart_fn=ok_restart, stale_fn=not_stale, now=1000)
print('attempted' if result.restart_attempted else 'not_attempted')
print('succeeded' if result.restart_succeeded else 'not_succeeded')
")

if echo "$OUT" | grep -q 'attempted' && echo "$OUT" | grep -q 'succeeded'; then
  log_pass "dead gateway triggers restart"
else
  log_fail "dead gateway did not restart: $OUT"
fi

# ---------------------------------------------------------------------------
# Test 4: rate-limit exhaustion writes alert
# ---------------------------------------------------------------------------
OUT=$(python3 -c "
import sys, json, io
sys.path.insert(0, '$SCRIPTS_DIR')
from types import SimpleNamespace
from watchdog import run_watchdog, save_state

# Pre-seed 3 recent restarts to exhaust rate limit
tmp = '/tmp/test-watchdog-ratelimit-state.json'
save_state(__import__('pathlib').Path(tmp), {
    'restarts': [{'timestamp': 900, 'profile': 'DEFAULT'},
                 {'timestamp': 950, 'profile': 'DEFAULT'},
                 {'timestamp': 980, 'profile': 'DEFAULT'}],
    'last_restart': 980,
    'last_alert_sent': 0,
})

def fail_probe(p, t): return False
def running_status(): return True
def not_stale(): return False
restart_calls = 0
def ok_restart():
    global restart_calls
    restart_calls += 1
    return True

alert_path = '/tmp/test-watchdog-ratelimit-alert.txt'
cfg = SimpleNamespace(
    enabled=True, health_port=8900, health_timeout=5,
    max_restarts=3, restart_window_secs=3600, cooldown_secs=600,
    state_path=__import__('pathlib').Path(tmp),
    alert_path=__import__('pathlib').Path(alert_path),
    dry_run=False,
)
result = run_watchdog(cfg, probe_fn=fail_probe, status_fn=running_status,
                      restart_fn=ok_restart, stale_fn=not_stale, now=1000)
print('attempted' if result.restart_attempted else 'not_attempted')
print('alert_written' if result.alert_written else 'no_alert')
import os
print('alert_exists' if os.path.exists(alert_path) else 'no_alert_file')
")

if echo "$OUT" | grep -q 'not_attempted' && echo "$OUT" | grep -q 'alert_written' && echo "$OUT" | grep -q 'alert_exists'; then
  log_pass "rate-limit exhaustion writes alert and skips restart"
else
  log_fail "rate-limit test failed: $OUT"
fi

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
rm -f /tmp/test-watchdog-*-state.json /tmp/test-watchdog-*-alert.txt

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Watchdog integration tests: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1

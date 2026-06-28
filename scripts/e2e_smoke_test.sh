#!/usr/bin/env bash
# =============================================================================
# e2e_smoke_test.sh — Full end-to-end smoke test for Daedalus plugin
#
# Validates Daedalus works end-to-end in a clean Hermes environment.
# Steps 1-3 (env setup, plugin load, dashboard checks) are fully active.
# Steps 4-5 (dispatch checks, cron durability) are gated behind issue closures
# (#79, #80, #81) — use skip guards.
#
# Usage:
#   export GITHUB_TOKEN=ghp_xxx
#   bash scripts/e2e_smoke_test.sh
#
# Exit codes:
#   0 — all active steps pass
#   1 — one or more active steps fail
#   2 — skipped steps (informational, not a failure)
# =============================================================================
set -euo pipefail

PASS=0
FAIL=0
SKIP=0

pass()  { PASS=$((PASS+1)); echo "  PASS  $1"; }
fail()  { FAIL=$((FAIL+1)); echo "  FAIL  $1"; }
skip()  { SKIP=$((SKIP+1)); echo "  SKIP  $1 — gated behind $2"; }

# ── Prerequisites ──────────────────────────────────────────────────────────────

if [ -z "${GITHUB_TOKEN:-}" ]; then
  echo "FATAL: GITHUB_TOKEN not set. Export it before running."
  exit 1
fi

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/daedalus"
SCRIPTS_DIR="$HERMES_HOME/scripts"
PROFILES_DIR="$HERMES_HOME/profiles"

echo ""
echo "Daedalus E2E Smoke Test"
echo "========================"
echo "HERMES_HOME: $HERMES_HOME"
echo ""

# ── Step 1: Environment setup ─────────────────────────────────────────────────
echo "--- Step 1: Environment setup ---"

# 1a. Hermes CLI is available
if command -v hermes &>/dev/null; then
  pass "hermes CLI is available"
else
  fail "hermes CLI is not available"
fi

# 1b. Daedalus plugin is installed
if [ -d "$PLUGIN_DIR" ]; then
  pass "Daedalus plugin directory exists"
else
  fail "Daedalus plugin directory missing at $PLUGIN_DIR"
fi

# 1c. Plugin manifest exists and is valid
if [ -f "$PLUGIN_DIR/plugin.yaml" ]; then
  pass "plugin.yaml exists"
else
  fail "plugin.yaml missing"
fi

# ── Step 2: Plugin load checks ─────────────────────────────────────────────────
echo ""
echo "--- Step 2: Plugin load checks ---"

# 2a. daedalus-cron.sh auto-installed (fix for #74)
CRON_WRAPPER="$SCRIPTS_DIR/daedalus-cron.sh"
if [ -f "$CRON_WRAPPER" ]; then
  pass "daedalus-cron.sh exists at $CRON_WRAPPER"
  if [ -x "$CRON_WRAPPER" ]; then
    pass "daedalus-cron.sh is executable"
  else
    fail "daedalus-cron.sh is not executable"
  fi
  if grep -q "daedalus_dispatch.py" "$CRON_WRAPPER"; then
    pass "daedalus-cron.sh references daedalus_dispatch.py"
  else
    fail "daedalus-cron.sh does not reference daedalus_dispatch.py"
  fi
  # Gateway watchdog (#799): wrapper invokes Python watchdog script.
  if grep -q "gateway_watchdog.py" "$CRON_WRAPPER" && \
     grep -q "mkdir" "$CRON_WRAPPER"; then
    pass "daedalus-cron.sh includes the gateway watchdog (#799)"
  else
    fail "daedalus-cron.sh missing gateway watchdog"
  fi
else
  fail "daedalus-cron.sh not found at $CRON_WRAPPER"
fi

# 2b. httpx available (fix for #75)
if python3 -c "import httpx" 2>/dev/null; then
  pass "httpx is importable"
else
  fail "httpx is not importable — run: pip install httpx"
fi

# 2c. GITHUB_TOKEN synced to all *-daedalus profiles (fix for #78)
TOKEN_SYNCED=0
TOKEN_MISSING=0
if [ -d "$PROFILES_DIR" ]; then
  for profile_dir in "$PROFILES_DIR"/*-daedalus; do
    [ -d "$profile_dir" ] || continue
    profile_name="$(basename "$profile_dir")"
    env_file="$profile_dir/.env"
    if [ -f "$env_file" ] && grep -q "GITHUB_TOKEN=" "$env_file" 2>/dev/null; then
      TOKEN_SYNCED=$((TOKEN_SYNCED+1))
    else
      TOKEN_MISSING=$((TOKEN_MISSING+1))
      echo "  WARN: $profile_name missing GITHUB_TOKEN in .env"
    fi
  done
  if [ "$TOKEN_MISSING" -eq 0 ] && [ "$TOKEN_SYNCED" -gt 0 ]; then
    pass "GITHUB_TOKEN synced to all $TOKEN_SYNCED *-daedalus profiles"
  elif [ "$TOKEN_SYNCED" -gt 0 ]; then
    fail "GITHUB_TOKEN missing from $TOKEN_MISSING of $((TOKEN_SYNCED+TOKEN_MISSING)) *-daedalus profiles"
  else
    fail "No *-daedalus profiles found — run postinstall.py first"
  fi
else
  fail "Profiles directory not found at $PROFILES_DIR"
fi

# ── Step 3: Dashboard checks ───────────────────────────────────────────────────
echo ""
echo "--- Step 3: Dashboard checks ---"

# 3a. Dashboard plugin API is importable
if python3 -c "import sys; sys.path.insert(0, '$PLUGIN_DIR'); from dashboard import plugin_api" 2>/dev/null; then
  pass "Dashboard plugin_api is importable"
else
  fail "Dashboard plugin_api import failed"
fi

# 3b. Daedalus dispatch script is importable
if python3 -c "import sys; sys.path.insert(0, '$PLUGIN_DIR'); from scripts import daedalus_dispatch" 2>/dev/null; then
  pass "daedalus_dispatch module is importable"
else
  fail "daedalus_dispatch module import failed"
fi

# 3c. Config loader works
if python3 -c "import sys; sys.path.insert(0, '$PLUGIN_DIR'); from config import ConfigLoader; print('OK')" 2>/dev/null; then
  pass "ConfigLoader is importable"
else
  fail "ConfigLoader import failed"
fi

# 3d. Core modules are importable
for mod in dispatch_state iterate providers kanban registry source_specs notify_templates; do
  if python3 -c "import sys; sys.path.insert(0, '$PLUGIN_DIR'); from core import $mod; print('OK')" 2>/dev/null; then
    pass "core.$mod is importable"
  else
    fail "core.$mod import failed"
  fi
done

# ── Step 4: Dispatch checks (gated) ────────────────────────────────────────────
echo ""
echo "--- Step 4: Dispatch checks (gated) ---"

# Gated behind #79, #80, #81 closure
skip "Create test GitHub issue, move to Ready, run daedalus-cron.sh" "#79, #80, #81"
skip "Verify issue picked up, assigned, PR opened, kanban_block called" "#81"

# ── Step 5: Cron durability (gated) ───────────────────────────────────────────
echo ""
echo "--- Step 5: Cron durability (gated) ---"

skip "Run hermes update — verify daedalus-daedalus cron auto-recreates" "#80"
skip "Run dispatch twice concurrently — verify no duplicate CI-retry crons" "#79"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "========================"
echo "Results: $PASS passed, $FAIL failed, $SKIP skipped"
echo "========================"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0

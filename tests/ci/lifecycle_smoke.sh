#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# lifecycle_smoke.sh — full Hermes + Daedalus plugin lifecycle smoke test (issue #88)
#
# Exercises install → dispatch → upgrade → uninstall → reinstall against an
# ISOLATED $HERMES_HOME using a lightweight `hermes` CLI stub (tests/ci/hermes),
# running the plugin's REAL scripts (postinstall.py, provision_roster.sh,
# register(), uninstall.sh). Hermes itself is private/not pip-installable, but the
# regressions this guards (#75 deps, #79 per-profile cron, #80 cron-lost-on-update)
# all live in the plugin's own scripts — so the stub is the right regression surface.
#
# Usage:   bash tests/ci/lifecycle_smoke.sh
# Env:     HERMES_CI_WORK  override the isolated work dir (default /tmp/hermes-ci-home)
#
# Runnable identically in CI and locally. Exits non-zero on the first failed
# assertion. Deliberately hermetic: all VCS tokens are unset so no network/auth.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STUB_DIR="$SCRIPT_DIR"
PYTHON="${PYTHON:-python3}"   # CI sets up python3; locally pass PYTHON=python3.14

# ── Isolated environment ──────────────────────────────────────────────────────
WORK="${HERMES_CI_WORK:-/tmp/hermes-ci-home}"
rm -rf "$WORK"
mkdir -p "$WORK"
export HOME="$WORK"                       # postinstall writes cron wrapper to $HOME/.hermes
export HERMES_HOME="$WORK/.hermes"        # everything else keys off HERMES_HOME ($HOME/.hermes)
export PATH="$STUB_DIR:$PATH"             # stub `hermes` wins over any real binary
# Hermetic: provision_roster.sh validates VCS tokens and rejects masked CI secrets,
# so run token-free (the kanban-only path — just warns).
unset GITHUB_TOKEN GH_TOKEN GITLAB_TOKEN AZURE_DEVOPS_PAT ROSTER_GH_TOKEN || true

PLUGIN="$HERMES_HOME/plugins/daedalus"
WRAPPER="$HERMES_HOME/scripts/daedalus-cron.sh"
REGISTRY="$HERMES_HOME/daedalus/projects"

# ── Assertion helpers ─────────────────────────────────────────────────────────
pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }
stage() { printf '\n\033[1m══ %s\033[0m\n' "$1"; }
assert_file()     { [ -f "$1" ] || fail "expected file: $1"; pass "file: $1"; }
assert_no_file()  { [ ! -e "$1" ] || fail "expected absent: $1"; pass "absent: $1"; }
assert_no_dir()   { [ ! -d "$1" ] || fail "expected dir removed: $1"; pass "dir removed: $1"; }
assert_exec()     { [ -x "$1" ] || fail "expected executable: $1"; pass "executable: $1"; }
assert_contains() { printf '%s' "$1" | grep -q -- "$2" || fail "expected /$2/ in output"; pass "matched: /$2/"; }
assert_absent()   { printf '%s' "$1" | grep -q -- "$2" && fail "did NOT expect /$2/ in output" || pass "absent: /$2/"; }

# Seed a minimal, isolated Hermes home (stands in for "install Hermes").
seed_hermes_home() {
  mkdir -p "$HERMES_HOME"/{scripts,profiles/default,hermes-agent/skills,skills,cron,daedalus,agent-hooks}
  cat > "$HERMES_HOME/config.yaml" <<'YAML'
model: stub-model
plugins:
  enabled: []
YAML
  cp "$HERMES_HOME/config.yaml" "$HERMES_HOME/profiles/default/config.yaml"
}

# Simulate `hermes plugins install benmarte/daedalus` (Hermes clones the repo) by
# copying the checkout into the install location.
install_plugin() {
  rm -rf "$PLUGIN"
  mkdir -p "$PLUGIN"
  rsync -a --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' "$REPO_ROOT/" "$PLUGIN/"
}

# Run the plugin's real register(ctx) entrypoint — the exact path a plugin load
# (and `hermes update`) takes through _ensure_cron_wrapper / _ensure_dispatch_crons.
run_register() {
  "$PYTHON" - <<PY
import sys
sys.path.insert(0, "$HERMES_HOME/plugins")
import daedalus

class Ctx:
    def register_auxiliary_task(self, **kw): pass
    def register_hook(self, *a, **kw): pass

daedalus.register(Ctx())
print("register() completed")
PY
}

# ══════════════════════════════════════════════════════════════════════════════
stage "Stage 1 — Fresh install"
seed_hermes_home

echo "→ requirements.txt resolves (regression guard #75)"
"$PYTHON" -m pip install -q -r "$REPO_ROOT/requirements.txt"
"$PYTHON" -c "import yaml, httpx, fastapi" && pass "imports resolve: yaml, httpx, fastapi"

echo "→ install plugin + run real postinstall.py"
install_plugin
"$PYTHON" "$PLUGIN/scripts/postinstall.py"

assert_file "$WRAPPER"
assert_exec "$WRAPPER"
grep -q "daedalus_dispatch.py" "$WRAPPER" && pass "wrapper invokes daedalus_dispatch.py" || fail "wrapper missing dispatch ref"
assert_file "$HERMES_HOME/agent-hooks/daedalus-ready.sh"

echo "→ roster profiles provisioned (#79: per-profile lifecycle roles)"
PROFILE_OUT="$(hermes profile list)"
assert_contains "$PROFILE_OUT" "developer-daedalus"
assert_contains "$PROFILE_OUT" "reviewer-daedalus"
assert_contains "$PROFILE_OUT" "project-manager-daedalus"

echo "→ health/smoke check (postinstall.py --check; stands in for 'hermes doctor')"
"$PYTHON" "$PLUGIN/scripts/postinstall.py" --check && pass "health check exit 0" || fail "health check failed"

# ══════════════════════════════════════════════════════════════════════════════
stage "Stage 2 — Dispatch smoke (--dry-run)"
RC=0; DISPATCH_OUT="$("$PYTHON" "$PLUGIN/scripts/daedalus_dispatch.py" --dry-run 2>&1)" || RC=$?
echo "$DISPATCH_OUT"
[ "$RC" -eq 0 ] && pass "dispatch --dry-run exit 0" || fail "dispatch exit $RC"
assert_contains "$DISPATCH_OUT" "DRY RUN"

# ══════════════════════════════════════════════════════════════════════════════
stage "Stage 3 — Upgrade (hermes update) — cron must survive (#80)"
echo "→ register a sample project + create its dispatch cron"
SAMPLE="$WORK/sample-repo"
mkdir -p "$SAMPLE/.hermes"
cat > "$SAMPLE/.hermes/daedalus.yaml" <<'YAML'
name: ci-sample
repo: acme/ci-sample
cron:
  schedule: "every 60m"
YAML
echo "$SAMPLE" > "$REGISTRY"

run_register
CRON_OUT="$(hermes cron list --all)"
assert_contains "$CRON_OUT" "ci-sample-daedalus"

echo "→ simulate 'hermes update' wiping the global cron store"
rm -f "$HERMES_HOME/cron/jobs.json"
assert_absent "$(hermes cron list --all)" "ci-sample-daedalus"

echo "→ plugin reload self-heals the missing cron"
run_register
assert_contains "$(hermes cron list --all)" "ci-sample-daedalus"

echo "→ dispatch smoke still green after upgrade"
"$PYTHON" "$PLUGIN/scripts/daedalus_dispatch.py" --dry-run >/dev/null 2>&1 && pass "post-upgrade dispatch exit 0" || fail "post-upgrade dispatch failed"

# ══════════════════════════════════════════════════════════════════════════════
stage "Stage 4 — Uninstall + reinstall (clean state)"
# Run uninstall from a copy: it deletes the plugin dir (incl. itself) at the end.
cp "$PLUGIN/scripts/uninstall.sh" "$WORK/uninstall.sh"
bash "$WORK/uninstall.sh" -y

echo "→ verify clean state — no stale cron, registry, or plugin dir"
assert_absent "$(hermes cron list --all)" "ci-sample-daedalus"
assert_no_dir "$HERMES_HOME/daedalus"
assert_no_dir "$PLUGIN"

echo "→ reinstall + re-provision"
install_plugin
"$PYTHON" "$PLUGIN/scripts/postinstall.py"
assert_file "$WRAPPER"
assert_contains "$(hermes profile list)" "developer-daedalus"

# ══════════════════════════════════════════════════════════════════════════════
stage "Stage 5 — Reinstall dispatch smoke"
RC=0; REOUT="$("$PYTHON" "$PLUGIN/scripts/daedalus_dispatch.py" --dry-run 2>&1)" || RC=$?
echo "$REOUT"
[ "$RC" -eq 0 ] && pass "reinstall dispatch --dry-run exit 0" || fail "reinstall dispatch exit $RC"
assert_contains "$REOUT" "DRY RUN"

printf '\n\033[1;32m✓ Plugin lifecycle smoke test passed — all 5 stages green.\033[0m\n'

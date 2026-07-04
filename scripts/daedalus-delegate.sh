#!/usr/bin/env bash
#
# daedalus-delegate.sh ISSUE_N BASE TASK OUT ERR RUN_CMD...
#
# Script-owned delegation lifecycle for the DEVELOPER role (issue #1280).
#
# The old flow made the OUTER orchestrator LLM own the wait: it spawned the
# coding agent with `background=True`, then burned model turns polling a wait
# command (`kill -0` liveness + `sleep 30` + `gh pr list`) until a PR appeared.
# That coupled completion to the LLM's turn budget — when the budget ran out
# mid-wait the card could be completed prematurely (Part C of #1276/#1280).
#
# This wrapper collapses spawn -> wait -> heartbeat -> outcome into ONE
# in-shell, blocking call. The outer LLM invokes it in a single terminal(...)
# call and spends at most two turns on the whole delegation. The bash `wait`
# loop — not model turns — owns liveness, the timeout ceiling, and PR detection.
#
# It MOVES the three guarantees that used to live in the dispatcher's
# `_wait_for_agent_cmd` / `_ROLE_AFTER_SPAWN["developer"]` builders here:
#   1. PID `kill -0` liveness  -> CODING_AGENT_DIED on silent death
#   2. `max_wait` wall-clock ceiling -> CODING_AGENT_TIMEOUT on hang
#   3. `daedalus-detect-pr.sh` handshake -> advance the moment a PR is open
# and adds a periodic `hermes kanban heartbeat` so the card never looks stale
# while the inner agent runs long.
#
# CRITICAL invariant (issue #1280): the inner/outer worker CANNOT self-complete
# its kanban card — the parent dispatcher holds the claim. So this wrapper NEVER
# calls `hermes kanban complete` or merges. It only captures the structured
# outcome (exit code, PR number, marker) to stdout + a metadata file; the outer
# body then blocks `review-required: ...` exactly as before. This preserves the
# human-only merge gate and claim ownership.
#
# Contract (mirrors daedalus-worktree-spawn.sh so any coding_agents failover
# entry — claude-code / codex / opencode / custom — works with no per-agent
# branching; RUN_CMD is opaque trailing args):
#   $1 ISSUE_N   issue number (worktree = <repo>/.worktrees/dev-<N>, branch fix/issue-<N>)
#   $2 BASE      base branch the worktree forks from (e.g. dev)
#   $3 TASK      task file piped to the inner agent's stdin
#   $4 OUT       inner agent stdout (the "PR URL: ... PR number: <n>" handshake)
#   $5 ERR       inner agent stderr (crash reason survives here)
#   $6.. RUN     the resolved coding-agent command (opaque, e.g. `claude ... -p`)
#
# Tuning knobs come from the environment (keeps the positional contract
# identical to daedalus-worktree-spawn.sh):
#   DAEDALUS_MAX_WAIT        wall-clock ceiling in seconds (default 3600)
#   DAEDALUS_HEARTBEAT_SECS  heartbeat cadence in seconds (default 60)
#   DAEDALUS_BOARD           kanban board slug for the heartbeat (optional)
#   HERMES_KANBAN_TASK       task id for the heartbeat (set in the worker env)
set -euo pipefail

# Harden temp-file handling: private-by-default perms on everything we create,
# so the per-run sidecar dir (below) and its contents are 0700/0600 (CWE-377).
umask 077

n="${1:?issue number required}"
base="${2:-dev}"
task="${3:?task file required}"
out="${4:?out file required}"
err="${5:?err file required}"
shift 5

# Validate the issue number is numeric before it flows into the mktemp template,
# the fix/issue-<N> branch name, or the metadata JSON — a non-numeric $n would
# let a caller smuggle path/format characters into those contexts.
case "$n" in
  ''|*[!0-9]*)
    echo "daedalus-delegate: issue number must be numeric (got: '$n')" >&2
    exit 2
    ;;
esac
# Remaining args ("$@") are the opaque RUN_CMD; forwarded verbatim to the
# worktree spawner, which runs them under `bash -c`.

MAX_WAIT="${DAEDALUS_MAX_WAIT:-3600}"
HEARTBEAT_SECS="${DAEDALUS_HEARTBEAT_SECS:-60}"
POLL_SECS="${DAEDALUS_POLL_SECS:-5}"
# Run the (network) detect-pr check only every Nth poll iteration instead of
# every POLL_SECS. detect-pr shells out to `gh pr list`; at POLL_SECS=5 for the
# full max_wait that is ~720 API calls/hour/developer, which trips GitHub's
# secondary rate limit under concurrency. The PID `kill -0` liveness check and
# the wall-clock timeout still run EVERY iteration; only the PR poll is throttled
# to the ~30s cadence (6 * 5s) a human-review handoff easily tolerates.
DETECT_PR_EVERY="${DAEDALUS_DETECT_PR_EVERY:-6}"

# Sibling scripts live next to this one — derive the dir so the wrapper works
# from any install location (worktree, installed plugin) without a hardcoded path.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SPAWN="$SCRIPT_DIR/daedalus-worktree-spawn.sh"
DETECT="$SCRIPT_DIR/daedalus-detect-pr.sh"

# Per-run PRIVATE sidecar dir (CWE-59/377). The pid/meta/signal/stdout/stderr
# sidecars used to live at predictable /tmp/dev-<N>-* paths, which let a hostile
# co-tenant pre-plant a symlink (TOCTOU) so our `>` writes followed it into an
# arbitrary file, or race the name into existence. `mktemp -d` gives an
# unguessable 0700 dir (umask 077 above keeps it private) that only we can write
# into. The passed-in OUT/ERR args are the dispatcher's contract, but nothing
# external reads those files — the outer LLM reads THIS wrapper's stdout — so we
# relocate the actual stdout/stderr capture into the private dir too and remove
# it on exit.
RUNDIR="$(mktemp -d "${TMPDIR:-/tmp}/daedalus-delegate-${n}.XXXXXX")"
trap 'rm -rf "$RUNDIR" 2>/dev/null || true' EXIT

out="$RUNDIR/out.txt"
err="$RUNDIR/err.txt"
pidfile="$RUNDIR/pid.txt"
meta="$RUNDIR/meta.json"
# Stop-hook signal file: a Claude Code Stop hook (deployed separately) can touch
# this to signal the inner session ended even before its stdout flushes. Watched
# here so the wait can break on it; absent hook => file never appears (no-op).
signal="$RUNDIR/stop.txt"

# Refuse to write through a symlink. RUNDIR is a fresh 0700 mktemp dir so
# pre-planting is already impossible, but guard the wrapper's own writes
# explicitly (defence-in-depth, CWE-59) before any `printf >` to a sidecar.
_no_symlink() {
  if [ -L "$1" ]; then
    echo "daedalus-delegate: refusing to write through symlink: $1" >&2
    exit 1
  fi
}

# Best-effort heartbeat — never fatal (guarded so it can't trip `set -e`).
# Board slug comes from DAEDALUS_BOARD, falling back to the worker env's
# HERMES_KANBAN_BOARD; task id from HERMES_KANBAN_TASK (set by hermes for the
# worker). If neither task id is set, there is nothing to heartbeat.
_heartbeat() {
  [ -n "${HERMES_KANBAN_TASK:-}" ] || return 0
  local board="${DAEDALUS_BOARD:-${HERMES_KANBAN_BOARD:-}}"
  if [ -n "$board" ]; then
    hermes kanban --board "$board" heartbeat "$HERMES_KANBAN_TASK" \
      >/dev/null 2>&1 || true
  else
    hermes kanban heartbeat "$HERMES_KANBAN_TASK" >/dev/null 2>&1 || true
  fi
}

# Mask common secret shapes in RELAYED output. The failure path emits the inner
# agent's stderr tail to stdout, which the orchestrator may mirror into kanban
# comments / Slack — a path that bypasses core/providers/http.py's token
# redaction. Mask tokens/keys here before they leave the process. sed -E only,
# so it stays portable across BSD (macOS) and GNU sed: no \b/\d, POSIX classes
# and {n,} intervals only. Specific token shapes run before the generic
# high-entropy rule so their labels survive.
_redact() {
  sed -E \
    -e 's/(gh[pousr]_)[A-Za-z0-9]+/\1[REDACTED]/g' \
    -e 's/(sk-)[A-Za-z0-9-]+/\1[REDACTED]/g' \
    -e 's/(xox[baprs]-)[A-Za-z0-9-]+/\1[REDACTED]/g' \
    -e 's/([Bb]earer )[A-Za-z0-9._-]+/\1[REDACTED]/g' \
    -e 's/([A-Za-z0-9_]*(TOKEN|KEY|SECRET)=)[^[:space:]]+/\1[REDACTED]/g' \
    -e 's/[A-Za-z0-9_-]{32,}/[REDACTED]/g'
}

# Background the inner agent inside its isolated per-issue worktree. worktree-spawn
# `exec`s the coding agent, so $! is the agent's own PID — the liveness check and
# detect-pr's kill both target it.
"$SPAWN" "$n" "$base" "$task" "$out" "$err" "$@" &
PID=$!
_no_symlink "$pidfile"
printf '%s\n' "$PID" > "$pidfile"

marker=""            # CODING_AGENT_DIED / CODING_AGENT_TIMEOUT, else empty
start=$SECONDS
last_hb=$SECONDS
iter=0               # poll counter — gates the throttled detect-pr check

# Wait on the FIRST of {inner exit, detect-pr handshake, stop-hook signal,
# max_wait}. The inner agent's stdout lands in $out; detect-pr also writes the
# handshake line to $out (and kills the agent) when a PR is already open, so a
# non-empty $out is the single "we're done, advance" condition either way.
while [ ! -s "$out" ]; do
  iter=$((iter + 1))
  # PR handshake: if an open PR already exists for fix/issue-<N>, this writes the
  # handshake line to $out and kills the still-running agent (#146). Quiet no-op
  # otherwise. Deterministic branch keeps detection race-free (#1131). Throttled
  # to every DETECT_PR_EVERY-th iteration (the `gh pr list` call is the rate-limit
  # risk) while liveness/timeout below still run every iteration.
  if [ $((iter % DETECT_PR_EVERY)) -eq 0 ]; then
    bash "$DETECT" "$out" "$pidfile" "fix/issue-$n" 2>/dev/null || true
    if [ -s "$out" ]; then
      break
    fi
  fi
  # Stop-hook signalled the session ended.
  if [ -f "$signal" ]; then
    break
  fi
  # Silent death: agent gone with nothing on stdout.
  if ! kill -0 "$PID" 2>/dev/null; then
    if [ ! -s "$out" ]; then
      marker="CODING_AGENT_DIED"
    fi
    break
  fi
  # Wall-clock ceiling.
  if [ $((SECONDS - start)) -ge "$MAX_WAIT" ]; then
    marker="CODING_AGENT_TIMEOUT"
    break
  fi
  # Periodic heartbeat so the card never looks stale during a long run.
  if [ $((SECONDS - last_hb)) -ge "$HEARTBEAT_SECS" ]; then
    _heartbeat
    last_hb=$SECONDS
  fi
  sleep "$POLL_SECS"
done

# Stop a still-running agent (timeout / stop-hook paths leave it alive) and reap
# it. `wait` yields the inner exit code; a killed agent yields ~143 — fine, the
# marker already records the real reason.
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID" 2>/dev/null || true
fi
rc=0
wait "$PID" 2>/dev/null || rc=$?

# Parse the PR number from the handshake line ("... PR number: <n>"), if any.
pr="$(grep -oE 'PR number: [0-9]+' "$out" 2>/dev/null | grep -oE '[0-9]+' | head -1 || true)"

# Verdict: infra_failure (marker) > pr_opened (PR number present) > no_pr.
if [ -n "$marker" ]; then
  verdict="infra_failure"
elif [ -n "$pr" ]; then
  verdict="pr_opened"
else
  verdict="no_pr"
fi

# Structured outcome -> metadata file (for the dispatcher's later use; the
# runtime completion path stays block-based per the #1280 invariant).
_no_symlink "$meta"
printf '{"daedalus_delegate":1,"issue":%s,"exit_code":%s,"pr":%s,"marker":"%s","verdict":"%s"}\n' \
  "$n" "$rc" "${pr:-null}" "$marker" "$verdict" > "$meta" 2>/dev/null || true

# Structured outcome -> stdout (what the outer LLM reads from the terminal call).
# Emit the inner agent's stdout (the handshake line) first, then any failure
# marker + stderr tail so the death reason (OOM / auth / crash) is visible.
cat "$out" 2>/dev/null || true
if [ -n "$marker" ]; then
  echo "$marker: developer coding agent failed (verdict=$verdict, rc=$rc). stderr tail:"
  # Redact secret shapes from the relayed stderr tail before it leaves the
  # process (the orchestrator may mirror it into kanban/Slack). Block markers
  # above are emitted separately, so they stay intact.
  tail -n 40 "$err" 2>/dev/null | _redact || true
fi

exit 0

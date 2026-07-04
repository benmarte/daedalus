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

n="${1:?issue number required}"
base="${2:-dev}"
task="${3:?task file required}"
out="${4:?out file required}"
err="${5:?err file required}"
shift 5
# Remaining args ("$@") are the opaque RUN_CMD; forwarded verbatim to the
# worktree spawner, which runs them under `bash -c`.

MAX_WAIT="${DAEDALUS_MAX_WAIT:-3600}"
HEARTBEAT_SECS="${DAEDALUS_HEARTBEAT_SECS:-60}"
POLL_SECS=5

# Sibling scripts live next to this one — derive the dir so the wrapper works
# from any install location (worktree, installed plugin) without a hardcoded path.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SPAWN="$SCRIPT_DIR/daedalus-worktree-spawn.sh"
DETECT="$SCRIPT_DIR/daedalus-detect-pr.sh"

# Derive the sidecar file paths from OUT so they share the per-issue prefix the
# dispatcher already uses (/tmp/dev-<N>-out.txt -> -pid.txt / -meta.json / -stop.txt).
stem="${out%-out.txt}"
pidfile="${stem}-pid.txt"
meta="${stem}-meta.json"
# Stop-hook signal file: a Claude Code Stop hook (deployed separately) can touch
# this to signal the inner session ended even before its stdout flushes. Watched
# here so the wait can break on it; absent hook => file never appears (no-op).
signal="${stem}-stop.txt"
rm -f "$signal" 2>/dev/null || true

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

# Background the inner agent inside its isolated per-issue worktree. worktree-spawn
# `exec`s the coding agent, so $! is the agent's own PID — the liveness check and
# detect-pr's kill both target it.
"$SPAWN" "$n" "$base" "$task" "$out" "$err" "$@" &
PID=$!
printf '%s\n' "$PID" > "$pidfile"

marker=""            # CODING_AGENT_DIED / CODING_AGENT_TIMEOUT, else empty
start=$SECONDS
last_hb=$SECONDS

# Wait on the FIRST of {inner exit, detect-pr handshake, stop-hook signal,
# max_wait}. The inner agent's stdout lands in $out; detect-pr also writes the
# handshake line to $out (and kills the agent) when a PR is already open, so a
# non-empty $out is the single "we're done, advance" condition either way.
while [ ! -s "$out" ]; do
  # PR handshake: if an open PR already exists for fix/issue-<N>, this writes the
  # handshake line to $out and kills the still-running agent (#146). Quiet no-op
  # otherwise. Deterministic branch keeps detection race-free (#1131).
  bash "$DETECT" "$out" "$pidfile" "fix/issue-$n" 2>/dev/null || true
  if [ -s "$out" ]; then
    break
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
printf '{"daedalus_delegate":1,"issue":%s,"exit_code":%s,"pr":%s,"marker":"%s","verdict":"%s"}\n' \
  "$n" "$rc" "${pr:-null}" "$marker" "$verdict" > "$meta" 2>/dev/null || true

# Structured outcome -> stdout (what the outer LLM reads from the terminal call).
# Emit the inner agent's stdout (the handshake line) first, then any failure
# marker + stderr tail so the death reason (OOM / auth / crash) is visible.
cat "$out" 2>/dev/null || true
if [ -n "$marker" ]; then
  echo "$marker: developer coding agent failed (verdict=$verdict, rc=$rc). stderr tail:"
  tail -n 40 "$err" 2>/dev/null || true
fi

exit 0

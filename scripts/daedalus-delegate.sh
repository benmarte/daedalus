#!/usr/bin/env bash
#
# daedalus-delegate.sh
#
# Script-owned delegation lifecycle — replaces the LLM poll loop (issue #1280).
#
# The outer Hermes worker runs this wrapper SYNCHRONOUSLY via terminal(). The
# wrapper owns every mechanical step:
#
#   1. Spawn the coding-agent CLI as a background process.
#   2. Bash wait-loop (no LLM turns): poll PID liveness every few seconds.
#   3. Send `hermes kanban heartbeat <card>` every --heartbeat-interval seconds
#      so the claim TTL never fires (best-effort — heartbeat failures do not
#      abort the wrapper).
#   4. Honour push-based early completion: if <out>.done appears (written by
#      an inner-agent Stop/session-end hook via C3), treat the run as done
#      even if the process is still winding down.
#   5. Enforce --max-wait: SIGTERM the process group; after a 10s grace period,
#      SIGKILL; exit with a distinct status (124) so callers can detect timeout.
#   6. Emit a machine-readable DELEGATE_RESULT: {...} line on stdout.
#
# The wrapper does NOT call `hermes kanban complete` or `block` — that remains
# the outer worker's job so classify_blocked() routing is unchanged (AC #3).
#
# Usage:
#   daedalus-delegate.sh \
#     --task-file <path>           Task body piped to the coding agent's stdin
#     --cmd <string>               Coding-agent CLI invocation (verbatim shell)
#     --card <task-id>             Kanban card ID for heartbeats
#     --board <slug>               Kanban board slug for heartbeats
#     --out <path>                 File receiving the agent's combined stdout+stderr
#     [--max-wait <secs>]          Timeout in seconds (default: 3600)
#     [--heartbeat-interval <secs>] Heartbeat period (default: 300)
#
# Exit codes:
#   0    Coding agent exited 0 (DELEGATE_RESULT status "ok")
#   N    Coding agent exited N (DELEGATE_RESULT status "failed", N != 0, != 124)
#   124  Timeout — agent killed (DELEGATE_RESULT status "timeout")
#
# Style: consistent with daedalus-worktree-spawn.sh and daedalus-detect-pr.sh.
# set -u — never unset variables. No set -e: we must preserve the child exit code
# and distinguish timeout (124) from agent failure; -e traps would mask both.
set -uo pipefail

# ── defaults ─────────────────────────────────────────────────────────────────
_max_wait=3600
_heartbeat_interval=300
_poll_interval=5      # PID liveness check granularity; override via --poll-interval (for tests)
_task_file=""
_cmd=""
_card=""
_board=""
_out=""

# ── argument parsing ──────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --task-file)          _task_file="${2:?--task-file requires a value}";          shift 2 ;;
    --cmd)                _cmd="${2:?--cmd requires a value}";                       shift 2 ;;
    --card)               _card="${2:?--card requires a value}";                     shift 2 ;;
    --board)              _board="${2:?--board requires a value}";                   shift 2 ;;
    --out)                _out="${2:?--out requires a value}";                       shift 2 ;;
    --max-wait)           _max_wait="${2:?--max-wait requires a value}";             shift 2 ;;
    --heartbeat-interval) _heartbeat_interval="${2:?--heartbeat-interval requires a value}"; shift 2 ;;
    --poll-interval)      _poll_interval="${2:?--poll-interval requires a value}";   shift 2 ;;
    *) echo "[delegate] unknown argument: $1" >&2; exit 2 ;;
  esac
done

# ── validate required args ────────────────────────────────────────────────────
for _var in _task_file _cmd _card _board _out; do
  if [ -z "${!_var:-}" ]; then
    echo "[delegate] missing required argument: --${_var#_}" >&2
    exit 2
  fi
done

[ -f "$_task_file" ] || { echo "[delegate] task-file not found: $_task_file" >&2; exit 2; }

_done_marker="${_out}.done"
_start_ts="$(date +%s)"

echo "[delegate] starting — card=$_card board=$_board max-wait=${_max_wait}s hb-interval=${_heartbeat_interval}s"
echo "[delegate] cmd: $_cmd"
echo "[delegate] out: $_out"

# ── spawn coding agent ────────────────────────────────────────────────────────
# Spawn the coding agent via a wrapper script that records its PID.
# We use bash -c so the agent command runs as a direct child of the delegate
# process — `kill -0 $_child_pid` reliably tracks liveness, and `wait $_child_pid`
# reaps it correctly. stdin ← task file; stdout+stderr → out file.
bash -c "$_cmd" < "$_task_file" > "$_out" 2>&1 &
_child_pid=$!
echo "[delegate] spawned PID=$_child_pid"

# ── helper: send a heartbeat (best-effort, never fatal) ──────────────────────
_last_hb_ts="$_start_ts"
_heartbeat() {
  hermes kanban heartbeat "$_card" --board "$_board" >/dev/null 2>&1 || true
  _last_hb_ts="$(date +%s)"
  echo "[delegate] heartbeat sent (card=$_card)"
}

# ── helper: emit DELEGATE_RESULT line ────────────────────────────────────────
_emit_result() {
  local _status="$1" _exit_code="$2"
  local _now_ts; _now_ts="$(date +%s)"
  local _duration=$(( _now_ts - _start_ts ))
  local _out_escaped; _out_escaped="$(printf '%s' "$_out" | sed 's/"/\\"/g')"
  printf 'DELEGATE_RESULT: {"status":"%s","exit":%d,"out":"%s","duration_s":%d}\n' \
    "$_status" "$_exit_code" "$_out_escaped" "$_duration"
}

# ── helper: kill the child cleanly ──────────────────────────────────────────
# Sends SIGTERM to the direct child PID, waits 2s, then SIGKILL.
# We intentionally target only the child PID — not the process group — because
# the delegate and its child share the same session/process-group by default,
# and group-kill would suicide the wrapper itself. Grandchildren of the agent
# that survive are adopted by init/launchd; that is acceptable: the inner
# coding agent is responsible for cleaning up its own tools.
_kill_child() {
  kill -TERM "$_child_pid" 2>/dev/null || true
  sleep 2
  kill -KILL "$_child_pid" 2>/dev/null || true
}

# ── main wait loop ────────────────────────────────────────────────────────────
_exit_code=0
_timed_out=0

while true; do
  # 1. Check for push-based early completion (done marker written by inner hook)
  if [ -f "$_done_marker" ]; then
    echo "[delegate] done-marker found — treating as complete"
    # Give the process a moment to flush output, then kill it cleanly.
    sleep 1
    kill -TERM "$_child_pid" 2>/dev/null || true
    # Read the actual exit code if the process has already finished.
    wait "$_child_pid" 2>/dev/null || true
    _exit_code=0
    break
  fi

  # 2. Check if the child has exited
  if ! kill -0 "$_child_pid" 2>/dev/null; then
    wait "$_child_pid"
    _exit_code=$?
    echo "[delegate] process exited (PID=$_child_pid exit=$_exit_code)"
    break
  fi

  # 3. Timeout check
  _now_ts="$(date +%s)"
  _elapsed=$(( _now_ts - _start_ts ))
  if [ "$_elapsed" -ge "$_max_wait" ]; then
    echo "[delegate] TIMEOUT after ${_elapsed}s — killing child (PID=$_child_pid)"
    _kill_child
    _timed_out=1
    break
  fi

  # 4. Send heartbeat if due
  _hb_elapsed=$(( _now_ts - _last_hb_ts ))
  if [ "$_hb_elapsed" -ge "$_heartbeat_interval" ]; then
    _heartbeat
  fi

  sleep "$_poll_interval"
done

# ── emit result ───────────────────────────────────────────────────────────────
if [ "$_timed_out" -eq 1 ]; then
  _emit_result "timeout" 124
  echo "[delegate] done (timeout)"
  exit 124
elif [ "$_exit_code" -eq 0 ]; then
  _emit_result "ok" 0
  echo "[delegate] done (ok)"
  exit 0
else
  _emit_result "failed" "$_exit_code"
  echo "[delegate] done (failed exit=$_exit_code)"
  exit "$_exit_code"
fi

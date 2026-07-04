#!/usr/bin/env bash
#
# daedalus-delegate.sh
#
# Script-owned delegation lifecycle — replaces the LLM poll loop (issue #1280).
#
# The outer Hermes worker spawns this wrapper with background=True (returns the
# terminal call immediately, no timeout exposure) and then ends its session. The
# wrapper owns every mechanical step until the kanban card is transitioned:
#
#   1. Spawn the coding-agent CLI in its own process group (setsid / perl POSIX
#      fallback). stdin ← task-file; stdout+stderr → out file.
#   2. Bash wait-loop (zero LLM turns): poll PID liveness every --poll-interval
#      seconds. Heartbeat runs in a background subshell so a slow hermes call
#      never blocks the loop.
#   3. Honour push-based early completion: if <out>.done appears (C3 inner-agent
#      hook), treat the run as done and kill the process group cleanly.
#   4. Enforce --max-wait: SIGTERM the process group; 5s grace; SIGKILL. Exit 124.
#   5. Emit DELEGATE_RESULT: {...} line to stdout.
#   6. With --transition: detect the opened PR via `gh` and call
#      `hermes kanban --board <board> block <card> "<signal phrase>"` where the
#      phrase is byte-identical to the developer SOUL's signal table so
#      classify_blocked() routing is unchanged.
#
# Usage:
#   daedalus-delegate.sh \
#     --task-file <path>            Task body piped to the coding agent's stdin
#     --cmd <string>                Coding-agent CLI invocation (verbatim shell)
#     --card <task-id>              Kanban card ID for heartbeats + transition
#     --board <slug>                Kanban board slug
#     --out <path>                  File receiving agent's combined stdout+stderr
#     [--repo <owner/repo>]         Required with --transition (for PR detection)
#     [--branch <branch>]           Required with --transition (deterministic branch)
#     [--max-wait <secs>]           Timeout in seconds (default: 3600)
#     [--heartbeat-interval <secs>] Heartbeat period (default: 300)
#     [--poll-interval <secs>]      PID check granularity (default: 5; tests use 1)
#     [--transition]                If set, wrapper calls hermes kanban block at end
#
# Exit codes:
#   0    Agent exited 0 (DELEGATE_RESULT status "ok")
#   N    Agent exited N (DELEGATE_RESULT status "failed", N != 0, != 124)
#   124  Timeout — agent killed (DELEGATE_RESULT status "timeout")
#   2    Wrapper setup error (bad args, unwritable out dir)
#
# Style: consistent with daedalus-worktree-spawn.sh and daedalus-detect-pr.sh.
# set -u: never unset variables. No set -e: must preserve child exit code and
# distinguish timeout (124) from agent failure; -e traps would mask both.
set -uo pipefail

# ── defaults ─────────────────────────────────────────────────────────────────
_max_wait=3600
_heartbeat_interval=300
_poll_interval=5        # PID liveness check granularity; tests override to 1
_task_file=""
_cmd=""
_card=""
_board=""
_out=""
_repo=""
_branch=""
_transition=0

# ── argument parsing ──────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --task-file)          _task_file="${2:?--task-file requires a value}";           shift 2 ;;
    --cmd)                _cmd="${2:?--cmd requires a value}";                        shift 2 ;;
    --card)               _card="${2:?--card requires a value}";                      shift 2 ;;
    --board)              _board="${2:?--board requires a value}";                    shift 2 ;;
    --out)                _out="${2:?--out requires a value}";                        shift 2 ;;
    --repo)               _repo="${2:?--repo requires a value}";                     shift 2 ;;
    --branch)             _branch="${2:?--branch requires a value}";                 shift 2 ;;
    --max-wait)           _max_wait="${2:?--max-wait requires a value}";              shift 2 ;;
    --heartbeat-interval) _heartbeat_interval="${2:?--heartbeat-interval requires a value}"; shift 2 ;;
    --poll-interval)      _poll_interval="${2:?--poll-interval requires a value}";    shift 2 ;;
    --transition)         _transition=1;                                              shift 1 ;;
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
if [ "$_transition" -eq 1 ]; then
  [ -n "$_repo"   ] || { echo "[delegate] --transition requires --repo"   >&2; exit 2; }
  [ -n "$_branch" ] || { echo "[delegate] --transition requires --branch" >&2; exit 2; }
fi

# ── ensure output directory exists ───────────────────────────────────────────
# Finding 5: an unwritable out-dir would look like a crashed agent. Create it
# upfront; emit a distinguishable wrapper-error if we can't.
_out_dir="$(dirname "$_out")"
if ! mkdir -p "$_out_dir" 2>/dev/null; then
  _err="cannot create output directory: $_out_dir"
  echo "[delegate] WRAPPER_ERROR: $_err" >&2
  printf 'DELEGATE_RESULT: {"status":"wrapper-error","exit":2,"out":"%s","duration_s":0}\n' "$_out"
  if [ "$_transition" -eq 1 ]; then
    hermes kanban --board "$_board" block "$_card" \
      "coding-agent-failed: wrapper-error: $_err" 2>/dev/null || true
  fi
  exit 2
fi

_done_marker="${_out}.done"
_start_ts="$(date +%s)"

echo "[delegate] starting — card=$_card board=$_board max-wait=${_max_wait}s hb-interval=${_heartbeat_interval}s"
echo "[delegate] cmd: $_cmd"
echo "[delegate] out: $_out"

# ── spawn coding agent in its own process group ───────────────────────────────
# Finding 2: use setsid to create a new session (pgid = child pid) so that on
# timeout we can kill -TERM/-KILL -$pgid to reach ALL grandchildren (sub-tools,
# compilers, test runners) the agent spawned. Without setsid the child shares
# the delegate's process group and kill -$pgid would suicide the wrapper.
# Falls back to perl POSIX::setsid() on macOS where setsid(1) is absent.
if command -v setsid >/dev/null 2>&1; then
  setsid bash -c "$_cmd" < "$_task_file" > "$_out" 2>&1 &
elif command -v perl >/dev/null 2>&1; then
  perl -e 'use POSIX; POSIX::setsid(); exec @ARGV' -- \
    bash -c "$_cmd" < "$_task_file" > "$_out" 2>&1 &
else
  # No setsid/perl: fall back to bare spawn; grandchild isolation is best-effort
  bash -c "$_cmd" < "$_task_file" > "$_out" 2>&1 &
fi
_child_pid=$!
# After setsid the child is its own process group leader (pgid == pid).
_child_pgid="$_child_pid"
echo "[delegate] spawned PID=$_child_pid PGID=$_child_pgid"

# ── helper: send heartbeat in background (non-blocking) ──────────────────────
# Finding 3: a slow or hanging hermes call must NOT block the PID-poll loop —
# run the heartbeat in a detached background subshell so the loop keeps ticking
# and max-wait is enforced on schedule regardless of hermes latency.
_last_hb_ts="$_start_ts"
_heartbeat() {
  # Run heartbeat in a background subshell with all stdio to /dev/null.
  # CRITICAL: the 3-way redirect must be OUTSIDE the ( ) so it applies to the
  # subshell process itself, not just to the hermes command inside it. If the
  # redirects were inside — ( cmd </dev/null >/dev/null 2>&1 ) & — the subshell
  # bash process would still hold fd 1 = pipe_write_end while waiting on hermes,
  # keeping Python's communicate() blocked until hermes exits.
  ( hermes kanban heartbeat "$_card" --board "$_board" || true ) </dev/null >/dev/null 2>&1 &
  _last_hb_ts="$(date +%s)"
  echo "[delegate] heartbeat sent (card=$_card)"
}

# ── helper: emit DELEGATE_RESULT line ────────────────────────────────────────
_emit_result() {
  local _status="$1" _ec="$2"
  local _now_ts; _now_ts="$(date +%s)"
  local _dur=$(( _now_ts - _start_ts ))
  local _esc; _esc="$(printf '%s' "$_out" | sed 's/"/\\"/g')"
  printf 'DELEGATE_RESULT: {"status":"%s","exit":%d,"out":"%s","duration_s":%d}\n' \
    "$_status" "$_ec" "$_esc" "$_dur"
}

# ── helper: kill the process group cleanly ───────────────────────────────────
# Finding 2+6: targets the pgid (not just the direct child pid) so all
# grandchildren are reaped. 5s grace period, then SIGKILL. Comment matches code.
_kill_child() {
  kill -TERM -"$_child_pgid" 2>/dev/null || kill -TERM "$_child_pid" 2>/dev/null || true
  local _i=0
  while [ $_i -lt 5 ] && kill -0 "$_child_pid" 2>/dev/null; do
    sleep 1
    _i=$(( _i + 1 ))
  done
  kill -KILL -"$_child_pgid" 2>/dev/null || kill -KILL "$_child_pid" 2>/dev/null || true
}

# ── helper: kanban card transition ───────────────────────────────────────────
# Called only when --transition is set. Detects the PR for --branch, then calls
# `hermes kanban --board <board> block <card> "<signal>"` with a phrase that is
# byte-identical to the developer SOUL signal table so classify_blocked() routes
# correctly. Retries the block call once; on failure logs and returns 1 so the
# caller can exit nonzero (sweeper stale-running detection remains the backstop).
#
# #1288 metadata transport: this is a BLOCKED handoff (review-required /
# coding-agent-failed), NOT a completion — `hermes kanban block` has no
# `--metadata` flag and a blocked card has no closing run to attach metadata to.
# So the outcome stays encoded as free-text in the block reason here. The native
# `complete --metadata` transport only applies to COMPLETION handoffs (see
# core/iterate/executors.py::_execute_advance). Eliminating free-text transport
# on this blocked/gate path awaits the #1290 DAG work (Phase 2).
_do_transition() {
  local _status="$1" _ec="$2"
  local _reason

  if [ "$_status" = "ok" ]; then
    # Detect open PR for the deterministic feature branch (mirrors daedalus-detect-pr.sh).
    export GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
    local _pr_num=""
    if command -v gh >/dev/null 2>&1; then
      _pr_num="$(gh pr list --repo "$_repo" --head "$_branch" --state open \
                    --json number \
                    --jq '.[0] | select(.number) | .number' \
                    2>/dev/null || echo "")"
      _pr_num="$(printf '%s' "$_pr_num" | tr -d '[:space:]')"
    fi
    if [ -n "$_pr_num" ] && [ "$_pr_num" != "null" ]; then
      _reason="review-required: PR #${_pr_num} — ${_branch}"
    else
      _reason="review-required: awaiting-pr"
    fi
  elif [ "$_status" = "timeout" ]; then
    _reason="coding-agent-failed: CODING_AGENT_TIMEOUT"
  else
    _reason="coding-agent-failed: exited with code ${_ec}"
  fi

  echo "[delegate] transition: block card $_card with: $_reason"
  local _ok=0
  hermes kanban --board "$_board" block "$_card" "$_reason" 2>/dev/null && _ok=1
  if [ "$_ok" -eq 0 ]; then
    sleep 5
    hermes kanban --board "$_board" block "$_card" "$_reason" 2>/dev/null && _ok=1
  fi
  if [ "$_ok" -eq 0 ]; then
    echo "[delegate] WARNING: kanban transition failed — sweeper stale-running detection is the backstop" >&2
    return 1
  fi
  echo "[delegate] transition complete"
  return 0
}

# ── main wait loop ────────────────────────────────────────────────────────────
_exit_code=0
_timed_out=0

while true; do
  # 1. Push-based early completion (C3 done-marker written by inner-agent hook)
  # Finding 4: kill the pgid (not just the pid) and include SIGKILL follow-up
  # after grace, same as _kill_child().
  if [ -f "$_done_marker" ]; then
    echo "[delegate] done-marker found — treating as complete"
    _kill_child
    _exit_code=0
    break
  fi

  # 2. Check if the child has exited naturally
  if ! kill -0 "$_child_pid" 2>/dev/null; then
    wait "$_child_pid" 2>/dev/null
    _exit_code=$?
    echo "[delegate] process exited (PID=$_child_pid exit=$_exit_code)"
    break
  fi

  # 3. Timeout enforcement
  _now_ts="$(date +%s)"
  _elapsed=$(( _now_ts - _start_ts ))
  if [ "$_elapsed" -ge "$_max_wait" ]; then
    echo "[delegate] TIMEOUT after ${_elapsed}s — killing process group (PGID=$_child_pgid)"
    _kill_child
    _timed_out=1
    break
  fi

  # 4. Heartbeat if due (non-blocking — runs in background subshell)
  _hb_elapsed=$(( _now_ts - _last_hb_ts ))
  if [ "$_hb_elapsed" -ge "$_heartbeat_interval" ]; then
    _heartbeat
  fi

  sleep "$_poll_interval"
done

# ── emit result and (optionally) transition the kanban card ──────────────────
if [ "$_timed_out" -eq 1 ]; then
  _emit_result "timeout" 124
  echo "[delegate] done (timeout)"
  if [ "$_transition" -eq 1 ]; then
    _do_transition "timeout" 124 || exit 1
  fi
  exit 124
elif [ "$_exit_code" -eq 0 ]; then
  _emit_result "ok" 0
  echo "[delegate] done (ok)"
  if [ "$_transition" -eq 1 ]; then
    _do_transition "ok" 0 || exit 1
  fi
  exit 0
else
  _emit_result "failed" "$_exit_code"
  echo "[delegate] done (failed exit=$_exit_code)"
  if [ "$_transition" -eq 1 ]; then
    _do_transition "failed" "$_exit_code" || exit 1
  fi
  exit "$_exit_code"
fi

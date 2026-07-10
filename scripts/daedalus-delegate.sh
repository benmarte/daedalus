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
#     [--pr-grace-secs <secs>]      Developer PR grace window (default: 120). After
#                                   the inner agent exits with no PR yet, poll for
#                                   the PR this long before declaring failure (#1375).
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
# CWE-377: ensure all sidecar files are private (mode 600/700).

# ── self-detach into a new session (issue #1356) ─────────────────────────────
# The outer Hermes worker spawns this wrapper as a child in Hermes' own process
# group and then ends its session. On session end / max-runtime enforcement,
# Hermes SIGTERMs that whole process group — killing the wrapper BEFORE it can
# call `hermes kanban complete/block` to transition the card, so the pipeline
# stalls between stages until the next (hourly) cron tick.
#
# Re-exec ourselves in a NEW session so the wrapper leaves Hermes' process group;
# a group-targeted SIGTERM then no longer reaches it and the wrapper lives long
# enough to relay the verdict and transition the card.
#
# Idempotent: only re-exec when we are NOT already a process-group leader
# (pgid != pid). After setsid our pgid == pid, so the guard is false on the
# re-exec'd invocation — no infinite loop. Because we re-exec ONLY when not a
# group leader, setsid(2) succeeds via exec WITHOUT forking, so our PID is
# preserved (the spawning parent keeps tracking the same process) — hence no
# `-f`. Args are passed through verbatim ("$@").
#
# Composes with the inner-agent setsid (~L230): this detaches the wrapper from
# Hermes; that later detaches the coding agent from the wrapper. They act on
# different processes in sequence and never fight. A DIRECT-PID SIGTERM (Hermes
# --max-runtime targets the wrapper pid, not its group) still reaches us and is
# handled by _term_handler (exit 124, no transition) — unchanged.
#
# macOS has no setsid(1); fall back to perl POSIX::setsid(), mirroring the inner
# spawn. If neither exists, continue undetached (best-effort) rather than loop.
_self_pgid="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')"
if [ -n "$_self_pgid" ] && [ "$_self_pgid" != "$$" ]; then
  if command -v setsid >/dev/null 2>&1; then
    exec setsid bash "$0" "$@"
  elif command -v perl >/dev/null 2>&1; then
    exec perl -e 'use POSIX; POSIX::setsid(); exec @ARGV' -- bash "$0" "$@"
  fi
  # No setsid/perl: continue undetached (best-effort). No re-exec → no loop.
fi

umask 077
set -uo pipefail

# ── defaults ─────────────────────────────────────────────────────────────────
_max_wait=3600
_heartbeat_interval=300
_poll_interval=5        # PID liveness check granularity; tests override to 1
_pr_grace_secs=120      # developer PR grace window (#1375): after the inner agent
                        # exits with no PR yet, poll this long before declaring
                        # coding-agent-failed. A slow-but-healthy developer is
                        # frequently mid-`gh pr create` at exit.
_task_file=""
_cmd=""
_card=""
_board=""
_out=""
_repo=""
_branch=""
_base="dev"           # --base <branch>: base to fork the developer worktree from
_transition=0
_relay=0            # --relay-verdict: transition a review/validator card by relaying
                    # the inner agent's emitted verdict/JSON (no PR detection)
_role=""            # --role <role>: the pipeline role of THIS card. Roles that
                    # COMPLETE (validator/pm/planner) are completed on relay; roles
                    # that gate (qa/reviewer/security/accessibility/documentation)
                    # are blocked so classify_blocked routes the signal (#1329 D2).
_start_ts=0         # initialised here so _term_handler can always reference it

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
    --base)               _base="${2:?--base requires a value}";                     shift 2 ;;
    --max-wait)           _max_wait="${2:?--max-wait requires a value}";              shift 2 ;;
    --heartbeat-interval) _heartbeat_interval="${2:?--heartbeat-interval requires a value}"; shift 2 ;;
    --poll-interval)      _poll_interval="${2:?--poll-interval requires a value}";    shift 2 ;;
    --pr-grace-secs)      _pr_grace_secs="${2:?--pr-grace-secs requires a value}";    shift 2 ;;
    --transition)         _transition=1;                                              shift 1 ;;
    --relay-verdict)      _relay=1;                                                    shift 1 ;;
    --role)               _role="${2:-}";                                              shift 2 ;;
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

# ── SIGTERM trap ──────────────────────────────────────────────────────────────
# Hermes enforces --max-runtime by sending SIGTERM to the wrapper process.
# Without this trap the wrapper dies while the setsid-isolated inner agent
# survives as an orphan — recreating the concurrent-dispatch hazard (#1289).
# The handler must be registered before the spawn so it fires even if the
# signal arrives during wrapper startup.  It is self-contained (no calls to
# helpers defined later) so forward-reference ordering is not an issue.
#
# On trap: reap the inner agent's process group cleanly, then emit a
# DELEGATE_RESULT line for forensic log analysis.  Kanban transition is
# intentionally SKIPPED — Hermes owns the requeue when --max-runtime fires.
# EXIT trap below handles RUNDIR cleanup after exit 124.
_term_handler() {
  if [ -n "${_child_pgid:-}" ]; then
    kill -TERM -"$_child_pgid" 2>/dev/null || kill -TERM "${_child_pid:-0}" 2>/dev/null || true
    local _ti=0
    while [ $_ti -lt 5 ] && kill -0 "${_child_pid:-0}" 2>/dev/null; do
      sleep 1; _ti=$(( _ti + 1 ))
    done
    kill -KILL -"$_child_pgid" 2>/dev/null || kill -KILL "${_child_pid:-0}" 2>/dev/null || true
  fi
  local _now_ts; _now_ts="$(date +%s)"
  local _dur=0
  [ "${_start_ts:-0}" -gt 0 ] && _dur=$(( _now_ts - _start_ts ))
  local _esc; _esc="$(printf '%s' "$_out" | sed 's/"/\\"/g')"
  printf 'DELEGATE_RESULT: {"status":"terminated","exit":124,"out":"%s","duration_s":%d}\n' \
    "$_esc" "$_dur"
  exit 124
}
trap '_term_handler' TERM

# ── private sidecar directory ──────────────────────────────────────────────────
# CWE-59 / CWE-377: create all wrapper-internal files in a per-run private dir
# (mode 0700 from umask 077 + mktemp). EXIT trap fires on any exit — including
# exit 124 from _term_handler — so RUNDIR is always cleaned up.
RUNDIR="$(mktemp -d "${TMPDIR:-/tmp}/daedalus-delegate-$$.XXXXXX")"
echo "[delegate] RUNDIR=$RUNDIR"
trap 'rm -rf "${RUNDIR:-}"' EXIT
_pid_file="${RUNDIR}/agent.pid"

# Detect-pr cadence: call daedalus-detect-pr.sh every _detect_pr_every iterations
# (default 6 ≈ 30s at 5s poll) so gh API calls are throttled. PID-liveness,
# timeout, and heartbeat checks remain per-iteration.
_detect_pr_every="${DETECT_PR_EVERY:-6}"

# ── ensure output directory exists ───────────────────────────────────────────
# Finding 5: an unwritable out-dir would look like a crashed agent. Create it
# upfront; emit a distinguishable wrapper-error if we can't.
_out_dir="$(dirname "$_out")"
if ! mkdir -p "$_out_dir" 2>/dev/null; then
  _err="cannot create output directory: $_out_dir"
  echo "[delegate] WRAPPER_ERROR: $_err" >&2
  printf 'DELEGATE_RESULT: {"status":"wrapper-error","exit":2,"out":"%s","duration_s":0}\n' "$_out"
  if [ "$_transition" -eq 1 ] || [ "$_relay" -eq 1 ]; then
    hermes kanban --board "$_board" block "$_card" \
      "coding-agent-failed: wrapper-error: $_err" 2>/dev/null || true
  fi
  exit 2
fi

# ── symlink guard on out-file ─────────────────────────────────────────────────
# CWE-59: refuse to spawn if the caller-specified out-file is already a symlink.
# A pre-planted symlink would redirect the agent's stdout to an attacker-chosen
# path (arbitrary-file-write via redirect).
if [ -L "$_out" ]; then
  echo "[delegate] SECURITY: --out is a symlink — refusing to write (CWE-59): $_out" >&2
  printf 'DELEGATE_RESULT: {"status":"wrapper-error","exit":2,"out":"","duration_s":0}\n'
  exit 2
fi

_done_marker="${_out}.done"
_start_ts="$(date +%s)"

echo "[delegate] starting — card=$_card board=$_board max-wait=${_max_wait}s hb-interval=${_heartbeat_interval}s"
echo "[delegate] cmd: $_cmd"
echo "[delegate] out: $_out"

# ── developer role: isolated per-delegate worktree (#1339 + #1375) ────────
# The developer writes code + opens a PR, so — unlike the review roles — it needs its
# OWN git worktree on a deterministic branch (fix/issue-<N>) forked from the base. This
# is the same isolation the legacy `daedalus-worktree-spawn.sh` gave it (fixes the
# shared-workdir branch/PR cross-wire, #1131), but spawned DIRECTLY here (no qwen hop).
# The coding-agent command then runs with the worktree as its cwd.
#
# #1375 per-delegate isolation: append timestamp + PID suffix so each delegate gets a
# unique worktree path. The deterministic `.worktrees/dev-<N>` path caused concurrent
# delegates (from false-failure re-spawns) to clobber each other's checkout — two agents
# editing one tree is a data-loss hazard. The per-delegate suffix eliminates the collision
# surface; the single-flight guard in direct_dispatch.py prevents the re-spawn in the first
# place, but this is defense-in-depth. Old worktrees accumulate under `.worktrees/` and
# must be pruned by a sweeper (TBD); the hazard of clobbering a live delegate is worse
# than disk bloat until that exists.
if [ "$_role" = "developer" ] && [ -n "$_repo" ] && [ -n "$_branch" ]; then
  _wt="$_repo/.worktrees/dev-${_branch##*issue-}-$(date +%s)-$$"
  {
    echo "[delegate] developer worktree: repo=$_repo base=$_base branch=$_branch wt=$_wt"
    git -C "$_repo" fetch origin "$_base" -q 2>&1 || true
    # Free branch $_branch from ANY worktree that currently holds it (#1404) before
    # creating this delegate's own worktree. A crashed/prior developer for this issue
    # can leave `fix/issue-<N>` checked out at another path; `worktree add -B` then
    # refuses to force-update it ("cannot force update the branch ... used by worktree")
    # and the run used to fall back to the shared repo root (losing branch-race
    # protection). Remove every stale holder so the add below always succeeds. The
    # single-flight guard (#1375/#1404) ensures the freed worktree is stale, not live.
    _main_toplevel="$(git -C "$_repo" rev-parse --show-toplevel 2>/dev/null || true)"
    while IFS= read -r _held; do
      [ -n "$_held" ] || continue
      # Safety guard (#1404 review): never touch the MAIN working tree. If the branch
      # happens to be checked out there (pre-#1404 residual state), `git worktree remove -f`
      # fails (main worktree can't be removed) and a blind `rm -rf` would delete the
      # repo root. Skip it and let the later `worktree add` error path handle it.
      if [ "$_held" = "$_main_toplevel" ]; then
        echo "[delegate] skipping main worktree (branch $_branch checked out at repo root): $_held"
        continue
      fi
      echo "[delegate] freeing $_branch held by worktree: $_held"
      if git -C "$_repo" worktree remove -f "$_held" 2>&1; then
        rm -rf "$_held" 2>&1 || true
      else
        echo "[delegate] worktree remove failed for $_held — leaving path alone"
      fi
    done < <(git -C "$_repo" worktree list --porcelain 2>/dev/null \
               | awk -v b="branch refs/heads/$_branch" '/^worktree /{p=substr($0,10)} $0==b{print p}')
    git -C "$_repo" worktree prune 2>&1 || true
    git -C "$_repo" worktree add -b "$_branch" "$_wt" "origin/$_base" 2>&1 \
      || git -C "$_repo" worktree add -b "$_branch" "$_wt" "$_base" 2>&1 \
      || { echo "[delegate] branch exists — try -B fallback" >&2
           git -C "$_repo" worktree add -f "$_wt" -B "$_branch" "origin/$_base" 2>&1 \
             || git -C "$_repo" worktree add -f "$_wt" -B "$_branch" "$_base" 2>&1 \
             || echo "[delegate] WORKTREE_SETUP_FAILED for $_wt (base=$_base)"; }
  } >>"$_out" 2>&1
  if [ -d "$_wt" ]; then
    _cmd="cd $(printf '%q' "$_wt") && $_cmd"
  else
    # Do NOT fall back to the shared repo root (#1404): running the developer there
    # loses per-issue branch isolation and lets a second developer clobber the tree.
    # Block the card as coding-agent-failed so crash-retry re-spawns from a fresh
    # worktree instead of silently corrupting the shared checkout.
    echo "[delegate] WORKTREE_ABORT: $_wt missing after setup — refusing repo-root fallback (#1404)" >>"$_out" 2>&1
    if [ "$_transition" -eq 1 ] || [ "$_relay" -eq 1 ]; then
      hermes kanban --board "$_board" block "$_card" \
        "coding-agent-failed: worktree setup failed for $_branch" 2>/dev/null || true
    fi
    printf 'DELEGATE_RESULT: {"status":"wrapper-error","exit":2,"out":"%s","duration_s":0}\n' "$_out"
    exit 2
  fi
fi

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
printf '%s\n' "$_child_pid" > "$_pid_file"
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
  # Guard: no-op if the child was never spawned (TERM may arrive before spawn
  # sets _child_pgid).  Also protects the main loop caller; in practice the
  # loop only calls this after spawn, but the guard keeps the function safe
  # at any call site.
  [ -n "${_child_pgid:-}" ] || return 0
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
# ── helper: detect the developer's open PR on the deterministic branch ────────
# Echoes the PR number (numeric only) or empty. Numeric-only validation rejects
# any non-integer to prevent injection into the block-reason string (CWE-74).
# Called once by _do_transition, then repeatedly by the #1375 grace poll.
_detect_developer_pr() {
  local _n=""
  if command -v gh >/dev/null 2>&1 && [ -n "$_repo" ]; then
    _n="$(cd "$_repo" 2>/dev/null && gh pr list --head "$_branch" --state open \
              --json number --jq '.[0] | select(.number) | .number' 2>/dev/null || echo "")"
    _n="$(printf '%s' "$_n" | tr -d '[:space:]')"
    case "$_n" in *[!0-9]*|'') _n="" ;; esac
  fi
  printf '%s' "$_n"
}

_do_transition() {
  local _status="$1" _ec="$2"
  local _reason

  if [ "$_status" = "ok" ] && [ "$_role" = "developer" ]; then
    # Developer (#1339): the deliverable is an OPEN PR, not a verdict. Detect it on the
    # deterministic branch (gh auto-detects the repo from the checkout); complete the
    # card with it so the QA gate opens. No PR => the agent failed/crashed => block as
    # coding-agent-failed so crash-retry re-spawns from a fresh worktree (self-heal).
    #
    # PR grace poll (#1375): a slow-but-healthy developer is frequently still
    # mid-`gh pr create` when its inner process exits — indistinguishable from a
    # dead one by a single PR check. Declaring `coding-agent-failed` immediately
    # makes crash-retry re-spawn a *second* developer onto the same branch/worktree
    # while the first is still finishing (concurrent-delegate data-loss hazard). So
    # when the first check finds no PR, poll for up to `--pr-grace-secs` seconds
    # before concluding failure. A PR that appears in-window completes normally.
    export GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
    local _pr_num=""
    _pr_num="$(_detect_developer_pr)"
    if [ -z "$_pr_num" ] && [ "${_pr_grace_secs:-0}" -gt 0 ]; then
      local _grace_start _grace_now _grace_elapsed
      _grace_start="$(date +%s)"
      echo "[delegate] no PR yet on ${_branch} — entering ${_pr_grace_secs}s PR grace poll (#1375)"
      while [ -z "$_pr_num" ]; do
        _grace_now="$(date +%s)"
        _grace_elapsed=$(( _grace_now - _grace_start ))
        [ "$_grace_elapsed" -ge "$_pr_grace_secs" ] && break
        sleep "$_poll_interval"
        _pr_num="$(_detect_developer_pr)"
      done
    fi
    if [ -n "$_pr_num" ]; then
      _reason="PR #${_pr_num} opened — ${_branch}"
    else
      _reason="coding-agent-failed: no PR detected on ${_branch}"
    fi
  elif [ "$_status" = "ok" ] && [ "$_relay" -eq 1 ]; then
    # Review/validator/pm role: relay the inner agent's emitted verdict (the SOUL
    # signal line and/or the structured JSON OutcomeRecord) from its output file,
    # so the outer (possibly weak) model never has to parse-and-transition itself.
    _reason="$(
      python3 - "$_out" <<'PYEOF' 2>/dev/null || true
import sys, re
try:
    t = open(sys.argv[1], encoding="utf-8", errors="replace").read()
except Exception:
    sys.exit(0)
# Prefer a fenced JSON outcome block (authoritative; parsed by outcomes.py).
m = re.search(r"```(?:json)?\s*\{[^`]*\"daedalus_outcome\"\s*:\s*1[^`]*\}\s*```", t, re.S)
if m:
    print(m.group(0)); sys.exit(0)
# Else the last line that starts with a canonical SOUL signal.
_sig = ("confirmed", "already_fixed", "duplicate", "needs_more_info",
        "security_threat", "block_for_review", "spec:", "assigned:",
        "qa-passed", "qa-failed", "review-approved", "review-changes",
        "security-approved", "security-changes", "security:", "approved:",
        "a11y", "accessibility", "changes requested", "docs posted",
        "planning complete", "plan:", "escalate:", "blocked:", "stop:")
for line in reversed(t.splitlines()):
    s = line.strip()
    if s and any(s.lower().startswith(p) for p in _sig):
        print(s); sys.exit(0)
PYEOF
    )"
    [ -n "$_reason" ] || _reason="coding-agent-failed: no verdict emitted"
  elif [ "$_status" = "ok" ]; then
    # Detect open PR for the deterministic feature branch (mirrors daedalus-detect-pr.sh).
    export GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
    local _pr_num=""
    if command -v gh >/dev/null 2>&1; then
      _pr_num="$(gh pr list --repo "$_repo" --head "$_branch" --state open \
                    --json number \
                    --jq '.[0] | select(.number) | .number' \
                    2>/dev/null || echo "")"
      _pr_num="$(printf '%s' "$_pr_num" | tr -d '[:space:]')"
      # Numeric-only validation: reject any non-integer to prevent injection
      # into the block-reason string (CWE-74).
      case "$_pr_num" in
        *[!0-9]*|'') _pr_num="" ;;
      esac
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

  # Role-aware transition (#1329 D2): validator/pm/planner cards must COMPLETE —
  # the dispatcher advances them via _check_confirmed_validators /
  # _check_completed_{pm,planner}, which scan DONE cards; a *blocked* validator is
  # an ESCALATE error and a blocked pm/planner is a no-op, either of which stalls
  # the pipeline. Gate roles (qa/reviewer/security/accessibility/documentation)
  # must BLOCK so classify_blocked routes the emitted signal. A crash reason
  # (coding-agent-failed:) always blocks so crash-retry owns the card.
  local _do_complete=0
  case "$_role" in
    validator|pm|project-manager|planner|developer) _do_complete=1 ;;
  esac
  case "$_reason" in
    coding-agent-failed:*) _do_complete=0 ;;
  esac
  local _ok=0 _verb="block"
  _kanban_transition() {
    if [ "$_do_complete" -eq 1 ]; then
      # #1329/#1361: if the inner agent self-completed the card despite the relay-mode
      # directive, `complete` no-ops on the already-done card AND returns exit 0, so a
      # `complete || edit` fallback never backfills — latest_summary stays empty and the
      # dispatcher's _check_confirmed_validators / _check_completed_* re-spawn the stage
      # in an empty-summary loop (#1361). Run `complete` (best-effort) then ALWAYS `edit`
      # the verdict onto result+summary (edit succeeds on an already-done card); the edit
      # is the authoritative backfill and its exit status gates the retry below.
      hermes kanban --board "$_board" complete "$_card" \
        --result "$_reason" --summary "$_reason" 2>/dev/null || true
      hermes kanban --board "$_board" edit "$_card" \
        --result "$_reason" --summary "$_reason" 2>/dev/null
    else
      hermes kanban --board "$_board" block "$_card" "$_reason" 2>/dev/null
    fi
  }
  [ "$_do_complete" -eq 1 ] && _verb="complete"
  echo "[delegate] transition: $_verb card $_card (role=${_role:-?}) with: $_reason"
  _kanban_transition && _ok=1
  if [ "$_ok" -eq 0 ]; then
    sleep 5
    _kanban_transition && _ok=1
  fi
  if [ "$_ok" -eq 0 ]; then
    echo "[delegate] WARNING: kanban transition failed — sweeper stale-running detection is the backstop" >&2
    return 1
  fi
  echo "[delegate] transition complete"
  # Near-real-time advance (#1339): the direct-delegate path runs `claude -p` directly,
  # NOT a `hermes -p <role>` session, so Hermes' profile `hooks.on_session_end`
  # (daedalus-advance.sh) never fires for delegated roles — advance would otherwise
  # wait for the next cron tick. Fire the scoped dispatch ourselves, detached, exactly
  # as the session-end hook would, so the next stage starts in seconds.
  if [ -n "$_repo" ]; then
    _advance_cron="$HOME/.hermes/scripts/daedalus-cron.sh"
    if [ -x "$_advance_cron" ] || [ -f "$_advance_cron" ]; then
      echo "[delegate] firing scoped advance dispatch for $_repo (role=${_role:-?})"
      # nohup (not setsid — absent on macOS) detaches from this wrapper so the dispatch
      # survives delegate.sh exiting, without depending on a setsid/perl fallback.
      nohup bash "$_advance_cron" --repo "$_repo" </dev/null \
        >>"$HOME/.hermes/logs/daedalus-advance-dispatch.log" 2>&1 &
    fi
  fi
  return 0
}

# ── main wait loop ────────────────────────────────────────────────────────────
_exit_code=0
_timed_out=0
_loop_iter=0

while true; do
  _loop_iter=$(( _loop_iter + 1 ))

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

  # 5. detect-pr cadence (throttled gh API polling for early PR detection)
  # Only when --transition is active (we expect a PR to be opened) and every
  # _detect_pr_every iterations. On finding a PR, detect-pr.sh writes the
  # handshake line to _out and kills the agent; the next PID-liveness check
  # then breaks the loop naturally.
  if [ "$_transition" -eq 1 ] && [ -n "$_branch" ] && \
     [ $(( _loop_iter % _detect_pr_every )) -eq 0 ]; then
    _script_dir="$(cd "$(dirname "$0")" && pwd)"
    if [ -x "${_script_dir}/daedalus-detect-pr.sh" ]; then
      "${_script_dir}/daedalus-detect-pr.sh" \
        "$_out" "$_pid_file" "$_branch" "${_repo:-}" 2>/dev/null || true
    fi
  fi

  sleep "$_poll_interval"
done

# ── emit result and (optionally) transition the kanban card ──────────────────
if [ "$_timed_out" -eq 1 ]; then
  _emit_result "timeout" 124
  echo "[delegate] done (timeout)"
  if [ "$_transition" -eq 1 ] || [ "$_relay" -eq 1 ]; then
    _do_transition "timeout" 124 || exit 1
  fi
  exit 124
elif [ "$_exit_code" -eq 0 ]; then
  _emit_result "ok" 0
  echo "[delegate] done (ok)"
  if [ "$_transition" -eq 1 ] || [ "$_relay" -eq 1 ]; then
    _do_transition "ok" 0 || exit 1
  fi
  exit 0
else
  _emit_result "failed" "$_exit_code"
  echo "[delegate] done (failed exit=$_exit_code)"
  if [ "$_transition" -eq 1 ] || [ "$_relay" -eq 1 ]; then
    _do_transition "failed" "$_exit_code" || exit 1
  fi
  exit "$_exit_code"
fi

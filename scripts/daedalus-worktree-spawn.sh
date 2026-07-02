#!/usr/bin/env bash
#
# daedalus-worktree-spawn.sh ISSUE_N BASE TASK OUT ERR RUN_CMD...
#
# Isolates each developer's coding-agent run in its OWN git worktree on a
# deterministic branch `fix/issue-<N>`, then runs the agent there. This fixes the
# shared-workdir branch race: with multiple developers active, they all used to
# run in the one shared working tree, so `git checkout -b` and the PR-detection
# `git rev-parse HEAD` cross-wired branches/PRs between issues (a #1131-style
# CODING_AGENT_DIED loop where the agent reported another issue's PR).
#
# Contract:
#   $1 ISSUE_N   issue number (worktree = <repo>/.worktrees/dev-<N>, branch fix/issue-<N>)
#   $2 BASE      base branch to fork from (e.g. dev); defaults to dev
#   $3 TASK      task file piped to the agent's stdin
#   $4 OUT       agent stdout (the "PR URL: ..." handshake line)
#   $5 ERR       agent stderr (also captures worktree-setup diagnostics)
#   $6.. RUN     the coding-agent command (e.g. `CLAUDE_CONFIG_DIR=... claude ... -p`)
#
# Deterministic branch names make retries idempotent (`-B` resets the branch to a
# fresh base) and make PR detection race-free (the caller passes the known branch
# to daedalus-detect-pr.sh instead of reading an ambiguous shared HEAD).
set -uo pipefail

n="${1:?issue number required}"
base="${2:-dev}"
task="${3:?task file required}"
out="${4:?out file required}"
err="${5:?err file required}"
shift 5
run="$*"

WORKDIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
WT="$WORKDIR/.worktrees/dev-$n"
BR="fix/issue-$n"

# Setup diagnostics go to ERR (appended) so a setup failure survives in the same
# stderr tail the outer wait-loop reports on CODING_AGENT_DIED.
{
  echo "[worktree-spawn] issue=$n base=$base workdir=$WORKDIR wt=$WT branch=$BR"
  git -C "$WORKDIR" fetch origin "$base" -q 2>&1 || true
  # Clear any stale worktree at this path (e.g. from a prior retry of this issue)
  # so `worktree add` can recreate it cleanly on a fresh base.
  git -C "$WORKDIR" worktree remove -f "$WT" 2>&1 || true
  rm -rf "$WT" 2>&1 || true
  git -C "$WORKDIR" worktree prune 2>&1 || true
  # Prefer forking from the freshly-fetched remote base; fall back to the local ref.
  git -C "$WORKDIR" worktree add -f "$WT" -B "$BR" "origin/$base" 2>&1 \
    || git -C "$WORKDIR" worktree add -f "$WT" -B "$BR" "$base" 2>&1 \
    || echo "WORKTREE_SETUP_FAILED: could not create $WT on $BR (base=$base)"
} >> "$err"

cd "$WT" 2>/dev/null || {
  echo "WORKTREE_CD_FAILED: falling back to $WORKDIR (branch race protection lost)" >> "$err"
  cd "$WORKDIR" || exit 1
}

# exec so the coding agent inherits this PID — the outer wait-loop's `kill -0`
# liveness check and daedalus-detect-pr.sh's kill target the recorded PID.
exec bash -c "$run" < "$task" > "$out" 2>> "$err"

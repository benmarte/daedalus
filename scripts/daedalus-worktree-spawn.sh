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
  # Free branch $BR from ANY worktree that currently holds it (#1404). A concurrent
  # or crashed prior developer for this issue can leave `fix/issue-<N>` checked out
  # at a DIFFERENT path; `git worktree add -B` then refuses to force-update it
  # ("cannot force update the branch ... used by worktree ...") and the run used to
  # fall back to the shared main tree (losing branch-race protection). Remove every
  # holder first so the re-create below always succeeds on a fresh base. The
  # single-flight guard (#1375/#1404) ensures the freed worktree is stale, not live.
  _main_toplevel="$(git -C "$WORKDIR" rev-parse --show-toplevel 2>/dev/null || true)"
  while IFS= read -r held; do
    [ -n "$held" ] || continue
    # Safety guard (#1404 review): never touch the MAIN working tree. If the branch
    # happens to be checked out there (pre-#1404 residual state), `git worktree remove -f`
    # fails (main worktree can't be removed) and a blind `rm -rf` would delete the
    # repo root. Skip it and let the later `worktree add -B` error path handle it.
    if [ "$held" = "$_main_toplevel" ]; then
      echo "[worktree-spawn] skipping main worktree (branch $BR checked out at repo root): $held"
      continue
    fi
    echo "[worktree-spawn] freeing $BR held by worktree: $held"
    if git -C "$WORKDIR" worktree remove -f "$held" 2>&1; then
      rm -rf "$held" 2>&1 || true
    else
      echo "[worktree-spawn] worktree remove failed for $held — leaving path alone"
    fi
  done < <(git -C "$WORKDIR" worktree list --porcelain 2>/dev/null \
             | awk -v b="branch refs/heads/$BR" '/^worktree /{p=substr($0,10)} $0==b{print p}')
  # Clear the deterministic path itself too (stale worktree from a prior retry).
  git -C "$WORKDIR" worktree remove -f "$WT" 2>&1 || true
  rm -rf "$WT" 2>&1 || true
  git -C "$WORKDIR" worktree prune 2>&1 || true
  # Prefer forking from the freshly-fetched remote base; fall back to the local ref.
  git -C "$WORKDIR" worktree add -f "$WT" -B "$BR" "origin/$base" 2>&1 \
    || git -C "$WORKDIR" worktree add -f "$WT" -B "$BR" "$base" 2>&1 \
    || echo "WORKTREE_SETUP_FAILED: could not create $WT on $BR (base=$base)"
} >> "$err"

cd "$WT" 2>/dev/null || {
  # Do NOT fall back to the shared main working tree (#1404): running the developer
  # there loses per-issue branch isolation and lets a second developer clobber the
  # checkout. Fail hard so the outer wait-loop reports CODING_AGENT_DIED and the
  # card blocks for a clean re-dispatch instead of silently corrupting the shared tree.
  echo "WORKTREE_ABORT: $WT unavailable after setup — refusing shared-tree fallback (#1404)" >> "$err"
  exit 1
}

# exec so the coding agent inherits this PID — the outer wait-loop's `kill -0`
# liveness check and daedalus-detect-pr.sh's kill target the recorded PID.
exec bash -c "$run" < "$task" > "$out" 2>> "$err"

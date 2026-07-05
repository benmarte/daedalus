#!/usr/bin/env bash
#
# daedalus-detect-pr.sh OUT_FILE [PID_FILE] [BRANCH]
#
# Provider-side completion handshake for the developer role (issue #146).
#
# The delegated coding agent runs as `claude -p`, which writes its final stdout
# (the "PR URL: ... PR number: <n>" line the wrapper waits for) ONLY when the
# process exits. In practice the agent can open the PR via its tools and then
# keep running (extra turns), so the wrapper's out-file stays empty and the
# developer card sits `running` until `coding_agent_max_wait` fires — then
# retries, spawning a SECOND agent that opens a DUPLICATE PR. See issue #146.
#
# This helper decouples completion from the agent exiting: if an OPEN PR already
# exists for the agent's current branch, it writes the same handshake line the
# wrapper expects to OUT_FILE and kills the still-running agent (PID in PID_FILE),
# so the wait loop exits immediately and the card advances to review.
#
# Quiet no-op (exit 0, writes nothing) when: gh is unavailable, the provider is
# not GitHub, the branch is a base branch, or no open PR exists yet. In those
# cases the wrapper falls back to its existing PID-liveness / timeout behavior,
# so this is purely additive and safe to call every poll.
#
# GitLab/other providers: gh is GitHub-only, so this no-ops there today; the
# wrapper's stdout-parse + timeout path still applies. A provider-aware detector
# is a follow-up.
set -uo pipefail

out="${1:-}"
pidfile="${2:-}"
branch="${3:-}"
repo="${4:-}"
[ -n "$out" ] || exit 0

command -v gh >/dev/null 2>&1 || exit 0
# gh reads GH_TOKEN then GITHUB_TOKEN; make sure one is exported for headless runs.
export GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"

# Prefer an explicitly-passed branch (the caller knows the deterministic
# fix/issue-<N> branch). This is race-free: reading `git HEAD` from the shared
# workdir would report whatever branch another concurrent agent left checked
# out, so a developer could be handed a DIFFERENT issue's PR (the #1131 loop).
br="$branch"
[ -n "$br" ] || br="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
[ -n "$br" ] || exit 0
case "$br" in
  HEAD|main|master|dev|develop|trunk) exit 0 ;;  # never fire on a base branch
esac

# When an explicit repo is passed (e.g. from daedalus-delegate.sh running in a
# non-git directory), export GH_REPO so gh can resolve the remote without git.
[ -n "$repo" ] && export GH_REPO="$repo"

# .[0] guards against multiple matches; select(.number) drops the null case so a
# branch with no open PR yields empty output (and we exit without touching OUT).
line="$(gh pr list --head "$br" --state open --json number,url \
          --jq '.[0] | select(.number) | "PR URL: \(.url) PR number: \(.number)"' \
          2>/dev/null || true)"
[ -n "$line" ] || exit 0

printf '%s\n' "$line" > "$out"

# The agent's work is done (PR is open); stop it so it can't burn more turns or
# open follow-up PRs. Best-effort — a dead/missing PID is fine.
if [ -n "$pidfile" ] && [ -f "$pidfile" ]; then
  p="$(cat "$pidfile" 2>/dev/null || true)"
  [ -n "$p" ] && kill "$p" 2>/dev/null || true
fi
exit 0

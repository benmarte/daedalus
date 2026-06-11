#!/usr/bin/env bash
#
# provision_roster.sh — idempotent provisioning of the native-Hermes lifecycle role roster.
#
# Creates six specialist profiles, each loading ONLY its lifecycle agent-skills, so the
# native Kanban decomposer can route a triage card through the agent-skills development
# lifecycle (planner -> developer -> reviewer -> security-analyst -> documentation, with
# project-manager owning intake + acceptance).
#
# Strategy: delete + recreate each role so the end state is identical every run (this script
# is the source of truth). Profiles are cloned from `default` for model + provider keys,
# created with --no-skills (no bundled skill packs -> genuinely specialized + opts out of
# `hermes update` skill re-sync), then seeded with exactly their matrix skills and a
# GITHUB_TOKEN in .env (so `gh` works inside the isolated profile HOME — Phase-0 blocker #2).
#
# Re-run any time to reset the roster to spec. Safe: only touches the six role names below.

set -euo pipefail

HERMES="${HERMES_HOME:-$HOME/.hermes}"
SRC="$HERMES/plugins/agent-skills/skills"
PROFILES="$HERMES/profiles"

if [ ! -d "$SRC" ]; then
  echo "FATAL: agent-skills source not found at $SRC" >&2
  exit 1
fi

# Token + git identity the workers use for gh/git. By DEFAULT this falls back to the host's own
# `gh` login with no commit-author override. To run the roster under a dedicated BOT identity — so
# PRs, commits, comments and reviews are attributed to the bot instead of your personal account —
# export these before running (or drop the token in ~/.hermes/.roster_bot_token, one line, chmod 600):
#   ROSTER_GH_TOKEN   the bot's GitHub token (machine-user fine-grained PAT, or App installation token)
#   ROSTER_BOT_NAME   git commit author name,  e.g. "RIZQ AI Agent"
#   ROSTER_BOT_EMAIL  git commit author email — the bot's GitHub no-reply address
#                     (machine user: <id>+<login>@users.noreply.github.com ; App: <appid>+<slug>[bot]@users.noreply.github.com)
GH_TOKEN="${ROSTER_GH_TOKEN:-}"
if [ -z "$GH_TOKEN" ] && [ -f "$HOME/.hermes/.roster_bot_token" ]; then
  GH_TOKEN="$(tr -d '\n' < "$HOME/.hermes/.roster_bot_token")"
fi
if [ -n "$GH_TOKEN" ]; then
  echo "Roster GitHub identity: BOT${ROSTER_BOT_NAME:+ ($ROSTER_BOT_NAME)}"
else
  GH_TOKEN="$(gh auth token 2>/dev/null || true)"
  echo "Roster GitHub identity: host gh user (set ROSTER_GH_TOKEN to use a bot)"
  [ -z "$GH_TOKEN" ] && echo "WARN: no bot token and 'gh auth token' is empty — profiles will lack gh auth" >&2
fi

# Remove legacy / stray profiles from earlier spikes so the roster is exactly the six below.
for legacy in builder probe-role; do
  hermes profile delete "$legacy" -y >/dev/null 2>&1 || true
done

setup_role() {
  local name="$1"; local desc="$2"; shift 2
  local skills=("$@")
  echo "=== ${name} ==="

  # Recreate fresh for a clean, uniform, specialized profile.
  # --clone copies config.yaml/.env/SOUL.md (model + provider keys) from the active (default)
  # profile. It also copies the full bundled skill set; we wipe that and reseed below so the
  # profile loads ONLY its lifecycle skills. (--no-skills can't combine with --clone.)
  hermes profile delete "$name" -y >/dev/null 2>&1 || true
  hermes profile create "$name" --clone --description "$desc" >/dev/null

  # Reseed skills to EXACTLY the matrix: nuke the cloned skill set, keep only agent-skills.
  local dst="$PROFILES/$name/skills/agent-skills"
  rm -rf "${PROFILES:?}/$name/skills"
  mkdir -p "$dst"
  for s in "${skills[@]}"; do
    if [ -d "$SRC/$s" ]; then
      cp -R "$SRC/$s" "$dst/$s"
    else
      echo "  WARN: source skill missing: $s" >&2
    fi
  done

  # Make `gh` work inside the worker. NOTE: putting GITHUB_TOKEN in the profile .env is NOT
  # enough — the kanban worker runs `gh` via the `terminal` tool, whose shell only inherits vars
  # listed in `terminal.env_passthrough` (default []), and each worker has an isolated HOME so the
  # host gh keychain isn't visible. The reliable fix is to authenticate gh INTO the profile's HOME
  # (writes ~/.config/gh/hosts.yml there), independent of any env plumbing.
  if [ -n "$GH_TOKEN" ]; then
    local home_dir="$PROFILES/$name/home"
    mkdir -p "$home_dir"
    # Keychain-free: the isolated HOME must never invoke osxkeychain. Workers
    # authenticate via the gh token / hosts.yml, not the macOS login keychain.
    env HOME="$home_dir" git config --global credential.helper ""
    printf '%s' "$GH_TOKEN" | env -u GH_TOKEN -u GITHUB_TOKEN GH_PROMPT_DISABLED=1 HOME="$home_dir" gh auth login --with-token --insecure-storage 2>/dev/null || echo "  WARN: gh auth login failed for $name" >&2
    # Belt-and-suspenders: also drop it in .env for any tool that reads the env directly.
    local env_file="$PROFILES/$name/.env"
    touch "$env_file"
    grep -q '^GITHUB_TOKEN=' "$env_file" 2>/dev/null || printf '\nGITHUB_TOKEN=%s\n' "$GH_TOKEN" >> "$env_file"
    # Attribute git commits to the bot (workers run with HOME=<profile>/home → this gitconfig).
    if [ -n "${ROSTER_BOT_NAME:-}" ] && [ -n "${ROSTER_BOT_EMAIL:-}" ]; then
      env HOME="$home_dir" git config --global user.name  "$ROSTER_BOT_NAME"
      env HOME="$home_dir" git config --global user.email "$ROSTER_BOT_EMAIL"
    fi
  fi

  # NOTE: project-specific conventions (base branch, pre-commit policy, …) are NOT
  # seeded here — the provisioner stays project-agnostic. They live in each repo's
  # .hermes/daedalus.yaml and the triage card body generated by the dispatcher.

  echo "  skills: $(ls "$dst" 2>/dev/null | tr '\n' ' ')"
}

# ── Role -> agent-skills matrix (lifecycle-aligned, 6-agent lean team) ──────────────────

setup_role project-manager \
  "Refines an issue into clear scope and acceptance criteria, breaks it into work, tracks acceptance, and runs the pre-ship checklist. Coordinates the team; writes no code." \
  idea-refine spec-driven-development planning-and-task-breakdown shipping-and-launch using-agent-skills

setup_role planner \
  "Turns a spec into an ordered, verifiable task graph and stable interface contracts. Owns architecture and decomposition; writes no code." \
  spec-driven-development planning-and-task-breakdown context-engineering source-driven-development api-and-interface-design using-agent-skills

setup_role developer \
  "Implements features and bug fixes: writes code plus tests in a git worktree, drives CI to green with no conflicts, and opens a PR." \
  context-engineering source-driven-development incremental-implementation test-driven-development frontend-ui-engineering api-and-interface-design debugging-and-error-recovery git-workflow-and-versioning using-agent-skills

setup_role reviewer \
  "Reviews diffs for correctness, quality, and performance; approves or blocks with specific, actionable findings." \
  code-review-and-quality code-simplification performance-optimization test-driven-development debugging-and-error-recovery git-workflow-and-versioning using-agent-skills

setup_role security-analyst \
  "Audits diffs for vulnerabilities (OWASP, authn/z, secrets, injection, SSRF); blocks on risk with severity-rated findings." \
  security-and-hardening code-review-and-quality source-driven-development debugging-and-error-recovery using-agent-skills

setup_role documentation \
  "Writes and updates READMEs, ADRs, and changelogs from merged work; verifies documentation against the actual code." \
  documentation-and-adrs source-driven-development context-engineering using-agent-skills

echo
echo "=== roster provisioned ==="
hermes profile list

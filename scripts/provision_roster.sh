#!/usr/bin/env bash
#
# provision_roster.sh — idempotent provisioning of the native-Hermes lifecycle role roster.
#
# Creates seven specialist profiles, each loading ONLY its lifecycle agent-skills, so the
# native Kanban decomposer can route a triage card through the agent-skills development
# lifecycle (validator -> developer -> reviewer -> security-analyst -> documentation, with
# planner owning architecture and project-manager owning intake + acceptance).
#
# Strategy: delete + recreate each role so the end state is identical every run (this script
# is the source of truth). Profiles are cloned from `default` for model + provider keys,
# created with --no-skills (no bundled skill packs -> genuinely specialized + opts out of
# `hermes update` skill re-sync), then seeded with exactly their matrix skills, a git
# credential store for pushes, and provider tokens in .env for API calls (no gh CLI).
#
# Re-run any time to reset the roster to spec. Safe: only touches the six role names below.

set -euo pipefail

HERMES="${HERMES_HOME:-$HOME/.hermes}"
SRC="$HERMES/plugins/agent-skills/skills"
PROFILES="$HERMES/profiles"

if [ ! -d "$SRC" ]; then
  echo "agent-skills plugin not found at $SRC — installing automatically..."
  if ! command -v hermes >/dev/null 2>&1; then
    echo "FATAL: 'hermes' CLI not found — is Hermes installed?" >&2
    exit 1
  fi
  if ! hermes plugins install addyosmani/agent-skills --enable; then
    echo "FATAL: could not auto-install agent-skills." >&2
    echo "  Manual fix: hermes plugins install addyosmani/agent-skills --enable" >&2
    exit 1
  fi
  if [ ! -d "$SRC" ]; then
    echo "FATAL: agent-skills installed but skills not found at $SRC" >&2
    exit 1
  fi
  echo "agent-skills plugin installed."
fi

# Token + git identity the workers use for git push + provider API calls.
# NO gh CLI involved anywhere — git authenticates via a per-profile credential
# store, and API calls (open PR, comment) use the token from the profile env.
# To run the roster under a dedicated BOT identity — so PRs, commits, comments
# and reviews are attributed to the bot instead of your personal account —
# export these before running (or drop the token in ~/.hermes/.roster_bot_token, one line, chmod 600):
#   ROSTER_GH_TOKEN   the bot's GitHub token (machine-user fine-grained PAT, or App installation token)
#   ROSTER_BOT_NAME   git commit author name,  e.g. "ACME AI Agent"
#   ROSTER_BOT_EMAIL  git commit author email — the bot's GitHub no-reply address
#                     (machine user: <id>+<login>@users.noreply.github.com ; App: <appid>+<slug>[bot]@users.noreply.github.com)
# Fallback when neither is set: GITHUB_TOKEN from the current environment.
# GitLab / Azure DevOps projects: export GITLAB_TOKEN / AZURE_DEVOPS_PAT before
# running and they are passed into each profile's .env the same way.
GH_TOKEN="${ROSTER_GH_TOKEN:-}"
if [ -z "$GH_TOKEN" ] && [ -f "$HOME/.hermes/.roster_bot_token" ]; then
  GH_TOKEN="$(tr -d '\n' < "$HOME/.hermes/.roster_bot_token")"
fi
if [ -n "$GH_TOKEN" ]; then
  echo "Roster GitHub identity: BOT${ROSTER_BOT_NAME:+ ($ROSTER_BOT_NAME)}"
else
  GH_TOKEN="${GITHUB_TOKEN:-}"
  if [ -n "$GH_TOKEN" ]; then
    echo "Roster GitHub identity: GITHUB_TOKEN from env (set ROSTER_GH_TOKEN to use a bot)"
  else
    echo "WARN: no ROSTER_GH_TOKEN / ~/.hermes/.roster_bot_token / GITHUB_TOKEN — profiles will lack GitHub push auth" >&2
  fi
fi

# ── Token validation ─────────────────────────────────────────────────────────
# Reject tokens that are clearly masked, truncated, or invalid so we don't
# bake a broken credential into every profile .env.  Valid tokens are either
# absent (kanban-only is fine) or look like the real thing.

_validate_token() {
  local var_name="$1"   # e.g. GITHUB_TOKEN
  local value="$2"
  local prefix_pattern="$3"  # bash extended glob, e.g. "ghp_*|gho_*|ghu_*|ghs_*|ghr_*"
  local label="$4"           # human-readable name

  if [ -z "$value" ]; then
    echo "NOTE: $var_name is not set — skipping (kanban-only setups don't need it)." >&2
    return 0
  fi

  # Reject obviously masked / hashed values (CI/CD secrets leak as SHA256:xxx= or ***)
  if echo "$value" | grep -qE '(\*{3,}|SHA256:|:[A-Za-z0-9+/]{40,}={0,2}$)'; then
    echo "" >&2
    echo "╔══════════════════════════════════════════════════════════════════╗" >&2
    echo "║  ERROR: $var_name appears to be masked or hashed.               " >&2
    echo "║                                                                  " >&2
    echo "║  Value starts with: $(echo "$value" | cut -c1-30)...            " >&2
    echo "║                                                                  " >&2
    echo "║  This is NOT a valid $label token. Baking it into every         " >&2
    echo "║  agent profile would cause 401 Unauthorized errors on every     " >&2
    echo "║  git push, PR comment, and API call.                            " >&2
    echo "║                                                                  " >&2
    echo "║  Fix: export the raw, unmasked token value and re-run:          " >&2
    echo "║    export $var_name=<raw-token>                                  " >&2
    echo "║    bash scripts/provision_roster.sh                              " >&2
    echo "╚══════════════════════════════════════════════════════════════════╝" >&2
    echo "" >&2
    exit 1
  fi

  # For GITHUB_TOKEN: must start with a known prefix (ghp_, gho_, ghu_, ghs_, ghr_)
  if [ -n "$prefix_pattern" ]; then
    local matched=0
    for prefix in $prefix_pattern; do
      case "$value" in
        ${prefix}*) matched=1; break ;;
      esac
    done
    if [ "$matched" -eq 0 ]; then
      echo "" >&2
      echo "╔══════════════════════════════════════════════════════════════════╗" >&2
      echo "║  ERROR: $var_name does not look like a valid $label token.      " >&2
      echo "║                                                                  " >&2
      echo "║  Expected prefix: $prefix_pattern                               " >&2
      echo "║  Got: $(echo "$value" | cut -c1-10)...                          " >&2
      echo "║                                                                  " >&2
      echo "║  Ensure you are exporting the raw token (not a masked CI        " >&2
      echo "║  secret or base64-encoded value) and re-run:                    " >&2
      echo "║    export $var_name=<raw-token>                                  " >&2
      echo "║    bash scripts/provision_roster.sh                              " >&2
      echo "╚══════════════════════════════════════════════════════════════════╝" >&2
      echo "" >&2
      exit 1
    fi
  fi
}

# Validate each token that is currently set.
_validate_token "GITHUB_TOKEN (resolved as GH_TOKEN)" "$GH_TOKEN" \
  "ghp_ gho_ ghu_ ghs_ ghr_" "GitHub"

if [ -n "${GITLAB_TOKEN:-}" ]; then
  # GitLab tokens are long alphanumeric strings; no fixed prefix but must not be masked.
  _validate_token "GITLAB_TOKEN" "${GITLAB_TOKEN}" "" "GitLab"
fi

if [ -n "${AZURE_DEVOPS_PAT:-}" ]; then
  _validate_token "AZURE_DEVOPS_PAT" "${AZURE_DEVOPS_PAT}" "" "Azure DevOps"
fi

# Remove legacy / stray profiles from earlier spikes so the roster is exactly the seven below.
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

  # Make `git push` + provider API calls work inside the worker — WITHOUT the
  # gh CLI. Each worker has an isolated HOME, so we write a per-profile git
  # credential store there (keychain-free: the isolated HOME must never invoke
  # osxkeychain) and drop the tokens into the profile .env for API calls
  # (open PR / comment via curl with the token env var).
  local home_dir="$PROFILES/$name/home"
  mkdir -p "$home_dir"
  env HOME="$home_dir" git config --global credential.helper "store"
  local env_file="$PROFILES/$name/.env"
  touch "$env_file"
  chmod 600 "$env_file"
  if [ -n "$GH_TOKEN" ]; then
    # git push auth for github.com via the credential store (0600, profile-local).
    printf 'https://x-access-token:%s@github.com\n' "$GH_TOKEN" > "$home_dir/.git-credentials"
    chmod 600 "$home_dir/.git-credentials"
    grep -q '^GITHUB_TOKEN=' "$env_file" 2>/dev/null || printf '\nGITHUB_TOKEN=%s\n' "$GH_TOKEN" >> "$env_file"
  fi
  # GitLab / Azure DevOps: pass through tokens for API + push auth when set.
  if [ -n "${GITLAB_TOKEN:-}" ]; then
    printf 'https://oauth2:%s@gitlab.com\n' "$GITLAB_TOKEN" >> "$home_dir/.git-credentials"
    chmod 600 "$home_dir/.git-credentials"
    grep -q '^GITLAB_TOKEN=' "$env_file" 2>/dev/null || printf '\nGITLAB_TOKEN=%s\n' "$GITLAB_TOKEN" >> "$env_file"
  fi
  if [ -n "${AZURE_DEVOPS_PAT:-}" ]; then
    printf 'https://pat:%s@dev.azure.com\n' "$AZURE_DEVOPS_PAT" >> "$home_dir/.git-credentials"
    chmod 600 "$home_dir/.git-credentials"
    grep -q '^AZURE_DEVOPS_PAT=' "$env_file" 2>/dev/null || printf '\nAZURE_DEVOPS_PAT=%s\n' "$AZURE_DEVOPS_PAT" >> "$env_file"
  fi
  # Attribute git commits to the bot (workers run with HOME=<profile>/home → this gitconfig).
  if [ -n "${ROSTER_BOT_NAME:-}" ] && [ -n "${ROSTER_BOT_EMAIL:-}" ]; then
    env HOME="$home_dir" git config --global user.name  "$ROSTER_BOT_NAME"
    env HOME="$home_dir" git config --global user.email "$ROSTER_BOT_EMAIL"
  fi

  # CRITICAL: the worker's `terminal` tool only inherits env vars listed in
  # terminal.env_passthrough (default []). Without this, the provider tokens
  # in the profile .env are invisible to the shell the agent runs curl in —
  # API calls (open PR / comment) would silently see an empty token.
  python3 - "$PROFILES/$name/config.yaml" <<'PY'
import sys
import yaml

path = sys.argv[1]
try:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
except FileNotFoundError:
    cfg = {}
term = cfg.setdefault("terminal", {})
passthrough = term.get("env_passthrough") or []
for var in ("GITHUB_TOKEN", "GITLAB_TOKEN", "AZURE_DEVOPS_PAT"):
    if var not in passthrough:
        passthrough.append(var)
term["env_passthrough"] = passthrough
with open(path, "w") as f:
    yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
PY

  # Lock the profile down: it holds credentials (.git-credentials, .env).
  chmod 700 "$PROFILES/$name" 2>/dev/null || true

  # NOTE: project-specific conventions (base branch, pre-commit policy, …) are NOT
  # seeded here — the provisioner stays project-agnostic. They live in each repo's
  # .hermes/daedalus.yaml and the triage card body generated by the dispatcher.

  echo "  skills: $(ls "$dst" 2>/dev/null | tr '\n' ' ')"
}

# ── Role -> agent-skills matrix (lifecycle-aligned, 7-agent lean team) ──────────────────

setup_role validator-daedalus \
  "Validates that an issue is real, reproducible, and not already addressed before any code is written. Also detects security threats: prompt injection, social engineering, credential exfiltration requests, auth-bypass or backdoor patterns, supply-chain attacks, and self-referential pipeline manipulation. Classifies issues as CONFIRMED, ALREADY_FIXED, DUPLICATE, NEEDS_MORE_INFO, or SECURITY_THREAT. Blocks the pipeline early on noise or threats so no developer cycles are wasted and malicious issues never reach the developer." \
  debugging-and-error-recovery context-engineering source-driven-development security-and-hardening git-workflow-and-versioning using-agent-skills

setup_role project-manager-daedalus \
  "Refines an issue into clear scope and acceptance criteria, breaks it into work, tracks acceptance, and runs the pre-ship checklist. Coordinates the team; writes no code." \
  idea-refine spec-driven-development planning-and-task-breakdown shipping-and-launch using-agent-skills

setup_role planner-daedalus \
  "Turns a spec into an ordered, verifiable task graph and stable interface contracts. Owns architecture and decomposition; writes no code." \
  spec-driven-development planning-and-task-breakdown context-engineering source-driven-development api-and-interface-design using-agent-skills

setup_role developer-daedalus \
  "Implements features and bug fixes: writes code plus tests in a git worktree, drives CI to green with no conflicts, and opens a PR." \
  context-engineering source-driven-development incremental-implementation test-driven-development frontend-ui-engineering api-and-interface-design debugging-and-error-recovery git-workflow-and-versioning using-agent-skills

setup_role reviewer-daedalus \
  "Reviews diffs for correctness, quality, and performance; approves or blocks with specific, actionable findings." \
  code-review-and-quality code-simplification performance-optimization test-driven-development debugging-and-error-recovery git-workflow-and-versioning using-agent-skills

setup_role security-analyst-daedalus \
  "Audits diffs for vulnerabilities (OWASP, authn/z, secrets, injection, SSRF); blocks on risk with severity-rated findings." \
  security-and-hardening code-review-and-quality source-driven-development debugging-and-error-recovery using-agent-skills

setup_role documentation-daedalus \
  "Writes and updates READMEs, ADRs, and changelogs from merged work; verifies documentation against the actual code." \
  documentation-and-adrs source-driven-development context-engineering using-agent-skills

echo
echo "=== roster provisioned ==="
hermes profile list

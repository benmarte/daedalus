#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# setup.sh — scaffold daedalus config into a target repo
#
# Run from inside the repo you want the daedalus to track:
#
#     cd /path/to/my-project
#     bash /path/to/daedalus/scripts/setup.sh
#
# Options:
#     --force          Overwrite an existing .hermes/daedalus.yaml
#     --name <value>   Override the project name (default: repo short name,
#                      e.g. 'daedalus' from 'benmarte/daedalus')
#
# What it does:
#   1. Derives name (repo short name from remote), repo (owner/repo from git
#      remote), workdir (pwd)
#   2. Scaffolds .hermes/daedalus.yaml from templates/daedalus.yaml
#   3. Registers the repo path in ~/.hermes/daedalus/projects (idempotent)
#
# Environment:
#   HERMES_ORCH_REGISTRY — override the registry file path (default:
#     ~/.hermes/daedalus/projects).  Passed through to core.registry.
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Resolve the daedalus install root ────────────────────────────────────
# This script lives at <daedalus-root>/scripts/setup.sh.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORCH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TEMPLATE="$ORCH_ROOT/templates/daedalus.yaml"
if [[ ! -f "$TEMPLATE" ]]; then
  echo "FATAL: template not found at $TEMPLATE" >&2
  exit 1
fi

# ── Parse flags ──────────────────────────────────────────────────────────────
FORCE=false
NAME_OVERRIDE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=true; shift ;;
    --name)  NAME_OVERRIDE="$2"; shift 2 ;;
    --name=*) NAME_OVERRIDE="${1#*=}"; shift ;;
    *)       echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

# ── Validate we are inside a git repo ────────────────────────────────────────
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "FATAL: this script must be run inside a git repository." >&2
  exit 1
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# ── Derive identity ──────────────────────────────────────────────────────────

# repo: owner/repo normalized from origin remote
REMOTE_URL="$(git remote get-url origin 2>/dev/null || true)"
if [[ -z "$REMOTE_URL" ]]; then
  echo "FATAL: no 'origin' remote found. Set one with: git remote add origin <url>" >&2
  exit 1
fi

# Normalise to owner/repo.  Handles:
#   https://github.com/owner/repo.git
#   git@github.com:owner/repo.git
#   ssh://git@github.com/owner/repo.git
#
# Strategy: strip the optional .git suffix first (bash regex is POSIX ERE
# and does not support non-greedy quantifiers), then match.
CLEAN_URL="$(echo "$REMOTE_URL" | sed 's/\.git$//')"
REPO=""
if [[ "$CLEAN_URL" =~ github\.com[:/]([^/]+/[^/]+)$ ]]; then
  REPO="${BASH_REMATCH[1]}"
elif [[ "$CLEAN_URL" =~ ^https?://.*/([^/]+/[^/]+)$ ]]; then
  REPO="${BASH_REMATCH[1]}"
elif [[ "$CLEAN_URL" =~ ^git@([^:]+):([^/]+/[^/]+)$ ]]; then
  REPO="${BASH_REMATCH[2]}"
elif [[ "$CLEAN_URL" =~ ^ssh://.*/([^/]+/[^/]+)$ ]]; then
  REPO="${BASH_REMATCH[1]}"
fi

if [[ -z "$REPO" ]]; then
  echo "FATAL: could not parse owner/repo from remote URL: $REMOTE_URL" >&2
  exit 1
fi

# name: repo short name (the part after the '/', e.g. 'daedalus' from 'benmarte/daedalus'),
# overridable with --name
if [[ -n "$NAME_OVERRIDE" ]]; then
  NAME="$NAME_OVERRIDE"
else
  NAME="${REPO##*/}"
fi

# workdir: absolute repo root
WORKDIR="$REPO_ROOT"

# ── Scaffold .hermes/daedalus.yaml ───────────────────────────────────────

CONFIG_DIR="$REPO_ROOT/.hermes"
CONFIG_FILE="$CONFIG_DIR/daedalus.yaml"

if [[ -f "$CONFIG_FILE" ]] && ! $FORCE; then
  echo "SKIP: $CONFIG_FILE already exists.  Use --force to overwrite."
else
  mkdir -p "$CONFIG_DIR"

  # Substitute placeholders into the template.
  sed \
    -e "s|{{NAME}}|$NAME|g" \
    -e "s|{{REPO}}|$REPO|g" \
    -e "s|{{WORKDIR}}|$WORKDIR|g" \
    "$TEMPLATE" > "$CONFIG_FILE"

  echo "Created $CONFIG_FILE"
fi

# ── Register in the daedalus project registry ────────────────────────────

PYTHONPATH="$ORCH_ROOT" python3 -c 'import sys; from core.registry import add_project; print("Registry: " + ("added" if add_project(sys.argv[1]) else "already present"))' "$WORKDIR"

# ── Create the kanban board (idempotent, non-fatal) ──────────────────────
# Derive board slug the same way the dispatcher does (_board_slug):
#   org/repo → org-repo, lowercased, non-alnum → '-', trim.
_board_slug() {
  local slug="${1:-$2}"
  slug="${slug//\//-}"
  slug="$(echo "$slug" | tr '[:upper:]' '[:lower:]')"
  slug="$(echo "$slug" | sed 's/[^a-z0-9_-]/-/g' | sed 's/^-//;s/-$//')"
  echo "${slug:-$2}"
}
BOARD_SLUG="$(_board_slug "$REPO" "$NAME")"
if command -v hermes >/dev/null 2>&1; then
  # boards create exits 0 on success OR already-exists.  Treat nonzero
  # with "already exists" stderr as success too (idempotent).
  set +e
  CREATE_OUT="$(hermes kanban boards create "$BOARD_SLUG" 2>&1)"
  CREATE_RC=$?
  set -e
  if [[ $CREATE_RC -eq 0 ]]; then
    echo "Kanban board: $BOARD_SLUG (created)"
  elif echo "$CREATE_OUT" | grep -qi 'already.exists'; then
    echo "Kanban board: $BOARD_SLUG (exists)"
  else
    echo "WARNING: could not create kanban board '$BOARD_SLUG': $CREATE_OUT" >&2
    # Non-fatal — registry and config are already written.
  fi
else
  echo "WARNING: 'hermes' not on PATH — kanban board '$BOARD_SLUG' not created" >&2
fi

echo "Done.  Edit $CONFIG_FILE to configure tracking, sources, and cron for this project."

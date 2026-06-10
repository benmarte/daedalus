#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# uninstall.sh — clean up daedalus host-side artifacts
#
# Usage:
#   bash ~/.hermes/plugins/daedalus/scripts/uninstall.sh [--roster] [--help]
#
# What it cleans (idempotent — safe to re-run):
#   - $HERMES_HOME/daedalus.yaml   (legacy global multi-project config)
#   - $HERMES_HOME/daedalus/        (registry dir / projects file)
#   - $HERMES_HOME/agent-hooks/ship-gate.sh + ship-gate.d/
#   - Daedalus cron jobs (script "daedalus-*.sh" or name ends "-daedalus")
#
# With --roster: also deletes the 6 role profiles.
#
# Manual follow-ups (NOT done automatically — destructive/data):
#   - hermes plugins uninstall daedalus
#   - hermes profile delete <role> -y  for 6 roles  (automated with --roster)
#   - hermes kanban boards rm <slug>
# ──────────────────────────────────────────────────────────────────────────────

# Safety: fail on unset variables and pipe failures, but NOT blanket -e —
# we want to keep going past missing files so re-runs are no-ops.
set -uo pipefail

# ── Safety guard: refuse to operate on an unsafe HERMES path ────────────────
# Defense-in-depth — a bad HERMES_HOME must never turn the rm -rf steps below
# into a footgun.  Refuse root, $HOME, empty/unset, non-directory, and any
# directory that doesn't look like a Hermes home.
HERMES="${HERMES_HOME:-${HOME:+$HOME/.hermes}}"
if [[ -z "$HERMES" ]]; then
  echo "FATAL: refusing to run — could not resolve a Hermes home (HERMES_HOME and HOME both unset)" >&2
  exit 1
fi
if [[ "$HERMES" == "/" ]] || [[ "$HERMES" == "$HOME" ]]; then
  echo "FATAL: refusing to run — '$HERMES' is unsafe (filesystem or home root)" >&2
  exit 1
fi
if [[ ! -d "$HERMES" ]]; then
  echo "FATAL: refusing to run — '$HERMES' is not a directory" >&2
  exit 1
fi

_is_hermes_home() {
  [[ -f "$HERMES/config.yaml" ]] && return 0
  [[ "$(basename "$HERMES")" == ".hermes" ]] && return 0
  return 1
}

if ! _is_hermes_home; then
  echo "FATAL: refusing to run — '$HERMES' is not a valid Hermes home" >&2
  exit 1
fi

ROSTER=false
HELP=false

# ── Parse flags ──────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --roster) ROSTER=true; shift ;;
    --help)   HELP=true; shift ;;
    -h)       HELP=true; shift ;;
    *)        echo "Unknown option: $1" >&2
              echo "Usage: bash scripts/uninstall.sh [--roster] [--help]" >&2
              exit 2 ;;
  esac
done

if $HELP; then
  echo "Usage: bash scripts/uninstall.sh [--roster] [--help]"
  echo ""
  echo "Clean up daedalus host-side artifacts from \$HERMES_HOME."
  echo "Idempotent — safe to re-run; absent items are skipped, not errors."
  echo ""
  echo "Cleans:"
  echo "  \$HERMES_HOME/daedalus.yaml     (legacy global config)"
  echo "  \$HERMES_HOME/daedalus/          (registry dir)"
  echo "  \$HERMES_HOME/agent-hooks/ship-gate.sh + ship-gate.d/"
  echo "  Daedalus cron jobs (script 'daedalus-*.sh' or name *-daedalus)"
  echo ""
  echo "Options:"
  echo "  --roster   Also delete the 6 role profiles (developer, reviewer,"
  echo "             security-analyst, documentation, planner, project-manager)"
  echo "  --help     Show this help and exit"
  echo ""
  echo "Manual follow-ups (NOT done automatically):"
  echo "  hermes plugins uninstall daedalus"
  echo "  hermes kanban boards rm <slug>"
  exit 0
fi

# ── Track what happened ──────────────────────────────────────────────────────
REMOVED=()
SKIPPED=()

# ── 1. Legacy global config ──────────────────────────────────────────────────
if [[ -f "$HERMES/daedalus.yaml" ]]; then
  rm -f "$HERMES/daedalus.yaml"
  REMOVED+=("$HERMES/daedalus.yaml")
else
  SKIPPED+=("$HERMES/daedalus.yaml (not present)")
fi

# ── 2. Registry dir ──────────────────────────────────────────────────────────
if [[ -d "$HERMES/daedalus" ]]; then
  rm -rf "$HERMES/daedalus"
  REMOVED+=("$HERMES/daedalus/")
else
  SKIPPED+=("$HERMES/daedalus/ (not present)")
fi

# ── 3. Ship-gate hook ────────────────────────────────────────────────────────
SHIP_GATE_SH="$HERMES/agent-hooks/ship-gate.sh"
SHIP_GATE_D="$HERMES/agent-hooks/ship-gate.d"

if [[ -f "$SHIP_GATE_SH" ]]; then
  rm -f "$SHIP_GATE_SH"
  REMOVED+=("$SHIP_GATE_SH")
else
  SKIPPED+=("$SHIP_GATE_SH (not present)")
fi

if [[ -d "$SHIP_GATE_D" ]]; then
  rm -rf "$SHIP_GATE_D"
  REMOVED+=("$SHIP_GATE_D/")
else
  SKIPPED+=("$SHIP_GATE_D/ (not present)")
fi

# Check for the hook config entry — just tell the user, don't edit YAML in bash.
if [[ -f "$HERMES/config.yaml" ]]; then
  if grep -q 'ship-gate' "$HERMES/config.yaml" 2>/dev/null; then
    echo ""
    echo "NOTE: A 'ship-gate' reference was detected in $HERMES/config.yaml."
    echo "  Remove the hooks.pre_tool_call entry for 'ship-gate' manually if it's"
    echo "  still there — this script does NOT hand-edit YAML."
  fi
fi

# ── 4. Daedalus cron jobs ────────────────────────────────────────────────
# Detect jobs whose Script is daedalus-*.sh OR whose name ends in -daedalus.
CRON_LIST="$(hermes cron list --all 2>/dev/null || true)"
if [[ -n "$CRON_LIST" ]]; then
  # Extract job names from the list output. The format looks like:
  #   jobname │ schedule │ script │ status
  # We grep for daedalus patterns in the Script column or name column.
  while IFS= read -r line; do
    # Skip header/footer lines
    [[ -z "$line" ]] && continue
    # Extract job name (first word/field)
    JOB_NAME="${line%% *}"
    # Check if it matches daedalus patterns
    if echo "$line" | grep -qE 'daedalus-[^ ]*\.sh|/[^ ]*-daedalus[^ ]*'; then
      if hermes cron remove "$JOB_NAME" 2>/dev/null; then
        REMOVED+=("cron job: $JOB_NAME")
      else
        SKIPPED+=("cron job: $JOB_NAME (removal failed)")
      fi
    fi
  done < <(echo "$CRON_LIST" | grep -i 'daedalus')
fi

# ── 5. --roster: delete role profiles ────────────────────────────────────────
if $ROSTER; then
  # ── Source-of-truth note: the 6 role names here MUST match provision_roster.sh.
  #     Update both if the roster changes so they don't silently drift.
  ROLES=(
    developer
    reviewer
    security-analyst
    documentation
    planner
    project-manager
  )
  for role in "${ROLES[@]}"; do
    # Attempt the delete and classify by exit code — robust against the table
    # format of `hermes profile list` (a grep pre-check on the listing is
    # fragile: each row is "<name>   <model>   <gateway> ...", not a bare name).
    if hermes profile delete "$role" -y >/dev/null 2>&1; then
      REMOVED+=("profile: $role")
    else
      SKIPPED+=("profile: $role (not present)")
    fi
  done
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo "  daedalus uninstall — summary"
echo "══════════════════════════════════════════"
echo ""

if [[ ${#REMOVED[@]} -gt 0 ]]; then
  echo "Removed:"
  for item in "${REMOVED[@]}"; do
    echo "  ✓ $item"
  done
  echo ""
fi

if [[ ${#SKIPPED[@]} -gt 0 ]]; then
  echo "Skipped (already clean):"
  for item in "${SKIPPED[@]}"; do
    echo "  - $item"
  done
  echo ""
fi

if [[ ${#REMOVED[@]} -eq 0 ]] && [[ ${#SKIPPED[@]} -gt 0 ]]; then
  echo "Nothing to remove — daedalus is already cleaned up."
fi

echo ""
echo "Manual follow-ups (not done by this script):"
echo "  1. Uninstall the plugin package:"
echo "       hermes plugins uninstall daedalus"
echo ""
echo "  2. Remove kanban boards:"
echo "       hermes kanban boards ls              # list boards"
echo "       hermes kanban boards rm <slug>       # remove each"
echo ""
echo "  3. If you used the ship-gate hook, remove its entry from"
echo "     hooks.pre_tool_call in $HERMES/config.yaml"
echo ""
if ! $ROSTER; then
  echo "  4. To also delete the 6 role profiles, re-run with --roster"
fi
echo ""
echo "Re-run this script any time — it's idempotent."

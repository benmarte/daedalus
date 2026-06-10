#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# uninstall.sh — clean up daedalus host-side artifacts
#
# Usage:
#   bash scripts/uninstall.sh [-y|--yes] [--keep-profiles] [--roster] [--help]
#
# What it cleans (idempotent — safe to re-run):
#   - $HERMES_HOME/daedalus.yaml   (legacy global multi-project config)
#   - $HERMES_HOME/daedalus/        (registry dir / projects file)
#   - $HERMES_HOME/agent-hooks/ship-gate.sh + ship-gate.d/
#   - Daedalus cron jobs (script "daedalus-*.sh" or name ends "-daedalus")
#   - Daedalus kanban boards (non-default boards found via hermes kanban boards ls)
#   - Roster profiles (by default; skip with --keep-profiles)
#   - Dashboard tab (hermes plugins disable daedalus)
#
# The script now shows a data-loss summary BEFORE removing anything and
# requires confirmation (unless -y/--yes). --roster is a no-op alias for
# back-compat — profiles are removed by default now; use --keep-profiles to
# keep them.
#
# Manual follow-ups (NOT done automatically — destructive/data):
#   - hermes plugins uninstall daedalus
# ──────────────────────────────────────────────────────────────────────────────

# Safety: fail on pipe failures, but NOT blanket -e or -u —
# we want to keep going past missing files so re-runs are no-ops.
# -u is too aggressive — empty arrays in discovery-phase loops trip
# "unbound variable" when hermes CLI calls fail in minimal test setups.
set -o pipefail

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

# ── Parse flags ──────────────────────────────────────────────────────────────
YES=false
KEEP_PROFILES=false
HELP=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --roster) shift ;;             # no-op alias for back-compat (profiles now removed by default)
    --keep-profiles) KEEP_PROFILES=true; shift ;;
    -y|--yes) YES=true; shift ;;
    --help|-h) HELP=true; shift ;;
    *)        echo "Unknown option: $1" >&2
              echo "Usage: bash scripts/uninstall.sh [-y|--yes] [--keep-profiles] [--roster] [--help]" >&2
              exit 2 ;;
  esac
done

if $HELP; then
  echo "Usage: bash scripts/uninstall.sh [-y|--yes] [--keep-profiles] [--roster] [--help]"
  echo ""
  echo "Clean up daedalus host-side artifacts from \$HERMES_HOME."
  echo "Idempotent — safe to re-run; absent items are skipped, not errors."
  echo ""
  echo "Shows a data-loss summary BEFORE removing anything. Requires"
  echo "confirmation (interactive) unless -y/--yes is passed."
  echo ""
  echo "Cleans:"
  echo "  \$HERMES_HOME/daedalus.yaml     (legacy global config)"
  echo "  \$HERMES_HOME/daedalus/          (registry dir)"
  echo "  \$HERMES_HOME/agent-hooks/ship-gate.sh + ship-gate.d/"
  echo "  Daedalus cron jobs (script 'daedalus-*.sh' or name *-daedalus)"
  echo "  Daedalus kanban boards (never removes 'default')"
  echo "  Roster profiles (by default; skip with --keep-profiles)"
  echo "  Dashboard tab (hermes plugins disable daedalus)"
  echo ""
  echo "Options:"
  echo "  -y, --yes        Skip confirmation prompt (non-interactive)"
  echo "  --keep-profiles  Keep the 6 role profiles (developer, reviewer,"
  echo "                   security-analyst, documentation, planner, project-manager)"
  echo "  --roster         Accepted no-op (back-compat — profiles now removed by default)"
  echo "  --help           Show this help and exit"
  echo ""
  echo "Manual follow-ups (NOT done automatically):"
  echo "  hermes plugins uninstall daedalus"
  exit 0
fi

# ══════════════════════════════════════════════════════════════════════════════
# DISCOVERY PHASE — discover what WILL be removed (do NOT modify anything)
# ══════════════════════════════════════════════════════════════════════════════

# ── 1. Host artifacts ────────────────────────────────────────────────────────
HOST_ARTIFACTS=()
[[ -f "$HERMES/daedalus.yaml" ]] && HOST_ARTIFACTS+=("$HERMES/daedalus.yaml")
[[ -d "$HERMES/daedalus" ]] && HOST_ARTIFACTS+=("$HERMES/daedalus/")
SHIP_GATE_SH="$HERMES/agent-hooks/ship-gate.sh"
SHIP_GATE_D="$HERMES/agent-hooks/ship-gate.d"
[[ -f "$SHIP_GATE_SH" ]] && HOST_ARTIFACTS+=("$SHIP_GATE_SH")
[[ -d "$SHIP_GATE_D" ]] && HOST_ARTIFACTS+=("$SHIP_GATE_D/")

# ── 2. Roster profiles ───────────────────────────────────────────────────────
ROLES=(
  developer
  reviewer
  security-analyst
  documentation
  planner
  project-manager
)
FOUND_PROFILES=()
for role in "${ROLES[@]}"; do
  if hermes profile list 2>/dev/null | grep -qw "$role"; then
    FOUND_PROFILES+=("$role")
  fi
done

# ── 3. Daedalus cron jobs ────────────────────────────────────────────────────
FOUND_CRON=()
CRON_LIST="$(hermes cron list --all 2>/dev/null || true)"
if [[ -n "$CRON_LIST" ]]; then
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    # Match: Script is daedalus-*.sh or name ends -daedalus
    if echo "$line" | grep -qE 'daedalus-[^ ]*\.sh|/[^ ]*-daedalus'; then
      JOB_NAME="${line%% *}"
      FOUND_CRON+=("$JOB_NAME")
    fi
  done < <(echo "$CRON_LIST" | grep -iF 'daedalus' || true)
fi

# ── 4. Kanban boards (never default) ─────────────────────────────────────────
FOUND_BOARDS=()
BOARDS_OUT="$(hermes kanban boards ls 2>/dev/null || true)"
if [[ -n "$BOARDS_OUT" ]]; then
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    # Skip header line
    [[ "$line" =~ ^SLUG ]] && continue
    # Skip "Current board:" footer line
    [[ "$line" =~ ^Current ]] && continue
    # Strip leading bullet/indent, then take first word as slug
    CLEAN="${line#●}"
    CLEAN="${CLEAN#"${CLEAN%%[![:space:]]*}"}"  # lstrip whitespace
    SLUG="${CLEAN%% *}"
    [[ -z "$SLUG" ]] && continue
    [[ "$SLUG" == "default" ]] && continue
    FOUND_BOARDS+=("$SLUG")
  done <<< "$BOARDS_OUT"
fi

# ── 5. Dashboard tab ─────────────────────────────────────────────────────────
DASHBOARD_ENABLED=false
if hermes plugins list --enabled 2>/dev/null | grep -qi 'daedalus'; then
  DASHBOARD_ENABLED=true
fi

# ── Check ship-gate hook config ──────────────────────────────────────────────
CONFIG_HOOK_REF=false
if [[ -f "$HERMES/config.yaml" ]]; then
  if grep -q 'ship-gate' "$HERMES/config.yaml" 2>/dev/null; then
    CONFIG_HOOK_REF=true
  fi
fi

# ══════════════════════════════════════════════════════════════════════════════
# DATA-LOSS SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

echo ""
echo "══════════════════════════════════════════"
echo "  daedalus uninstall — what will be removed"
echo "══════════════════════════════════════════"
echo ""

any_found=false

echo "Host artifacts:"
if [[ ${#HOST_ARTIFACTS[@]} -gt 0 ]]; then
  for item in "${HOST_ARTIFACTS[@]}"; do
    echo "  • $item"
  done
  any_found=true
else
  echo "  (none found)"
fi

echo ""
echo "Roster profiles"
if $KEEP_PROFILES; then
  echo "  (kept — --keep-profiles flag is set)"
elif [[ ${#FOUND_PROFILES[@]} -gt 0 ]]; then
  for role in "${FOUND_PROFILES[@]}"; do
    echo "  • profile: $role"
  done
  any_found=true
else
  echo "  (none found)"
fi

echo ""
echo "Cron jobs:"
if [[ ${#FOUND_CRON[@]} -gt 0 ]]; then
  for job in "${FOUND_CRON[@]}"; do
    echo "  • $job"
  done
  any_found=true
else
  echo "  (none found)"
fi

echo ""
echo "Kanban boards (never removes 'default'):"
if [[ ${#FOUND_BOARDS[@]} -gt 0 ]]; then
  for board in "${FOUND_BOARDS[@]}"; do
    echo "  • $board"
  done
  any_found=true
else
  echo "  (none found)"
fi

echo ""
echo "Dashboard tab:"
if $DASHBOARD_ENABLED; then
  echo "  • will run: hermes plugins disable daedalus"
  any_found=true
else
  echo "  (not enabled)"
fi

if $CONFIG_HOOK_REF; then
  echo ""
  echo "  NOTE: A 'ship-gate' reference was detected in $HERMES/config.yaml."
  echo "  Remove the hooks.pre_tool_call entry for 'ship-gate' manually if it's"
  echo "  still there — this script does NOT hand-edit YAML."
fi

echo ""
echo "⚠  This permanently removes the above Daedalus data and cannot be undone."
echo ""

# If nothing was found at all, exit early.
if ! $any_found && ! $DASHBOARD_ENABLED && ! $CONFIG_HOOK_REF; then
  echo "Nothing to remove — daedalus is already cleaned up."
  echo ""
  echo "Manual follow-ups (not done by this script):"
  echo "  hermes plugins uninstall daedalus"
  echo ""
  exit 0
fi

# ══════════════════════════════════════════════════════════════════════════════
# CONFIRMATION
# ══════════════════════════════════════════════════════════════════════════════

if $YES; then
  echo "Proceeding (--yes flag set)..."
  echo ""
else
  echo -n "Continue? [y/N] "
  read -r CONFIRM
  if [[ ! "$CONFIRM" =~ ^[yY]$ ]]; then
    echo ""
    echo "Aborted, nothing removed."
    exit 0
  fi
  echo ""
fi

# ══════════════════════════════════════════════════════════════════════════════
# REMOVAL PHASE
# ══════════════════════════════════════════════════════════════════════════════

REMOVED=()
SKIPPED=()

# ── 1. Host artifacts ────────────────────────────────────────────────────────
for item in "${HOST_ARTIFACTS[@]}"; do
  if [[ -f "$item" ]]; then
    rm -f "$item"
    REMOVED+=("$item")
  elif [[ -d "$item" ]]; then
    rm -rf "$item"
    REMOVED+=("$item")
  fi
done
# Also check ship-gate even if not in the discovered list (race-safe):
if [[ -f "$SHIP_GATE_SH" ]]; then
  rm -f "$SHIP_GATE_SH"
  REMOVED+=("$SHIP_GATE_SH")
fi
if [[ -d "$SHIP_GATE_D" ]]; then
  rm -rf "$SHIP_GATE_D"
  REMOVED+=("$SHIP_GATE_D/")
fi

# ── 2. Dashboard tab: disable plugin ─────────────────────────────────────────
if $DASHBOARD_ENABLED; then
  if hermes plugins disable daedalus >/dev/null 2>&1; then
    REMOVED+=("dashboard tab (hermes plugins disable daedalus)")
    echo ""
    echo "NOTE: The daedalus dashboard tab has been disabled."
    echo "  Restart the dashboard for the change to take effect."
  else
    SKIPPED+=("dashboard tab (hermes plugins disable daedalus failed — try manually)")
  fi
fi

# ── 3. Cron jobs ─────────────────────────────────────────────────────────────
for job_name in "${FOUND_CRON[@]}"; do
  if hermes cron remove "$job_name" 2>/dev/null; then
    REMOVED+=("cron job: $job_name")
  else
    SKIPPED+=("cron job: $job_name (removal failed)")
  fi
done

# ── 4. Profiles (removed by default unless --keep-profiles) ──────────────────
if ! $KEEP_PROFILES; then
  for role in "${FOUND_PROFILES[@]}"; do
    if hermes profile delete "$role" -y >/dev/null 2>&1; then
      REMOVED+=("profile: $role")
    else
      SKIPPED+=("profile: $role (deletion failed)")
    fi
  done
fi

# ── 5. Kanban boards (never default) ─────────────────────────────────────────
for board in "${FOUND_BOARDS[@]}"; do
  if [[ "$board" == "default" ]]; then
    SKIPPED+=("kanban board: default (never removed)")
    continue
  fi
  if hermes kanban boards rm "$board" >/dev/null 2>&1; then
    REMOVED+=("kanban board: $board")
  else
    SKIPPED+=("kanban board: $board (removal failed — may not exist)")
  fi
done

# ══════════════════════════════════════════════════════════════════════════════
# FINAL SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

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
  echo "Skipped:"
  for item in "${SKIPPED[@]}"; do
    echo "  - $item"
  done
  echo ""
fi

if [[ ${#REMOVED[@]} -eq 0 ]] && [[ ${#SKIPPED[@]} -gt 0 ]]; then
  echo "Nothing was removed."
fi

echo ""
echo "Manual follow-ups (not done by this script):"
echo "  hermes plugins uninstall daedalus"
echo ""

if $KEEP_PROFILES; then
  echo "  Profiles were kept (--keep-profiles). To remove them later:"
  for role in "${ROLES[@]}"; do
    echo "    hermes profile delete $role -y"
  done
  echo ""
fi

echo "Re-run this script any time — it's idempotent."

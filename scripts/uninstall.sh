#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# uninstall.sh — complete daedalus uninstaller (host state + plugin package)
#
# Usage:
#   bash scripts/uninstall.sh [-y|--yes] [--keep-profiles] [--keep-plugin] [--roster] [--help]
#
# What it cleans (idempotent — safe to re-run):
#   - $HERMES_HOME/daedalus.yaml   (legacy global multi-project config)
#   - $HERMES_HOME/daedalus/        (registry dir / projects file)
#   - $HERMES_HOME/agent-hooks/ship-gate.sh + ship-gate.d/
#   - Daedalus cron jobs (script "daedalus-*.sh" or name ends "-daedalus")
#   - Daedalus kanban boards (non-default boards found via hermes kanban boards ls)
#   - Roster profiles (by default; skip with --keep-profiles)
#   - Dashboard tab (hermes plugins disable daedalus)
#   - Plugin package (by default; skip with --keep-plugin — removed deferred
#     as the final action so the running script doesn't delete itself mid-run)
#
# The script shows a data-loss summary BEFORE removing anything and
# requires confirmation (unless -y/--yes). --roster is a no-op alias for
# back-compat — profiles are removed by default now; use --keep-profiles to
# keep them.
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
KEEP_PLUGIN=false
HELP=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --roster) shift ;;             # no-op alias for back-compat (profiles now removed by default)
    --keep-profiles) KEEP_PROFILES=true; shift ;;
    --keep-plugin) KEEP_PLUGIN=true; shift ;;
    -y|--yes) YES=true; shift ;;
    --help|-h) HELP=true; shift ;;
    *)        echo "Unknown option: $1" >&2
              echo "Usage: bash scripts/uninstall.sh [-y|--yes] [--keep-profiles] [--keep-plugin] [--roster] [--help]" >&2
              exit 2 ;;
  esac
done

if $HELP; then
  echo "Usage: bash scripts/uninstall.sh [-y|--yes] [--keep-profiles] [--keep-plugin] [--roster] [--help]"
  echo ""
  echo "Clean up daedalus host-side artifacts from \$HERMES_HOME and remove the plugin."
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
  echo "  Plugin package (by default; skip with --keep-plugin)"
  echo ""
  echo "Options:"
  echo "  -y, --yes        Skip confirmation prompt (non-interactive)"
  echo "  --keep-profiles  Keep the 6 role profiles (developer, reviewer,"
  echo "                   security-analyst, documentation, planner, project-manager)"
  echo "  --keep-plugin    Keep the plugin package installed (skip deferred removal)"
  echo "  --roster         Accepted no-op (back-compat — profiles now removed by default)"
  echo "  --help           Show this help and exit"
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
# Parse the cron list into blocks.  A new block starts at a line matching
#   ^\s*[0-9a-fA-F]{6,}\s+\[
# (e.g. "  ba57e4afbba0 [active]").  Inside each block, capture the Name:
# and Script: values.  Collect the name from any block whose Name ends in
# "-daedalus" OR whose Script matches "daedalus-*.sh".
FOUND_CRON=()
CRON_LIST="$(hermes cron list --all 2>/dev/null || true)"
if [[ -n "$CRON_LIST" ]]; then
  _in_block=false
  _cron_name=""
  _cron_script=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^[[:space:]]*[0-9a-fA-F]{6,}[[:space:]]+\[ ]]; then
      # Flush previous block
      if $_in_block && [[ -n "$_cron_name" ]]; then
        if [[ "$_cron_name" == *-daedalus ]] || [[ "$_cron_script" =~ daedalus-[^/]*\.sh$ ]]; then
          FOUND_CRON+=("$_cron_name")
        fi
      fi
      _in_block=true
      _cron_name=""
      _cron_script=""
      continue
    fi
    if $_in_block; then
      # Extract Name: and Script: fields from the block
      if [[ "$line" =~ ^[[:space:]]*Name:[[:space:]]+(.*) ]]; then
        _cron_name="${BASH_REMATCH[1]}"
        _cron_name="${_cron_name%%[[:space:]]*}"  # trim trailing whitespace
      elif [[ "$line" =~ ^[[:space:]]*Script:[[:space:]]+(.*) ]]; then
        _cron_script="${BASH_REMATCH[1]}"
        _cron_script="${_cron_script%%[[:space:]]*}"  # trim trailing whitespace
      fi
    fi
  done <<< "$CRON_LIST"
  # Flush the final block
  if $_in_block && [[ -n "$_cron_name" ]]; then
    if [[ "$_cron_name" == *-daedalus ]] || [[ "$_cron_script" =~ daedalus-[^/]*\.sh$ ]]; then
      FOUND_CRON+=("$_cron_name")
    fi
  fi
  # Dedup (in case name and script both matched the same block)
  FOUND_CRON=( $(printf '%s\n' "${FOUND_CRON[@]}" | sort -u) )
fi

# ── 4. Kanban boards (only registry-derived daedalus slugs) ───────────────────
# Before removing the registry (step 1 below), read registered project paths
# and derive their board slugs the same way the dispatcher does
# (_board_slug = org/repo -> org-repo, lowercased, non-alnum -> -).
# Only remove these slugs — never "default", never the table header/footer,
# never boards not derived from a registered daedalus project.
# If the registry file is already gone, skip board removal entirely.
_build_board_slug() {
  # $1 = repo (org/repo), $2 = fallback name
  local _slug="${1:-$2}"
  _slug="${_slug//\//-}"
  _slug="$(echo "$_slug" | tr '[:upper:]' '[:lower:]')"
  _slug="$(echo "$_slug" | sed 's/[^a-z0-9_-]/-/g' | sed 's/--*/-/g' | sed 's/^-//;s/-$//')"
  echo "${_slug:-$2}"
}

FOUND_BOARDS=()
REGISTRY_FILE="$HERMES/daedalus/projects"
if [[ -f "$REGISTRY_FILE" ]]; then
  # Read registered workdir paths, derive repo + board slug for each
  while IFS= read -r workdir_line || [[ -n "$workdir_line" ]]; do
    workdir_line="$(echo "$workdir_line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [[ -z "$workdir_line" || "$workdir_line" == \#* ]] && continue

    # Try to read the daedalus.yaml in the workdir to get the repo
    repo=""
    name=""
    if [[ -f "$workdir_line/.hermes/daedalus.yaml" ]]; then
      repo="$(grep -E '^[[:space:]]*repo:' "$workdir_line/.hermes/daedalus.yaml" 2>/dev/null | head -1 | sed 's/.*repo:[[:space:]]*//;s/[[:space:]]*$//')"
      name="$(grep -E '^[[:space:]]*name:' "$workdir_line/.hermes/daedalus.yaml" 2>/dev/null | head -1 | sed 's/.*name:[[:space:]]*//;s/[[:space:]]*$//')"
    fi

    if [[ -n "$repo" ]]; then
      board_slug="$(_build_board_slug "$repo" "$name")"
      if [[ -n "$board_slug" && "$board_slug" != "default" ]]; then
        FOUND_BOARDS+=("$board_slug")
      fi
    fi
  done < "$REGISTRY_FILE"

  # Dedup
  if [[ ${#FOUND_BOARDS[@]} -gt 0 ]]; then
    FOUND_BOARDS=( $(printf '%s\n' "${FOUND_BOARDS[@]}" | sort -u) )
  fi
fi

# ── 5. Dashboard tab ─────────────────────────────────────────────────────────
# Always attempt to disable — gating on the enablement list is unreliable
# (daedalus may not show as "enabled" even when listed in config.yaml
# plugins.enabled).  The disable command is idempotent, so calling it
# unconditionally is safe.
DASHBOARD_DISABLE=true

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
echo "  • will run: hermes plugins disable daedalus"
any_found=true

echo ""
echo "Plugin package:"
if $KEEP_PLUGIN; then
  echo "  (kept — --keep-plugin flag is set)"
else
  echo "  • will run: hermes plugins remove daedalus"
  any_found=true
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
if ! $any_found && ! $CONFIG_HOOK_REF; then
  echo "Nothing to remove — daedalus is already cleaned up."
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

# ── 2. Dashboard tab: disable plugin (unconditional — idempotent) ───────────
if hermes plugins disable daedalus >/dev/null 2>&1; then
  REMOVED+=("dashboard tab (hermes plugins disable daedalus)")
  echo ""
  echo "NOTE: The daedalus dashboard tab has been disabled."
  echo "  Restart the dashboard for the change to take effect."
else
  # May fail if daedalus is already disabled or the command is unavailable;
  # this is harmless — report as skipped rather than error.
  SKIPPED+=("dashboard tab (hermes plugins disable daedalus failed — may already be disabled)")
fi

# ── 2b. Strip the lingering plugins.enabled/.disabled entry from config.yaml ──
# `hermes plugins disable` only MOVES daedalus from plugins.enabled to
# plugins.disabled, and `hermes plugins remove` (core) never touches either list
# — so a `- daedalus` entry lingers in config.yaml after a full uninstall. Remove
# it with a TARGETED line edit (never round-trip the YAML through a parser) so all
# comments and unrelated structure are preserved. Only ever drops a line that is
# exactly `  - daedalus` inside the plugins: block under enabled:/disabled:.
_CFG="$HERMES/config.yaml"
if [[ -f "$_CFG" ]] && grep -qE '^[[:space:]]+-[[:space:]]+daedalus[[:space:]]*$' "$_CFG"; then
  _cfg_tmp="$(mktemp)"
  if awk '
    /^[^[:space:]#]/ { if ($0 ~ /^plugins:/) { inp=1; inlist=0 } else { inp=0; inlist=0 } }
    inp && /^[[:space:]]+enabled:/  { inlist=1; print; next }
    inp && /^[[:space:]]+disabled:/ { inlist=1; print; next }
    inp && inlist && /^[[:space:]]+-[[:space:]]+daedalus[[:space:]]*$/ { next }
    { print }
  ' "$_CFG" > "$_cfg_tmp" && [[ -s "$_cfg_tmp" ]] && ! cmp -s "$_CFG" "$_cfg_tmp"; then
    cp "$_CFG" "$_CFG.daedalus-uninstall.bak" 2>/dev/null || true
    mv "$_cfg_tmp" "$_CFG"
    REMOVED+=("config.yaml plugins.enabled/.disabled daedalus entry")
  else
    rm -f "$_cfg_tmp"
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
  if hermes kanban boards rm "$board" --delete >/dev/null 2>&1; then
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

if $KEEP_PLUGIN; then
  echo "Plugin package was kept (--keep-plugin). To remove it later:"
  echo "  hermes plugins remove daedalus"
  echo ""
fi

if $KEEP_PROFILES; then
  echo "Profiles were kept (--keep-profiles). To remove them later:"
  for role in "${ROLES[@]}"; do
    echo "    hermes profile delete $role -y"
  done
  echo ""
fi

if $CONFIG_HOOK_REF; then
  echo "  NOTE: A 'ship-gate' reference was detected in $HERMES/config.yaml."
  echo "  Remove the hooks.pre_tool_call entry for 'ship-gate' manually if it's"
  echo "  still there — this script does NOT hand-edit YAML."
  echo ""
fi

echo "Re-run this script any time — it's idempotent."
echo ""

# ══════════════════════════════════════════════════════════════════════════════
# PLUGIN REMOVAL (synchronous — must run before exit so restart sees a clean state)
# ══════════════════════════════════════════════════════════════════════════════
# Running synchronously is safe even when this script lives inside the plugin
# directory: bash has already read and parsed the script before we delete it.
if ! $KEEP_PLUGIN; then
  echo "Removing the plugin package… (daedalus)"
  if hermes plugins remove daedalus >/dev/null 2>&1; then
    echo "  ✓ plugin removed"
  else
    echo "  - plugin removal failed (may already be removed)"
  fi
fi

exit 0

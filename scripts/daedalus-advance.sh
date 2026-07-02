#!/usr/bin/env bash
# Fires the daedalus dispatcher when a daedalus agent session ends — SCOPED to the
# finishing worker's project so a hook-triggered sweep cannot leak another
# project's cards onto the wrong board (a global all-projects sweep run as a child
# of an active board worker was the cross-project leak vector).
# Reads JSON payload from stdin (Hermes shell hook wire protocol).
set -euo pipefail

payload=$(cat -)
profile=$(echo "$payload" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('extra',{}).get('profile',''))" 2>/dev/null || echo "")

# Also check env var set by hermes kanban worker
profile="${profile:-${HERMES_PROFILE:-}}"

# Only react to daedalus pipeline agents.
if [[ "$profile" != *"daedalus"* ]]; then
  printf '{}\n'
  exit 0
fi

# Resolve which project this worker belongs to (task id -> board -> registry
# project) and advance ONLY that project.
repo_path=$(python3 "$HOME/.hermes/agent-hooks/daedalus_resolve_project.py" "$payload" 2>/dev/null || echo "")

ts=$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo '')
log="$HOME/.hermes/logs/daedalus-advance.log"
# Dispatch output is captured (not /dev/null) so a dispatch dropped on lock
# contention is visible instead of silently stalling the handoff (issue #1160).
dispatch_log="$HOME/.hermes/logs/daedalus-advance-dispatch.log"
mkdir -p "$HOME/.hermes/logs" 2>/dev/null || true
if [[ -n "$repo_path" ]]; then
  echo "$ts advance: scoped dispatch --repo $repo_path (profile=$profile)" >>"$log" 2>/dev/null || true
  {
    echo "$ts advance-dispatch: --repo $repo_path (profile=$profile)"
    bash "$HOME/.hermes/scripts/daedalus-cron.sh" --repo "$repo_path" </dev/null
  } >>"$dispatch_log" 2>&1 &
else
  # Could NOT resolve the project — do not run a global sweep (that is what leaked
  # cross-project cards). The next scheduled cron tick advances it instead.
  echo "$ts advance: could not resolve project (profile=$profile) — skipping global sweep; cron will catch up. payload=$payload" >>"$log" 2>/dev/null || true
fi
printf '{}\n'

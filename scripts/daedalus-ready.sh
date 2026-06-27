#!/usr/bin/env bash
# Hermes webhook handler for daedalus event-driven dispatch.
# Reads webhook payload from stdin, normalizes via core/webhook_normalizer.py,
# and fires the dispatcher if the item moved to the Ready column.
#
# Usage:
#   hermes webhook subscribe daedalus-ready \
#     --events "projects_v2_item,issue,workitem.updated,kanban.status_changed" \
#     --description "Fire Daedalus dispatcher when a VCS item moves to Ready" \
#     --script ~/.hermes/agent-hooks/daedalus-ready.sh
set -euo pipefail

payload=$(cat -)

# Infer provider from payload structure
provider=$(echo "$payload" | python3 -c "
import sys, json; d = json.load(sys.stdin)
if 'projects_v2_item' in d: print('github')
elif 'object_attributes' in d and d.get('object_kind') == 'issue': print('gitlab')
elif 'resource' in d and 'workItemId' in d.get('resource', {}): print('azure')
elif 'new_status' in d: print('hermes')
else: print('unknown')
" 2>/dev/null || echo "unknown")

if [[ "$provider" == "unknown" ]]; then
  printf '{"status": "ignored", "reason": "unknown provider"}\n'
  exit 0
fi

# Run normalizer — for a Ready event, resolve the LOCAL repo path of the project
# the payload belongs to so the dispatch scopes to it instead of sweeping every
# registered repo (issue #137). Output is one of:
#   ""        — not a Ready event (ignore)
#   "ALL"     — Ready, but no registered project matched (legacy global sweep)
#   <path>    — Ready, scope the dispatch to this repo path
scope=$(echo "$payload" | python3 -c "
import sys, json
sys.path.insert(0, '$HOME/.hermes/plugins/daedalus')
from core.webhook_normalizer import normalize
from core import registry
from config import ConfigLoader
d = json.load(sys.stdin)
ev = normalize('$provider', d)
if not ev:
    print(''); sys.exit(0)
loader = ConfigLoader()
ident = (ev.repo or '').strip()
for rp in registry.list_projects():
    try:
        r = loader.resolve_repo_config(rp)
    except Exception:
        continue
    if ident and (r.get('repo') or '').strip() == ident:
        print((r.get('workdir') or rp).strip()); break
else:
    print('ALL')
" 2>/dev/null || echo "")

if [[ -z "$scope" ]]; then
  printf '{"status": "ignored", "reason": "not a Ready event"}\n'
  exit 0
elif [[ "$scope" == "ALL" ]]; then
  # Ready event but no local project matched — fall back to a global sweep.
  bash ~/.hermes/scripts/daedalus-cron.sh </dev/null >/dev/null 2>&1 &
  printf '{"status": "dispatched", "scope": "all"}\n'
else
  # Fire dispatcher in background, scoped to the matched project.
  bash ~/.hermes/scripts/daedalus-cron.sh --repo "$scope" </dev/null >/dev/null 2>&1 &
  printf '{"status": "dispatched", "repo": "%s"}\n' "$scope"
fi

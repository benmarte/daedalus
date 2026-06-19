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

# Run normalizer — extract if this is a Ready event
is_ready=$(echo "$payload" | python3 -c "
import sys, json
sys.path.insert(0, '$HOME/.hermes/plugins/daedalus')
from core.webhook_normalizer import normalize
d = json.load(sys.stdin)
result = normalize('$provider', d)
print('yes' if result else 'no')
" 2>/dev/null || echo "no")

if [[ "$is_ready" == "yes" ]]; then
  # Fire dispatcher in background — don't block the webhook response
  bash ~/.hermes/scripts/daedalus-cron.sh </dev/null >/dev/null 2>&1 &
  printf '{"status": "dispatched"}\n'
else
  printf '{"status": "ignored", "reason": "not a Ready event"}\n'
  exit 0
fi

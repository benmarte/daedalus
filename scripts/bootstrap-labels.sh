#!/usr/bin/env bash
# Create the pipeline:* label state machine in the current repo (idempotent).
# Usage: bash scripts/bootstrap-labels.sh [owner/repo]
set -euo pipefail
REPO="${1:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
echo "Bootstrapping pipeline labels in $REPO"

# label:color:description
labels=(
  "pipeline:ready:0e8a16:Queued for the pipeline — validator picks it up"
  "pipeline:confirmed:1d76db:Validated as real & in-scope — PM writes the spec"
  "pipeline:dev:5319e7:Spec ready — developer implements + opens PR"
  "pipeline:review:fbca04:PR open — QA then reviewer/security/docs"
  "pipeline:approved:0e8a16:All stages passed — orchestrator merges when CI is green"
  "pipeline:blocked:b60205:Halted — a human needs to act (see comments)"
  "qa:pass:c2e0c6:QA verified acceptance criteria"
  "review:approved:c2e0c6:Code review approved"
  "security:approved:c2e0c6:Security review clear"
  "docs:done:c2e0c6:Documentation updated"
)

for entry in "${labels[@]}"; do
  name="${entry%%:*}"; rest="${entry#*:}"
  color="${rest%%:*}"; desc="${rest#*:}"
  gh label create "$name" --color "$color" --description "$desc" --repo "$REPO" 2>/dev/null \
    && echo "  + $name" \
    || gh label edit "$name" --color "$color" --description "$desc" --repo "$REPO" >/dev/null \
    && echo "  ~ $name (updated)"
done
echo "Done. Add 'pipeline:ready' to an issue to start the pipeline."

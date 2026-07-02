#!/usr/bin/env bash
# Hermes webhook handler for daedalus event-driven dispatch.
#
# Thin wrapper around core/webhook_dispatch.py. Reads the raw webhook payload
# from stdin, verifies its HMAC signature (when vcs.webhook_secret_env is set)
# BEFORE normalizing it, and fires the dispatcher only when the item moved to
# the Ready column. All logic lives in the importable, unit-tested module so
# the raw request body is read exactly once (no shell byte-mangling of the HMAC
# input) — see issue #1141.
#
# Usage:
#   hermes webhook subscribe daedalus-ready \
#     --events "projects_v2_item,issue,workitem.updated,kanban.status_changed" \
#     --description "Fire Daedalus dispatcher when a VCS item moves to Ready" \
#     --script ~/.hermes/agent-hooks/daedalus-ready.sh
set -euo pipefail

DAEDALUS_HOME="${DAEDALUS_HOME:-$HOME/.hermes/plugins/daedalus}"
cd "$DAEDALUS_HOME"
exec python3 -m core.webhook_dispatch

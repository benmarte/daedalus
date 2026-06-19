# Spec: Pipeline Reliability — Handoff Fix & Autonomous Routing (2026-06-19)

## Objective

Eliminate stalled pipeline handoffs by wiring Hermes's native `on_session_end` hook to auto-fire the Daedalus dispatcher whenever any daedalus agent session ends — crash-safe, no agent cooperation required. Simultaneously fix all known routing gaps so every blocked state is handled autonomously. Fix the GitHub Projects v2 "No Status" column by ensuring all created items are enrolled and given "Backlog" status atomically.

**Target:** The Daedalus pipeline itself. Users are the agents running in the pipeline and the human overseeing it.

---

## Acceptance Criteria

1. When any daedalus agent session ends (normally, on crash, or on timeout), `daedalus-cron.sh` fires automatically within seconds — without relying on the agent calling it.
2. All new GitHub project items land in **Backlog** status, never "No Status".
3. Every blocked card state for every agent profile (developer, reviewer, security, docs, planner, PM) routes autonomously to the correct next agent.
4. Stalled `in-progress` cards (session ended without state transition) are detected and re-routed within one dispatch tick.
5. Done validator cards with a non-`CONFIRMED:` summary get re-triaged instead of silently dropped.
6. Security/harm escalations (`ESCALATE:` prefix) continue to route exclusively to human review — never auto-resolved.

---

## Scope

### In scope
- Hermes `on_session_end` shell hook in `~/.hermes/config.yaml`
- Hook guard script `~/.hermes/agent-hooks/daedalus-advance.sh`
- `planner-daedalus/SOUL.md` — add missing Pipeline Advancement section
- `core/iterate.py` — fix `classify_blocked()` to handle all profiles
- `scripts/daedalus_dispatch.py` — fix silent drops for non-CONFIRMED validators and developer-no-PR case
- `scripts/daedalus_dispatch.py` — add stall detection for `in-progress` cards
- `core/providers/github.py` — add `addProjectV2ItemById` mutation; default new items to Backlog

### Out of scope
- New agent profiles
- Notification/alerting for human escalations (separate feature)
- Changing the cron schedule (remains 60m as ultimate fallback)

---

## Component Design

### 1. `on_session_end` Shell Hook

**Config change** — `~/.hermes/config.yaml`:
```yaml
hooks:
  on_session_end:
    - command: ~/.hermes/agent-hooks/daedalus-advance.sh
      timeout: 90
```

**Guard script** — `~/.hermes/agent-hooks/daedalus-advance.sh`:
```bash
#!/usr/bin/env bash
# Fires daedalus dispatcher when a daedalus agent session ends.
# Reads JSON payload from stdin (Hermes shell hook wire protocol).
set -euo pipefail

payload=$(cat -)
profile=$(echo "$payload" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('extra',{}).get('profile',''))" 2>/dev/null || echo "")

# Also check env var set by hermes kanban worker
profile="${profile:-${HERMES_PROFILE:-}}"

if [[ "$profile" == *"daedalus"* ]]; then
  bash ~/.hermes/scripts/daedalus-cron.sh &
fi
printf '{}\n'
```

Key design decisions:
- Runs `daedalus-cron.sh` in background (`&`) so it doesn't block the hook timeout
- Falls back to `HERMES_PROFILE` env var if payload doesn't carry profile
- Outputs `{}` for Hermes wire protocol compliance (no-op, no blocking)
- If neither source gives a daedalus profile, exits silently — safe for all non-daedalus sessions
- Dispatcher is idempotent so duplicate firings are harmless

### 2. `planner-daedalus/SOUL.md` Pipeline Advancement Section

Add the identical Pipeline Advancement block present in all other daedalus profiles:

```markdown
## Pipeline Advancement

After every terminal state (done, blocked, review-required, awaiting-fix), run:

```bash
bash ~/.hermes/scripts/daedalus-cron.sh
```

This fires the Daedalus dispatcher so the next agent can start immediately.
If Hermes marks this task done before you finish, run the script anyway.
```

### 3. `classify_blocked()` Routing Gaps — `core/iterate.py`

**Current state:** Returns `""` (silent skip) for planner, PM, documentation profiles, and for developer cards with no PR.

**Fix:** Extend the match logic:
- `planner-daedalus` blocked → `"PM_ROUTE"` (PM consultation)
- `documentation-daedalus` blocked → `"PM_ROUTE"`
- `project-manager-daedalus` blocked → `"ESCALATE"` (PM itself can't consult PM — human gate)
- Developer blocked with no PR and no `review-required` → `"PM_ROUTE"` (currently returns `""`)

The `ESCALATE` path remains the human gate (security/harm escalation is unchanged).

### 4. Non-`CONFIRMED:` Validator Done Cards — `scripts/daedalus_dispatch.py:942`

**Current state:** Done validator card without `CONFIRMED:` prefix → silently skipped, pipeline stalls.

**Fix:** Add a re-triage path:
- If summary starts with `BLOCKED:` or `STOP:` → create a PM consultation task
- If summary is empty or unrecognized → create a new validator task (retry once, idempotency key `validator-retry-{n}`)
- If summary starts with `ESCALATE:` → existing human escalation path (unchanged)

### 5. Stall Detection for In-Progress Cards — `scripts/daedalus_dispatch.py`

Add a new dispatcher phase `_check_stalled_in_progress()`:
- Fetch all cards in `in-progress` state
- Check card's `updated_at` timestamp (available via `hermes kanban show --json`)
- If `now - updated_at > stall_threshold` (default: 30 minutes, configurable in `.hermes/daedalus.yaml` as `dispatch.stall_timeout_minutes`): move card to `blocked` with summary `STALLED: session ended without completing`
- This makes it visible to `_check_team_blockers()` on the next tick, which routes it to PM consultation

The stall threshold should be greater than the longest expected agent task runtime. Default 30m is conservative; adjust down if tasks reliably complete faster.

### 6. GitHub Projects v2 "No Status" Fix — `core/providers/github.py`

**Current state:** `board_set_status()` calls `updateProjectV2ItemFieldValue` but has no `addProjectV2ItemById`. If item is not enrolled in the project, the call silently returns `False`.

**Fix:**
1. Add `_board_add_item(issue_number: int) -> str | None` method that calls `addProjectV2ItemById` mutation, returns the project item ID, or `None` if already present.
2. Modify `board_set_status()` to call `_board_add_item()` first if the item is not found in `_items()`.
3. Add `board_ensure_backlog(issue_number: int)` convenience method that enrolls item and sets status to `"Backlog"` — call this from every new task creation path in `daedalus_dispatch.py`.

All new tasks created by `_check_confirmed_validators`, `_check_completed_pm`, `_check_team_blockers`, and `run()` must call `board_ensure_backlog(issue_number)` after the issue is created.

---

## Files Changed

| File | Change |
|------|--------|
| `~/.hermes/config.yaml` | Add `hooks.on_session_end` entry |
| `~/.hermes/agent-hooks/daedalus-advance.sh` | New guard script (create + chmod +x) |
| `~/.hermes/profiles/planner-daedalus/SOUL.md` | Add Pipeline Advancement section |
| `core/iterate.py` | Extend `classify_blocked()` for all profiles |
| `scripts/daedalus_dispatch.py` | Fix validator silent drop, add stall detection, call `board_ensure_backlog` |
| `core/providers/github.py` | Add `_board_add_item()`, `board_ensure_backlog()`, update `board_set_status()` |

---

## Testing Strategy

1. **Unit tests** (`tests/`) — mock `hermes kanban` CLI responses:
   - `classify_blocked()` returns correct action for each profile
   - `_check_stalled_in_progress()` moves cards older than threshold to blocked
   - `github.py board_ensure_backlog()` calls `addProjectV2ItemById` then `updateProjectV2ItemFieldValue`

2. **Integration smoke test** — run dispatcher against a test board:
   - Create a card assigned to `planner-daedalus` in `in-progress`, wait 31 minutes (or mock timestamp), verify it moves to `blocked`
   - Verify a new GitHub issue gets "Backlog" status, never "No Status"

3. **Hook test** — `hermes hooks test on_session_end --payload-file /tmp/test-payload.json` with a daedalus and a non-daedalus payload:
   - Daedalus payload → `daedalus-cron.sh` fires
   - Non-daedalus payload → no-op

4. **Regression** — run existing `tests/` suite, confirm no breakage in happy-path dispatch flow.

---

## Security / Harm Escalation (Unchanged)

Cards with `ESCALATE:` prefix in their summary continue to route exclusively to the human gate via `_check_team_blockers`'s existing skip at line 1118–1119 and `_enforce_validator_blocks`. This is never auto-resolved. The `ESCALATE` path in `classify_blocked()` for PM-is-blocked is also a human gate.

No changes to security boundaries.

---

## Resolved Questions

- **`HERMES_PROFILE` env var:** Confirmed present in daedalus profile sessions (referenced in validator and PM profile skill scripts). The guard script can rely on it as primary signal; `extra.profile` from the JSON payload is the fallback.
- **`stall_timeout_minutes`:** Default 30m. Configurable in `.hermes/daedalus.yaml` under `dispatch.stall_timeout_minutes`. The dispatch script already reads a similar `dispatch_stale_timeout_seconds: 1800` from `~/.hermes/config.yaml` kanban section — use that existing value (30m) for consistency.

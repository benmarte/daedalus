# Spec: Fix Validator Inner Agent Calling kanban complete Without Summary (#1121)

## Objective

The validator pipeline stage uses a two-agent architecture. The **outer agent**
(`validator-daedalus` Hermes profile) spawns an **inner claude subprocess**,
reads its stdout, then calls `hermes kanban complete <id> <verdict>`. The bug
is that the inner agent's task body (`_validator_body()`) tells it to "complete
YOUR OWN card" — so the inner agent directly calls `hermes kanban complete <id>`
without a summary argument, producing `summary: None`. The dispatcher then
retries indefinitely until the cap is exhausted.

## Root Cause

`_validator_body()` in `scripts/daedalus_dispatch.py` (lines 2046–2048, 2087,
2095–2098, 2100–2102, 2107–2120) contains instructions telling the inner agent
to complete or block its own kanban card. The `_ROLE_AFTER_SPAWN["validator"]`
delegation block (lines 352–357) correctly tells the **outer** agent to read
stdout and call complete — but the inner agent races ahead and calls complete
first with no summary.

Key offending lines in `_validator_body()`:
- Line 2047: `"The only kanban write allowed is completing or blocking YOUR OWN card."`
- Line 2048: `"Your ONLY deliverable is a classification decision written as your kanban card summary."`
- Lines 2100–2102: All `→ Complete your card with summary starting '...'` instructions
- Lines 2087, 2095–2098, 2107–2120: All `→ Block your card / Complete your card` instructions

## Fix Strategy — Option A

Reword `_validator_body()` so the inner agent **prints its verdict to stdout**
instead of calling `hermes kanban complete`. The outer agent already reads
stdout (SOUL.md step 6: "Complete YOUR kanban card with: `<verdict line from the output>`").

### Changes required

**File:** `scripts/daedalus_dispatch.py`

1. **Lines 2046–2048** — Replace the kanban-write permission grant:
   - Remove: `"The only kanban write allowed is completing or blocking YOUR OWN card."`
   - Change: `"classification decision written as your kanban card summary"` →
     `"classification decision printed to stdout (the outer agent reads your stdout and calls kanban complete for you)"`
   - Add explicit prohibition: `"DO NOT call hermes kanban complete or kanban block — the outer agent does this."`

2. **All outcome action blocks** — Replace every
   `→ Complete your card with summary starting 'X: ...'` with
   `→ Print to stdout: 'X: <description>'`
   Affects outcomes: CONFIRMED, CANNOT_REPRODUCE, ALREADY_FIXED, DUPLICATE,
   NEEDS_MORE_INFO (complete → print).

3. **SECURITY_THREAT and BLOCK_FOR_REVIEW** — Replace every
   `→ Block your card with summary starting 'X: ...'` with
   `→ Print to stdout: 'X: <description>'`

No changes to `_ROLE_AFTER_SPAWN["validator"]` or SOUL.md — they are already
correct.

## Acceptance Criteria

- **AC1**: Inner coding agent does NOT call `hermes kanban complete` or
  `hermes kanban block`. Only the outer `validator-daedalus` agent calls those.
- **AC2**: When the outer agent completes the card, `summary` contains the
  full verdict string (e.g., `CONFIRMED: reproduced on main at ...`), not `None`.
- **AC3**: Running the validator for any issue produces exactly 1 validator task
  completing with a non-None summary — no retry loop.
- **AC4**: A unit test covers `_validator_body()` output and asserts:
  - No occurrence of `Complete your card` in the returned string
  - No occurrence of `Block your card` in the returned string
  - All outcome blocks contain `Print to stdout:` instead

## Branch & PR Target

- **Branch:** `fix/issue-1121-validator-inner-agent-kanban-complete`
- **PR target:** `dev`
- **Files changed:** `scripts/daedalus_dispatch.py` (string edits to `_validator_body()`)
- **No schema changes, no migration, no config changes**

## Out of Scope

- Option B (outer agent re-completes if summary is None) — unnecessary once inner
  agent stops calling complete
- Option C (dispatcher guard checking output file) — unnecessary and fragile;
  fix the source, not the symptom
- Changes to SOUL.md or `_ROLE_AFTER_SPAWN` — already correct

## Testing Strategy

1. **Unit test** (new): `tests/test_issue_1121_validator_body.py`
   - Call `_validator_body(...)` with a synthetic issue
   - Assert `"Complete your card"` does not appear in output
   - Assert `"Block your card"` does not appear in output
   - Assert `"Print to stdout:"` appears for each outcome block
2. **Integration smoke**: Run dispatcher against a real issue with
   `coding_agent=claude-code`; confirm validator task completes with non-None summary

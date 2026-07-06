# Spec: Issue #1123 — Developer task body cleanup + clean inner-agent failure fallback

## Root Cause

Three distinct bugs in the developer pipeline:

**Bug 1 — `/review` + `/code-simplify` in `_dev_task_body()`**
`scripts/daedalus_dispatch.py` lines 2420–2421 tell the inner coding agent to run `/review` (five-axis quality gate) and `/code-simplify` (complexity reduction). These are the reviewer agent's entire responsibility. The developer agent running them first means the reviewer re-runs duplicate checks and the developer session runs ~2 skill-invocations longer than needed.

**Bug 2 — No "no PR URL" guard in `_ROLE_AFTER_SPAWN["developer"]`**
`_ROLE_AFTER_SPAWN["developer"]` (lines 344–350) handles `CODING_AGENT_DIED` and `CODING_AGENT_TIMEOUT` but has **no explicit instruction** for when the inner agent produces stdout without a `PR URL:` line. Step 5 says "On success the agent will have opened a PR" — but says nothing about what to do when that's absent. The outer Hermes agent fills the gap by falling back to its helpfulness instinct, starting to read files, grep code, and implement the fix itself.

**Bug 3 — `messaging` toolset unknown warning**
All 9 daedalus agent profiles at `~/.hermes/profiles/*/config.yaml` list `messaging` under `platform_toolsets.cli` (line 700) and `platform_toolsets.slack` (line 724). Hermes does not register `messaging` as a known toolset, so it emits a warning on every agent startup. These profiles are not in the daedalus git repo — they are Hermes-installed configurations.

---

## Fix Strategy

### Fix 1 — Strip `/review` and `/code-simplify` from `_dev_task_body()`

**File**: `scripts/daedalus_dispatch.py`  
**Lines to remove**: 2420–2421

Remove these two lines from the f-string in `_dev_task_body()`:
```python
f"  /review        → five-axis quality gate (correctness, readability, arch, security, perf)\n"
f"  /code-simplify → reduce complexity with no behavior change\n"
```

Also remove the `Iterate up to {iterations}x if review fails.` line (line 2425) since without `/review` in the developer body, there is nothing to iterate on from within the developer session. The reviewer agent owns that loop.

Result: developer task body steps are `spec → plan → build → test` then lint → PR → block. No review/simplify.

### Fix 2 — Add "no PR URL → block" guard to `_ROLE_AFTER_SPAWN["developer"]`

**File**: `scripts/daedalus_dispatch.py`  
**Location**: `_ROLE_AFTER_SPAWN` dict, `"developer"` entry (lines 344–350)

Update the developer after-spawn steps to add an explicit guard between steps 5 and 6:

```python
"developer": (
    '  4. Wait for the coding agent to finish: terminal("{wait_cmd}")\n'
    "  4b. {failed_note}\n"
    "  5. On success the agent will have opened a PR and output: 'PR URL: ... PR number: <n>'\n"
    "  5b. If the output does NOT contain both 'PR URL:' and 'PR number:', the inner agent "
    "failed to open a PR — block your card with "
    'kanban_block("coding-agent-failed: inner agent produced no PR URL — check stderr above") '
    "and STOP. Do NOT read files, grep code, or attempt to implement the changes yourself.\n"
    '  6. Block your card: kanban_block("review-required: PR #<n> — <branch>")\n'
    "  STOP — do NOT open the PR yourself. Wait for coding agent output then block with the real PR number.\n"
),
```

### Fix 3 — Remove `messaging` from daedalus agent profile toolsets

**Files**: `~/.hermes/profiles/*/config.yaml` for all 9 daedalus profiles:
- developer-daedalus
- reviewer-daedalus
- validator-daedalus
- qa-daedalus
- planner-daedalus
- project-manager-daedalus
- accessibility-daedalus
- documentation-daedalus
- security-analyst-daedalus

In each profile, remove `- messaging` from both `platform_toolsets.cli` and `platform_toolsets.slack` lists.

**Note**: These are installed Hermes profiles, not tracked in the daedalus git repo. The developer must edit them in place at `~/.hermes/profiles/`. Check if there is a profile template or generation script in daedalus; if found, fix the source as well.

---

## Acceptance Criteria

- [ ] **AC1**: `_dev_task_body()` does NOT include `/review` or `/code-simplify` instructions; only `spec → plan → build → test` skills are listed
- [ ] **AC2**: `_dev_task_body()` does NOT include the "iterate up to N× if review fails" line
- [ ] **AC3**: `_ROLE_AFTER_SPAWN["developer"]` contains step 5b explicitly instructing the outer agent to `kanban_block("coding-agent-failed: ...")` and stop when inner agent output lacks `PR URL:` / `PR number:` — no fallback implementation
- [ ] **AC4**: `messaging` is removed from `platform_toolsets.cli` and `platform_toolsets.slack` in all 9 daedalus profiles; `hermes` startup no longer emits `Warning: Unknown toolsets: messaging`
- [ ] **AC5**: Unit tests for `_dev_task_body()` confirm `/review` and `/code-simplify` are absent from the generated body
- [ ] **AC6**: Unit tests for `_ROLE_AFTER_SPAWN["developer"]` (or a helper that renders it) confirm step 5b is present and contains `kanban_block("coding-agent-failed: ...")` instruction

---

## Files to Change

| File | Change |
|------|--------|
| `scripts/daedalus_dispatch.py` | Remove lines 2420–2421 (`/review`, `/code-simplify`) and line 2425 (iterate) from `_dev_task_body()` |
| `scripts/daedalus_dispatch.py` | Add step 5b to `_ROLE_AFTER_SPAWN["developer"]` (lines ~344–350) |
| `~/.hermes/profiles/*/config.yaml` (9 files) | Remove `- messaging` from `platform_toolsets.cli` and `platform_toolsets.slack` |
| `tests/` | Add/update tests for `_dev_task_body()` and `_ROLE_AFTER_SPAWN["developer"]` |

---

## Branch and PR

- **Branch**: `fix/issue-1123-dev-task-body-cleanup`
- **Base**: `dev`
- **PR target**: `dev`
- PR body must include `Closes #1123`

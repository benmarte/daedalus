# Spec: Issue #1105 — Validator read-only enforcement for kanban writes

**Branch:** `fix/issue-1105-validator-kanban-readonly`  
**PR target:** `dev`

---

## Root Cause

The `_validator_body()` function in `scripts/daedalus_dispatch.py` (lines 2017–2021) has a `⛔ READ-ONLY` instruction block that prohibits file writes, git commits, and PR creation, but **never mentions kanban write operations**. The validator SOUL at `config/souls/validator-daedalus.md` has a similar gap.

Because `hermes kanban create` is an available tool and is not listed as forbidden, the validator agent interpreted creating kanban tasks as a legitimate investigation technique. During #1098, it created three live tasks (`t_10e73790`, `t_0b5dd028`, `t_04876dac`), one of which spawned a real `qa-daedalus` session that consumed a slot for several minutes.

---

## Fix Strategy

Two-location fix (no architectural change needed):

### 1. `scripts/daedalus_dispatch.py` — `_validator_body()` READ-ONLY block

Extend the existing `⛔ READ-ONLY` sentence to explicitly prohibit all kanban write operations except completing or blocking the validator's own card.

**Current text (lines 2017–2021):**
```
⛔ READ-ONLY — You may run existing tests to verify bug reproduction but MUST NOT write,
modify, or commit any code. DO NOT create or modify files. DO NOT run `git commit`,
`git add`, or any git write command. DO NOT open pull requests.
Your ONLY deliverable is a classification decision written as your kanban card summary.
The developer agent will implement the fix AFTER you confirm the issue is valid and safe.
```

**Append immediately after that paragraph:**
```
⛔ KANBAN WRITE BAN — NEVER call `hermes kanban create`, `hermes kanban complete` (on any
card other than your own), `hermes kanban block` (on any card other than your own), or
`hermes kanban archive` on any task. You are read-only on the kanban board. The ONLY
kanban write allowed is completing or blocking YOUR OWN validator card at the end of your
investigation. Creating demo tasks to reproduce a bug is FORBIDDEN and will spawn real
agent sessions.
```

### 2. `config/souls/validator-daedalus.md` — add kanban prohibition

Add an explicit `⛔ KANBAN WRITE BAN` section immediately after the existing `## Steps` intro (after "no file writes" note, before step a). Also add it to the `# Your Role: Validator` preamble under `## Steps`.

**Add at end of the `## Steps` header block, before step `a)`:**
```
⛔ KANBAN WRITE BAN: NEVER call `hermes kanban create` or any kanban write command — you
are read-only. The ONLY kanban write allowed is completing or blocking YOUR OWN card.
Creating demonstration or reproduction tasks is FORBIDDEN — it spawns real agent sessions.
```

### 3. Unit test

Add a test in `tests/test_validator_kanban_readonly.py` that:

1. Calls `_validator_body()` with a synthetic issue and inspects the returned task body string.
2. Asserts the string contains the phrase `NEVER call` (or a stable substring from the new prohibition text).
3. This confirms the prohibition is actually emitted for every validator task, not accidentally omitted by a future refactor.

No need for a full integration test that runs a live agent — the body-string assertion is sufficient and deterministic.

---

## Files to Change

| File | Change |
|------|--------|
| `scripts/daedalus_dispatch.py` | Extend `⛔ READ-ONLY` block in `_validator_body()` with kanban write ban |
| `config/souls/validator-daedalus.md` | Add `⛔ KANBAN WRITE BAN` block under `## Steps` and under role preamble |
| `tests/test_validator_kanban_readonly.py` | New test asserting kanban prohibition text is present in `_validator_body()` output |

---

## Acceptance Criteria

- [ ] `_validator_body()` output explicitly contains `NEVER call hermes kanban create` (or equivalent phrasing) in the `⛔ READ-ONLY` section
- [ ] `config/souls/validator-daedalus.md` `## Steps` block contains the kanban write ban statement before step `a)`
- [ ] `tests/test_validator_kanban_readonly.py` passes and asserts the prohibition is present in the task body
- [ ] `pytest tests/test_validator_kanban_readonly.py` exits 0 with no errors
- [ ] No existing tests broken

---

## Out of Scope

- Runtime enforcement (tool-call interception) — the prohibition must be in the LLM prompt; intercepting the tool call at the harness level is a separate, larger project
- Changes to other agent SOULs (developer, reviewer, qa) — this issue is validator-specific
- Modifying the `_task_body()` multi-role template (that template embeds the validator section too, but the primary validator path is `_validator_body()`)

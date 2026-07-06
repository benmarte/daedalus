# Spec: Gate Epic-Level QA Dispatch Until Sub-Issue PR Exists (#1098)

**Branch:** `fix/issue-1098-gate-epic-qa-dispatch`
**PR Target:** `dev`
**Issue:** https://github.com/benmarte/daedalus/issues/1098

---

## Root Cause

The dispatcher creates a `qa-{n}` kanban card when an issue is triaged. For
epic issues the QA card is parented to the epic-level developer card. When that
developer card completes (e.g. via a planner-decompose handoff), Hermes marks
the QA card runnable and `kanban.dispatch()` spawns a QA agent — before any
sub-issue developer has opened a PR.

The QA agent finds zero branches, PRs, or code changes, emits
`qa-failed: …`, and burns a retry slot. `classify_blocked()` in
`core/iterate.py` routes the card to `QA_FIX`, creating a spurious fix card
(or `PENDING_SIGNAL` for re-dispatch next tick). The cycle repeats.

**Observed:** task `t_aaf9c8d6` (QA for epic #1074) ran twice and self-blocked
before any sub-issue developer had opened a PR.

---

## Fix Strategy

**Pre-dispatch gate + undefer helpers in `scripts/daedalus_dispatch.py`.**

Two functions run every tick before `kanban.dispatch()`:

1. **`_gate_epic_qa_tasks(slug, issues_map, kanban_mod, *, epic_config, dry_run)`**
   — Scans non-blocked, non-done QA cards (`qa-daedalus`). For each, if the
   issue is an epic and no sub-issue developer card has a
   `review-required: PR #` signal in its summary, the card is blocked with
   `qa-deferred: no sub-issue PRs open yet for epic #{N}`. Idempotent —
   already-blocked cards are skipped.

2. **`_maybe_undefer_epic_qa_tasks(slug, issues_map, kanban_mod, *, epic_config)`**
   — Scans blocked QA cards containing `qa-deferred:` in their summary. For
   each, calls `_check_epic_qa_ready()`. If a sub-issue developer card now
   signals `review-required: PR #`, the card is unblocked so `kanban.dispatch()`
   will spawn QA on the next tick.

**Helper: `_check_epic_qa_ready(slug, issue_number, issue, kanban_mod, *, epic_config)`**
   — Returns `True` (dispatch allowed) when: the issue is not an epic, the
   epic body has no parseable sub-issue numbers, kanban errors occur (fail-open),
   or at least one sub-issue developer card has `review-required:` + `PR #` in
   its latest summary. Returns `False` only for epics where no sub-issue PR
   signal is found.

**Belt-and-suspenders in `core/iterate.py`:**
In `classify_blocked()` `qa-daedalus` branch, add an explicit `qa-deferred`
check **before** `qa-failed`. A card that reaches `classify_blocked` with a
`qa-deferred:` reason routes to `PENDING_SIGNAL` (no retry cap burn, no fix
card) instead of `QA_FIX`.

---

## Files to Modify

| File | Change |
|---|---|
| `scripts/daedalus_dispatch.py` | Add `_check_epic_qa_ready()`, `_gate_epic_qa_tasks()`, `_maybe_undefer_epic_qa_tasks()`; call both in `run()` tick before `kanban.dispatch()` |
| `core/iterate.py` | In `classify_blocked()` `qa-daedalus` branch, add `if "qa-deferred" in summary: return PENDING_SIGNAL` before the `qa-failed` check |
| `tests/test_epic_qa_gate.py` | New file: unit tests for all three helpers and the `qa-deferred` classify_blocked path |

---

## Integration Point

In `run()` (`scripts/daedalus_dispatch.py`), call the helpers each tick before
the first `kanban.dispatch()` call:

```python
if not dry_run:
    _maybe_undefer_epic_qa_tasks(slug, issues_map, kanban, epic_config=epic_config)
    _gate_epic_qa_tasks(slug, issues_map, kanban, epic_config=epic_config, dry_run=dry_run)
```

Undefer runs first so that a PR that appeared since the last tick is unblocked
before the gate re-evaluates (preventing an immediate re-block).

---

## Edge Cases

| Scenario | Expected behavior |
|---|---|
| Single-issue (non-epic) QA | Guard skips (not an epic); QA dispatches immediately |
| Epic, no sub-issue PRs | QA card blocked with `qa-deferred:`; re-evaluated next tick |
| Epic, ≥1 sub-issue dev card has `review-required: PR #N` | `_maybe_undefer_epic_qa_tasks` unblocks; QA dispatched next tick |
| QA already done | Gate skips `done`/`completed`/`archived` cards — no-op |
| Already-deferred QA card | Gate skips cards already in `blocked` status |
| `kanban.list_tasks` raises | Both helpers catch exception, return 0 — fail open |
| Epic body has no parseable sub-issue numbers | `_check_epic_qa_ready` returns True (fail open) |
| `review-required:` without `PR #` | Not counted as having a PR; QA stays deferred |

---

## Acceptance Criteria

- [ ] Epic QA card is pre-blocked (`qa-deferred:` reason) when no sub-issue developer card has a `review-required: PR #N` summary
- [ ] Epic QA card is unblocked on the next tick after at least one sub-issue developer card signals `review-required: PR #N`
- [ ] Single-issue (non-epic) QA dispatch is unaffected — gate is a no-op for non-epics
- [ ] A `qa-deferred:` card processed by `classify_blocked()` routes to `PENDING_SIGNAL` (not `QA_FIX`), consuming no retry cap slot
- [ ] `_check_epic_qa_ready()` uses only `kanban.list_tasks()` — no extra GitHub API calls
- [ ] Gate is idempotent: already-blocked/done cards are not re-processed
- [ ] Tests cover: non-epic (gate skipped), epic+no-PR (blocked), epic+PR (unblocked), full gate→undefer cycle, `qa-deferred` signal in `classify_blocked()`
- [ ] Existing `qa-failed` / `qa-passed` / `QA_FIX` signal paths are unaffected

---

## Out of Scope

- Restructuring PM or planner task creation pipeline
- Changing kanban task data model (no new card fields)
- Modifying QA SOUL.md
- Changing cron tick interval or retry cap values

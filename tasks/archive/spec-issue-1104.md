# Spec: Issue #1104 — Extend empty-summary retry to PM and developer roles

**Branch:** `fix/issue-1104-empty-summary-pm-dev-retry`
**PR Target:** `dev`
**Primary File:** `scripts/daedalus_dispatch.py`

---

## Root Cause

Two dispatch paths fail to cap empty-summary retries:

### Gap 1 — PM github-fallback path (no retry cap)

`_check_confirmed_validators` has a github-comment-fallback branch (~line 3986) that dispatches a PM task when the validator summary is `None` but a GitHub comment contains `CONFIRMED`. This branch previously had **no call** to `_pm_task_state` or `_resolve_max_pm_retries`, so a new PM task was created on every tick with no cap — 6 consecutive PM tasks for #1098, all empty.

The primary PM stale path (the `pm_state == "stale"` block at ~line 4291) always enforced the cap; only the github-fallback branch was missing the guard.

### Gap 2 — Developer completion path (no stale detection, no retry)

When `_check_completed_pm` creates the team triage, QA is linked to the developer task via `parents=[dev_id]`. The kanban parent-child dependency auto-promotes QA when the developer card goes `done`, regardless of summary content. No dispatch code checked whether the developer's summary contained a PR number before QA ran.

`_developer_task_state` and `_resolve_max_developer_retries` existed as dead scaffolding — never called.

---

## Current State of Working Tree (code audit 2026-06-30)

The branch already has **uncommitted** changes that partially implement the fix. A developer must understand what's done vs. what's still missing before writing any code.

### Already implemented (working tree, not yet committed)

| Symbol | Location | Status |
|---|---|---|
| `_resolve_max_developer_retries` | line ~718 | Added, default cap = 2 |
| `_developer_task_state` | line ~2709 | Added, detects `stale` = done with no PR |
| `_check_completed_developer` | line ~5015 | **Fully implemented**, not wired into `run()` |
| `_send_retry_cap_notification` | existing | Extended with `role="developer"` branch |
| `_fire_webhook_notification` | existing | Extended with `role="developer"` branch |
| `_send_retry_attempt_notification` | existing | Extended with `role="developer"` branch |
| `_check_confirmed_validators` github-fallback | ~line 3994 | PM retry cap added with `_has_notified_block` guard |
| `_check_completed_pm` | ~line 4804 | Warning log added for empty-summary PM |
| Tests in `tests/test_daedalus.py` | — | 12 new tests covering the above |

### The single remaining gap

`_check_completed_developer` is **not called from `run()`**. It is a complete, correct function that implements the full developer stale retry pattern. The only missing piece is the call site in `run()`.

---

## Fix Strategy

### Part 1 — Wire `_check_completed_developer` into `run()`

In `run()` (around line 5995, after the `pm_triggered` block and its `kanban.dispatch` call), add:

```python
developer_triggered = _check_completed_developer(
    slug,
    repo,
    issues_map,
    iterations,
    workdir,
    base_branch,
    provider.name,
    profiles=profiles,
    role_skills=role_skills,
    coding_agent=coding_agent,
    coding_agent_cmd=coding_agent_cmd,
    role_agents=role_agents,
    label_overrides=_label_ovr,
    dry_run=dry_run,
    provider=provider,
    resolved=resolved,
)
if developer_triggered and not dry_run:
    kanban.dispatch(slug, max_spawns=max_dispatch)
```

This is the **only code change required** in `daedalus_dispatch.py`. Do not modify `_check_completed_developer`, `_developer_task_state`, or `_resolve_max_developer_retries` — they are already correct.

### Part 2 — Verify tests cover the wired call site

The 12 tests already in the working tree cover the unit-level behavior of the helper functions. Verify that at least one test exercises the `run()` path end-to-end (or add one if missing), confirming that `_check_completed_developer` is reachable through the dispatch loop.

---

## Acceptance Criteria

- [ ] When a developer card completes with `summary=None` (no PR number), dispatcher retries up to `max_developer_retries` (default 2, configurable via `execution.max_developer_retries`)
- [ ] When developer retry cap exhausted: `_send_retry_cap_notification(role="developer")` called, GitHub comment posted, `_mark_notified_block` called; no new developer task on subsequent ticks
- [ ] Log emitted on each retry: `dispatch: developer for #N completed with no summary — scheduling retry (run X/Y)`
- [ ] Developer retry key pattern: `developer-{n}-r{stale_count}`
- [ ] PM github-fallback path capped at `max_pm_retries` (existing code, already in working tree — must stay correct)
- [ ] Existing behavior unchanged for well-formed developer PR summaries and PM SPEC: summaries
- [ ] All 12 tests in the working tree pass
- [ ] `run()` calls `_check_completed_developer` and dispatches if any retries triggered

---

## Files to Change

| File | Change |
|---|---|
| `scripts/daedalus_dispatch.py` | Wire `_check_completed_developer` into `run()` after `pm_triggered` block (~line 5995) |
| `tests/test_daedalus.py` | Verify existing 12 tests pass; add `run()`-level integration test if missing |

**Do not create new files.** All scaffolding is already in the working tree.

---

## Out of Scope

- Changing retry defaults (`max_pm_retries=3`, `max_developer_retries=2`)
- Changing QA blocking behavior
- Modifying `_check_completed_developer`, `_developer_task_state`, or `_resolve_max_developer_retries`
- Planner, reviewer, docs roles

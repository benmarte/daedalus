# Spec: fix/issue-1072-planner-nonstandard-summary-fallback

## Issue
benmarte/daedalus#1072

## Root Cause

`_check_completed_planner()` in `scripts/daedalus_dispatch.py` (line 3513) silently drops
any done `planner-daedalus` task whose summary does **not** contain `PLANNING COMPLETE`.
The `continue` statement has no fallback — the pipeline stalls forever with no error,
no PM card, no escalation.

The planner's system prompt instructs it to complete with `PLANNING COMPLETE: ready for
decomposition`, but real agents sometimes emit `PLAN: <description>` instead (observed:
task `t_963a9359`, issue #1071). Both formats signal the same intent: the issue is large
enough to decompose and a planning analysis is complete.

The `_check_planner_not_suitable` handler (line ~3638) handles its own unexpected
summaries gracefully via `logger.debug` + `continue`, but that path only fires for
`NOT SUITABLE` patterns — it does not catch the stuck-done-planner case either.

## Fix Strategy

**Preferred approach: treat `PLAN:` as an accepted synonym for `PLANNING COMPLETE:`.**

The rationale:
- Both signal decomposition intent; the distinction is cosmetic.
- Accepting the synonym is the minimum-invasive fix — one extra `or` condition at line 3513.
- Avoids creating new escalation infrastructure for a case that is not an error.
- The `_check_planner_not_suitable` path (blocked-card fallback) already covers truly
  wrong summaries (e.g. "NOT SUITABLE") — this fix fills the gap for planner tasks that
  completed correctly but with a non-standard success summary.

**Implementation (line 3513):**

```python
# Before
if "PLANNING COMPLETE" not in summary_raw.upper():
    continue

# After
_summary_up = summary_raw.upper()
if "PLANNING COMPLETE" not in _summary_up and not _summary_up.startswith("PLAN:"):
    logger.warning(
        "dispatch: planner done #%s — unexpected summary format, skipping: %r",
        task.get("id"),
        summary_raw[:120],
    )
    continue
```

The `logger.warning` (instead of silent `continue`) ensures the drop is visible in logs
even for summaries that match neither pattern, preserving observability without blocking.

**No new routing infrastructure needed** — the fix is surgical (3–5 lines).

## Acceptance Criteria

- [ ] A done `planner-daedalus` task with summary starting `PLAN:` triggers
      `_execute_planner_decompose` (same as `PLANNING COMPLETE`).
- [ ] A done `planner-daedalus` task with summary `PLANNING COMPLETE: ...` is unaffected.
- [ ] A done `planner-daedalus` task with an unrecognized summary emits a `WARNING` log
      and does not trigger decompose (preserved silent-drop safety, now visible).
- [ ] Issue #1071 would have been unblocked by this change (task `t_963a9359` re-check).
- [ ] All 62 existing tests pass.
- [ ] New unit tests cover:
  - `PLAN:` prefix → decompose triggered
  - `PLANNING COMPLETE:` prefix → decompose triggered (regression guard)
  - Unrecognized summary → decompose NOT triggered, WARNING logged

## Branch & PR

- **Branch:** `fix/issue-1072-planner-nonstandard-summary-fallback`
- **Base branch:** `dev`
- **PR title:** `fix: accept PLAN: prefix as planner completion signal in _check_completed_planner`

## Files to Change

- `scripts/daedalus_dispatch.py` — patch lines ~3513–3514 in `_check_completed_planner`
- `tests/test_planner_signal_integration.py` — add 3 new test cases for `PLAN:` prefix,
  regression guard, and unrecognized-summary warning

## Out of Scope

- Modifying the planner's system prompt (that is a separate quality improvement)
- Changing `_check_planner_not_suitable` behaviour
- Any changes to `_execute_planner_decompose`

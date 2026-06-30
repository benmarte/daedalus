# SPEC: fix _check_completed_planner silent-drop for non-PLANNING-COMPLETE summaries

## Issue
GitHub #1072

## Objective
`_check_completed_planner()` in `scripts/daedalus_dispatch.py` silently `continue`s when a done
planner task summary does not contain `PLANNING COMPLETE`, leaving the parent issue stuck
"In Progress" forever with no developer tasks and no human notification.

## Root Cause
```python
# line 3513
if "PLANNING COMPLETE" not in summary_raw.upper():
    continue   # silent no-op
```

Real-world case: task `t_963a9359` completed with `PLAN: Add _DEV_MODE_ENV constant...` —
a valid completed-plan summary that uses `PLAN:` instead of `PLANNING COMPLETE`.

## Fix Strategy
Two-pronged approach:
1. **Accept `PLAN:` as a synonym** for `PLANNING COMPLETE` — both signals indicate the planner
   finished its analysis and the issue warrants decomposition. Route to `_execute_planner_decompose`.
2. **For all other unexpected summaries**, log a warning and skip — the `_check_planner_not_suitable`
   handler will catch NOT SUITABLE; anything else is an edge case that should be logged, not silently
   dropped.

This is the minimal safe fix. It does not introduce a new `_route_unexpected_planner_done` function
(which would require `issues_map`, `repo`, etc. — params not available to `_check_completed_planner`).

## Acceptance Criteria
- [ ] A done planner task with summary starting `PLAN:` triggers decomposition (not silently skipped)
- [ ] A done planner task with `PLANNING COMPLETE` in the summary still triggers decomposition (no regression)
- [ ] A done planner task with any other summary logs a warning instead of silently continuing
- [ ] Unit tests cover: `PLAN:` synonym path, warning-log path
- [ ] No regression in existing test suite (`tests/test_planner_signal_integration.py`)

## Files to Change
- `scripts/daedalus_dispatch.py` — `_check_completed_planner` function (~line 3513)
- `tests/test_planner_signal_integration.py` — add tests for new paths

## Code Style
- Match existing logging patterns (`logger.info`, `logger.warning`, `logger.debug`)
- Keep the function signature unchanged
- No new helper functions unless necessary

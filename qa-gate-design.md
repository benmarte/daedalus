# QA Signal Gating for Auto-Merge — Design Specification

## Overview

This spec documents the design of the QA pass signal gate that prevents auto-merge of PRs before QA approval, implemented in PR #998.

## Design Goals

- Prevent auto-merge until QA explicitly passes
- Support three signal states: passed, failed, absent (still running)
- Fail-closed: missing or ambiguous signals block the merge
- Case-insensitive signal matching
- Graceful handling of kanban DB errors

## Signal Injection Point

**Location:** `core/iterate.py`, in the auto-merge path within `process_blocked_cards()`

The QA gate check is injected BEFORE the merge decision:

```python
# In the auto-merge path (around line 2500)
if should_auto_merge(docs_card, repo):
    issue_number = extract_issue_number(docs_card)
    if not _qa_passed_for_issue(board_slug, issue_number):
        logger.info(f"No QA pass signal for issue #{issue_number} — skipping auto-merge")
        continue
    # ... proceed with merge
```

## Signal Format

**Signal source:** Kanban card `latest_summary` field of the QA card

**Signal pattern:**
- PASSED: `latest_summary` contains `qa-passed` (case-insensitive)
- FAILED: `latest_summary` contains `qa-failed` (case-insensitive)
- ABSENT: No QA card exists, or summary is empty, or summary doesn't match either pattern

**Helper function:** `_qa_passed_for_issue(board_slug, issue_number)`

Returns:
- `True` if QA card exists and summary contains `qa-passed`
- `False` otherwise (failed, absent, no card, DB error)

## Wait Behavior (Green Tests but No QA Signal)

When PR has green CI but no QA pass signal:

1. Extract issue number from docs card
2. Call `_qa_passed_for_issue(slug, issue_number)`
3. Helper searches kanban board for card with assignee `qa-daedalus` and idempotency_key `qa-{issue_number}`
4. If no card found: return `False` (QA hasn't run yet)
5. If card found but summary is empty/None: return `False` (QA still running)
6. If card found and summary matches signal: return `True` or `False` based on pattern

**Auto-merge decision:**
- Only proceed if `_qa_passed_for_issue()` returns `True`
- Log info message and skip merge if it returns `False`
- Do NOT log as warning (expected state, not an error)

## QA Failed Handling

When QA card summary contains `qa-failed`:

1. `_qa_passed_for_issue()` returns `False`
2. Auto-merge path logs info: "No QA pass signal for issue #X — skipping auto-merge"
3. Auto-merge is skipped (continue to next docs card)
4. PR remains open for human review
5. No automatic retry — human must fix and re-run QA

**Note:** The signal gating does NOT trigger automatic fix workflows. It simply blocks the merge path until QA passes.

## Timeout / Retry Behavior

The QA gate helper does NOT implement its own timeout logic. Instead:

1. **Dispatcher handles stalls:** The daedalus dispatcher already monitors for stalled tasks (configurable timeout, default 2 hours)
2. **Helper is stateless:** Each call to `_qa_passed_for_issue()` queries current QA card state
3. **No caching:** Every auto-merge attempt re-checks the QA signal
4. **Stalled QA card:** If QA card is stalled (no heartbeat for timeout period), dispatcher may escalate or notify

## Configuration & Dependencies

**Dependencies:**
- Kanban system: `kanban.list_tasks(slug)` and `kanban.show_card(slug, task_id)`
- Issue number extraction: `extract_issue_number()` helper in `core/util.py`

**No new configuration options added.** The gate uses existing:
- Auto-merge flag (already exists)
- Kanban board access (already configured)
- QA card format (already standardized)

**Signal robustness:**
- Case-insensitive: `qa-passed` / `QA-PASSED` / `Qa-Passed` all match
- Whitespace-tolerant: `.strip()` applied to summary before matching
- Fail-closed: Any DB error (list_tasks or show_card fails) returns `False`

## Implementation Reference

**Commit:** `bbdeee0` (feat: gate auto-merge on QA pass signal (#998))

**Code location:** `core/iterate.py:2626-2679`

**Test coverage:** `tests/test_qa_gate_auto_merge.py` (13 tests, all passing)
- Signal detection tests (passed, failed, absent, empty, None)
- Exception handling tests (DB errors fail-closed)
- Case-insensitivity test
- No-QA-card test

## Acceptance Criteria Checklist

- [x] Auto-merge blocked when QA has not passed
- [x] Auto-merge allowed when QA has passed
- [x] Handles missing QA card (wait, don't merge)
- [x] Handles failed QA (don't merge)
- [x] Handles QA still running (wait, don't merge)
- [x] Case-insensitive signal matching
- [x] Database errors fail-closed
- [x] No new configuration required
- [x] Comprehensive test coverage (13 tests)
- [x] Clear logging for troubleshooting

## Edge Cases Handled

1. **No QA card exists:** Returns `False` — PR waits for QA to run
2. **QA card exists, empty summary:** Returns `False` — QA still in progress
3. **QA card exists, summary=None:** Returns `False` — QA just started
4. **Wrong issue number:** No card found, returns `False`
5. **DB error fetching tasks:** Returns `False` — fail-closed, don't merge
6. **DB error fetching card details:** Returns `False` — fail-closed, don't merge
7. **Multiple QA cards (different issues):** Idempotency key filters correctly
8. **Mixed case signals:** Case-insensitive match handles all variants

## Example Log Messages

**QA not passed (expected):**
```
INFO No QA pass signal for issue #42 — skipping auto-merge
```

**QA passed, merging:**
```
INFO Issue #42 QA passed — proceeding with auto-merge
INFO Successfully merged PR #123
```

**DB error (unexpected):**
```
ERROR Failed to check QA pass signal for issue #42
INFO No QA pass signal for issue #42 — skipping auto-merge
```

## Related Work

- Issue #998: Track QA pass status in auto-merge monitor
- PR #998: Implemented QA signal gating (this spec)
- PR #983: Fixed approve signal matching (related signal detection work)

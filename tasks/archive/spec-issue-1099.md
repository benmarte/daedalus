# Spec: fix/issue-1099-validator-empty-summary-retry

**Issue**: #1099 — validator completes with empty summary; dispatcher silently drops the card without advancing pipeline  
**Branch**: `fix/issue-1099-validator-empty-summary-retry`  
**PR target**: `dev`

---

## Root Cause

In `_check_confirmed_validators` (`scripts/daedalus_dispatch.py`, line 3447), one confirmed bug
produces the silent drop. A previously suspected second bug does not exist.

### The single confirmed bug — silent drop when issue is unresolvable (line 3829)

`_get_task_summary` (line 1490) already null-guards the kanban card's summary fields with `or ""`,
so it always returns a string — never `None`. `summary_raw.lower()` at line 3508 therefore cannot
throw a `TypeError`. That is **not** the bug.

The actual bug is at line 3829:

```python
if not issue_nr:
    # issue_nr not in issues_map — skip retry, but the cap check above
    # has already emitted the notification if retries are exhausted (#378)
    continue    # ← SILENT DROP
```

**Trigger sequence:**
1. Validator card completes with `summary: None` / `""` (agent crash or timeout).
2. `_get_task_summary` returns `""`. Code enters the empty-summary branch (line 3680).
3. `_validator_github_comment_outcome` scans GitHub comments. The auto-posted "Completed — no
   summary was recorded on the kanban card." comment doesn't contain "confirmed", so `gh_outcome = ""`.
4. Retry-cap check runs (lines 3738–3828). Empty summary doesn't burn the cap
   (`_validator_summary_burns_cap` returns `False`), so `cap_count = 0` and the cap gate is never
   tripped. The cap-exhausted notification at line 3786 is never sent.
5. At line 3829: if the issue is not in `issues_map` (closed, old, or not in the pagination window)
   **and** `_fetch_issue_cached` at line 3685 also returns `None`, `issue_nr` stays `None`.
6. `continue` executes — no warning, no notification, no retry card. **Issue permanently stuck.**

Note: when `issue_nr` **is** resolvable, the retry path at lines 3846–3883 does fire correctly and
creates a retry card. The retry warning at line 3875 and the `absolute_max` ceiling at line 3767
are working as intended for that case.

---

## Fix Strategy

### 1. Early warning when summary is empty/None (after line 3510)

Immediately after the empty-summary branch is entered and the issue number extracted, log a
WARNING so operators can see the card was flagged:

```python
if not summary:
    logger.warning(
        "dispatch: validator for #%s completed with no summary — scheduling retry",
        n_nr,
    )
```

### 2. Replace silent drop with warning + notification at line 3829

When `issue_nr` is `None` and we have an empty summary, emit a warning and trigger the retry-cap
notification (idempotency-guarded) instead of silently continuing:

```python
if not issue_nr:
    if not summary:
        logger.warning(
            "dispatch: validator for #%s completed with no summary "
            "but issue is unresolvable — cannot retry without issue context; "
            "manual intervention required",
            n_nr,
        )
        if resolved is not None and not _has_notified_block(
            slug, n_nr, validator_profile=p["validator"], marker=_RETRY_CAP_MARKER
        ):
            _send_retry_cap_notification(
                role="validator",
                issue_number=n_nr,
                retry_count=retry_count,
                max_retries=max_validator_retries,
                resolved=resolved,
                dry_run=dry_run,
            )
            if not dry_run:
                _mark_notified_block(
                    slug, n_nr,
                    validator_profile=p["validator"],
                    marker=_RETRY_CAP_MARKER,
                )
    continue
```

### 3. No structural changes needed elsewhere

`_validator_summary_burns_cap` is already correct (returns `False` for empty/None — #916).  
The retry path at lines 3846–3883 already works when `issue_nr` is resolvable.  
The `absolute_max` ceiling (#958) is already in place.  
CONFIRMED / STOP / BLOCKED / ESCALATE branches are untouched.

---

## Acceptance Criteria

- [ ] Dispatcher detects validator `done` cards with `summary` that is `None`, empty string, or
  lacks any recognized prefix (`CONFIRMED:`, `STOP:`, `BLOCKED:`, `ESCALATE:`)
- [ ] Such cards are re-queued for validator retry when `issue_nr` is resolvable (existing path,
  already works; verify no regression)
- [ ] `WARNING`-level log emitted: `"validator for #N completed with no summary — scheduling retry"`
  whenever a retry card is about to be created
- [ ] When retry is skipped because `issue_nr` is unresolvable, a `WARNING` log is emitted AND the
  retry-cap notification fires (no silent `continue`)
- [ ] Retry cap (`absolute_max = max(max_validator_retries * 3, max_validator_retries + 3)`)
  is respected; cap-exhausted notification fires and no new card is created
- [ ] All existing behavior for well-formed summaries (`CONFIRMED:`, `STOP:`, `BLOCKED:`,
  `ESCALATE:`) is unchanged
- [ ] Unit tests cover:
  - Empty-string summary + issue resolvable → retry card created, warning logged
  - `None` summary (from show_card) + issue resolvable → same as above (no crash)
  - Issue-unresolvable path with empty summary → warning logged, notification sent, no silent drop
  - Cap-exhausted path (all-empty summaries, `retry_count >= absolute_max`) → cap notification
    sent, no new card created

---

## Files to Change

| File | Change |
|------|--------|
| `scripts/daedalus_dispatch.py` | Add `logger.warning` after empty-summary branch entry (~line 3510); replace silent `continue` with warning + notification at line 3829 |
| `tests/test_retry_cap_github_comment.py` | Add/verify 4 test cases listed in Acceptance Criteria |

**Two touch points in the dispatcher, four unit tests. No new functions or modules.**

---

## Scope Boundaries

- Do **not** change retry cap values or `_validator_summary_burns_cap` (already correct)
- Do **not** change `_check_confirmed_validators` signature or return value
- Do **not** modify CONFIRMED / STOP / BLOCKED / ESCALATE branches
- Do **not** change `_get_task_summary` — it already handles `None` correctly

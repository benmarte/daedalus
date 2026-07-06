# ADR-006: Max-Fix-Attempts Escalation Notification

**Status:** Accepted
**Date:** 2026-06-29
**PR:** #225

---

## Context

When a QA-assigned fix card exhausts `MAX_FIX_ATTEMPTS` (the file-counter limit), the pipeline stalls silently. The operator has no signal that human intervention is required. The only way to detect escalation was to monitor kanban cards directly.

Separately, the existing `qa-failed` notification fires when a fix card is *created* (first failure). When the card hits the attempt ceiling, that is a qualitatively different event — it means automated retries are exhausted and manual triage is required.

## Decision

1. Extend `run_iterate()` to return a 5-tuple: `(counts, advance_prs, pending_signal_cards, qa_failed_cards, escalated_cards)`.

2. Classify a QA fix card as *escalated* (vs. just *failed*) by checking the file counter after the executor returns `ok=True`:

   ```python
   _file_count = _read_fix_attempts(workdir).get(_tid, 0) if workdir and _tid else 0
   _escalated = (_file_count >= MAX_FIX_ATTEMPTS)
   if _escalated:
       escalated_cards.append(entry)
   else:
       qa_failed_cards.append(entry)
   ```

3. Add `"max-fix-attempts"` to `NOTIFY_EVENTS` and implement `_notify_max_fix_attempts()` following the exact pattern of `_notify_qa_failed`. Operators subscribe by adding `{"events": ["max-fix-attempts"]}` to the notification config.

4. Deduplicate via `_MAX_FIX_NOTIFIED: set` (same pattern as ADR-005).

## Consequences

**Good:**
- Operators receive a distinct alert when automated retries are exhausted, distinct from the first-failure `qa-failed` alert
- The classification reuses the existing file counter — no new state, no new kanban reads
- Fully opt-in: only subscribers to `max-fix-attempts` receive this alert
- The 5-tuple extension is backward-compatible via Python's `*_` unpack pattern in all existing callers

**Trade-off:**
- The 5-tuple extends the `run_iterate()` API; all callers were updated to use `*_` suffix unpacking to absorb future slots without breaking changes
- File-counter check happens in the dispatcher after `ok=True`; if the workdir is missing, escalation detection falls back to treating the card as a plain `qa-failed`

## Alternatives Considered

- **Add escalation field to kanban card** — persistent but requires kanban API changes across providers
- **Separate escalation executor** — over-engineered; the file counter already tracks attempts
- **Re-use `qa-failed` event with a flag** — confuses the semantic distinction between "first fail" and "retries exhausted"; separate events are clearer for operators

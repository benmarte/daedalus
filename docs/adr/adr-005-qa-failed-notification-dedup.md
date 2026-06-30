# ADR-005: QA-Failed Notification Deduplication

**Status:** Accepted
**Date:** 2026-06-29
**PR:** #225

---

## Context

When a QA fix card is created and remains blocked for multiple dispatcher ticks, `_notify_qa_failed()` fired every tick — one Slack message per cron cycle. This is notification spam for the operator and adds no information after the first alert.

The root cause: `run_iterate()` re-processes all blocked cards every tick. A QA card that needs manual intervention stays blocked indefinitely, so the notification path re-fires every 60 minutes.

## Decision

Add a module-level set `_QA_FAILED_NOTIFIED: set` keyed on `(issue_n, pr)`. Before sending, check if the key is already present. If it is, skip send silently. If not, send and add the key.

```python
_QA_FAILED_NOTIFIED: set = set()

def _notify_qa_failed(issue_number, pr_number, reason, resolved, dry_run=False):
    key = (issue_number, pr_number)
    if key in _QA_FAILED_NOTIFIED:
        return
    ...
    _QA_FAILED_NOTIFIED.add(key)
```

## Consequences

**Good:**
- One notification per blocked QA card per process lifetime (eliminates per-tick spam)
- Zero external storage required; in-memory is sufficient for the cron pattern
- No new dependencies

**Trade-off:**
- If the dispatcher process restarts (e.g., cron job restarts), the set resets and one additional notification fires. For the typical cron interval (60 min), this is acceptable — the operator gets at most one notification per restart, not per tick.
- Full persistent dedup would require stamping a comment on the kanban card. Filed as a follow-up if the restart rate proves too noisy in practice.

## Alternatives Considered

- **Kanban comment stamping** — persistent but adds a kanban write per notification; chosen as the follow-up path if restarts are frequent
- **TTL cache** — adds complexity without solving the restart case (restarts reset it anyway)
- **Database/file-based dedup** — overkill for a cron; the per-process set is simpler and correct

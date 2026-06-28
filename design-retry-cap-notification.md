# Design Spec: Slack/Discord Notification for PM/Validator Retry Cap Exhaustion

**Issue**: #181 (Phase 3) — PM retry cap hit → no notification
**Author**: planner-daedalus
**Date**: 2026-06-27
**Status**: Ready for implementation

---

## 1. Problem Statement

When the PM or validator agent exhausts its retry cap, the dispatcher logs an error but does not notify the human operator. This causes silent pipeline stalls that require manual log inspection to detect.

**Current behavior**:
- PM retry cap exhausted → `logger.error(...)` at `daedalus_dispatch.py:2129`
- Validator retry cap exhausted → `logger.error(...)` at `daedalus_dispatch.py:2090`
- No Slack/Discord/Telegram notification sent
- Human must discover the stall via dashboard or log inspection

**Desired behavior**:
- When either retry cap is exhausted, send a notification to configured targets
- Use existing `hermes send` infrastructure
- Support all configured platforms (Slack, Discord, Telegram, etc.)
- Include actionable context: issue number, role, retry count, failure reason

---

## 2. Architecture Overview

### 2.1 Existing Notification Infrastructure

The dispatcher already has a mature notification system:

**Event types** (`NOTIFY_EVENTS` at line 63):
```python
NOTIFY_EVENTS = ("doc-report", "dispatch-summary", "pipeline-failure", "pr-ready",
                 "security-escalation", "comment-mirror")
```

**Target resolution** (`_notify_targets()` at line 547):
- Reads `cron.notifications[]` from `daedalus.yaml`
- Falls back to legacy `cron.deliver` single target
- Returns list of `hermes send` target strings: `slack:C123`, `discord:123456`, `telegram:-100...`

**Delivery function** (`_hermes_send()` at line 3182):
```python
def _hermes_send(notify_target: str, report_body: str,
                 *, thread_id: Optional[str] = None) -> tuple[bool, Optional[str]]:
    """Send `report_body` via `hermes send` from dispatcher context."""
```

**Usage example** (doc-report delivery at line 2811):
```python
slack_delivered = _deliver_doc_reports(
    slug, provider, _notify_targets(resolved, "doc-report"), dry_run=dry_run,
)
```

### 2.2 Retry Cap Exhaustion Points

**Validator retry cap** (`_check_confirmed_validators()` at line 2093):
```python
_MAX_VALIDATOR_RETRIES = 2  # line 1386

if retry_count >= _MAX_VALIDATOR_RETRIES + 1:
    logger.error(
        "dispatch: validator for #%s has %d runs (cap %d) with no CONFIRMED — "
        "manual intervention required",
        n_nr, retry_count, _MAX_VALIDATOR_RETRIES,
    )
    continue  # ← NO NOTIFICATION CURRENTLY
```

**PM retry cap** (`_check_confirmed_validators()` at line 2132):
```python
_MAX_PM_RETRIES = 3  # inline at line 2131

if stale_count >= _MAX_PM_RETRIES:
    logger.error(
        "dispatch: PM for #%s has %d stale premature completions — "
        "manual intervention required (hermes kanban edit + SPEC: summary)",
        n, stale_count,
    )
    continue  # ← NO NOTIFICATION CURRENTLY
```

---

## 3. Implementation Plan

### 3.1 Add New Event Type

**File**: `scripts/daedalus_dispatch.py`
**Location**: Line 63 (NOTIFY_EVENTS tuple)
**Change**:
```python
NOTIFY_EVENTS = ("doc-report", "dispatch-summary", "pipeline-failure", "pr-ready",
                 "security-escalation", "comment-mirror", "retry-cap-exhausted")
```

**Rationale**: Extends the existing event system. Subscribers can filter on this event type in `daedalus.yaml`.

### 3.2 Create Notification Helper Function

**File**: `scripts/daedalus_dispatch.py`
**Location**: After `_hermes_send()` (after line 3182)

**Function signature**:
```python
def _send_retry_cap_notification(
    *,
    role: str,  # "pm" or "validator"
    issue_nr: int,
    retry_count: int,
    max_retries: int,
    resolved: Dict[str, Any],
    dry_run: bool,
) -> None:
    """Send notification when a role's retry cap is exhausted."""
```

**Implementation**:
```python
def _send_retry_cap_notification(
    *,
    role: str,
    issue_nr: int,
    retry_count: int,
    max_retries: int,
    resolved: Dict[str, Any],
    dry_run: bool,
) -> None:
    """Notify configured targets when PM/validator retry cap is exhausted."""
    targets = _notify_targets(resolved, "retry-cap-exhausted")
    if not targets:
        return
    
    # Format plain-text message (hermes send expects markdown/plain)
    body = (
        f"⚠️ **Retry Cap Exhausted: {role.upper()}**\n\n"
        f"Issue #{issue_nr} has failed {retry_count} times (max: {max_retries}).\n\n"
        f"**Role**: {role}\n"
        f"**Retry count**: {retry_count}/{max_retries}\n"
        f"**Status**: Manual intervention required\n\n"
    )
    
    if role == "pm":
        body += (
            "**Likely cause**: PM agent completed without `SPEC:` summary.\n"
            "**Recovery**: `hermes kanban edit <task-id>` and add `SPEC:` summary, "
            "or manually requeue with fresh context."
        )
    else:  # validator
        body += (
            "**Likely cause**: Validator agent completed without `CONFIRMED` "
            "(context window overflow, agent crash, or silent failure).\n"
            "**Recovery**: Check agent logs, verify issue context, then manually "
            "requeue validator or escalate to human review."
        )
    
    # Deliver to each target
    for target in targets:
        if dry_run:
            logger.info("[dry-run] would send retry-cap notification to %s for #%s", target, issue_nr)
            continue
        ok, thread = _hermes_send(target, body)
        if ok:
            logger.info("sent retry-cap notification to %s for #%s (role=%s)", target, issue_nr, role)
        else:
            logger.warning("failed to send retry-cap notification to %s for #%s", target, issue_nr)
```

### 3.3 Hook Into Validator Cap Exhaustion

**File**: `scripts/daedalus_dispatch.py`
**Location**: Line 2093-2096 (validator retry cap check)

**Current code**:
```python
if retry_count >= _MAX_VALIDATOR_RETRIES + 1:
    logger.error(
        "dispatch: validator for #%s has %d runs (cap %d) with no CONFIRMED — "
        "manual intervention required",
        n_nr, retry_count, _MAX_VALIDATOR_RETRIES,
    )
    continue
```

**Modified code**:
```python
if retry_count >= _MAX_VALIDATOR_RETRIES + 1:
    logger.error(
        "dispatch: validator for #%s has %d runs (cap %d) with no CONFIRMED — "
        "manual intervention required",
        n_nr, retry_count, _MAX_VALIDATOR_RETRIES,
    )
    _send_retry_cap_notification(
        role="validator",
        issue_nr=n_nr,
        retry_count=retry_count,
        max_retries=_MAX_VALIDATOR_RETRIES,
        resolved=resolved,
        dry_run=dry_run,
    )
    continue
```

**Note**: `resolved` and `dry_run` are already in scope at this point (function parameters of `_check_confirmed_validators()`).

### 3.4 Hook Into PM Cap Exhaustion

**File**: `scripts/daedalus_dispatch.py`
**Location**: Line 2131-2135 (PM retry cap check)

**Current code**:
```python
_MAX_PM_RETRIES = 3
if stale_count >= _MAX_PM_RETRIES:
    logger.error(
        "dispatch: PM for #%s has %d stale premature completions — "
        "manual intervention required (hermes kanban edit + SPEC: summary)",
        n, stale_count,
    )
    continue
```

**Modified code**:
```python
_MAX_PM_RETRIES = 3
if stale_count >= _MAX_PM_RETRIES:
    logger.error(
        "dispatch: PM for #%s has %d stale premature completions — "
        "manual intervention required (hermes kanban edit + SPEC: summary)",
        n, stale_count,
    )
    _send_retry_cap_notification(
        role="pm",
        issue_nr=n,
        retry_count=stale_count,
        max_retries=_MAX_PM_RETRIES,
        resolved=resolved,
        dry_run=dry_run,
    )
    continue
```

**Note**: Same scoping as validator — `resolved` and `dry_run` available.

---

## 4. Configuration

### 4.1 User Configuration (daedalus.yaml)

Users subscribe to the new event type in their project config:

```yaml
cron:
  notifications:
    - platform: "Slack"
      target: "slack:C0CHANNEL1"
      events: ["pipeline-failure", "retry-cap-exhausted"]  # ← new event
    - platform: "Discord"
      target: "discord:1234567890123456789"
      events: ["retry-cap-exhausted"]  # receive only this event
    - platform: "Telegram"
      target: "telegram:-1001234567890"
      # no `events` filter → receives ALL events including retry-cap-exhausted
```

### 4.2 Backward Compatibility

- **No breaking changes**: Existing configs without `retry-cap-exhausted` in their `events` list continue to work
- **Default behavior**: If a user has no `events` filter (catch-all), they automatically receive retry-cap notifications
- **No config migration needed**: New event type is additive

---

## 5. Notification Payload Schema

### 5.1 Message Format (Plain Text / Markdown)

```
⚠️ **Retry Cap Exhausted: PM**

Issue #151 has failed 3 times (max: 3).

**Role**: pm
**Retry count**: 3/3
**Status**: Manual intervention required

**Likely cause**: PM agent completed without `SPEC:` summary.
**Recovery**: `hermes kanban edit <task-id>` and add `SPEC:` summary, or manually requeue with fresh context.
```

### 5.2 Fields

| Field | Type | Description |
|-------|------|-------------|
| **Role** | `string` | `"pm"` or `"validator"` |
| **Issue number** | `int` | GitHub issue number (e.g., `#151`) |
| **Retry count** | `int` | Actual number of attempts made |
| **Max retries** | `int` | Configured cap (`_MAX_PM_RETRIES` or `_MAX_VALIDATOR_RETRIES`) |
| **Status** | `string` | Always `"Manual intervention required"` |
| **Likely cause** | `string` | Role-specific explanation |
| **Recovery** | `string` | Role-specific recovery steps |

### 5.3 Delivery Mechanism

- **Transport**: `hermes send -t <target> --file <tmpfile> --json`
- **Targets**: Resolved via `_notify_targets(resolved, "retry-cap-exhausted")`
- **Thread ID**: Not used (no threading for retry-cap notifications)
- **Idempotency**: Not enforced in v1 (duplicate notifications possible if dispatcher runs multiple times; low risk since cap exhaustion is rare)

---

## 6. Interface Contract

### 6.1 Caller → `_send_retry_cap_notification()`

**Caller** (retry cap check in `_check_confirmed_validators()`):
```python
_send_retry_cap_notification(
    role="pm" | "validator",
    issue_nr=<int>,
    retry_count=<int>,
    max_retries=<int>,
    resolved=<Dict[str, Any]>,
    dry_run=<bool>,
)
```

**Contract**:
- `role`: Must be `"pm"` or `"validator"` (used for message formatting)
- `issue_nr`: GitHub issue number (positive integer)
- `retry_count`: Actual number of attempts (≥ `max_retries`)
- `max_retries`: Configured cap (`_MAX_PM_RETRIES = 3` or `_MAX_VALIDATOR_RETRIES = 2`)
- `resolved`: Full resolved config dict (passed through to `_notify_targets()`)
- `dry_run`: If `True`, log intent but do not send

### 6.2 `_send_retry_cap_notification()` → `_hermes_send()`

**Internal call**:
```python
ok, thread = _hermes_send(target, body)
```

**Contract**:
- `target`: Single `hermes send` target string (e.g., `"slack:C123"`)
- `body`: Plain-text/markdown message (see §5.1)
- Returns: `(success: bool, thread_id: Optional[str])`

### 6.3 Error Handling

- **No targets configured**: Function returns early (no error, no notification)
- **Delivery failure**: Logs warning, continues to next target (non-fatal)
- **All deliveries fail**: Last warning logged, execution continues (pipeline not blocked)

---

## 7. Testing Strategy

### 7.1 Unit Tests

**File**: `tests/test_daedalus.py`

**Test cases**:
1. **Happy path**: Mock `_hermes_send()` → verify called with correct args when validator cap exhausted
2. **Happy path**: Mock `_hermes_send()` → verify called with correct args when PM cap exhausted
3. **No targets**: Verify no error when `_notify_targets()` returns empty list
4. **Dry run**: Verify `_hermes_send()` not called when `dry_run=True`
5. **Delivery failure**: Mock `_hermes_send()` to return `(False, None)` → verify warning logged, no exception
6. **Event filtering**: Verify config with `events: ["pipeline-failure"]` does NOT receive retry-cap notification

**Mock strategy**:
```python
with mock.patch.object(disp, "_hermes_send") as mock_send:
    mock_send.return_value = (True, "thread-123")
    # trigger retry cap exhaustion
    mock_send.assert_called_once()
    call_args = mock_send.call_args
    assert "Retry Cap Exhausted" in call_args[0][1]
    assert "pm" in call_args[0][1].lower() or "validator" in call_args[0][1].lower()
```

### 7.2 Integration Test

**Scenario**: End-to-end test with mock `hermes send` binary
1. Configure `daedalus.yaml` with `events: ["retry-cap-exhausted"]`
2. Simulate validator retry cap exhaustion (mock kanban to return 3 validator tasks)
3. Verify `hermes send` invoked with correct target and body

---

## 8. Risks & Edge Cases

| Risk | Mitigation |
|------|------------|
| **Duplicate notifications** (dispatcher runs multiple times after cap exhausted) | Acceptable in v1 (cap exhaustion is rare). v2 could add idempotency via DB flag or hidden comment sentinel |
| **No targets configured** | Function returns early — no error, no notification (silent failure is acceptable) |
| **Invalid `resolved` dict** | `_notify_targets()` handles malformed config gracefully (returns empty list) |
| **`hermes send` binary missing** | Existing error handling in `_hermes_send()` logs failure, does not crash |
| **Message formatting breaks on certain platforms** | Plain-text/markdown is universally supported; no platform-specific formatting yet |
| **Thread ID not used** | Retry-cap notifications are standalone alerts, not part of a conversation thread. Could add threading in v2 if needed |

---

## 9. Out of Scope

- **Automatic recovery**: This design does NOT include auto-recovery logic (e.g., auto-requeue with fresh context). Manual intervention is still required.
- **Retry cap configurability**: Caps remain hardcoded (`_MAX_PM_RETRIES = 3`, `_MAX_VALIDATOR_RETRIES = 2`). Making them configurable in `daedalus.yaml` is a separate feature.
- **Dashboard UI**: No changes to the dashboard plugin. Notifications are via `hermes send` only.
- **Slack/Discord webhook direct integration**: Uses existing `hermes send` infrastructure, not raw webhooks. This keeps the design consistent with other notifications.
- **Idempotency tracking**: v1 does not track "already notified" state. Duplicate notifications are possible but acceptable.

---

## 10. Implementation Checklist

- [ ] Add `"retry-cap-exhausted"` to `NOTIFY_EVENTS` tuple (line 63)
- [ ] Create `_send_retry_cap_notification()` helper function (after line 3182)
- [ ] Hook into validator retry cap exhaustion (line 2093)
- [ ] Hook into PM retry cap exhaustion (line 2131)
- [ ] Write unit tests for `_send_retry_cap_notification()` in `tests/test_daedalus.py`
- [ ] Write integration test with mock `hermes send`
- [ ] Update `docs/INSTALLATION_GUIDE.md` with new event type and example config
- [ ] Update `templates/daedalus.yaml` with commented example

---

## 11. Summary

**Implementation effort**: ~100 lines of code (helper + 2 hooks + tests)
**Dependencies**: Existing `_hermes_send()` and `_notify_targets()` infrastructure
**Breaking changes**: None (additive event type)
**Config changes**: None required (opt-in via `events` filter)

**Files to modify**:
| File | Change |
|------|--------|
| `scripts/daedalus_dispatch.py` | Add event type (line 63), helper function (after line 3182), hook validator cap (line 2093), hook PM cap (line 2131) |
| `tests/test_daedalus.py` | 6 unit tests + 1 integration test |
| `docs/INSTALLATION_GUIDE.md` | Document new event type |
| `templates/daedalus.yaml` | Add commented example |

**Ready for implementation**: Yes
**Blockers**: None

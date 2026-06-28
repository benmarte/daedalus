# Test Plan: Manual Issue → Ready Regression Scenarios

**Purpose:** Identify and document all regression test scenarios for transitioning issues to Ready status, covering both manual paths and interactions with the auto-advance sub-issues to Ready feature (tasks #919-#923, epic #915).

---

## Overview

The Daedalus dispatcher supports two paths to move issues to Ready status:

1. **Auto-advance (tier promotion):** When a sub-issue's PR merges (issue closes), the dispatcher's next tick calls `core/tier_promotion.promote_waiting_tiers()` to label and board-move dependent sub-issues to Ready if their dependencies are all closed.

2. **Manual transition:** A human manually moves an issue to Ready status via the GitHub Projects board UI or provider API.

The regression scenarios below ensure manual Ready transitions continue to work correctly after the auto-advance feature was introduced.

---

## Scenario 1: Manually transition a standalone issue to Ready

**Context:** A standalone issue (not part of an epic, no sub-issues, no dependencies) is manually moved to Ready status.

**Preconditions:**
- Issue exists and is open
- Issue has no parent epic reference (`Part of epic #N` in body)
- Issue has no `depends_on:` metadata
- Issue board status is NOT Ready (e.g., Backlog, Todo)

**Steps:**
1. Manually move issue to Ready status via GitHub Projects UI or provider API
2. Run dispatcher tick

**Expected State Transitions:**
- Issue board status = Ready
- Issue labels include "Ready" (if labels enabled)
- Issue becomes dispatchable (appears in dispatcher's candidate list)
- If no PR exists yet, dispatcher creates spec card (no developer card yet)

**Side Effects:**
- Tier promotion logic does NOT fire (issue has no epic reference)
- No dependent issues are promoted (issue has no sub-issues)
- Dispatcher's Ready-gating allows the issue to be dispatched on next tick
- If `board_configured` is True, issue must have board status Ready to be dispatched

**UI/API Contracts:**
- `provider.board_set_status(n, "Ready")` succeeds
- `provider.has_label(n, "Ready")` returns True (if labels enabled)
- Issue appears in `provider.board_numbers_with_statuses(["Ready"])`
- Dispatcher log shows: `dispatch: #N is Ready, dispatching...`

**Regression Risks:**
- Auto-advance logic accidentally filters out standalone issues
- Ready-gating incorrectly blocks manually-Ready standalone issues
- Dispatcher fails to create spec card for manually-Ready standalone issues

---

## Scenario 2: Manually transition a sub-issue to Ready while its parent is not yet Ready

**Context:** A sub-issue (part of an epic) is manually moved to Ready, but the parent epic itself is not in Ready status.

**Preconditions:**
- Parent epic exists and is open
- Parent epic board status is NOT Ready (e.g., Backlog, In Progress)
- Sub-issue exists and is open
- Sub-issue body contains `Part of epic #N` reference
- Sub-issue may or may not have `depends_on:` metadata
- Sub-issue board status is NOT Ready

**Steps:**
1. Manually move sub-issue to Ready status via GitHub Projects UI or provider API
2. Run dispatcher tick

**Expected State Transitions:**
- Sub-issue board status = Ready
- Sub-issue labels include "Ready" (if labels enabled)
- Sub-issue becomes dispatchable (appears in dispatcher's candidate list)
- Dispatcher creates spec card for sub-issue (if no PR exists)

**Side Effects:**
- Tier promotion logic does NOT fire (sub-issue is not yet closed)
- Sibling sub-issues are NOT promoted (tier promotion only fires on issue closure)
- Parent epic status remains unchanged
- If sub-issue has unmet dependencies (deps not closed), dispatcher's dependency-aware gating blocks dispatch until deps close

**UI/API Contracts:**
- `provider.board_set_status(n, "Ready")` succeeds
- `provider.has_label(n, "Ready")` returns True (if labels enabled)
- Sub-issue appears in `provider.board_numbers_with_statuses(["Ready"])`
- Dispatcher log shows: `dispatch: #N is Ready but blocked by #M — skipping until closed` (if deps not met)
- OR: `dispatch: #N is Ready, dispatching...` (if no deps or deps already closed)

**Regression Risks:**
- Auto-advance logic assumes sub-issues can only be Ready via tier promotion
- Dispatcher incorrectly blocks manually-Ready sub-issues whose parent is not Ready
- Tier promotion logic accidentally overwrites manual Ready status (e.g., removes Ready label)
- Dependency-aware gating incorrectly allows dispatch before deps close

---

## Scenario 3: Manually transition a sub-issue to Ready when dependencies are not yet satisfied

**Context:** A sub-issue with `depends_on:` metadata is manually moved to Ready, but its dependencies are still open.

**Preconditions:**
- Sub-issue exists and is open
- Sub-issue body contains `Part of epic #N` reference
- Sub-issue body contains `depends_on: #M, #K` metadata
- Issues #M and #K are still open (not closed)
- Sub-issue board status is NOT Ready

**Steps:**
1. Manually move sub-issue to Ready status via GitHub Projects UI or provider API
2. Run dispatcher tick

**Expected State Transitions:**
- Sub-issue board status = Ready
- Sub-issue labels include "Ready" (if labels enabled)
- Sub-issue does NOT become dispatchable (blocked by dependency-aware gating)

**Side Effects:**
- Tier promotion logic does NOT fire (sub-issue is not yet closed)
- Dispatcher's dependency-aware gating blocks dispatch until deps #M and #K close
- Once deps close, sub-issue becomes dispatchable WITHOUT needing tier promotion (manual Ready already set)

**UI/API Contracts:**
- `provider.board_set_status(n, "Ready")` succeeds
- `provider.has_label(n, "Ready")` returns True (if labels enabled)
- Sub-issue appears in `provider.board_numbers_with_statuses(["Ready"])`
- Dispatcher log shows: `dispatch: #N is Ready but blocked by #M, #K — skipping until closed`
- When deps close, dispatcher log shows: `dispatch: #N is Ready, dispatching...`
- Tier promotion does NOT post duplicate "tier-promoted" comment (manual Ready already set)

**Regression Risks:**
- Dependency-aware gating incorrectly allows dispatch before deps close
- Tier promotion accidentally overwrites manual Ready status (e.g., removes Ready label or posts duplicate comment)
- Dispatcher fails to recognize manually-Ready sub-issue as dispatchable after deps close
- Tier promotion logic assumes sub-issues can only be Ready via auto-advance

---

## Scenario 4: Manually transition an issue that was previously auto-advanced

**Context:** An issue was auto-advanced to Ready via tier promotion (all deps closed, dispatcher labeled it Ready), and then a human manually changes its status (e.g., moves it back to Backlog, then back to Ready).

**Preconditions:**
- Sub-issue was auto-advanced to Ready via tier promotion
- Sub-issue board status is Ready
- Sub-issue labels include "Ready"
- Sub-issue has `depends_on:` metadata, all deps closed

**Steps:**
1. Manually move sub-issue to Backlog status via GitHub Projects UI or provider API
2. Run dispatcher tick
3. Manually move sub-issue back to Ready status
4. Run dispatcher tick

**Expected State Transitions:**
- After step 1: Sub-issue board status = Backlog
- After step 2: Sub-issue does NOT become dispatchable (not Ready)
- After step 3: Sub-issue board status = Ready
- After step 4: Sub-issue becomes dispatchable

**Side Effects:**
- Tier promotion does NOT re-promote (issue was already promoted once; idempotency guard skips)
- Tier promotion does NOT post duplicate "tier-promoted" comment (issue already has Ready label)
- Dispatcher's dependency-aware gating allows dispatch (deps still closed, status is Ready)

**UI/API Contracts:**
- `provider.has_label(n, "Ready")` returns True (label persists across manual status changes)
- `provider.board_set_status(n, "Backlog")` succeeds
- `provider.board_set_status(n, "Ready")` succeeds
- Dispatcher log shows: `dispatch: #N is Ready, dispatching...`
- Tier promotion does NOT fire again (issue was not just closed; deps are closed but issue was not just-closed)

**Regression Risks:**
- Tier promotion logic assumes once-promoted issues stay Ready forever (idempotency guard too aggressive)
- Dispatcher incorrectly blocks manually-Ready issues that were previously auto-advanced
- Ready label is removed when board status changes manually (label and board status out of sync)
- Tier promotion posts duplicate "tier-promoted" comment on re-promotion attempt

---

## Scenario 5: Manual Ready + auto-advance race condition

**Context:** A sub-issue is manually moved to Ready at the same time its dependencies close, triggering both manual Ready and auto-advance tier promotion.

**Preconditions:**
- Sub-issue exists and is open
- Sub-issue body contains `depends_on: #M` metadata
- Issue #M is open
- Sub-issue board status is NOT Ready

**Steps:**
1. Close issue #M (PR merged)
2. Manually move sub-issue to Ready status (before dispatcher tick)
3. Run dispatcher tick

**Expected State Transitions:**
- Sub-issue board status = Ready (already set manually)
- Sub-issue labels include "Ready" (already set manually)
- Tier promotion detects sub-issue is already Ready (idempotency guard)
- Tier promotion does NOT post duplicate "tier-promoted" comment

**Side Effects:**
- Tier promotion checks `provider.has_label(n, "Ready")` → returns True
- Tier promotion skips `provider.add_label(n, "Ready")` (already Ready)
- Tier promotion skips `provider.board_set_status(n, "Ready")` (already Ready)
- Tier promotion skips `provider.post_issue_comment(n, "tier-promoted: ...")` (idempotency)
- Dispatcher log shows: `dispatch: #N is Ready, dispatching...`

**UI/API Contracts:**
- `provider.has_label(n, "Ready")` returns True
- No duplicate "tier-promoted" comment posted
- No duplicate Ready label applied
- No duplicate board status change

**Regression Risks:**
- Tier promotion does not check `has_label` before applying label (duplicate label)
- Tier promotion posts duplicate "tier-promoted" comment
- Tier promotion fails to detect manually-Ready issue as already promoted

---

## Scenario 6: Manual Ready on tier-0 sub-issue (no dependencies)

**Context:** A tier-0 sub-issue (no dependencies, created during epic decomposition) is manually moved to Ready after decomposition.

**Preconditions:**
- Epic was decomposed into sub-issues
- Tier-0 sub-issues (no `depends_on:`) were auto-labeled Ready during decomposition
- Sub-issue board status is Ready (set during decomposition)

**Steps:**
1. Manually move sub-issue to Backlog status
2. Run dispatcher tick
3. Manually move sub-issue back to Ready status
4. Run dispatcher tick

**Expected State Transitions:**
- After step 1: Sub-issue board status = Backlog
- After step 2: Sub-issue does NOT become dispatchable (not Ready)
- After step 3: Sub-issue board status = Ready
- After step 4: Sub-issue becomes dispatchable

**Side Effects:**
- Tier promotion does NOT fire (tier-0 issues are dispatched at creation, not promoted)
- Dispatcher's dependency-aware gating allows dispatch (no deps, status Ready)

**UI/API Contracts:**
- Sub-issue appears in `provider.board_numbers_with_statuses(["Ready"])`
- Dispatcher log shows: `dispatch: #N is Ready, dispatching...`

**Regression Risks:**
- Tier promotion accidentally tries to promote tier-0 issues (should skip, they're dispatched at creation)
- Dispatcher incorrectly blocks manually-Ready tier-0 sub-issues

---

## Scenario 7: Manual Ready on issue with external dependencies

**Context:** A sub-issue references external dependencies (issue numbers not part of the same epic) and is manually moved to Ready.

**Preconditions:**
- Sub-issue body contains `Part of epic #N` reference
- Sub-issue body contains `depends_on: #M` metadata
- Issue #M is NOT a sibling of this epic (external dep)
- Issue #M may be open or closed
- Sub-issue board status is NOT Ready

**Steps:**
1. Manually move sub-issue to Ready status
2. Run dispatcher tick

**Expected State Transitions:**
- Sub-issue board status = Ready
- Sub-issue labels include "Ready" (if labels enabled)
- External dependencies are treated as satisfied (not blocking dispatch)
- Sub-issue becomes dispatchable

**Side Effects:**
- Tier promotion only considers internal (sibling) dependencies
- External dependencies are dropped from tier computation
- Dispatcher's dependency-aware gating treats external deps as satisfied
- Sub-issue becomes dispatchable regardless of external dep state

**UI/API Contracts:**
- `provider.blockers(n)` returns only internal blockers (external deps excluded)
- Dispatcher log shows: `dispatch: #N is Ready, dispatching...`

**Regression Risks:**
- Tier promotion accidentally considers external deps as blockers
- Dispatcher incorrectly blocks manually-Ready sub-issues with external deps
- External deps prevent dispatch even when internal deps are satisfied

---

## Summary

| Scenario | Description | Key Regression Risk |
|----------|-------------|---------------------|
| 1 | Standalone issue → Ready | Auto-advance filters out standalone issues |
| 2 | Sub-issue → Ready (parent not Ready) | Dispatcher blocks sub-issues whose parent is not Ready |
| 3 | Sub-issue → Ready (deps not satisfied) | Tier promotion overwrites manual Ready |
| 4 | Previously auto-advanced → manual Ready | Idempotency guard too aggressive |
| 5 | Manual Ready + auto-advance race | Duplicate Ready label/comment |
| 6 | Tier-0 sub-issue → manual Ready | Tier promotion tries to promote tier-0 |
| 7 | External dependencies + manual Ready | External deps block dispatch |

---

## Implementation Notes

- **Tier promotion idempotency:** `core/tier_promotion.py:promote_waiting_tiers()` checks `provider.has_label(n, "Ready")` before applying label. Already-Ready issues are skipped without making provider calls.
- **Dependency-aware gating:** `scripts/daedalus_dispatch.py` filters Ready issues by `provider.blockers(n)` to block dispatch until deps close.
- **Board vs label:** Both board status AND label must be set for an issue to be dispatchable. Manual Ready transitions should set both.
- **External deps:** Only internal (sibling) dependencies are considered by tier promotion. External deps are dropped from tier computation.
- **Tier-0 dispatch:** Tier-0 sub-issues (no deps) are dispatched at creation during epic decomposition, not via tier promotion.

---

## Test Execution Checklist

- [ ] Scenario 1: Standalone issue → Ready
- [ ] Scenario 2: Sub-issue → Ready (parent not Ready)
- [ ] Scenario 3: Sub-issue → Ready (deps not satisfied)
- [ ] Scenario 4: Previously auto-advanced → manual Ready
- [ ] Scenario 5: Manual Ready + auto-advance race
- [ ] Scenario 6: Tier-0 sub-issue → manual Ready
- [ ] Scenario 7: External dependencies + manual Ready

---

**Owner:** qa-daedalus  
**Date:** 2026-06-28  
**Related:** Epic #915, Tasks #919-#923

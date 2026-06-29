# User Guide: New Behaviors (Current Release)

This guide documents all new user-facing behaviors introduced since v1.0.0-beta.30. Each section explains what the behavior does, how you interact with it, and any relevant configuration or prerequisites.

**Last updated:** 2026-06-28  
**Coverage:** 39 behaviors across 6 feature areas

---

## Table of Contents

1. [Epic & Sub-issue Management](#1-epic--sub-issue-management)
   - 1.1 [Epic Detection](#11-epic-detection-phase-1)
   - 1.2 [Epic Sub-issue Creation](#12-epic-sub-issue-creation-phase-3)
   - 1.3 [Source File Reading for Sub-issue Context](#13-source-file-reading-for-sub-issue-context)
   - 1.4 [Epic-Context-Informed Source Reading](#14-epic-context-informed-source-reading)
   - 1.5 [File/Module References Embedded in Sub-issue Bodies](#15-filemodule-references-embedded-in-sub-issue-bodies)
   - 1.6 [Decomposition Idempotency Marker Detection](#16-decomposition-idempotency-marker-detection)
   - 1.7 [Planner Not-Suitable Fallback](#17-planner-not-suitable-fallback)
2. [Dependency-Aware Dispatch](#2-dependency-aware-dispatch)
   - 2.1 [Dependency-Aware Ready-Gating](#21-dependency-aware-ready-gating)
   - 2.2 [Tier Promotion](#22-tier-promotion)
   - 2.3 [Conditional Ready Labeling for Sub-issues](#23-conditional-ready-labeling-for-sub-issues)
   - 2.4 [Tier Promotion Guard (Max One per Epic per Tick)](#24-tier-promotion-guard-max-one-per-epic-per-tick)
   - 2.5 [Promotion Idempotency via has_label()](#25-promotion-idempotency-via-has_label)
3. [Self-Healing & Auto-Advance](#3-self-healing--auto-advance)
   - 3.1 [Self-Healing Loop](#31-self-healing-loop-iterate)
   - 3.2 [Stale Blocked-Card Sweeper (48h)](#32-stale-blocked-card-sweeper-48h)
   - 3.3 [Stale Running-Card Sweeper (24h)](#33-stale-running-card-sweeper-24h)
   - 3.4 [Gateway Watchdog](#34-gateway-watchdog-silent-death-detection)
   - 3.5 [Separate blocked/stop Handlers](#35-separate-blockedstop-handlers)
   - 3.6 [Post Comment on Issue Closure When Validator Stops](#36-post-comment-on-issue-closure-when-validator-stops)
   - 3.7 [Correct STOP: Reason Slice](#37-correct-stop-reason-slice)
4. [Notification & Alerting](#4-notification--alerting)
   - 4.1 [Notification Threading (Per-Issue Threads)](#41-notification-threading-per-issue-threads)
   - 4.2 [Retry-Cap Notification (Slack/Discord Dual-Channel)](#42-retry-cap-notification-slackdiscord-dual-channel)
   - 4.3 [Intermediate Retry Notifications](#43-intermediate-retry-notifications-distinct-from-cap-exhaustion)
   - 4.4 [Dedup Guard on PM Retry-Cap Notifications](#44-dedup-guard-on-pm-retry-cap-notifications)
   - 4.5 [Suppress Retry-Attempt Notification at Cap Boundary](#45-suppress-retry-attempt-notification-at-cap-boundary)
   - 4.6 [Webhook Notification on Validator Retry Cap Exhausted](#46-webhook-notification-on-validator-retry-cap-exhausted)
   - 4.7 [Broadcast Thread Reply Support for Slack](#47-broadcast-thread-reply-support-for-slack)
   - 4.8 [Validator-Blocked Notification with Incrementing Idempotency Keys](#48-validator-blocked-notification-with-incrementing-idempotency-keys)
5. [Reliability & Infrastructure](#5-reliability--infrastructure)
   - 5.1 [Auto-Pagination of _fetch_issues](#51-auto-pagination-of-_fetch_issues)
   - 5.2 [Default Fetch Limit Raised to 100](#52-default-fetch-limit-raised-to-100)
   - 5.3 [issues_map Miss → Fallback get_issue() with Retry](#53-issues_map-miss--fallback-get_issue-with-retry)
   - 5.4 [Retry on GitHub Secondary Rate Limit (403)](#54-retry-on-github-secondary-rate-limit-403)
   - 5.5 [GitHub Projects Enrollment Node-ID Retry with Backoff](#55-github-projects-enrollment-node-id-retry-with-backoff)
6. [Dispatch & Pipeline](#6-dispatch--pipeline)
   - 6.1 [Dispatch History Persistence](#61-dispatch-history-persistence)
   - 6.2 [Skip show_card in Orphan Repair for Valid Assignees](#62-skip-show_card-in-orphan-repair-for-valid-assignees)
   - 6.3 [Agent Comment Header Enforcement](#63-agent-comment-header-enforcement)
   - 6.4 [--plugin-dir Flag for daedalus-cron.sh](#64-plugin-dir-flag-for-daedalus-cronsh)

---

## 1. Epic & Sub-issue Management

### 1.1 Epic Detection (Phase 1)

**What it does:**  
The dispatcher automatically identifies issues as "epic-sized" using three OR-combined heuristics:
- ≥4 markdown checklist items (`- [ ]`) in the issue body
- The `epic` label (case-insensitive)
- Issue body ≥2000 characters

Detection is non-destructive — it never raises errors and tolerates provider dicts or `IssueSummary` objects.

**How you interact with it:**  
Large, multi-part issues are automatically recognized as epics without manual tagging. You can short-circuit detection by adding the `epic` label to an issue, but in most cases the heuristic detection handles it transparently.

**Prerequisites:**  
None. Detection runs automatically on every dispatcher tick.

**Configuration:**  
No user-facing configuration. Detection thresholds are hardcoded in the source.

**Source implementation:**  
`core/providers/base.py:is_epic()` (line 126), `core/providers/base.py:IssueSummary`

---

### 1.2 Epic Sub-issue Creation (Phase 3)

**What it does:**  
When the planner agent completes its kanban card with `PLANNING COMPLETE:`, the dispatcher automatically decomposes the parent epic into sub-issues using one of two strategies:

- **Case A:** One sub-issue per checklist item in the epic body (capped at 10 sub-issues)
- **Case B:** Three default sub-issues:
  1. Research & Scoping
  2. Implementation
  3. Testing & Documentation

Sub-issues inherit parent labels (minus `epic`) and add the `subtask` label. An idempotency marker (`<!-- daedalus:sub-issues:[N1,N2,...] -->`) is embedded in the epic body to prevent duplicate decomposition.

**How you interact with it:**  
Epics are automatically broken into actionable sub-issues that enter the pipeline without manual intervention. You don't need to manually create sub-issues or track which checklist items map to which sub-tasks — the system handles decomposition after the planner finishes.

**Prerequisites:**  
- The epic must pass through Phase 2 (planner agent) and complete with `PLANNING COMPLETE:` in the card result.

**Configuration:**  
No user-facing configuration. Strategy selection (Case A vs Case B) is automatic based on checklist presence.

**Source implementation:**  
`core/iterate.py:_execute_planner_decompose()` (line 1503), `core/iterate.py:has_decomposed_marker()` (line 905), `core/iterate.py:_default_sub_issue_titles()` (line 932), `core/iterate.py:_sub_issue_body()` (line 973)

---

### 1.3 Source File Reading for Sub-issue Context

**What it does:**  
When decomposing epics, the planner reads relevant source files from the codebase and analyzes them to derive per-sub-issue context (file paths and symbols). The source file *contents* are deliberately not injected into sub-issue bodies — doing so blew past GitHub's 65,536-char body limit and produced a 422 "body is too long" (issue #899). Instead, bodies carry only the concise checklist-derived scope plus capped affected-files/symbols metadata. Constraints:
- Up to 10 files per sub-issue
- 50KB per file maximum
- Binary file detection (skips binary files)
- Path-traversal prevention (prevents reading files outside the repo)
- Graceful fallback when files are unavailable

**How you interact with it:**  
Sub-issues include relevant code context (file paths and symbol names) so assignees can start implementation without re-discovering the codebase. This reduces the cognitive load on downstream workers — they see the actual files and functions that need to change, not just a high-level description.

**Prerequisites:**  
- Files must exist in the repository at the paths referenced in the epic or planner output.
- Files must be text (not binary) and under 50KB.

**Configuration:**  
No user-facing configuration. Limits are hardcoded for safety and performance.

**Source implementation:**  
`core/iterate.py:read_source_files()` (line 1422), `core/iterate.py:identify_relevant_files()` (line 1249), `core/iterate.py:build_sub_issue_context()` (line 1477)

---

### 1.4 Epic-Context-Informed Source Reading

**What it does:**  
Enhances source reading with epic-level context. The system:
1. Extracts keywords from the parent epic
2. Matches them to component names using a known-components index
3. Identifies relevant files across the codebase based on those components

**How you interact with it:**  
Source context is more targeted — files match the epic's domain, not just keyword overlap. For example, if the epic mentions "authentication" and "token validation", the system identifies files in the auth module rather than just grepping for those keywords.

**Prerequisites:**  
- The codebase should have a recognizable component structure (e.g., `src/auth/`, `lib/database/`).
- The known-components index is loaded from the repository at dispatch time.

**Configuration:**  
No user-facing configuration. Component matching is automatic.

**Source implementation:**  
`core/iterate.py:extract_epic_context()` (line 1056), `core/iterate.py:load_known_components()` (line 1120), `core/iterate.py:filter_context_for_sub()` (line 1160)

---

### 1.5 File/Module References Embedded in Sub-issue Bodies

**What it does:**  
Sub-issue bodies include an "Affected Files" section listing the specific source files/modules relevant to that sub-task. The list is rendered from the epic-context source reading (see 1.4).

**How you interact with it:**  
Each sub-issue explicitly names which files to modify — no guesswork. Downstream workers can immediately see the scope of changes without searching the codebase.

**Prerequisites:**  
- Source file reading must succeed (see 1.3).
- Files must be identifiable as relevant to the sub-task (via epic context or keyword matching).

**Configuration:**  
No user-facing configuration. The "Affected Files" section is automatically generated.

**Source implementation:**  
`core/iterate.py:_render_affected_files_section()` (line 944), `core/iterate.py:_sub_issue_body()` (line 973)

---

### 1.6 Decomposition Idempotency Marker Detection

**What it does:**  
The `has_decomposed_marker()` function inspects issue body text for the `<!-- daedalus:sub-issues:[N1,N2,...] -->` marker. Before searching, it strips code blocks (``` ... ```) to prevent false positives from code examples that happen to contain the marker syntax.

**How you interact with it:**  
Epics are never double-decomposed even if the dispatcher re-processes them. You can safely mark an issue as `Ready` multiple times, or the dispatcher can tick repeatedly — decomposition happens exactly once.

**Prerequisites:**  
- The marker must be present in the issue body (added automatically by 1.2).

**Configuration:**  
No user-facing configuration. Idempotency is enforced automatically.

**Source implementation:**  
`core/iterate.py:has_decomposed_marker()` (line 905), `core/iterate.py:_strip_code_blocks()` (line 896)

---

### 1.7 Planner Not-Suitable Fallback

**What it does:**  
When the planner agent completes its kanban card but concludes the parent issue is not suitable for decomposition (e.g., the issue is already small, blocked on a dependency, or already simple enough for direct implementation), it signals `NOT SUITABLE FOR DECOMPOSITION` instead of `PLANNING COMPLETE:`. The dispatcher detects this via a case-insensitive regex match, skips the planner's normal decomposition path, looks up the parent issue, and creates a validator task for it — routing the issue through the standard validator → PM → developer flow rather than leaving it stuck in In Progress with no active child task.

**Defense-in-depth extension (fix for issue #969).** The previous implementation (`_check_planner_not_suitable()`) only scanned cards with `status="done"`. If the planner blocked its card — emitting the signal as a block reason instead of a completion summary — the handler was blind to it, and the issue stayed stuck In Progress forever. The handler now iterates **both `done` and `blocked` planner cards**, matching the signal on either status. Duplicate routing is prevented by the `planner-fallback-validator-{n}` idempotency key (only one validator per issue across both scans). The handler also emits diagnostic `info`/`debug` logs at every skip point (empty summary, non-matching pattern, missing issue, out-of-scope issue number) so silent failure modes from issue #969 no longer recur.

**How you interact with it:**  
No manual intervention required. If the planner determines an issue doesn't need decomposition, the system automatically reassigns it to the validator for normal pipeline processing. The parent issue will not get stuck — it will continue through the standard flow, even if the planner incorrectly blocks its card instead of completing it.

**Prerequisites:**  
- The parent issue must have been routed to the planner (via epic detection or manual assignment).
- The planner must **complete** (preferred) or **block** its kanban card with `NOT SUITABLE FOR DECOMPOSITION` in the summary or block reason.

**Configuration:**  
No user-facing configuration. Detection is automatic via case-insensitive regex matching of the planner's summary signal on both done and blocked planner cards.

**Source implementation:**  
`scripts/daedalus_dispatch.py:_check_planner_not_suitable()` (line ~3088)

---

## 2. Dependency-Aware Dispatch

### 2.1 Dependency-Aware Ready-Gating

**What it does:**  
The dispatcher refuses to start a `Ready` issue while any of its blockers are still open. Blockers are resolved per-provider:
- **GitHub:** Native `blocked_by` field from issue metadata
- **GitLab:** `is_blocked_by` field
- **Azure DevOps:** `Predecessor` work item links
- **Portable fallback:** `Depends on: #N` syntax in the issue body

The dispatcher re-checks blockers every tick (typically every 5 minutes), so a dependent issue auto-unblocks once its blockers' PRs merge.

**How you interact with it:**  
Dependent issues wait for their prerequisites automatically — no manual board juggling. If issue #123 depends on #100, and #100's PR is not yet merged, #123 will remain in `Ready` status but won't be dispatched. Once #100 merges, #123 auto-promotes to active dispatch on the next tick.

**Prerequisites:**  
- Dependencies must be declared using one of the supported syntaxes (provider-native or `Depends on: #N`).

**Configuration:**  
No user-facing configuration. Dependency resolution is automatic and provider-aware.

**Source implementation:**  
`core/providers/base.py:parse_depends_on()` (line 216), `core/providers/base.py:blockers()` (line 384), `core/providers/base.py:_depends_on_blockers()` (line 367)

---

### 2.2 Tier Promotion

**What it does:**  
After a sub-issue's PR merges, the dispatcher re-evaluates sibling sub-issues and promotes the next eligible tier. A tier is eligible when all its dependencies are closed. Promotion applies both the `Ready` label AND sets the board status to `Ready`, triggering normal dispatch.

**How you interact with it:**  
Cascading sub-issues auto-advance as prerequisites close — the next tier becomes workable automatically. For example, if you have three sub-issues where C depends on B, and B depends on A:
1. A is labeled `Ready` immediately (no dependencies)
2. After A's PR merges, B is auto-promoted to `Ready`
3. After B's PR merges, C is auto-promoted to `Ready`

You don't need to manually update labels or board status — tier promotion handles it.

**Prerequisites:**  
- Sub-issues must have dependencies declared (e.g., `sub-issue-C` body contains `Depends on: sub-issue-B`).
- The parent epic must have completed decomposition (see 1.2).

**Configuration:**  
No user-facing configuration. Tier promotion is automatic and cascading.

**Source implementation:**  
`core/tier_promotion.py:promote_waiting_tiers()`, `core/tier_promotion.py:DependencySnapshot`, `core/tier_promotion.py:compute_tiers()`

---

### 2.3 Conditional Ready Labeling for Sub-issues

**What it does:**  
On creation, sub-issues with no dependencies (`depends_on` empty) are immediately labeled `Ready` and enrolled on the project board with `Ready` status. Sub-issues with unmet dependencies are enrolled on the board with `Todo` status and skip the Ready label until tier promotion fires (see 2.2). Board enrollment failures are non-fatal and logged — sibling sub-issues still get processed.

**How you interact with it:**  
Independent sub-tasks start immediately and appear on the project board in the Ready column; dependent sub-tasks appear in the Todo column and move to Ready automatically when their blockers close. You don't need to manually label sub-issues as `Ready` or drag cards between columns — the system handles both based on dependency status.

**Prerequisites:**  
- Sub-issues must be created via epic decomposition (see 1.2).

**Configuration:**  
No user-facing configuration. Conditional labeling is automatic.

**Source implementation:**  
`core/tier_promotion.py`, `core/iterate.py` (tier promotion integration in dispatch loop)

---

### 2.4 Tier Promotion Guard (Max One per Epic per Tick)

**What it does:**  
Limits tier promotion to at most one tier advancement per parent epic per dispatcher tick. This prevents cascading label/comment spam and race conditions when multiple sub-issues close simultaneously.

**How you interact with it:**  
No duplicate notifications or label thrashing during rapid cascade promotions. If three sub-issues merge in quick succession, you'll see one promotion comment per tick (not three), keeping the issue thread readable.

**Prerequisites:**  
None. The guard is enforced automatically.

**Configuration:**  
No user-facing configuration. The limit is hardcoded to one promotion per epic per tick.

**Source implementation:**  
`core/tier_promotion.py` (promotion guard logic)

---

### 2.5 Promotion Idempotency via has_label()

**What it does:**  
`VCSProvider.has_label()` is now implemented for GitHub by inspecting the `labels` field from `get_issue()`. Already-Ready issues are excluded from re-promotion, preventing duplicate labels and comments.

**How you interact with it:**  
No duplicate `Ready` labels or promotion comments on every tick. If an issue is already labeled `Ready`, the dispatcher skips re-applying the label and posting a duplicate comment.

**Prerequisites:**  
- The VCS provider must support label inspection (GitHub, GitLab, Azure DevOps all do).

**Configuration:**  
No user-facing configuration. Idempotency is enforced automatically.

**Source implementation:**  
`core/providers/base.py:has_label()` (line 313), `core/providers/github.py` (GitHub implementation)

---

## 3. Self-Healing & Auto-Advance

### 3.1 Self-Healing Loop (iterate)

**What it does:**  
For every blocked card, `classify_blocked()` categorizes its state into one of:
- `advance`: Green CI → complete the card and advance the dependency chain
- `dev_fix_ci`: Red CI → create a fix card for the developer
- `pending_ci`: CI still running → wait and re-check next tick
- `pending_pr`: Awaiting PR → search VCS and auto-link the PR to the card
- `pm_route`: Reviewer requested changes → create a PM routing card to address feedback
- `approve_advance`: PR approved → complete the card and advance
- `escalate`: Max iterations exhausted → notify the team
- `planner_decompose`: Planner agent done → create sub-issues from the plan

Each action has a dedicated executor function that performs the appropriate VCS operations (label changes, comments, card completions, etc.).

**A note on approval signals:** `approve_advance` fires when a card's handoff text contains an explicit approval signal — `approved`, `lgtm`, `qa-passed`, `a11y-passed`, `security-approved`, and similar role-prefixed tokens. The bare word `pass` is **not** a signal: it used to be, but it false-triggered on ordinary QA notes like "all tests pass" (and even "password"), advancing cards that were never approved. If you author agent souls or write QA pass notes, you can describe a passing run freely — only the explicit signals above are read as approval.

**How you interact with it:**  
Blocked cards are automatically diagnosed and routed — most resolve without human intervention. You don't need to manually unblock cards or figure out why a card is stuck — the self-healing loop identifies the blocker and takes corrective action.

For example:
- If CI fails, a `dev_fix_ci` card is created and assigned to a developer
- If a PR is approved, the card auto-completes and the next tier promotes
- If an agent exhausts retries, the team is notified (see 4.2)

**Prerequisites:**  
None. The self-healing loop runs automatically on every dispatcher tick.

**Configuration:**  
No user-facing configuration. Classification logic is automatic.

**Source implementation:**  
`core/iterate.py:classify_blocked()` (line 100), `core/iterate.py:_execute_advance()` (line 380), `core/iterate.py:_execute_dev_fix_ci()` (line 531), `core/iterate.py:_execute_pending_pr()` (line 587), `core/iterate.py:_execute_pm_route()` (line 645), `core/iterate.py:_execute_escalate()` (line 829), `core/iterate.py:_execute_planner_decompose()` (line 1503)

---

### 3.2 Stale Blocked-Card Sweeper (48h)

**What it does:**  
Detects blocked cards with no activity for >48 hours and logs a warning. Optionally archives them off the active board to reduce noise. The threshold is configurable via `DEFAULT_STALE_HOURS`.

**How you interact with it:**  
Stuck cards surface as warnings in the dispatcher logs instead of silently rotting. If a card has been blocked for >48h with no progress, you'll see a warning like:
```
WARNING: Card t_abc123 blocked for >48h with no activity
```

This helps you identify issues that may need manual intervention or re-scoping.

**Prerequisites:**  
None. The sweeper runs automatically on every dispatcher tick.

**Configuration:**  
- `DEFAULT_STALE_HOURS` (in `core/sweeper.py`): Threshold for stale blocked cards. Default: 48 hours.

**Source implementation:**  
`core/sweeper.py:find_stale_blocked()` (line 92), `core/sweeper.py:DEFAULT_STALE_HOURS` (line 36)

---

### 3.3 Stale Running-Card Sweeper (24h)

**What it does:**  
Detects cards in `running` status whose summary hasn't advanced for >24 hours. This identifies workers that died or wedged — otherwise invisible since the board still shows them as in-progress.

**How you interact with it:**  
Dead workers are detected and surfaced — cards don't silently hang forever. If a card has been `running` for >24h with no progress, you'll see a warning like:
```
WARNING: Card t_xyz789 running for >24h with no activity
```

This helps you identify workers that may have crashed or are stuck in an infinite loop.

**Prerequisites:**  
None. The sweeper runs automatically on every dispatcher tick.

**Configuration:**  
- `DEFAULT_RUNNING_STALE_HOURS` (in `core/sweeper.py`): Threshold for stale running cards. Default: 24 hours.

**Source implementation:**  
`core/sweeper.py:find_stale_running()` (near line 92+), `core/sweeper.py:DEFAULT_RUNNING_STALE_HOURS` (line 37)

---

### 3.4 Gateway Watchdog (Silent Death Detection)

**What it does:**  
Detects when the Hermes gateway process is alive but the dispatcher goroutine is no longer ticking (silent death). Attempts restart with exponential backoff, rate-limited to max 3 restarts per 1-hour window with configurable cooldown. State is persisted to a JSON file to survive restarts.

**How you interact with it:**  
Gateway crashes are detected and auto-recovered without manual intervention. If the dispatcher stops ticking (e.g., due to a goroutine panic or deadlock), the watchdog:
1. Detects the stall (heartbeat timeout)
2. Attempts to restart the gateway
3. Logs the restart attempt
4. Backs off exponentially if restarts fail

You'll see logs like:
```
INFO: Gateway watchdog detected stalled dispatcher (no heartbeat for 300s)
INFO: Gateway watchdog attempting restart (attempt 1 of 3)
```

**Prerequisites:**  
- The watchdog must be enabled in the Hermes gateway configuration.
- The gateway must be running as a long-lived process (not a one-shot script).

**Configuration:**  
- `MAX_RESTARTS_PER_HOUR` (in `scripts/gateway_watchdog.py`): Maximum restart attempts per hour. Default: 3.
- `COOLDOWN_SECONDS` (in `scripts/gateway_watchdog.py`): Cooldown between restart attempts. Default: 60 seconds.

**Source implementation:**  
`scripts/gateway_watchdog.py`, `scripts/watchdog.py`

---

### 3.5 Separate blocked/stop Handlers

**What it does:**  
`blocked:` cards stay in the pipeline (awaiting human intervention), while `stop:` cards trigger the dedicated auto-close path that archives the card and closes the issue. Previously both shared one code path, causing incorrect behavior.

**How you interact with it:**  
- `blocked:` signals correctly wait for human input — the card remains on the board and the issue stays open until you manually resolve the blocker.
- `stop:` signals correctly close issues — the card is archived and the issue is closed automatically.

For example:
- If an agent posts `blocked: need clarification on API design`, the card waits for you to respond.
- If an agent posts `stop: duplicate of #100`, the card auto-closes and issue is closed.

**Prerequisites:**  
None. Handler separation is enforced automatically.

**Configuration:**  
No user-facing configuration.

**Source implementation:**  
`core/iterate.py` (action routing in `classify_blocked` and executor dispatch)

---

### 3.6 Post Comment on Issue Closure When Validator Stops

**What it does:**  
When a validator agent stops (exhausts retries, encounters a security threat, etc.), a closing comment is automatically posted to the GitHub issue explaining the outcome. The comment includes the stop reason and next steps.

**How you interact with it:**  
Issue closure has a visible explanation — no orphaned issues with no context. If a validator stops, you'll see a comment like:
```
**Agent: validator**
Issue closed: SECURITY_THREAT detected. Manual review required.
```

This helps you understand why an issue was closed without digging through dispatcher logs.

**Prerequisites:**  
- The issue must be a Daedalus-managed issue (tracked on the kanban board).

**Configuration:**  
No user-facing configuration. Closing comments are posted automatically.

**Source implementation:**  
`core/iterate.py` (validator stop handler)

---

### 3.7 Correct STOP: Reason Slice

**What it does:**  
Fixed the STOP: reason parsing to use `[5:]` instead of `[4:]`, correctly removing the leading colon from stop reasons. Previously, stop reasons displayed with a stray `:` prefix (e.g., `: duplicate` instead of `duplicate`).

**How you interact with it:**  
Stop reasons display correctly in comments and notifications (no stray `:` prefix). This is a bug fix — you don't need to do anything, but you'll notice cleaner output in agent comments.

**Prerequisites:**  
None. The fix is applied automatically.

**Configuration:**  
No user-facing configuration.

**Source implementation:**  
`core/iterate.py` (STOP parsing)

---

## 4. Notification & Alerting

### 4.1 Notification Threading (Per-Issue Threads)

**What it does:**  
Every Daedalus-managed issue gets one persistent thread per configured notification target (Slack channel, Discord channel, etc.). Agent comments on the issue and its linked PR are mirrored as replies into that thread. Threading is platform-agnostic:
- **Slack:** Uses `thread_ts` anchors stored in dispatch state
- **Discord:** Uses `message_id` anchors

Cross-tick duplicate suppression prevents reposting the same comment multiple times. Self-healing: if a thread root is deleted (e.g., by a user), the next event posts a fresh root and re-establishes the thread.

**How you interact with it:**  
All discussion about an issue appears in one organized thread per channel — no scattered messages. For example, if issue #123 has comments from the validator, developer, and reviewer, all those comments appear in a single Slack thread or Discord thread, making it easy to follow the conversation.

**Prerequisites:**  
- Notification targets (Slack/Discord webhooks or `hermes send` targets) must be configured.
- The notification platform must support threading (Slack and Discord do; SMS does not).

**Configuration:**  
No user-facing configuration. Threading is automatic for supported platforms.

**Source implementation:**  
`core/thread_delivery.py`, `core/dispatch_state.py` (threads key), `docs/notification-threading.md`

---

### 4.2 Retry-Cap Notification (Slack/Discord Dual-Channel)

**What it does:**  
When a PM or validator retry cap is exhausted (typically after 3 attempts), the dispatcher fires a `retry-cap-exhausted` alert on two independent channels:
1. **Structured webhook:** `NotificationPayload` sent to Slack/Discord webhooks (severity=critical, non-blocking, 10s timeout)
2. **Message bubble:** Broadcast to every `hermes send` target subscribed to the event

The notification includes:
- Role (PM or validator)
- Retry count
- Error classification
- Recovery instructions

An idempotency marker on the issue guarantees only one notification per cap-exhaustion, even across multiple dispatcher ticks.

**How you interact with it:**  
Team gets alerted on Slack AND Discord when an agent is stuck — recovery instructions included. For example:
```
🚨 CRITICAL: Validator retry cap exhausted for issue #123
Role: validator
Attempts: 3/3
Error: CI failure (lint)
Recovery: Manual intervention required — check PR #456 for details
```

This ensures the team is aware of persistent failures that need human attention.

**Prerequisites:**  
- Notification targets must be configured (see [Notifications](#notifications) in the main README).
- The issue must have exhausted its retry cap (typically 3 attempts).

**Configuration:**  
No user-facing configuration. Dual-channel notifications are automatic.

**Source implementation:**  
`core/notification_sender.py:send()`, `core/notification_sender.py:NotificationPayload`, `core/notification_sender.py:format_slack()`, `core/notification_sender.py:format_discord()`

---

### 4.3 Intermediate Retry Notifications (Distinct from Cap Exhaustion)

**What it does:**  
Intermediate retry attempts generate their own notification (severity=warning) distinct from the final cap-exhaustion alert (severity=critical). Users see progressive failure signals, not just the final one.

**How you interact with it:**  
You know when an agent is struggling before it fully fails — earlier intervention possible. For example:
- Attempt 1 fails → warning notification: "Validator retry 1/3 failed"
- Attempt 2 fails → warning notification: "Validator retry 2/3 failed"
- Attempt 3 fails → critical notification: "Retry cap exhausted — manual intervention required"

This lets you intervene early if you see a pattern of failures, rather than waiting for the final cap-exhaustion.

**Prerequisites:**  
- Notification targets must be configured.
- The issue must be on an intermediate retry attempt (not the final one).

**Configuration:**  
No user-facing configuration. Intermediate notifications are automatic.

**Source implementation:**  
`core/iterate.py` (retry notification path), `core/notification_sender.py`

---

### 4.4 Dedup Guard on PM Retry-Cap Notifications

**What it does:**  
The PM retry-cap notification path includes a dedup guard ensuring the same issue's cap-exhaustion is only notified once, even across multiple dispatcher ticks. The guard checks for an idempotency marker embedded in the issue body.

**How you interact with it:**  
No spam — one notification per cap-exhaustion event. Even if the dispatcher ticks 10 times while the issue is in a cap-exhausted state, you'll only see one critical notification.

**Prerequisites:**  
None. The dedup guard is enforced automatically.

**Configuration:**  
No user-facing configuration.

**Source implementation:**  
`core/iterate.py` (dedup guard in PM retry path)

---

### 4.5 Suppress Retry-Attempt Notification at Cap Boundary

**What it does:**  
When a validator is at exactly `MAX_FIX_ATTEMPTS` (typically 3), the intermediate retry notification is suppressed to avoid a duplicate with the imminent cap-exhaustion notification. This prevents a confusing sequence where you see both "retry 3/3 failed" and "retry cap exhausted" in rapid succession.

**How you interact with it:**  
No double-notification at the exact moment of failure. You'll see the final critical notification ("retry cap exhausted") without an intermediate warning immediately before it.

**Prerequisites:**  
None. Boundary suppression is automatic.

**Configuration:**  
No user-facing configuration.

**Source implementation:**  
`core/iterate.py` (boundary suppression logic)

---

### 4.6 Webhook Notification on Validator Retry Cap Exhausted

**What it does:**  
The validator retry-cap exhaustion path fires a webhook notification (via `core/notification_sender.py`) with:
- Role (validator)
- Retry count
- Error classification
- Recovery instructions

The notification fires even when `issue_nr` is missing (e.g., if the issue was deleted or the card is orphaned).

**How you interact with it:**  
External monitoring systems get structured failure data for alerting dashboards. For example, you can forward webhook notifications to PagerDuty, Datadog, or a custom Slack bot for centralized incident tracking.

**Prerequisites:**  
- Webhook endpoints must be configured in the notification settings.
- The issue must have exhausted its retry cap.

**Configuration:**  
- Webhook URLs are configured in the Hermes notification settings (see [Notifications](#notifications) in the main README).

**Source implementation:**  
`core/notification_sender.py`, `core/iterate.py`

---

### 4.7 Broadcast Thread Reply Support for Slack

**What it does:**  
The `broadcast_thread_reply` function mirrors agent comments as threaded replies on Slack, using `thread_ts` anchors stored in dispatch state. This matches the Discord threading behavior (see 4.1).

**How you interact with it:**  
Issue conversations are threaded in Slack, matching the Discord behavior. All comments about issue #123 appear in a single Slack thread, making it easy to follow the conversation without cluttering the main channel.

**Prerequisites:**  
- Slack must be configured as a notification target.
- The Slack channel must support threading (all Slack channels do).

**Configuration:**  
No user-facing configuration. Threading is automatic for Slack.

**Source implementation:**  
`core/thread_delivery.py:broadcast_thread_reply()`

---

### 4.8 Validator-Blocked Notification with Incrementing Idempotency Keys

**What it does:**  
When a validator blocks with `BLOCKED:`, the dispatcher creates a PM consultation task. Previously, the idempotency key was static (`validator-blocked-{n}`), so after the first consultation completed and the validator blocked again on a subsequent tick, no new consultation was created — the issue stalled indefinitely with no human notification. Now the key increments per block cycle: `validator-blocked-{n}`, `validator-blocked-{n}-r1`, `validator-blocked-{n}-r2`, etc., ensuring each block creates a fresh consultation. Additionally, the dispatcher fires a new `validator-blocked` notification to Slack/Discord on every block (including repeat blocks) so stalled issues surface to humans immediately. An in-flight guard prevents duplicate consultations while one is already active for the same issue.

**How you interact with it:**  
If a validator blocks multiple times on the same issue (e.g., the PM resolves one blocker but the validator encounters another), you now get:
1. A new PM consultation task on each block (not just the first)
2. A Slack/Discord notification on every block, so you're alerted immediately
3. No duplicate consultations while a PM is actively working on the current block

This prevents silent stalls where an issue sits blocked with no human awareness.

**Prerequisites:**  
- Slack or Discord must be configured as notification targets to receive alerts.
- The issue must have a validator that can block with `BLOCKED:`.

**Configuration:**  
No user-facing configuration. The incrementing key and notification are applied automatically.

**Source implementation:**  
`scripts/daedalus_dispatch.py:_check_confirmed_validators()`, `scripts/daedalus_dispatch.py:_notify_validator_blocked()`

---

## 5. Reliability & Infrastructure

### 5.1 Auto-Pagination of _fetch_issues

**What it does:**  
`_fetch_issues()` now auto-paginates to retrieve all open issues, preventing silent truncation on boards with >20 (now default 100) open issues. Previously, only the first page of results was fetched, causing issues beyond the limit to be ignored.

**How you interact with it:**  
Large boards with many open issues are fully processed — nothing silently skipped. If your board has 250 open issues, all 250 are fetched and considered for dispatch, not just the first 100.

**Prerequisites:**  
None. Auto-pagination is applied automatically.

**Configuration:**  
No user-facing configuration. Pagination is handled transparently.

**Source implementation:**  
`core/providers/base.py` (`_fetch_issues` pagination logic)

---

### 5.2 Default Fetch Limit Raised to 100

**What it does:**  
Default limit for `_fetch_issues()` raised from 20 to 100. This reduces the number of API calls needed to fetch all issues on large boards.

**How you interact with it:**  
Larger boards are handled by default without configuration changes. You don't need to manually tune the fetch limit — the new default (100) is sufficient for most projects.

**Prerequisites:**  
None. The new default is applied automatically.

**Configuration:**  
No user-facing configuration. The limit is hardcoded in the source.

**Source implementation:**  
`core/providers/base.py` (default limit constant)

---

### 5.3 issues_map Miss → Fallback get_issue() with Retry

**What it does:**  
When the dispatcher's `issues_map` cache misses an issue number, it falls back to a direct `get_issue()` call with retry on transient failure instead of failing outright. This is applied at all dispatcher call sites where `issues_map` is accessed.

**How you interact with it:**  
Transient cache misses don't crash the dispatcher. If an issue is not in the cache (e.g., due to a race condition or timing issue), the dispatcher fetches it directly from the VCS provider with retry logic.

**Prerequisites:**  
None. Fallback is applied automatically.

**Configuration:**  
No user-facing configuration.

**Source implementation:**  
`core/iterate.py` (issues_map miss fallback at dispatch sites)

---

### 5.4 Retry on GitHub Secondary Rate Limit (403)

**What it does:**  
The GitHub provider now retries on 403 responses that indicate secondary rate limits (as opposed to auth failures), with appropriate backoff. Secondary rate limits occur when you're making too many API calls in a short period.

**How you interact with it:**  
GitHub's "you're making too many requests" errors are handled gracefully instead of causing failures. The dispatcher automatically backs off and retries, so you don't see spurious failures during high-activity periods.

**Prerequisites:**  
None. Retry logic is applied automatically.

**Configuration:**  
No user-facing configuration. Backoff parameters are hardcoded for safety.

**Source implementation:**  
`core/providers/github.py` (403 retry logic)

---

### 5.5 GitHub Projects Enrollment Node-ID Retry with Backoff

**What it does:**  
When the GitHub Projects GraphQL API returns a transient failure resolving the node ID for an issue, the dispatcher retries with exponential backoff. This handles temporary GraphQL API outages or rate limits.

**How you interact with it:**  
Transient GraphQL failures don't block issue enrollment. If the GitHub Projects API is temporarily unavailable, the dispatcher retries automatically instead of failing the entire dispatch tick.

**Prerequisites:**  
- GitHub Projects must be enabled for the repository.

**Configuration:**  
No user-facing configuration. Retry logic is applied automatically.

**Source implementation:**  
`core/providers/github.py` (enrollment retry logic)

---

## 6. Dispatch & Pipeline

### 6.1 Dispatch History Persistence

**What it does:**  
Dispatch history is persisted to `history.jsonl` (a JSON Lines file) with a `--history` CLI viewer for inspecting past dispatch decisions. Each line in the file represents one dispatch event (e.g., "dispatched issue #123 to developer-daedalus").

**How you interact with it:**  
You can review what the dispatcher did and why, across runs. Use the `--history` flag with `daedalus_dispatch.py` to view the history:
```bash
python scripts/daedalus_dispatch.py --history
```

This shows a log of all dispatch decisions, including:
- Which issue was dispatched
- Which agent profile was assigned
- Why the issue was dispatched (e.g., "labeled Ready", "tier promotion")
- Timestamp of the dispatch event

**Prerequisites:**  
None. History persistence is automatic.

**Configuration:**  
- `history.jsonl` is written to the Daedalus working directory (typically `.hermes/`).

**Source implementation:**  
`core/dispatch_state.py`, `scripts/daedalus_dispatch.py` (history persistence)

---

### 6.2 Skip show_card in Orphan Repair for Valid Assignees

**What it does:**  
Performance optimization — skips the `show_card` diagnostic call during orphan repair when the assignee profile is known-valid. This reduces unnecessary API calls and speeds up dispatch ticks.

**How you interact with it:**  
Faster dispatch ticks on boards with many orphan repair events. You don't need to do anything — the optimization is applied automatically when the assignee profile is recognized as valid.

**Prerequisites:**  
None. Optimization is applied automatically.

**Configuration:**  
No user-facing configuration.

**Source implementation:**  
`core/iterate.py` (orphan repair path)

---

### 6.3 Agent Comment Header Enforcement

**What it does:**  
All SOUL files now route comments through `scripts/agent_comment.py`, which enforces the mandatory `**Agent: <name>**` header as the first line of every comment. This ensures every agent comment is identifiable by author.

**How you interact with it:**  
Every agent comment is identifiable by author — comments can be filtered/parsed by role. For example:
```
**Agent: developer-daedalus**
Implementation complete. PR #456 opened.
```

This makes it easy to see which agent posted which comment, especially in busy issue threads.

**Prerequisites:**  
None. Header enforcement is automatic for all agent comments.

**Configuration:**  
No user-facing configuration. The header format is enforced by `agent_comment.py`.

**Source implementation:**  
`scripts/agent_comment.py`

---

### 6.4 --plugin-dir Flag for daedalus-cron.sh

**What it does:**  
The `daedalus-cron.sh` script accepts a `--plugin-dir` flag to load a local development plugin without reinstalling. This is useful for testing plugin changes during development.

**How you interact with it:**  
Developers can test plugin changes locally without a full reinstall cycle. For example:
```bash
./scripts/daedalus-cron.sh --plugin-dir /path/to/local/plugin
```

This loads the plugin from the specified directory instead of the installed location, allowing you to iterate on plugin code without reinstalling.

**Prerequisites:**  
- The plugin directory must contain a valid Hermes plugin (with `plugin.yaml` or equivalent manifest).

**Configuration:**  
- `--plugin-dir <path>`: Path to the local plugin directory.

**Source implementation:**  
`scripts/daedalus-cron.sh`

---

## Summary

This guide documents **31 new user-facing behaviors** across **6 feature areas**:
- **Epic & Sub-issue Management (6 behaviors):** Automatic epic detection, decomposition, and context injection
- **Dependency-Aware Dispatch (5 behaviors):** Ready-gating, tier promotion, and idempotency
- **Self-Healing & Auto-Advance (7 behaviors):** Automatic diagnosis and routing of blocked cards
- **Notification & Alerting (7 behaviors):** Threading, retry-cap alerts, and webhook integration
- **Reliability & Infrastructure (5 behaviors):** Auto-pagination, retry logic, and rate-limit handling
- **Dispatch & Pipeline (4 behaviors):** History persistence, performance optimizations, and comment enforcement

All behaviors are verified against the source code implementation and are active in the current release (since v1.0.0-beta.30). No user-facing configuration is required for most behaviors — they are automatic and transparent.

For more details on specific features, see:
- [Notification Threading](./notification-threading.md)
- [Installation Guide](./INSTALLATION_GUIDE.md)
- [CHANGELOG](../CHANGELOG.md)

---

**Document version:** 1.0  
**Last verified:** 2026-06-28  
**Source catalog:** `.hermes/release_behavior_catalog.md`

# User Guide: New Behaviors (Current Release)

> **Note on line-number drift:** Source line numbers cited throughout this document (e.g., “line 126”) are approximate and may drift as the codebase evolves. If a referenced line has shifted, search for the function or symbol name in the source instead.

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
   - 6.5 [QA Gate for Auto-Merge](#65-qa-gate-for-auto-merge)
   - 6.6 [Dispatcher Concurrency (FileLock Mutex)](#66-dispatcher-concurrency-filelock-mutex)
   - 6.7 [Status-Blind Re-Triage](#67-status-blind-re-triage)
   - 6.8 [Dispatcher CLI Flags (--dry-run, --self-test, --history)](#68-dispatcher-cli-flags---dry-run---self-test---history)
   - 6.9 [Dev Mode Redirect (Local Dev Checkout)](#69-dev-mode-redirect-local-dev-checkout)

**Last updated:** 2026-06-30  
**Coverage:** 39 behaviors across 6 feature areas

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
`core/providers/base.py:is_epic()` (line ~126), `core/providers/base.py:IssueSummary`

---

### 1.2 Epic Sub-issue Creation (Phase 3)

**What it does:**  
When the planner agent completes its kanban card with `PLANNING COMPLETE:` (or the synonym `PLAN:`), the dispatcher automatically decomposes the parent epic into sub-issues using one of two strategies:

- **Case A:** One sub-issue per checklist item in the epic body (capped at 10 sub-issues)
- **Case B:** Three default sub-issues:
  1. Research & Scoping
  2. Implementation
  3. Testing & Documentation

Sub-issues inherit parent labels (minus `epic`) and add the `subtask` label. An idempotency marker (`<!-- daedalus:sub-issues:[N1,N2,...] -->`) is embedded in the epic body to prevent duplicate decomposition.

**How you interact with it:**  
Epics are automatically broken into actionable sub-issues that enter the pipeline without manual intervention. You don't need to manually create sub-issues or track which checklist items map to which sub-tasks — the system handles decomposition after the planner finishes.

**Prerequisites:**  
- The epic must pass through Phase 2 (planner agent) and complete with `PLANNING COMPLETE:` or `PLAN:` in the card result.

**Configuration:**  
No user-facing configuration. Strategy selection (Case A vs Case B) is automatic based on checklist presence.

**Source implementation:**  
`core/iterate.py:_execute_planner_decompose()` (line ~1503), `core/iterate.py:has_decomposed_marker()` (line ~905), `core/iterate.py:_default_sub_issue_titles()` (line ~932), `core/iterate.py:_sub_issue_body()` (line ~973)

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
`core/iterate.py:read_source_files()` (line ~1422), `core/iterate.py:identify_relevant_files()` (line ~1249), `core/iterate.py:build_sub_issue_context()` (line ~1477)

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
`core/iterate.py:extract_epic_context()` (line ~1056), `core/iterate.py:load_known_components()` (line ~1120), `core/iterate.py:filter_context_for_sub()` (line ~1160)

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
`core/iterate.py:_render_affected_files_section()` (line ~944), `core/iterate.py:_sub_issue_body()` (line ~973)

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
`core/iterate.py:has_decomposed_marker()` (line ~905), `core/iterate.py:_strip_code_blocks()` (line ~896)

---

### 1.7 Planner Not-Suitable Fallback

**What it does:**  
When the planner agent completes its kanban card but concludes the parent issue is not suitable for decomposition (e.g., the issue is already small, blocked on a dependency, or already simple enough for direct implementation), it signals `NOT SUITABLE FOR DECOMPOSITION` instead of `PLANNING COMPLETE:`. The dispatcher detects this via a case-insensitive regex match, skips the planner's normal decomposition path, looks up the parent issue, and creates a validator task for it — routing the issue through the standard validator → PM → developer flow rather than leaving it stuck In Progress with no active child task. If the planner summary contains neither `PLANNING COMPLETE:`, `PLAN:`, nor `NOT SUITABLE FOR DECOMPOSITION`, the dispatcher emits a `WARNING`-level log instead of silently dropping the task (fix for #1072).

**Defense-in-depth extension (fix for issue #969).** The previous implementation (`_check_planner_not_suitable()`) only scanned cards with `status="done"`. If the planner blocked its card — emitting the signal as a block reason instead of a completion summary — the handler was blind to it, and the issue stayed stuck In Progress forever. The handler now iterates **both `done` and `blocked` planner cards**, matching the signal on either status. Duplicate routing is prevented by the `planner-fallback-validator-{n}` idempotency key (only one validator per issue across both scans). The handler also emits diagnostic `info`/`debug` logs at every skip point (empty summary, non-matching pattern, missing issue, out-of-scope issue number) so silent failure modes from issue #969 no longer recur.

**How you interact with it:**  
No manual intervention required. If the planner determines an issue doesn't need decomposition, the system automatically reassigns it to the validator for normal pipeline processing. The parent issue will not get stuck — it will continue through the standard flow, even if the planner incorrectly blocks its card instead of completing it.

**Prerequisites:**  
- The parent issue must have been routed to the planner (via epic detection or manual assignment).
- The planner must **complete** (preferred) or **block** its kanban card with `NOT SUITABLE FOR DECOMPOSITION` in the summary or block reason.

**Configuration:**  
No user-facing configuration. Detection is automatic via case-insensitive regex matching of the planner's summary signal on both done and blocked planner cards.

**Source implementation:**  
`scripts/daedalus_dispatch.py:_check_planner_not_suitable()` (line ~3214)

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
`core/providers/base.py:parse_depends_on()` (line ~216), `core/providers/base.py:blockers()` (line ~384), `core/providers/base.py:_depends_on_blockers()` (line ~367)

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
`core/providers/base.py:has_label()` (line ~313), `core/providers/github.py` (GitHub implementation)

---

## 3. Self-Healing & Auto-Advance

### 3.1 Self-Healing Loop (iterate)

**What it does:**  
For every blocked card, `classify_blocked()` categorizes its state into one of:
- `advance`: Developer card with open PR + `review-required` → complete the card and advance the dependency chain (CI no longer gates this — enforced at merge-time per epic #1074)
- `qa_fix`: QA card with failing tests → create a fix card for the developer
- `pending_signal`: QA/accessibility card with unrecognized QA/a11y signal → wait and re-check next tick
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
- If QA reports failing tests, a `qa_fix` card is created and assigned to a developer
- If a PR is approved, the card auto-completes and the next tier promotes
- If an agent exhausts retries, the team is notified (see 4.2)

**Prerequisites:**  
None. The self-healing loop runs automatically on every dispatcher tick.

**Configuration:**  
No user-facing configuration. Classification logic is automatic.

**Source implementation:**  
`core/iterate.py:classify_blocked()` (line ~100), `core/iterate.py:_execute_advance()` (line ~380), `core/iterate.py:_execute_qa_fix()` (line ~531), `core/iterate.py:_execute_pending_pr()` (line ~587), `core/iterate.py:_execute_pm_route()` (line ~645), `core/iterate.py:_execute_escalate()` (line ~829), `core/iterate.py:_execute_planner_decompose()` (line ~1503)

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
`core/sweeper.py:find_stale_blocked()` (line ~92), `core/sweeper.py:DEFAULT_STALE_HOURS` (line ~36)

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
`core/sweeper.py:find_stale_running()` (near line ~92+), `core/sweeper.py:DEFAULT_RUNNING_STALE_HOURS` (line ~37)

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
Fixed the STOP: reason parsing to use `[5:]` instead of `[4:]`, correctly removing the leading colon from stop reasons. Previously, stop reasons displayed with a stray `:` prefix (e.g., `:duplicate` instead of `duplicate`).

**How you interact with it:**  
STOP: reasons display correctly in card summaries and Slack notifications. No manual intervention required — the fix is applied automatically.

**Prerequisites:**  
None. The fix is in the `iterate.py:_format_stop_message()` function.

**Configuration:**  
No user-facing configuration.

**Source implementation:**  
`core/iterate.py:_format_stop_message()` (line ~747)

---

## 4. Notification & Alerting

### 4.1 Notification Threading (Per-Issue Threads)

**What it does:**  
Slack and Discord notifications now use per-issue threading. Each issue gets its own thread, so updates (validation confirmed, PR opened, CI passed, etc.) appear as replies to the original notification rather than cluttering the main channel. This keeps the channel readable and makes it easy to see the full history of a specific issue.

**How you interact with it:**  
All notifications related to a specific issue appear in a single thread. You can follow an issue's progress by looking at its thread. For example:
- Initial notification: "Issue #100 marked Ready"
- Reply: "Validator confirmed issue"
- Reply: "Developer opened PR #200"
- Reply: "CI passed, PR merged, issue closed"

No configuration needed — threading is automatic for all Daedalus notifications.

**Prerequisites:**  
- Slack or Discord must be configured as notification targets.

**Configuration:**  
No user-facing configuration. Threading is automatic.

**Source implementation:**  
`core/iterate.py` (notification dispatch logic), `core/scheduler.py:_notify()` (thread routing)

---

### 4.2 Retry-Cap Notification (Slack/Discord Dual-Channel)

**What it does:**  
When an agent exhausts its retry budget, the dispatcher sends a notification to **both** Slack and Discord (if both are configured), not just the last channel that received a notification for that issue. This ensures the team is alerted on retry-cap exhaustion regardless of which channel was used earlier.

**How you interact with it:**  
If an agent exhausts retries, you'll see a notification in both Slack and Discord (if both are configured). The notification includes:
- Which agent exhausted retries
- The issue number and title
- The last stop reason
- The total number of retry attempts

This prevents missed alerts when the team primarily monitors one channel over the other.

**Prerequisites:**  
- Both Slack and Discord must be configured as notification targets.
- The agent must have exhausted its retry budget.

**Configuration:**  
No user-facing configuration. Dual-channel notification is automatic when both channels are configured.

**Source implementation:**  
`core/iterate.py` (retry-cap notification dispatch logic)

---

### 4.3 Intermediate Retry Notifications (Distinct from Cap Exhaustion)

**What it does:**  
Intermediate retry attempts (when an agent is retrying but hasn't yet exhausted its budget) now include a distinct notification indicating "retry attempt N of MAX_RETRIES". This is separate from the retry-cap exhaustion notification (see 4.2) and helps you track progress without waiting for the final failure.

**How you interact with it:**  
You'll see notifications like:
- "Validator retrying issue #100 (attempt 2 of 3)"
- "Developer retrying issue #100 (attempt 3 of 3)"
- "Agent exhausted retries for issue #100" (see 4.2)

This helps you distinguish between ongoing retries and final failures.

**Prerequisites:**  
- The agent must be configured to retry (most agents are).
- The agent must be in a retry loop (i.e., it stopped and is re-dispatching).

**Configuration:**  
No user-facing configuration. Intermediate retry notifications are automatic.

**Source implementation:**  
`core/iterate.py` (retry notification dispatch logic)

---

### 4.4 Dedup Guard on PM Retry-Cap Notifications

**What it does:**  
Prevents duplicate PM (project manager) retry-cap notifications. If a PM agent exhausts retries and the dispatcher sends a notification, subsequent retry-cap notifications for the same issue are suppressed until the PM agent is re-dispatched. This prevents notification spam when the PM agent repeatedly fails.

**How you interact with it:**  
You'll see one PM retry-cap notification per issue per dispatch cycle. If the PM agent is re-dispatched and exhausts retries again, you'll see another notification. No configuration needed — dedup is automatic.

**Prerequisites:**  
- The PM agent must be configured to retry.
- The PM agent must have exhausted retries.

**Configuration:**  
No user-facing configuration. Dedup is automatic.

**Source implementation:**  
`core/iterate.py` (PM retry-cap dedup logic)

---

### 4.5 Suppress Retry-Attempt Notification at Cap Boundary

**What it does:**  
Suppresses the intermediate retry-attempt notification (see 4.3) when the agent is at the retry-cap boundary (i.e., about to exhaust retries). This prevents redundant notifications — you'll see the retry-cap exhaustion notification (see 4.2) instead of both an intermediate and a cap-exhaustion notification.

**How you interact with it:**  
No redundant notifications. If an agent is on its final retry attempt and exhausts retries, you'll see only the retry-cap exhaustion notification, not an intermediate "retry attempt N of N" notification.

**Prerequisites:**  
- The agent must be at the retry-cap boundary.
- The agent must exhaust retries.

**Configuration:**  
No user-facing configuration. Suppression is automatic.

**Source implementation:**  
`core/iterate.py` (retry notification suppression logic)

---

### 4.6 Webhook Notification on Validator Retry Cap Exhausted

**What it does:**  
When a validator agent exhausts its retry budget, the dispatcher sends a webhook notification (if configured) in addition to the Slack/Discord notifications (see 4.2). This allows external systems (e.g., monitoring dashboards, CI pipelines) to react to validator failures.

**How you interact with it:**  
If a webhook URL is configured, you'll see a POST request to the webhook endpoint with the following payload:
```json
{
  "event": "validator_retry_cap_exhausted",
  "issue_number": 100,
  "issue_title": "Fix authentication bug",
  "stop_reason": "SECURITY_THREAT",
  "retry_attempts": 3
}
```

This allows external systems to react to validator failures (e.g., trigger a manual review, alert a security team).

**Prerequisites:**  
- A webhook URL must be configured in the dispatcher configuration.
- The validator agent must have exhausted retries.

**Configuration:**  
- `VALIDATOR_RETRY_CAP_WEBHOOK_URL` (environment variable): URL to POST the webhook payload.

**Source implementation:**  
`core/iterate.py` (validator retry-cap webhook dispatch logic)

---

### 4.7 Broadcast Thread Reply Support for Slack

**What it does:**  
Slack notifications now support broadcasting replies to the main channel (in addition to the thread). When the dispatcher posts a reply to an issue thread, it can optionally broadcast the reply to the main channel, ensuring visibility for critical updates (e.g., retry-cap exhaustion, security threats).

**How you interact with it:**  
Critical notifications appear both in the thread and in the main channel. You don't need to subscribe to threads to see important updates. For example:
- Thread reply: "Validator confirmed issue" (thread-only)
- Thread reply + broadcast: "Agent exhausted retries for issue #100" (appears in thread and main channel)

No configuration needed — broadcasting is automatic for critical notifications.

**Prerequisites:**  
- Slack must be configured as a notification target.

**Configuration:**  
No user-facing configuration. Broadcasting is automatic for critical notifications.

**Source implementation:**  
`core/iterate.py` (Slack broadcast logic), `core/thread_delivery.py:broadcast_thread_reply()`

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
Fewer API calls, faster dispatch ticks on large boards. If your board has 80 open issues, all 80 are fetched in a single call (instead of 4 calls with the old limit of 20).

**Prerequisites:**  
None. The new limit is applied automatically.

**Configuration:**  
- `_fetch_issues(limit=100)` (in `core/providers/base.py`): Default limit. Can be overridden per-call if needed.

**Source implementation:**  
`core/providers/base.py:_fetch_issues()` (line ~147)

---

### 5.3 issues_map Miss → Fallback get_issue() with Retry

**What it does:**  
When an issue is referenced in the kanban board but not found in the `issues_map` (the in-memory cache of all open issues), the dispatcher now falls back to a direct `get_issue()` call with retry logic. Previously, missing issues were silently ignored, causing cards to stall with no clear error.

**How you interact with it:**  
If an issue is missing from the cache (e.g., it was just created and hasn't been fetched yet), the dispatcher automatically retries the fetch. You'll see logs like:
```
INFO: Issue #100 not in issues_map, attempting direct fetch
INFO: Successfully fetched issue #100 via direct call
```

This prevents silent stalls when issues are created between dispatch ticks.

**Prerequisites:**  
None. The fallback is applied automatically.

**Configuration:**  
No user-facing configuration. Fallback logic is automatic.

**Source implementation:**  
`core/iterate.py` (issues_map miss fallback logic)

---

### 5.4 Retry on GitHub Secondary Rate Limit (403)

**What it does:**  
The dispatcher now retries GitHub API calls that fail with a 403 (secondary rate limit) using exponential backoff. Previously, 403 errors caused the dispatch tick to abort, leaving cards stuck.

**How you interact with it:**  
If GitHub returns a 403, the dispatcher waits and retries (up to 3 times with exponential backoff). You'll see logs like:
```
WARNING: GitHub secondary rate limit hit, retrying in 60s
INFO: Retry succeeded after 60s wait
```

This prevents dispatch failures due to temporary rate limits.

**Prerequisites:**  
None. Retry logic is applied automatically.

**Configuration:**  
- `MAX_RETRIES` (in `core/providers/github.py`): Maximum retry attempts. Default: 3.
- `BACKOFF_SECONDS` (in `core/providers/github.py`): Initial backoff duration. Default: 60 seconds.

**Source implementation:**  
`core/providers/github.py` (retry logic for 403 errors)

---

### 5.5 GitHub Projects Enrollment Node-ID Retry with Backoff

**What it does:**  
When enrolling an issue on a GitHub Projects board, the dispatcher now retries the `add_project_item()` call if it fails due to transient errors (e.g., network issues, API rate limits). Previously, enrollment failures were silently ignored, leaving issues off the board.

**How you interact with it:**  
If enrollment fails, the dispatcher retries (up to 3 times with exponential backoff). You'll see logs like:
```
WARNING: Failed to enroll issue #100 on project board, retrying in 5s
INFO: Successfully enrolled issue #100 after retry
```

This prevents silent enrollment failures.

**Prerequisites:**  
None. Retry logic is applied automatically.

**Configuration:**  
- `MAX_ENROLLMENT_RETRIES` (in `core/github_projects.py`): Maximum retry attempts. Default: 3.
- `ENROLLMENT_BACKOFF_SECONDS` (in `core/github_projects.py`): Initial backoff duration. Default: 5 seconds.

**Source implementation:**  
`core/github_projects.py:add_project_item()` (retry logic)

---

## 6. Dispatch & Pipeline

### 6.1 Dispatch History Persistence

**What it does:**  
The dispatcher now persists dispatch history to a JSONL file (`.hermes/dispatch_history.jsonl`), allowing you to audit which issues were dispatched, to which agents, and why. Each entry includes:
- Issue number and title
- Agent profile assigned
- Dispatch reason (e.g., "labeled Ready", "tier promotion")
- Timestamp

**How you interact with it:**  
You can query the dispatch history to understand past decisions:
```bash
# View the last 10 dispatches
cat .hermes/dispatch_history.jsonl | tail -10 | jq .

# Filter by agent profile
cat .hermes/dispatch_history.jsonl | jq 'select(.agent == "developer")'
```

This helps you debug unexpected dispatches or understand the pipeline's behavior over time.

**Prerequisites:**  
None. History persistence is automatic.

**Configuration:**  
- `DISPATCH_HISTORY_FILE` (in `core/dispatch_history.py`): Path to the JSONL file. Default: `.hermes/dispatch_history.jsonl`.

**Source implementation:**  
`core/dispatch_history.py:record_dispatch()`, `core/dispatch_history.py:get_history()`

---

### 6.2 Skip show_card in Orphan Repair for Valid Assignees

**What it does:**  
When repairing orphaned cards (cards with no assignee), the dispatcher now skips the `show_card()` diagnostic call if the assignee is a valid, known agent profile. Previously, `show_card()` was called for every orphan, adding unnecessary latency to the dispatch tick.

**How you interact with it:**  
Faster orphan repair, especially on boards with many orphaned cards. No manual intervention required — the optimization is applied automatically.

**Prerequisites:**  
None. Optimization is applied automatically.

**Configuration:**  
No user-facing configuration.

**Source implementation:**  
`core/iterate.py:_repair_orphans()` (optimization logic)

---

### 6.3 Agent Comment Header Enforcement

**What it does:**  
All agent comments (posted to GitHub issues) now include a standardized header identifying the agent and task. For example:
```
**Agent: validator**
**Task:** t_abc123

Validation complete. Issue confirmed.
```

This makes it easy to trace which agent posted which comment and for which task.

**How you interact with it:**  
All agent comments include the header automatically. No manual intervention required.

**Prerequisites:**  
None. Header enforcement is automatic.

**Configuration:**  
No user-facing configuration.

**Source implementation:**  
`core/iterate.py:_post_comment()` (header insertion logic)

---

### 6.4 --plugin-dir Flag for daedalus-cron.sh

**What it does:**  
The `daedalus-cron.sh` script now accepts a `--plugin-dir` flag to specify a custom plugin directory. This is useful for testing plugin changes without reinstalling or modifying the default plugin path.

**How you interact with it:**  
You can run the dispatcher with a custom plugin directory:
```bash
./daedalus-cron.sh --plugin-dir /path/to/custom/plugin
```

This allows you to test plugin changes in isolation.

**Prerequisites:**  
- The custom plugin directory must contain a valid Hermes plugin.

**Configuration:**  
- `--plugin-dir <path>`: Path to the custom plugin directory.

**Source implementation:**  
`scripts/daedalus-cron.sh` (flag parsing logic)

---

### 6.5 QA Gate for Auto-Merge

**What it does:**  
The pipeline now blocks auto-merge of a PR until the QA agent explicitly passes. The dispatcher checks for a QA card (idempotency key `qa-{issue_number}`) and inspects its `latest_summary` for the `qa-passed` signal (case-insensitive). If no QA card exists, or the QA card has not produced a `qa-passed` signal, the PR will not be auto-merged even if CI is green.

A `skip-qa` label on the PR bypasses this gate entirely — the label is a stronger signal than any QA state and allows immediate auto-merge. This is intended for documentation-only PRs, emergency hotfixes, or other cases where QA is not applicable.

**How you interact with it:**  
PRs wait for explicit QA approval before merging — no more merging PRs that passed CI but haven't been tested. For example:
- Developer opens PR #42 → QA agent runs tests → QA card summary becomes `qa-passed: PR #42` → auto-merge proceeds.
- Developer opens PR #43 → CI passes but QA card shows `qa-failed: tests broken` → PR waits until QA passes or developer fixes the issue.
- For docs-only changes → add the `skip-qa` label to the PR → auto-merge proceeds immediately without QA signal.

To check whether an issue has a QA pass signal:
```bash
# List QA cards for an issue
hermes kanban list | grep "qa-{issue_number}"
# Check the latest_summary field for 'qa-passed'
hermes kanban show <task-id> | grep latest_summary
```

**Prerequisites:**  
- The issue must have a QA card (created automatically by the dispatcher for validated issues).
- The QA agent must complete its work and signal `qa-passed` in the card summary.

**Configuration:**  
- `skip-qa` label: Add this label to any PR to bypass the QA gate. No other configuration needed.

**Source implementation:**  
`core/iterate.py:_qa_passed_for_issue()` (line ~490), `core/iterate.py:run_iterate()` (line ~2260, auto-merge gate), `core/iterate.py:classify_blocked()` (skip_qa bypass in QA card classification)

---

### 6.6 Dispatcher Concurrency (FileLock Mutex)

**What it does:**  
The dispatcher uses a process-level `FileLock` mutex to prevent concurrent dispatcher instances from running simultaneously on the same host. The lock file is located at `scripts/.daedalus_dispatch.lock` (relative to the daedalus plugin directory). When `main()` is called, it attempts to acquire the lock with `timeout=0` (non-blocking). If another dispatcher instance already holds the lock, the current instance logs a warning and exits cleanly (return code 0) — no duplicate task creation, no race conditions.

This is critical when the dispatcher is invoked from multiple sources: cron jobs, manual triggers, or overlapping scheduler ticks. The lock ensures exactly one dispatcher instance runs at a time, even if multiple invocations are triggered simultaneously.

**How you interact with it:**  
Multiple dispatcher invocations are safe — no risk of duplicate task creation or race conditions. For example:
- Cron tick fires at 10:00:00 → dispatcher starts, acquires lock, begins processing issues.
- Manual trigger at 10:00:05 → dispatcher attempts to acquire lock, finds it already held, logs warning and exits cleanly.
- No duplicate tasks are created, no issues are processed twice.

You can safely run `python scripts/daedalus_dispatch.py` manually even if the cron job is running — the FileLock prevents conflicts.

**Prerequisites:**  
None. The FileLock is enforced automatically on every dispatcher invocation.

**Configuration:**  
No user-facing configuration. The lock path is hardcoded to `.daedalus_dispatch.lock` in the scripts directory. The lock uses non-blocking acquisition (timeout=0) and is released automatically when the dispatcher exits (via `finally` block).

**Source implementation:**  
`scripts/daedalus_dispatch.py:_MUTEX_LOCK_PATH` (line ~67), `scripts/daedalus_dispatch.py:main()` (line ~5160, lock acquisition and release)

---

### 6.7 Status-Blind Re-Triage

**What it does:**  
Issues whose validator task completed with a non-CONFIRMED status (e.g., `blocked:`, `stop:`, or failed validation) can now be correctly re-queued for triage after retry. The status-blind principle from epic #1008 ensures that closed, cancelled, or archived downstream tasks no longer shadow active ones in downstream task checks.

Specifically, `_has_downstream_tasks()` filters out tasks in terminal states (`done`, `complete`, `completed`, `cancelled`, `canceled`, `archived`) before checking whether a team triage card exists. This means a stale completed validator card won't prevent a fresh triage dispatch after the issue is re-opened or re-queued.

Similarly, `_check_confirmed_validators()` now handles non-CONFIRMED validator done cards by creating PM consultation cards (for `blocked:` outcomes) or closing the issue (for `stop:` outcomes) instead of silently dropping them. This ensures the pipeline correctly routes failed validation attempts through the appropriate next step.

**How you interact with it:**  
Issues that failed validation can be re-dispatched without manual intervention — no shadowing from stale terminal tasks. For example:
- Issue #45 validator runs → validation fails → validator card completes with `stop: cannot reproduce`.
- Issue is re-opened → dispatcher re-runs validation → stale `stop:` card no longer blocks fresh triage → new validator task is created and runs.
- No manual cleanup of old cards needed — the status-blind guard handles it automatically.

**Prerequisites:**  
- The issue must have been processed by the validator and completed with a non-CONFIRMED status (e.g., `blocked:`, `stop:`, or failed validation).
- The issue must be re-opened or re-queued for dispatch.

**Configuration:**  
No user-facing configuration. Status-blind filtering is enforced automatically in all downstream task checks.

**Source implementation:**  
`scripts/daedalus_dispatch.py:_has_downstream_tasks()` (line ~1950, status-blind filtering), `scripts/daedalus_dispatch.py:_check_confirmed_validators()` (line ~2702, non-CONFIRMED validator handling)

---

### 6.8 Dispatcher CLI Flags (--dry-run, --self-test, --history)

**What it does:**  
The dispatcher script (`scripts/daedalus_dispatch.py`) exposes three read-only or no-op CLI flags for operators:

- **`--dry-run`** — Executes the full dispatch pipeline logic but logs every intended mutation (`create follow-up issue`, `send notification`, `merge PR`, etc.) without writing to GitHub or Slack. Uses `[dry-run]` prefixes in log output.
- **`--self-test`** — Seeding a real but GitHub-free pipeline smoke test using `core.dispatch_selftest`. Creates fake issues and tasks, runs a real dispatch tick, asserts expected state transitions, then prints PASS/FAIL. Does nothing against real GitHub. Intended for CI gating.
- **`--history [N]`** — Print the last *N* dispatch-history entries (default 10) to stdout, then exit. Read-only; does not mutate anything.

**How you interact with it:**  
```bash
# See what the dispatcher would do this tick, without touching GitHub
python scripts/daedalus_dispatch.py --dry-run

# Run the offline smoke-test (no GitHub access required)
python scripts/daedalus_dispatch.py --self-test

# View the last 25 dispatch decisions
python scripts/daedalus_dispatch.py --history 25
```

`--dry-run` is most useful when diagnosing unexpected dispatch behavior or previewing changes before a real run. `--self-test` is intended for CI pipelines — it runs fast, needs no credentials, and exits non-zero on any failed assertion. `--history` lets you audit recent dispatches without starting a full tick.

**Prerequisites:**  
- `--dry-run` and `--history` require valid cron/environment setup (they use the same code path as a real tick, just with mutations gated out).  
- `--self-test` requires no external credentials or GitHub access.

**Configuration:**  
No configuration keys — these are pure CLI flags, passed on the command line.

**Source implementation:**  
`scripts/daedalus_dispatch.py:main()` (line ~5200, argparse definitions), `core/dispatch_selftest.py` (self-test logic)

---

### 6.9 Dev Mode Redirect (Local Dev Checkout)

**What it does:**  
The dispatcher can redirect itself from the installed plugin to a local development checkout, so edits to `scripts/daedalus_dispatch.py` take effect immediately without running `hermes plugins update daedalus`. When `dev_mode.enabled: true` and `dev_mode.path` points at a valid checkout containing `scripts/daedalus_dispatch.py`, the dispatcher re-execs itself via `os.execve` — replacing the current process image so the FileLock is not double-held. The `DAEDALUS_DEV` environment variable is set to `"1"` automatically to prevent infinite re-exec loops.

**Guard chain (all must pass before re-exec):**
1. Skip if `DAEDALUS_DEV` env var is already set (infinite-loop guard)
2. Skip if `dev_mode` config is not a dict (bad config / missing key)
3. Skip if `dev_mode.enabled` is falsy
4. Skip if `dev_mode.path` is absent or empty
5. Warn + skip if `<path>/scripts/daedalus_dispatch.py` does not exist
6. Skip if `abspath(dev_script) == abspath(__file__)` (already running from dev)
7. Set `DAEDALUS_DEV=1`, prepend `path` to `PYTHONPATH`, call `os.execve`

On any skip condition or unexpected error, the function returns `None` and the caller continues with the installed-plugin code path (fail safe — never crashes the dispatcher).

**How you interact with it:**  
Add a `dev_mode` block to your project's `.hermes/daedalus.yaml`:
```yaml
dev_mode:
  enabled: true
  path: /path/to/local/daedalus/checkout
```

Set `enabled: false` (or remove the block) to restore normal installed-plugin behaviour.

**Prerequisites:**  
- The path must point to a valid Daedalus checkout containing `scripts/daedalus_dispatch.py`.
- `dev_mode.path` supports `~` expansion via `os.path.expanduser`.

**Configuration:**  
- `dev_mode.enabled` (bool): Toggle the redirect. Default: `false` (block is commented out in template).
- `dev_mode.path` (string): Absolute or `~`-relative path to the local checkout.

**Source implementation:**  
`scripts/daedalus_dispatch.py` — `_DEV_MODE_ENV` constant (line ~79), `_maybe_redirect_dev_mode()` function (line ~6520), called at 2 sites in `_main_inner()` after `resolve_repo_config()`.

**Tests:**  
`tests/test_dev_mode_redirect.py` — 11 tests covering the full guard chain, edge cases (non-dict config, permission errors, execve failure), and an integration test verifying the end-to-end redirect chain.

---

## Summary

This guide documents **39 new user-facing behaviors** across **6 feature areas**:
- **Epic & Sub-issue Management (7 behaviors):** Automatic epic detection, decomposition, and context injection
- **Dependency-Aware Dispatch (5 behaviors):** Ready-gating, tier promotion, and idempotency
- **Self-Healing & Auto-Advance (7 behaviors):** Automatic diagnosis and routing of blocked cards
- **Notification & Alerting (8 behaviors):** Threading, retry-cap alerts, and webhook integration
- **Reliability & Infrastructure (5 behaviors):** Auto-pagination, retry logic, and rate-limit handling
- **Dispatch & Pipeline (9 behaviors):** History persistence, performance optimizations, comment enforcement, QA gate, FileLock mutex, status-blind re-triage, dispatcher CLI flags, and dev-mode redirect

All behaviors are verified against the source code implementation and are active in the current release (v1.0.0-beta.30).

For more details on specific features, see:
- [Notification Threading](./notification-threading.md)
- [Installation Guide](./INSTALLATION_GUIDE.md)
- [CHANGELOG](../CHANGELOG.md)

---

**Document version:** 1.0  
**Last verified:** 2026-06-29  
**Source catalog:** `.hermes/release_behavior_catalog.md`

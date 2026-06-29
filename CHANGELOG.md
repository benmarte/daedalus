## [bug: kanban pipeline cards orphaned when issue reaches Done on board before pipeline completes](https://github.com/benmarte/daedalus/issues/957) — [PR #984](https://github.com/benmarte/daedalus/pull/984)

## [bug: 'pass' substring in approve_signals false-triggers APPROVE_ADVANCE on reviewer/security cards](https://github.com/benmarte/daedalus/issues/956) — [PR #983](https://github.com/benmarte/daedalus/pull/983)

## [bug: 'pass' substring in approve_signals false-triggers APPROVE_ADVANCE on reviewer/security cards](https://github.com/benmarte/daedalus/issues/956) — [PR #983](https://github.com/benmarte/daedalus/pull/983)

## [bug: advance hook does not fire after planner-daedalus session ends, causing 60-minute pipeline stall](https://github.com/benmarte/daedalus/issues/962) — [PR #981](https://github.com/benmarte/daedalus/pull/981)

## [bug: advance hook does not fire after planner-daedalus session ends, causing 60-minute pipeline stall](https://github.com/benmarte/daedalus/issues/962) — [PR #981](https://github.com/benmarte/daedalus/pull/981)

## [perf: dispatcher processes validator retry logic per-task instead of per-issue, burning O(N) API calls](https://github.com/benmarte/daedalus/issues/961) — [PR #980](https://github.com/benmarte/daedalus/pull/980)

## [bug: planner NOT SUITABLE FOR DECOMPOSITION leaves issue stuck In progress forever (no validator created)](https://github.com/benmarte/daedalus/issues/969) — [PR #976](https://github.com/benmarte/daedalus/pull/976)

## [bug: planner NOT SUITABLE FOR DECOMPOSITION leaves issue stuck In progress forever (no validator created)](https://github.com/benmarte/daedalus/issues/969) — [PR #976](https://github.com/benmarte/daedalus/pull/976)

### Bug

The `_check_planner_not_suitable()` handler introduced in PRs #941 / #943 for issue #931 only scanned planner cards with `status="done"`. Combined with two other failure modes — (1) the planner soul never instructed the planner *when* to emit the `NOT SUITABLE FOR DECOMPOSITION` signal, and (2) the planner could block its card with the signal rather than complete it (making the card invisible to the handler) — issues that the planner deemed unsuitable for decomposition were left `In Progress` on the board indefinitely with no downstream validator task, no comment, and no diagnostic log message explaining why.

### Fix

The planner soul (`config/souls/planner-daedalus.md`) now documents the `NOT SUITABLE FOR DECOMPOSITION` signal as a valid **completion** summary (Path C in the dispatcher signal reference) and explicitly warns that emitting it as a block reason will route to `PM_ROUTE`. The handler is extended to scan **both `done` and `blocked` planner cards** (defense in depth) so a blocked card with the signal no longer falls through silently. Diagnostic logging is added at every skip point — empty summary, non-matching pattern, missing issue, missing issue number — so future silent failures surface in the dispatch log. The `planner-fallback-validator-{n}` idempotency key prevents duplicate validator creation when the same issue appears in both done and blocked cards.

### Tests

- 14 existing tests in `tests/test_planner_not_suitable.py` + new tests covering blocked-card detection, idempotency between done/blocked, and soul contract verification.
- Integration test added in `tests/test_planner_signal_integration.py` (`test_blocked_planner_with_not_suitable_triggers_handler`) verifying a blocked planner card with the signal still creates a validator task.

### Affected files

- `config/souls/planner-daedalus.md` — added Path C documentation + canonical-form guidance + what-breaks-self-healing note
- `scripts/daedalus_dispatch.py` — `_check_planner_not_suitable()` now iterates done+blocked, adds `processed_ids` guard, emits diagnostic logs
- `tests/test_planner_not_suitable.py` — AC-3 / AC-4 tests
- `tests/test_planner_signal_integration.py` — blocked-card integration test

---

## [bug: QA races developer mid-edit on shared working tree, sees uncommitted changes](https://github.com/benmarte/daedalus/issues/953) — [PR #954](https://github.com/benmarte/daedalus/pull/954)

## [bug: validator completes with summary=None when Claude Code delegation fails, silently burning retry cap](https://github.com/benmarte/daedalus/issues/916) — [PR #952](https://github.com/benmarte/daedalus/pull/952)

## [feat: add advance hook (daedalus-advance.sh + daedalus_resolve_project.py) to postinstall so it ships with the plugin](https://github.com/benmarte/daedalus/issues/936) — [PR #950](https://github.com/benmarte/daedalus/pull/950)

## [No regression in manual issue → Ready flow](https://github.com/benmarte/daedalus/issues/930) — [PR #948](https://github.com/benmarte/daedalus/pull/948)

## [After planner decomposition, all sub-issues appear on the project board](https://github.com/benmarte/daedalus/issues/919) — [PR #947](https://github.com/benmarte/daedalus/pull/947)

## [After planner decomposition, all sub-issues appear on the project board](https://github.com/benmarte/daedalus/issues/919) — [PR #947](https://github.com/benmarte/daedalus/pull/947)

## [Integration test: issue routed to planner → planner returns NOT SUITABLE → validator created automatically](https://github.com/benmarte/daedalus/issues/935) — [PR #946](https://github.com/benmarte/daedalus/pull/946)

## [Integration test: issue routed to planner → planner returns NOT SUITABLE → validator created automatically](https://github.com/benmarte/daedalus/issues/935) — [PR #946](https://github.com/benmarte/daedalus/pull/946)

## [When a planner task completes with `NOT SUITABLE FOR DECOMPOSITION` in summary, a validator task is created for the parent issue](https://github.com/benmarte/daedalus/issues/931) — [PR #941](https://github.com/benmarte/daedalus/pull/941)

## [fix: Add dispatcher handler for planner NOT SUITABLE FOR DECOMPOSITION signal](https://github.com/benmarte/daedalus/issues/931) — [PR #941](https://github.com/benmarte/daedalus/pull/941)

## [feat: auto-advance sub-issues to Ready after planner decomposition, respecting dependency order](https://github.com/benmarte/daedalus/issues/915) — [PR #937](https://github.com/benmarte/daedalus/pull/937)

## [feat(e2e): regression assertions for #891 (no duplicate sub-issues) and #894 (agent comments posted)](https://github.com/benmarte/daedalus/issues/902) — [PR #917](https://github.com/benmarte/daedalus/pull/917)

## [feat(e2e): dry-run mode flag for dispatcher — seed test issues/tasks without touching real GitHub](https://github.com/benmarte/daedalus/issues/900) — [PR #914](https://github.com/benmarte/daedalus/pull/914)

## [feat(e2e): multi-tick pipeline harness — run N dispatcher ticks and assert stage progression](https://github.com/benmarte/daedalus/issues/901) — [PR #913](https://github.com/benmarte/daedalus/pull/913)

## [bug: planner_decompose injects source-file context into sub-issue bodies, causing 422 body-too-long from GitHub](https://github.com/benmarte/daedalus/issues/899) — [PR #912](https://github.com/benmarte/daedalus/pull/912)

## [feat(e2e): make e2e target + CI nightly schedule](https://github.com/benmarte/daedalus/issues/903) — [PR #910](https://github.com/benmarte/daedalus/pull/910)

## [bug: agents silently fail to post issue comments when GITHUB_TOKEN not set in cron env](https://github.com/benmarte/daedalus/issues/894) — [PR #895](https://github.com/benmarte/daedalus/pull/895)

## [bug: concurrent dispatcher ticks re-decompose epics when idempotency marker format changed](https://github.com/benmarte/daedalus/issues/891) — [PR #893](https://github.com/benmarte/daedalus/pull/893)

## [docs: update SOUL.md profiles and user guide with self-healing pipeline behavior](https://github.com/benmarte/daedalus/issues/180) — [PR #866](https://github.com/benmarte/daedalus/pull/866)

## [docs: update SOUL.md profiles and user guide with self-healing pipeline behavior](https://github.com/benmarte/daedalus/issues/180) — [PR #866](https://github.com/benmarte/daedalus/pull/866)

## [feat: auto-paginate _fetch_issues to prevent silent truncation on large boards](https://github.com/benmarte/daedalus/issues/228) — [PR #846](https://github.com/benmarte/daedalus/pull/846)

## [feat: auto-paginate _fetch_issues to prevent silent truncation on large boards](https://github.com/benmarte/daedalus/issues/228) — [PR #846](https://github.com/benmarte/daedalus/pull/846)

## [Unit tests / integration tests: planner creates sub-issues with file-specific acceptance criteria when source is available; graceful fallback when source is not](https://github.com/benmarte/daedalus/issues/806) — [PR #871](https://github.com/benmarte/daedalus/pull/871)

## [Unit tests / integration tests: planner creates sub-issues with file-specific acceptance criteria when source is available; graceful fallback when source is not](https://github.com/benmarte/daedalus/issues/806) — [PR #871](https://github.com/benmarte/daedalus/pull/871)

## [docs: clarify epic #180 line references and document deferred behaviors](https://github.com/benmarte/daedalus/issues/180) — PR #866

### Documentation

- README "Self-healing behaviors (epic #180)" line references corrected to
  current `core/iterate.py` (drifted 13–27 lines since PR #816 landed).
- Added explicit note that three epic #180 behaviors are deferred
  (`MAX_PENDING_PR_TICKS` timeout, `PENDING_CI` fix-attempt escalation for
  QA/a11y, and empty-summary developer skip) and what the dispatcher actually
  does today in each case.
- Added `a11y-skipped:` to the canonical accessibility signals list.
- "What breaks self-healing" paragraph for QA/a11y corrected to say these
  signals stay pending indefinitely (no sweep escalation for non-canonical
  signals yet).

## [PM SOUL.md explains the importance of unblocking the original card after consultation](https://github.com/benmarte/daedalus/issues/785) — [PR #812](https://github.com/benmarte/daedalus/pull/812)

## [feat: end-to-end integration test driving a full 7-stage pipeline scenario](https://github.com/benmarte/daedalus/issues/230) — [PR #445](https://github.com/benmarte/daedalus/pull/445)

## [fix: tier promotion guard — max one promotion per parent epic per dispatcher tick](https://github.com/benmarte/daedalus/issues/231) — [PR #349](https://github.com/benmarte/daedalus/pull/349)

## [feat: extend sweeper to detect stale 'running' cards stuck without updates >24h](https://github.com/benmarte/daedalus/issues/232) — [PR #259](https://github.com/benmarte/daedalus/pull/259)

## [feat: --plugin-dir flag for daedalus-cron.sh to load local dev plugin without reinstalling](https://github.com/benmarte/daedalus/issues/233) — [PR #249](https://github.com/benmarte/daedalus/pull/249)

## [docs: update README, user guide, and all feature documentation for current release](https://github.com/benmarte/daedalus/issues/234) — [PR #258](https://github.com/benmarte/daedalus/pull/258)

# Changelog

All notable changes to Daedalus are documented here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
`1.0.0-beta.N` pre-release versioning.

## [Unreleased]

### Added

- **Epic sub-issue creation (Phase 3)** (#151, PR #179) — when the planner agent
  completes its kanban card with `PLANNING COMPLETE:`, the dispatcher automatically
  decomposes the parent epic into sub-issues, posts an idempotency marker
  (`<!-- daedalus:sub-issues:[N1,N2,...] -->`) on the parent, applies the `epic`
  label, and creates triage cards so each sub-issue enters the validator pipeline
  on the next tick. Two decomposition strategies: Case A (checklist items in the
  epic body → one sub-issue per item, capped at 10) and Case B (no checklist →
  three fixed default sub-issues: Research & Scoping, Implementation, Testing &
  Documentation). Sub-issues inherit parent labels (minus `epic`) and add
  `subtask`. `VCSProvider.add_label()` added to base with GitHub implementation
  via the Issues API; no-op on GitLab/Azure DevOps. 18 new tests in
  `tests/test_subissue_creation.py`.

- **Dependency-aware ready-gating** (#139, PR #148) — the dispatch sweep now refuses
  to start a `Ready` issue while any of its blockers are still open, re-checking
  every tick so a dependent auto-unblocks once its blockers' PRs merge. Blockers
  are resolved per-provider (GitHub native dependencies via `blocked_by`, GitLab
  `is_blocked_by` issue links, Azure DevOps `Predecessor` work-item links) all
  merged with a portable `Depends on: #N` body fallback that works on any
  provider. Dispatch summaries gain a new **⛓ Waiting on Dependencies** section
  that surfaces *why* an issue is being held back.

- **Epic-issue detection (Phase 1)** (#138, PR #155) — new `is_epic()` helper in
  `core.providers.base` flags issues as "epic-sized" using three disjunct
  heuristics (≥4 markdown checklist items, `epic` label case-insensitive, or
  body ≥2000 chars). Accepts provider dicts and `IssueSummary` objects; never
  raises. 34 tests in `tests/test_epic_detection.py` cover all three heuristics,
  boundary values, mixed input shapes, and the OR-combination contract. This is
  detection only — dispatcher wiring ships in Phase 2.

### Changed

- **Tier promotion applies Ready board status** (#208, PR #227) — when a sub-issue's
  tier level drops to 0 (all blockers closed), the promotion pass now applies both
  the `Ready` label *and* sets the issue's board status to `Ready`, triggering the
  normal dispatch flow (validator → PM → dev team) on the next tick. Previously only
  the label was applied, requiring manual board-status updates.

- **Planner reads source files for sub-issue context** (#138, PR #242) — when
  decomposing epics into sub-issues, the planner now reads relevant source files
  from the codebase and injects their content into sub-issue bodies to provide
  implementation context. Up to 10 files per sub-issue, 50KB per file, with binary
  detection and path-traversal prevention.

### Fixed

- **Promotion idempotency: implement `has_label` to prevent duplicate labels/comments**
  (#220, PR #244) — `VCSProvider.has_label()` was a no-op returning `False`; the
  GitHub provider now implements it by inspecting the `labels` field from
  `get_issue()`. Tier promotion is now idempotent: already-Ready issues are excluded
  from re-promotion, preventing duplicate `add_label` and `post_issue_comment` calls
  on every dispatcher tick. `sub_issues_of` base-class regex widened to match all
  `EPIC_REF_RE` formats (`Epic #N`, `Epic: #N`, `Part of: #N`, `Part of #N`,
  `Part of epic: #N`, `Part of epic #N`, hyphenated variants). Case-insensitive
  throughout. 16 new regression tests in `tests/test_bugfixes_regex_and_haslabel.py`.

- **GitHub Projects enrollment node-ID retry with backoff** (#236, PR #245) — when
  the GitHub Projects GraphQL API returns a transient failure resolving the node ID
  for an issue, the dispatcher now retries with exponential backoff instead of
  failing the enrollment outright.

- **Separate blocked/stop handlers** (#2075, PR #222) — the dispatcher previously
  handled `blocked:` and `stop:` signals through the same code path. They are now
  separate: `blocked:` cards stay in the pipeline (awaiting human intervention),
  while `stop:` cards trigger the dedicated auto-close path that archives the card
  and closes the issue.

- **Validator retry-cap exhaustion notification deduplication** (#183, PR #226) —
  when a validator exhausts its retry cap, the dispatcher now posts a single
  `validator-retry-cap-exhausted` notification instead of posting once per
  subsequent tick.

- **PM/validator retry-cap → Slack/Discord dual-channel notification** (#378, epics
  #181, PRs #813, #818, #820, #823, #842, #849, #850) — when the PM or validator
  retry cap is exhausted, the dispatcher now fires a `retry-cap-exhausted` alert on
  two independent channels: (a) structured `NotificationPayload` to Slack/Discord
  webhooks via `core/notification_sender.py` (severity=critical,
  non-blocking daemon thread, 10s HTTP timeout), and (b) message-bubble to every
  `hermes send` target subscribed to the `retry-cap-exhausted` event. An
  idempotency marker (`<!-- daedalus:retry-cap-notified -->`) on the issue
  guarantees only one notification per cap-exhaustion. A GitHub comment with role,
  retry-count, error classification, and recovery instructions is also posted.
  Configured via `SLACK_WEBHOOK_URL` / `DISCORD_WEBHOOK_URL` environment variables.
  Notification failures are logged but never block the dispatcher.

- **`_fetch_issues` default limit raised to 100** (#203, PR #224) — the default
  limit for `_fetch_issues()` was 20, which silently truncated boards with >20 open
  issues. Default raised to 100 so larger boards are not silently skipped.

- **`issues_map` miss → fallback `get_issue()` with retry** (#185, PR #221) — when
  the dispatcher's `issues_map` cache misses an issue number, it now falls back to
  a direct `get_issue()` call with retry on transient failure, instead of failing
  outright.

## [1.0.0-beta.30] — 2026-06-26

### ⚠️ Notable behavior change — automatic comment-mirror threading

Daedalus now mirrors every agent comment into a **persistent per-issue thread**
on each notification target (see Added → Notification threading). This is
delivered as a new `comment-mirror` event, and **any catch-all
`cron.notifications` entry** — one with no `events` filter or `events: []` —
**receives it automatically, with no opt-in.**

If you have existing Slack/Discord targets without an `events` filter, they will
start receiving threaded comment mirrors after upgrading. On an active board this
can be a significant increase in message volume. **To exclude it,** list the
events you *do* want on that entry and leave `comment-mirror` out:

```yaml
cron:
  notifications:
    - platform: "Slack"
      target: "slack:C0CHANNEL1"
      events: ["dispatch-summary", "pipeline-failure"]  # no comment-mirror
```

### Added

- **Notification threading** (#126, closes #121) — every daedalus-managed issue
  gets one persistent thread per configured `cron.notifications` target. Agent
  comments on the issue and its linked PR (spec posts, progress, review feedback)
  are mirrored into that thread as replies; PR-open and merge events post replies
  too. Threading is **platform-agnostic**: Slack anchors on `thread_ts`, Discord
  on `message_id`, both captured via `hermes send --json`. No new config keys are
  required — it works automatically with existing notification targets.
  - `daedalus_dispatch_state.json` gains a `threads` key per issue:
    `{"<issue_number>": {"threads": {"<target>": "<anchor_id>"},
    "thread_events": {"<target>": ["<event_key>"]}}}`.
  - Cross-tick duplicate suppression — the same event is never posted twice to a
    target.
  - Self-healing anchor — if a thread's root message is deleted, the next event
    posts a fresh root and updates the stored anchor.
  - See [docs/notification-threading.md](docs/notification-threading.md).

### Changed

- **Agent comment header enforcement** (#122, closes #120) — all soul files now
  route comments through `scripts/agent_comment.py`, which enforces the mandatory
  `**Agent: <name>**` header as the first line of every comment. Previously souls
  could omit it; it is now structural. Tooling that parses or filters daedalus
  comments by author can rely on this header always being present.

### Fixed

PR #244 — implement `provider.has_label()` so tier promotion no longer re-labels and re-comments already-Ready issues on every dispatch tick. `sub_issues_of` regex in `core/providers/base.py` widened to accept the same format variants as `EPIC_REF_RE` (with/without colon, hyphens, `part of epic #N`). 16 regression tests added.

- **Standalone test runner** (#124, closes #123) — internal fix to the standalone
  `__main__` test runner. No user-facing impact.

### Caveats / follow-ups (from PR #126 review)

- **Silent opt-in** — catch-all notification entries receive `comment-mirror`
  with no explicit opt-in (documented above). A future opt-out config key is
  under consideration.
- **Per-tick API cost** — `_mirror_issue_threads` fetches each open issue's issue
  and PR comments every tick before dedup. Fine for small boards; may be
  noticeable on large boards with many open issues.
- **Root-failure edge** — if the initial root post fails but a later reply
  succeeds, that reply becomes the thread anchor. Self-heals; rare; not a blocker.

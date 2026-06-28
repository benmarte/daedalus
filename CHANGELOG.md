## [fix: remaining self-healing gaps — retry cap notifications, gateway watchdog, and orphaned cards](https://github.com/benmarte/daedalus/issues/181) — [PR #204](https://github.com/benmarte/daedalus/pull/204)

## [fix: remaining self-healing gaps — retry cap notifications, gateway watchdog, and orphaned cards](https://github.com/benmarte/daedalus/issues/181) — [PR #204](https://github.com/benmarte/daedalus/pull/204)

## [Detect epic-sized issues and analyze+decompose into sub-issues before the developer stage](https://github.com/benmarte/daedalus/issues/138) — [PR #155](https://github.com/benmarte/daedalus/pull/155)

## [Dependency-aware ready-gating: don't dispatch an issue until its blockers are closed (native GitLab/GitHub issue links + Depends-on fallback)](https://github.com/benmarte/daedalus/issues/139) — [PR #148](https://github.com/benmarte/daedalus/pull/148)

## [Phase 1: Epic detection heuristic](https://github.com/benmarte/daedalus/issues/149) — [PR #156](https://github.com/benmarte/daedalus/pull/156)

## [Auto-configure VCS board settings in daedalus.yaml during setup (GitLab: label_board + status labels + default branch)](https://github.com/benmarte/daedalus/issues/133) — [PR #135](https://github.com/benmarte/daedalus/pull/135)

## [bug: _reconcile_cron skips schedule conversion, creating one-shot crons instead of repeating jobs](https://github.com/benmarte/daedalus/issues/134) — [PR #136](https://github.com/benmarte/daedalus/pull/136)

## [docs: update README and user guide for beta.30 release](https://github.com/benmarte/daedalus/issues/127) — [PR #128](https://github.com/benmarte/daedalus/pull/128)

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

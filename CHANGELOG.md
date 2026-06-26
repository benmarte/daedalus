# Changelog

All notable changes to Daedalus are documented here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
`1.0.0-beta.N` pre-release versioning.

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

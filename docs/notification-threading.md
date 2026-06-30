# Notification threading

> Since **beta.30** (PR #126, issue #121).

Daedalus mirrors the agent conversation for each managed issue into **one
persistent thread per notification target**, so the full pipeline exchange —
spec, plan, progress, review feedback, PR-open and merge — is readable in your
chat platform without opening GitHub.

Threading is **platform-agnostic**. The mechanics differ per platform, but the
behavior is the same everywhere Hermes can send: Slack, Discord, Telegram,
Signal, WhatsApp, etc.

---

## What gets mirrored

For every open, daedalus-managed issue, on each target that receives the
`comment-mirror` event:

1. A **root** message (the thread anchor) on the first event.
2. One **reply** per agent comment on the issue and on its linked PR. Only
   agent-authored comments (those carrying the `**Agent: <name>**` header) are
   mirrored; the dispatcher's own bookkeeping comments are skipped.
3. A **reply** when the PR is opened and when it is merged. The final reply is
   posted before thread state is cleared on merge.

## Configuration

There is **nothing to configure** beyond your existing notification targets.
Threading attaches to `cron.notifications` entries (and the legacy single
`cron.deliver` target) automatically.

```yaml
cron:
  notifications:
    - platform: "Slack"
      target: "slack:C0CHANNEL1"
      # no events filter → catch-all → receives comment-mirror automatically
    - platform: "Discord"
      target: "discord:1234567890123456789"
      events: ["dispatch-summary", "pr-ready", "comment-mirror"]  # explicit opt-in
```

### ⚠️ Automatic opt-in for catch-all targets

The `comment-mirror` event is delivered to any **catch-all** entry — one with no
`events` filter, or `events: []`. Existing targets without an `events` filter
will start receiving threaded comment mirrors after upgrading to beta.30, with no
explicit opt-in. On active boards this can be a meaningful increase in message
volume.

**To exclude threading from a target,** list the events you *do* want and leave
`comment-mirror` out:

```yaml
    - platform: "Slack"
      target: "slack:C0CHANNEL1"
      events: ["dispatch-summary", "pipeline-failure"]  # no comment-mirror → no thread
```

## Dispatch state schema

Thread bookkeeping lives in `daedalus_dispatch_state.json`. Each issue entry
gains a `threads` and a `thread_events` map:

```json
{
  "127": {
    "threads": {
      "slack:C0CHANNEL1": "1718900000.001200",
      "discord:1234567890123456789": "1180000000000000000"
    },
    "thread_events": {
      "slack:C0CHANNEL1": ["root", "comment:issue:456", "comment:pr:789", "pr-opened:99"]
    }
  }
}
```

- `threads` — the stored **anchor** per target. This is the opaque thread id the
  platform returned for the root message (Slack `thread_ts`, Discord
  `message_id`). Subsequent events reply under this anchor.
- `thread_events` — the set of `event_key`s already mirrored to each target. Used
  for cross-tick duplicate suppression: an event recorded here is never resent.

Re-dispatching an issue preserves `threads`/`thread_events`, so a re-run never
wipes the existing thread anchors.

## How anchors work

`hermes send --json` returns the posted message's anchor. Daedalus captures it
on the **root** post and stores it under `threads[target]`. Every later event for
that issue is sent as a reply via the target form
`platform:chat_id:thread_id`.

- **First event** → post a root (no thread id) → store the returned anchor.
- **Later events** → post a reply using the stored anchor.
- **Deleted parent** → if a reply fails (e.g. the root message was deleted), the
  next event posts a fresh root and updates the stored anchor. The thread
  self-heals.

## Caveats

- **Per-tick API cost.** On each tick, every open issue's issue and PR comments
  are fetched before dedup decides what to mirror. This is fine for small boards;
  on large boards with many open issues it adds VCS API calls per tick.
- **Root-failure edge.** If the initial root post fails but a later reply
  succeeds, that reply becomes the thread anchor. This self-heals and is rare.
- **Slack thread broadcast (default: true).** Each notification entry may set
  `thread_broadcast: false` to disable Slack's `reply_broadcast` flag on
  mirrored replies — thread replies stay thread-only instead of also appearing
  in the channel feed. Defaults to `true`. Discord does not have an equivalent;
  the key has no effect on non-Slack targets.

```yaml
notifications:
  - platform: "Slack"
    target: "slack:C0CHANNEL1"
    thread_broadcast: false   # only in-thread, no channel feed noise
```


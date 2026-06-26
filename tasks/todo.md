# Issue #121 — Deliver agent issue/PR comments to platform threads

Branch: `fix/issue-121-slack-threads` (off `dev`).

## Goal
Mirror every daedalus agent comment (issue + linked PR) into a per-issue thread
on each configured `cron.notifications` platform. Root anchor on dispatch;
replies for comments / PR-open / merge; graceful fallback when the anchor is
gone; dedup across ticks; works for every platform (Slack `thread_ts`, Discord
`message_id`) with no new config keys.

## Threading contract (verified via `hermes send --help` + source)
- `hermes send -t platform:chat_id:thread_id --file f --json` posts into a thread.
- `--json` returns `{"success": true, "message_id": "<ts-or-id>"}` — `message_id`
  is the thread anchor (Slack ts / Discord message id).

## Tasks
- [ ] `core/dispatch_state.py`: per-issue `threads` (target→anchor) + `thread_events`
      (target→[event]) accessors; preserve sub-keys in `record_dispatch`.
- [ ] `core/thread_delivery.py` (new, pure/testable): `deliver_event()` send +
      dedup + anchor fallback; `select_comments()` agent-comment picker.
- [ ] `core/notify_templates.py`: `render_thread_root` / `_comment` / `_pr_event`.
- [ ] `scripts/daedalus_dispatch.py`: `_hermes_send()` (threaded, returns anchor),
      `_send_via_hermes` delegates; `_mirror_issue_threads()` wired into run loop;
      add `comment-mirror` to `NOTIFY_EVENTS`; summary `threads_mirrored`.
- [ ] Tests: `tests/test_thread_delivery.py`, `tests/test_dispatch_state.py`.
- [ ] Lint, run full suite, review, simplify, PR, comment, block card, dispatcher.

## Notes
- `threads` keyed by full target string (`slack:C123`) not just platform — a
  Slack ts is channel-specific, so multi-channel correctness requires it.
- Dedup is per (target, event_key); event marked only after a successful send.
- Merged path: post the final reply BEFORE `clear_dispatch` wipes thread state.

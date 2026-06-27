# Issue #137 — thread/dedupe dispatch summaries + scope crons to --repo

## Spec / Acceptance
- AC1: in-progress issue, no state change → no new Slack messages on later ticks.
- AC2: identical consecutive summaries are threaded/deduped, not top-level.
- AC3: a cron/hook/webhook tick processes only the relevant project.

## Plan (DONE — 823 tests pass, 8 new)
### Problem 1 — thread + dedupe + suppress project dispatch summaries
- [x] `scripts/daedalus_dispatch.py`: route `_notify_project_summary` through
      `thread_delivery.deliver_event` with a per-project anchor
      (`_PROJECT_SUMMARY_ANCHOR = 0`) + content-hash `event_key`.
      Silent ticks already return "" (suppression). Falls back to plain send when
      no workdir. `import hashlib`.

### Problem 2 — scope crons/hooks/webhook to one project
- [x] `scripts/daedalus_dispatch.py`: `_resolve_repo_arg` (path or owner/repo
      slug → repo path) + `_resolve_repo_from_cwd`; `main()` scopes to --repo,
      else cwd-matched project, else legacy registry sweep.
- [x] `__init__.py`: `_on_session_end` passes `--repo` (cwd→registry match via
      `_resolve_project_for_task`). `_ensure_dispatch_crons` adds `--workdir`.
- [x] `dashboard/plugin_api.py`: `_reconcile_cron` adds `--workdir` (create+edit).
- [x] `scripts/daedalus-ready.sh`: resolve project from payload repo, pass `--repo`.

### Tests
- [x] threaded/deduped summary (sent once, reply on change, skip on repeat) + silent tick.
- [x] `_resolve_repo_arg` / `_resolve_repo_from_cwd`.
- [x] main() cwd-scoping.
- [x] `_on_session_end` passes --repo; cron create/edit carry --workdir.

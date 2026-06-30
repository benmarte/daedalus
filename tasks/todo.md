# Session — Beta.32 Audit + Feature Additions (PR #225)

## Bug Fixes
- [x] AC1 — Gate `qa_failed_cards` on executor `ok=True`
- [x] AC2 — Add `enrollment_failures` to kanban-only dispatch summary
- [x] AC3 — Combine PUT+GET failure into single warning in `merge_pr()`
- [x] AC4 — Extend `_redact()` to cover URL-percent-encoded token variants
- [x] AC5 — `ensure_labels()` calls `list_labels()` exactly once
- [x] AC6 — `_resolve_web_path()`: lazy-fetch + sanitize raw API output
- [x] AC7 — `VCSProvider.enrollment_failures` as instance attr in `__init__`
- [x] AC8 — Document `_execute_dev_fix_ci` return semantics

## New Features
- [x] AC9 — `max-fix-attempts` NOTIFY_EVENT + `_notify_max_fix_attempts()`
- [x] AC10 — `run_iterate()` returns 5-tuple; 5th slot = `escalated_cards`
- [x] AC11 — `_notify_qa_failed()` deduped per `(issue_n, pr)` via `_QA_FAILED_NOTIFIED`
- [x] AC12 — `_notify_max_fix_attempts()` deduped per `(issue_n, pr)` via `_MAX_FIX_NOTIFIED`

## E2E Tests
- [x] AC13 — 5-scenario QA gate smoke suite (`test_e2e_qa_gate_filelock_smoke.py`)
- [x] AC14 — Scenario 2 deterministic with `threading.Event` barriers

## Test Coverage
- [x] AC15–AC19 — ok=False path, ensure_labels dedup, enrollment_failures, combined warning, notification dedup

## Lifecycle
- [x] Spec: `tasks/spec-session-beta32-audit.md`
- [x] Build: all ACs above
- [x] Test: 2551 passed, 0 failures
- [x] Review: GO — all findings applied
- [x] Ship: GO — PR #225 open; human gate: user merges dev → main
- [ ] PR merge: **HUMAN GATE** — user merges PR #225

---

# Phase 3 (#151) — Epic Sub-Issue Creation

## Tasks
- [ ] T1: `core/providers/base.py` — add `add_label()` stub
- [ ] T2: `core/providers/github.py` — implement `add_label()` via Labels API
- [ ] T3: `core/iterate.py` — PLANNER_DECOMPOSE constant + `_execute_planner_decompose()` (pass provider to executors)
- [ ] T4: `scripts/daedalus_dispatch.py` — update `_planner_body()` to instruct agent to output `PLANNING COMPLETE:`
- [ ] T5: `tests/test_subissue_creation.py` — 13-test suite covering both template paths, idempotency, dry-run, regressions

## Checkpoints
- [ ] After T1+T2: existing tests still pass
- [ ] After T3+T5: all 13 new tests pass
- [ ] Final: full `pytest tests/ -v` — no regressions

---

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

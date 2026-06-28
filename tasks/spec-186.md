# Spec: Stale blocked card sweeper (#186)

Part of epic #181 (remaining self-healing gaps).

## Problem
Kanban cards can get stuck in `blocked` status indefinitely with no visibility.
Nothing detects or surfaces these stale blocked cards, so they accumulate
silently on the active board.

## Requirements
1. Detect cards in `blocked` status with no activity for > a threshold (default 48h).
2. Log a `WARNING` for each stale blocked card (id, title/assignee, age).
3. Optionally archive stale cards off the active board (`hermes kanban archive`).
4. Integrate into the existing dispatch tick (runs alongside native diagnostics).
5. Configurable via `tracking.stale_blocked.{hours,archive}`.

## Design
- New module `core/sweeper.py`:
  - `DEFAULT_STALE_HOURS = 48`.
  - `blocked_since(card)` → epoch secs of last activity:
    `last_heartbeat_at` → `started_at` → `created_at` (first present wins), else `None`.
  - `find_stale_blocked(cards, *, now, threshold_hours)` → pure; returns
    `[(card, age_hours)]` for blocked cards older than threshold, oldest first.
  - `sweep_stale_blocked(slug, *, threshold_hours, archive, now, dry_run)` →
    pulls `kanban.list_blocked`, enriches missing `last_heartbeat_at` from the
    board DB (graceful), warns per stale card, optionally archives; returns
    stale ids.
- New `core/kanban.py` helper `archive_task(slug, task_id)` wrapping
  `hermes kanban archive <id>`.

## Why timestamps come from the `tasks` table
The board's `task_runs` table is empty in this deployment, so the most reliable
"last progress" signal is `last_heartbeat_at` (frozen once the worker stops on a
block), falling back to `started_at`/`created_at`. `list --json` omits
`last_heartbeat_at`, so the sweeper enriches blocked cards with a single direct
SQLite read (mirrors `kanban.rename_task`'s direct-DB precedent).

## Acceptance criteria
- [ ] Cards blocked > 48h are detected.
- [ ] A warning is logged per stale card.
- [ ] Optional archive moves the card off the active board.
- [ ] Unit tests for detection + sweep (dual-mode: pytest + `__main__`).
- [ ] Wired into the dispatch tick.

# Spec — Issue #1114: Orphaned worktree cleanup sweep

## Objective

The dispatcher tells agents to `git worktree remove --force` on cleanup but never
enforces it, so orphaned worktrees (25+ under `.worktrees/`) accumulate unboundedly
when agents crash or are reclaimed before cleanup. Add an enforcement sweep to
`scripts/daedalus_dispatch.py` that removes worktrees whose issue has no active
kanban task, once per dispatch tick.

## Design (from PM spec on issue #1114)

New function `_sweep_orphan_worktrees(workdir, slug, *, dry_run=False)`:

1. Enumerate registered worktrees via `git -C <workdir> worktree list --porcelain`.
2. Per entry, extract path, branch (`branch refs/heads/...` line), and issue number
   from branch name `fix/issue-<N>`, falling back to worktree dirname `dev-<N>`.
3. Skip: the main worktree (path == workdir, realpath-compared) and entries with no
   derivable issue number (conservative — never remove what we can't attribute).
4. Build the active-issue set once from `kanban.list_tasks(slug)`: issue numbers
   (via `extract_issue_number` on titles) of tasks whose status is NOT terminal
   (`done`, `complete`, `completed`, `cancelled`, `archived`).
5. If a worktree's issue has no active task → `git -C <workdir> worktree remove
   --force <path>`; log INFO `dispatch: swept orphan worktree <path> (branch=<b>, issue=#<N>)`.
6. Each removal wrapped in try/except; failure logged WARNING and skipped.
7. `git -C <workdir> worktree prune` after any removals.
8. `dry_run=True` → log `[dry-run] would remove orphan worktree <path>` without mutating.
9. Function never raises; returns count of removed worktrees.

Call site: start of `run()` tick, right after `kanban.ensure_board(slug)`, gated on
`workdir` being set (git failures short-circuit to a no-op).

## Acceptance criteria

- [ ] `_sweep_orphan_worktrees` implemented alongside the other per-tick sweeps
- [ ] Called at start of `run()` after workdir/slug resolution, before dispatch
- [ ] Parses porcelain output; issue number from branch or path
- [ ] Only removes worktrees with no active (non-terminal) kanban task
- [ ] Main worktree never removed; unattributable worktrees skipped
- [ ] Failed removal → WARNING + skip, tick never aborts
- [ ] `git worktree prune` after removals
- [ ] dry_run logs intent, no mutation
- [ ] Tests in `tests/test_worktree_sweep.py` (real git repos in tmp_path):
  orphan removed, active preserved, failed removal skipped, main worktree kept,
  dry_run no-op, detached/unattributable skipped
- [ ] `make test` and `make lint` pass

## Boundaries

- Do NOT touch agent prompt text or `daedalus-worktree-spawn.sh`
- Do NOT merge; PR into `dev`, blocked kanban card with `review-required:`

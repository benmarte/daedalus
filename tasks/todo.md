# Issue #1114 — orphan worktree cleanup sweep

## Tasks

1. [ ] T1 Implement `_sweep_orphan_worktrees(workdir, slug, *, dry_run=False)` in
       scripts/daedalus_dispatch.py alongside `_repair_orphan_tasks`. Porcelain parse →
       issue attribution (branch `fix/issue-<N>`, fallback dirname `dev-<N>`) →
       active-task guard via `kanban.list_tasks(slug)` + `extract_issue_number` →
       per-removal try/except (WARNING + skip) → `worktree prune` → dry_run → never
       raises, returns removal count.
2. [ ] T2 Wire the call into run() right after `kanban.ensure_board(slug)`, gated on
       `workdir`.
3. [ ] T3 Tests in tests/test_worktree_sweep.py with real git repos in tmp_path:
       orphan removed; active preserved; failed removal skipped without abort; main
       worktree kept; dry_run no-op; detached/no-issue skipped; non-git workdir no-op.
       Use `assert` (not soft `check()`).
4. [ ] T4 Verify: `python3.14 -m pytest tests/test_worktree_sweep.py -q`, then
       `make test` + `make lint` (commit before lint — it diffs vs origin/dev).
5. [ ] T5 Push `fix/issue-1114`, open PR into dev (Closes #1114), block kanban card
       `review-required: PR #<n> — fix/issue-1114-worktree-cleanup-sweep`.

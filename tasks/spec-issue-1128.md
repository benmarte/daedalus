# Spec: Issue #1128 — Repo hygiene: .gitignore gaps, stray artifacts, archive closed specs

- **Issue**: benmarte/daedalus#1128
- **Branch**: `fix/issue-1128-repo-hygiene`
- **PR target**: `dev`
- **Type**: chore (no runtime code changes)

## Root cause

The repo root accumulates runtime artifacts because `.gitignore` never gained
patterns for the dispatcher/QA pipeline's byproducts: QA log files (`qa-*.log`),
agent worktree directories (`worktree-*/`, `.worktrees/`), and sqlite state
files. Separately, stale docs (`SPEC.md`, `design-retry-cap-notification.md`)
and per-issue spec files for closed issues linger at root / in `tasks/`,
misleading humans and AI agents about the current system state. `uv.lock` was
never committed, so installs are not reproducible.

## Verified current state (2026-07-01, on this checkout)

- `.gitignore` lacks: `qa-*.log`, `worktree-*/`, `.worktrees/`, `*.db`, `*.sqlite`.
  It already contains `kanban.db`.
- `kanban.db` is **already untracked and ignored** → the issue's
  `git rm --cached kanban.db` sub-item is a **no-op; skip it**.
- All four root dirs `worktree-994/`, `worktree-t_fbb5b29a/`,
  `worktree-t-cb82da1d/`, `worktree-t-da45f24c/` are **registered active git
  worktrees** (confirmed via `git worktree list`). They MUST be removed with
  `git worktree remove <path>` (add `--force` only if dirty), never plain `rm -rf`.
- Three untracked QA logs at root: `qa-full-test-results.log`,
  `qa-integration-suite-results.log`, `qa-integration-test-results.log`.
- `SPEC.md` (tracked, root) duplicates `tasks/spec-issue-1072.md` content and
  misleads readers into thinking it is the system spec → delete via `git rm`.
- `design-retry-cap-notification.md` (tracked, root) → `git mv` to `docs/`.
- `uv.lock` exists at root, untracked → commit it.
- `tasks/spec-issue-*.md`: tracked ones are `spec-issue-88.md`,
  `spec-issue-961.md` (plus `spec-186.md`); the rest (1053, 1072, 1098, 1099,
  1104, 1105, 1121, 1123, 997) are untracked working files.

## Fix strategy (ordered)

1. **`.gitignore`**: append a "Runtime artifacts" section with:
   `qa-*.log`, `worktree-*/`, `.worktrees/`, `*.db`, `*.sqlite`.
   Keep the existing `kanban.db` line (harmless, now redundant with `*.db`).
   Verify no currently-tracked file matches the new patterns
   (`git ls-files | grep -E '\.(db|sqlite)$|^qa-.*\.log$'` must be empty).
2. **Remove registered worktrees**: `git worktree remove` each of the four
   root `worktree-*` dirs listed above (use `--force` if a tree is dirty —
   they are abandoned agent scratch trees). Then `git worktree prune`.
   Do NOT touch `.worktrees/` contents or any worktree outside the four listed —
   other worktrees under `/tmp` and `.worktrees/` may belong to live agents.
3. **Delete QA logs**: `rm` the three `qa-*.log` files at root (untracked).
4. **Commit `uv.lock`**: `git add uv.lock`. Add a short note to `SETUP.md`
   stating `uv sync` is the supported install path and `uv.lock` is committed
   for reproducible installs.
5. **Delete root `SPEC.md`**: `git rm SPEC.md`.
6. **Move design doc**: `git mv design-retry-cap-notification.md docs/`.
7. **Archive closed-issue specs**: create `tasks/archive/` (exists already —
   verify). For each `tasks/spec-issue-*.md` and `tasks/spec-186.md`, check the
   referenced issue state with `gh issue view <n> --json state`; move files for
   CLOSED issues into `tasks/archive/` (`git mv` for tracked files, plain `mv`
   + `git add` for untracked ones). Leave specs for open issues in place.

## Scope guards

- No source-code or plugin behavior changes; this is file hygiene only.
- Do not delete or modify `docs/documentation-update-plan.md`,
  `docs/gap-analysis.md`, `tasks/improvement-plan.md`, `tasks/plan.md`,
  `tasks/todo.md`, or `tasks/lessons.md`.
- Do not remove any worktree not in the explicit four-item list.

## Task plan (ordered, each independently verifiable)

- [ ] T1. Branch `fix/issue-1128-repo-hygiene` off fresh `dev` — verify: `git branch --show-current`
- [ ] T2. Add failing guard test `tests/test_repo_hygiene.py` (gitignore patterns present; no tracked `*.db`/`*.sqlite`/`qa-*.log`; no root `SPEC.md`; no root `worktree-*` dirs) — verify: pytest fails before fix, passes after
- [ ] T3. Append runtime-artifact patterns to `.gitignore` — verify: `git check-ignore qa-x.log worktree-x/ x.db x.sqlite .worktrees`
- [ ] T4. `git worktree remove --force` the four root worktrees + `git worktree prune` — verify: `git worktree list` clean, dirs gone
- [ ] T5. Delete three root `qa-*.log` files — verify: `ls qa-*.log` empty
- [ ] T6. `git add uv.lock`; SETUP.md note re `uv sync` — verify: `git ls-files uv.lock`
- [ ] T7. `git rm SPEC.md`; `git mv design-retry-cap-notification.md docs/` — verify: paths
- [ ] T8. Archive closed-issue specs (tracked: spec-issue-88, spec-186, spec-issue-961 via `git mv`; untracked: 997/1053/1072/1098/1099/1104/1105/1121/1123 via `mv` + `git add`) — verify: only open-issue specs left in `tasks/`
- [ ] T9. Item 0.7: `git add docs/documentation-update-plan.md docs/gap-analysis.md` — verify: tracked
- [ ] T10. Full suite `python3.14 -m pytest` green; lint; commit; push; PR into dev

## Acceptance criteria

1. `git status` on the branch shows no `qa-*.log`, `worktree-*/`, `.worktrees/`,
   `*.db`, or `*.sqlite` entries (tracked or untracked-visible).
2. Repo root contains no `worktree-*` directories and no `qa-*.log` files;
   `git worktree list` no longer lists the four removed paths.
3. `uv.lock` is tracked; `SETUP.md` mentions `uv sync` as the supported path.
4. Root `SPEC.md` is gone; `design-retry-cap-notification.md` lives in `docs/`.
5. Every `tasks/spec-issue-*.md` remaining outside `tasks/archive/` refers to
   an OPEN issue; closed-issue specs are in `tasks/archive/`.
6. Full test suite green: `python3.14 -m pytest` passes (hygiene change must
   not break test discovery/paths).
7. `kanban.db` still exists locally and remains untracked.

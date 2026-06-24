# Contributing to Daedalus

## Branching Strategy

Daedalus uses a **dev → main** release workflow:

```
feat/* ──→ dev ──→ main (release only)
fix/*  ──→ dev ──→ main (release only)
```

- **`dev`** — integration branch. All feature and fix PRs land here.
- **`main`** — stable, releasable branch. Receives merges **only** from `dev` via release PRs. No direct feature/fix PRs target `main`.

## Workflow

1. **Create a branch** from `dev`:
   ```bash
   git checkout dev
   git pull origin dev
   git checkout -b feat/my-feature
   ```

2. **Make your changes** and commit following [Conventional Commits](https://www.conventionalcommits.org/):
   - `feat:` — new feature
   - `fix:` — bug fix
   - `refactor:` — code restructuring
   - `docs:` — documentation
   - `test:` — tests
   - `chore:` — tooling, dependencies, config

3. **Open a PR** targeting `dev`:
   ```bash
   gh pr create --base dev --head feat/my-feature
   ```

4. **CI must pass** before merge. All PRs require status checks.

5. **Release PRs** (`dev` → `main`) are created when `dev` is stable and ready for release. These are the **only** PRs that target `main`.

## Branch Protection

- **`main`**: requires PR, requires status checks, no direct pushes.
- **`dev`**: requires PR, no direct pushes.

## PR Guidelines

- Keep PRs focused — one logical change per PR.
- Write clear PR descriptions explaining what and why.
- Link related issues with `Closes #N` in the PR body.
- Request review from appropriate team members.

## Code Style

- Follow existing patterns in the codebase.
- Run type checking and linting before committing.
- Write tests for new functionality.

## Questions?

Open an issue or ask in the repository discussions.

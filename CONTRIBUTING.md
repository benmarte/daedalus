# Contributing to Daedalus

## Branching Strategy

Daedalus uses a **stable-branch workflow** with three tiers:

```
feat/*  ──→  dev  ──→  main
fix/*         (PR)      (release only)
```

| Branch | Purpose | Direct pushes | PRs accepted |
|--------|---------|---------------|--------------|
| `main` | **Release only** — always stable and deployable | ❌ Blocked | Only release PRs from `dev` |
| `dev` | **Integration** — all feature/fix PRs land here | ❌ Blocked | All feature/fix PRs |
| `feat/*` / `fix/*` | **Work branches** — one per issue/feature | ✅ Allowed | N/A (merged into `dev`) |

### Why this structure

- `main` is the public face of the project. It must always be green, tagged, and releasable.
- `dev` absorbs all feature work and acts as a staging area before promotion to `main`.
- Feature branches isolate work — no two features collide, and `dev` stays buildable.

### Workflow

1. **Start work:** branch off `dev` — `git checkout -b fix/issue-N-description dev`
2. **Implement + test:** write code, run tests, pass the ship-gate
3. **Open PR:** target `dev` (not `main`)
4. **CI must pass:** the Daedalus pipeline auto-advances only on green CI
5. **Merge to dev:** after review, security audit, and documentation
6. **Release to main:** periodically, a release PR is opened from `dev` → `main`. Merging this PR triggers the [release pipeline](.github/workflows/release.yml) which auto-bumps the version, creates a git tag, and publishes a GitHub Release.

## Branch Protection Rules

The following rules are enforced via GitHub branch protection (Settings → Branches):

### `main` branch

| Rule | Value |
|------|-------|
| Require a pull request before merging | ✅ Enabled |
| Require approvals | 1 approval |
| Dismiss stale pull request approvals when new commits are pushed | ✅ Enabled |
| Require status checks to pass before merging | ✅ Enabled |
| Require branches to be up to date before merging | ✅ Enabled |
| Allow force pushes | ❌ Disabled |
| Allow deletions | ❌ Disabled |
| Block direct pushes (restrict to PRs only) | ✅ Everyone |

### `dev` branch

| Rule | Value |
|------|-------|
| Require a pull request before merging | ✅ Enabled |
| Require approvals | 1 approval |
| Allow force pushes | ❌ Disabled |
| Allow deletions | ❌ Disabled |
| Block direct pushes (restrict to PRs only) | ✅ Everyone |

## Release Process

Releases are automated via the [`.github/workflows/release.yml`](.github/workflows/release.yml) workflow:

1. A release PR is opened from `dev` → `main`
2. When merged, the workflow triggers on the push to `main`
3. The workflow:
   - Finds the last `v*` tag
   - Collects all `feat:` and `fix:` commits since that tag
   - Computes the next beta version (e.g. `v1.0.0-beta.26`)
   - Updates `plugin.yaml` version and pushes the bump commit
   - Creates a git tag
   - Publishes a GitHub Release with the changelog

### Dry-run testing

Trigger the workflow manually via **Actions → Release → Run workflow**. This runs all steps except the actual tag, release, and version bump — it prints what *would* happen.

### Recursive trigger guard

The version-bump commit pushed by the workflow would re-trigger the workflow. The workflow detects commits with `chore: bump version to` in the message and exits early to prevent infinite loops.

## Commit Conventions

- `feat:` — new features
- `fix:` — bug fixes
- `chore:` — maintenance (version bumps, config changes)
- `docs:` — documentation only
- `refactor:` — code changes that neither fix nor add features

Only `feat:` and `fix:` commits appear in release changelogs.

## PR Guidelines

- One PR per issue — no bundling unrelated changes
- PR title prefix matches the branch prefix (e.g. `fix:` for `fix/*` branches)
- All PRs target `dev` unless it's a release PR (`dev` → `main`)
- CI must be green before the pipeline advances past QA
- The Daedalus pipeline handles review, security audit, accessibility audit (UI work), and documentation — you only need to merge

## Development Setup

See [SETUP.md](SETUP.md) for provisioning the agent roster and [docs/INSTALLATION_GUIDE.md](docs/INSTALLATION_GUIDE.md) for the full step-by-step installation guide.

# Implementation Plan: Installable plugin + per-repo config/registry + multi-project dashboard

## Overview
Land the tested working code on `main`, then build the installable plugin with a
per-repo `.hermes/daedalus.yaml` + global registry, and finally the headline
deliverable: a **multi-project daedalus dashboard** showing every project's status
with an editable config (repo/workdir read-only). Engine stays native; the plugin adds
only the validated non-native pieces (CI-aware auto-advance + iterate, multi-repo sweep).

## Architecture decisions (locked)
- Clean main via a **reviewable PR** (working branch → main), not a force-reset.
- Config = **per-repo `.hermes/daedalus.yaml` + global registry** (`~/.hermes/daedalus/projects`); build this foundation **before** the dashboard.
- Dashboard cards surface: **kanban summary · open/in-review PRs (+CI) · last run + cron · needs-attention + tracking**.
- **`repo` and `workdir` are read-only** in the editor (editing breaks orchestration).
- **Use native Hermes Plugin SDK components** (`SDK.components.*` — Button, Checkbox, inputs, cards, etc.) for the dashboard UI so it looks consistent with the rest of the Hermes dashboard. NO bespoke inline-styled HTML widgets (the slice-2 reviewer flagged exactly this "widget inconsistency"). Discover available components from `window.__HERMES_PLUGIN_SDK__.components` before building.
- No custom CLI; trigger is native (`kanban create --triage`); roster bundled (post-install).
- Existing **dycotomic + daedalus** projects get **migrated** into per-repo `.hermes/` + registry at cutover (global `projects[]` removed); final plugin then installed onto them to verify.

## Dependency graph
```
Phase 0 clean main
   └── Phase 1 plugin.yaml + register + registry + per-repo ConfigLoader + setup.sh
         ├── Phase 2 dispatcher: registry sweep + spec source + CI-aware advance/iterate
         └── Phase 3 dashboard backend (status API + per-repo config API)
                └── Phase 3 dashboard frontend (project cards + editor)
         └── Phase 4 post-install roster + clean-instance e2e
```

---

## Phase 0 — Clean `main`

### Task 0.1: Remove the broken plugin scaffolding
**Description:** Strip the dead `register()` (imports deleted `.tools`/`.schemas`/`skill/`) so the repo has no crash-on-load code; leave a minimal package `__init__.py`.
- **Acceptance:** `__init__.py` imports with zero missing-module errors; no references to `.tools`/`.schemas`/`cli`.
- **Verify:** `python3 -c "import __init__"` (or import the package) raises nothing; existing suites green.
- **Files:** `__init__.py`. **Size:** XS.

### Task 0.2: PR the tested working code → `main`
**Description:** Open a PR from the tested branch into `main`; the diff removes the old engine + plugin-era cruft (`cli.py`, `core/{runner,source,trigger,notifier,lifecycle}.py`, `platforms/`, `schemas.py`, `tools.py`, `skill/`, old `plugin.yaml`, old `SPEC*.md`) and adds the lean tested code.
- **Acceptance:** PR diff contains only intended removals/additions; CI green; merged → `main` matches the tested state.
- **Verify:** `gh pr checks` green; after merge `git ls-tree main` shows no dead files; `python3 tests/test_daedalus.py && pytest -q` green on main.
- **Deps:** 0.1. **Files:** PR (many deletions). **Size:** M (mostly deletions).

### Task 0.3: Delete stale branches
**Description:** Remove merged/abandoned branches (`feat/dashboard-config-api`, `fix/runner-lifecycle-cron`, and feature branches once merged).
- **Acceptance:** only active branches remain on origin.
- **Verify:** `git branch -r` clean. **Size:** XS.

### Checkpoint: main is the clean baseline
- [ ] main builds + both test suites green · no dead files · stale branches gone · human review.

---

## Phase 1 — Foundation: installable plugin + per-repo config + registry

### Task 1.1: `plugin.yaml` + import-safe `register()`
**Description:** Add root `plugin.yaml` (name `daedalus`, version, description) and rewrite `register(ctx)` to wire ONLY the auxiliary (cron) task; dashboard tab auto-discovered.
- **Acceptance:** `hermes plugins install <local repo> --enable` → `hermes plugins list` shows `daedalus` enabled; gateway loads with no error.
- **Verify:** install locally; `hermes plugins list`; gateway log clean. **Deps:** Phase 0. **Files:** `plugin.yaml`, `__init__.py`. **Size:** S.

### Task 1.2: `core/registry.py`
**Description:** Read/write `~/.hermes/daedalus/projects` (managed repo paths); add/remove/list, idempotent, graceful.
- **Acceptance:** add/list/remove round-trips; dedups; missing file = empty.
- **Verify:** unit test `tests/test_registry.py`. **Files:** `core/registry.py`, test. **Size:** S.

### Task 1.3: `templates/daedalus.yaml` + `scripts/setup.sh`
**Description:** `setup.sh` (run in a target repo) scaffolds `<repo>/.hermes/daedalus.yaml` from the template AND appends the repo path to the registry.
- **Acceptance:** running it in a repo writes a valid config + registers the repo; re-run is idempotent.
- **Verify:** run in a temp repo; assert file + registry entry. **Deps:** 1.2. **Files:** `templates/daedalus.yaml`, `scripts/setup.sh`. **Size:** S.

### Task 1.4: per-repo config resolution
**Description:** `ConfigLoader` resolves `<repo>/.hermes/daedalus.yaml` (by path), merged over packaged defaults; keep the dual GitHub/kanban modes.
- **Acceptance:** loader returns a resolved per-repo config; existing resolve tests adapted + green.
- **Verify:** `tests/test_daedalus.py` (config cases). **Files:** `config/__init__.py`, tests. **Size:** S.

### Checkpoint: installable + onboardable
- [ ] install loads · `setup.sh` scaffolds+registers · per-repo config resolves · suites green · human review.

---

## Phase 2 — Dispatcher rework (registry sweep + spec source + CI-aware advance)

### Task 2.1: registry sweep + per-repo run
**Description:** `daedalus_dispatch.py` reads the registry → for each repo resolves `<repo>/.hermes/daedalus.yaml` → runs the per-repo tick. Drop the global `projects[]` model.
- **Acceptance:** sweeping N registered repos processes each; `--repo <path>` runs one; dry-run safe.
- **Verify:** dual-mode + sweep unit tests. **Deps:** 1.2, 1.4. **Files:** `scripts/daedalus_dispatch.py`, tests. **Size:** M.

### Task 2.2: spec-file source
**Description:** `core/source_specs.py` turns `<repo>/.hermes/pending/*.md` into pinned triage cards (idempotent per file); wired as a source when `sources.local_specs.enabled`.
- **Acceptance:** dropping a spec.md creates one triage card; re-run doesn't duplicate.
- **Verify:** unit test + the e2e in 4.2. **Files:** `core/source_specs.py`, dispatch wiring, test. **Size:** S.

### Task 2.3: CI-aware auto-advance + iterate-on-red (validated requirement)
**Description:** Harden the existing auto-advance: complete the developer card ONLY on green CI; on a developer review-required handoff with RED CI, re-engage the developer (fresh fix card) instead of stalling. (Exactly the manual nudges proven in the native-core validation.)
- **Acceptance:** green-CI handoff → advances to reviewer; red-CI handoff → a dev fix card is created, not a stall.
- **Verify:** unit tests for both branches (mock `pr_ci_green`); covered again in 4.2 e2e. **Files:** `scripts/daedalus_dispatch.py`, `core/kanban.py`, tests. **Size:** M.

### Checkpoint: pipeline runs unattended
- [ ] sweep + spec source + CI-aware advance/iterate green in tests; ready for dashboard + e2e.

---

## Phase 3 — Multi-project dashboard (HEADLINE)

### Task 3.1: backend — projects + status API
**Description:** `plugin_api.py`: `GET /projects` returns each registered project with its **brief**: kanban summary (counts by state from `hermes kanban list --json`), open/in-review PRs + CI state (`gh pr list`/`pr_ci_green`), last dispatch run + cron presence, needs-attention (blocked/gave_up cards), tracking mode + enabled sources. Read-only aggregation; never returns secrets.
- **Acceptance:** returns one entry per registered repo with all five brief fields; missing data degrades gracefully (nulls, not errors).
- **Verify:** `tests/test_dashboard_api.py` with mocked kanban/gh; manual `curl` against a registered repo. **Deps:** Phase 2. **Files:** `dashboard/plugin_api.py`, tests. **Size:** M.

### Task 3.2: backend — per-repo config read/write (repo/workdir read-only)
**Description:** `GET /project/<name>/config` reads that repo's `.hermes/daedalus.yaml`; `POST` validates + writes it, **rejecting any change to `repo` or `workdir`** (422) and stripping secrets.
- **Acceptance:** POST persists editable fields; attempts to change `repo`/`workdir` are rejected; invalid config 422s.
- **Verify:** round-trip + read-only-field tests in `tests/test_dashboard_api.py`. **Deps:** 3.1. **Files:** `dashboard/plugin_api.py`, tests. **Size:** M.

### Task 3.3: frontend — project cards (the brief)
**Description:** `App.jsx`: grid of project cards showing name + repo (read-only) + the brief (status summary, PRs+CI badges, last run/cron, needs-attention badge, tracking badge). Loads from `GET /projects`. **Built from `SDK.components.*` (native Hermes widgets) — no bespoke inline-styled HTML — for visual consistency with the Hermes dashboard.**
- **Acceptance:** every registered project renders a card with all brief fields; needs-attention/red-CI visually flagged.
- **Verify:** build clean (`node build.js`), bundle parses; manual load in the dashboard. **Deps:** 3.1. **Files:** `dashboard/src/App.jsx`, `dist/index.js`. **Size:** M.

### Task 3.4: frontend — per-project config editor (repo/workdir read-only)
**Description:** Card → editor for the editable fields (tracking number, vcs target branch, sources toggles, cron deliver/schedule); `repo` and `workdir` shown **disabled/read-only**. Save → `POST /project/<name>/config`; surfaces 422.
- **Acceptance:** editing + Save round-trips through the backend; `repo`/`workdir` inputs are disabled; validation errors shown inline.
- **Verify:** manual edit round-trip; disabled fields can't submit changes. **Deps:** 3.2, 3.3. **Files:** `dashboard/src/App.jsx`, `dist/index.js`. **Size:** M.

### Checkpoint: dashboard complete
- [ ] lists every project with live status · per-project edit round-trips · repo/workdir non-editable · build clean · human review.

---

## Phase 4 — Post-install roster + clean-instance e2e

### Task 4.1: post-install roster provisioning
**Description:** `scripts/postinstall.py` runs `provision_roster.sh` with loud prerequisite checks (default profile, agent-skills, `gh` auth); wired so install provisions the roster (or `hermes daedalus`-less fallback: documented one-liner).
- **Acceptance:** on a clean machine, install → 6 profiles exist (or a clear actionable error). Idempotent.
- **Verify:** run on the clean instance in 4.2. **Deps:** Phase 1. **Files:** `scripts/postinstall.py`. **Size:** S.

### Task 4.2: clean-instance end-to-end
**Description:** Backup → full teardown (incl. dycotomic board, per earlier choice) → `hermes plugins install <local> --enable` → roster auto-provisioned → `setup.sh` in a throwaway repo → dashboard shows it → drop a spec.md → dispatcher runs → reviewed PR with green CI.
- **Acceptance:** the whole chain works from clean install with no manual symlink/CLI; dashboard reflects status.
- **Verify:** scripted run + screenshots/logs. **Deps:** all. **Size:** L (orchestration, not new code).

### Checkpoint: ship
- [ ] all acceptance criteria met · clean-instance e2e passes · push branch · open PR · update README/SETUP.

---

## Risks & mitigations
| Risk | Impact | Mitigation |
|------|--------|-----------|
| Dropping global `projects[]` breaks current dycotomic/daedalus runs | Med | Migration step in 2.1: `setup.sh` existing repos into the registry before removing the global path |
| `hermes plugins install` lacks a post-install hook | Med | Verify in 1.1; fall back to a documented `postinstall.py` one-liner / first-run trigger |
| Status API too slow (gh/kanban calls per project) | Low | cache per tick; lazy/async; degrade to nulls |
| Dashboard rework regresses slice-2 round-trip | Low | keep `test_dashboard_api.py` green throughout |

## Open questions
- Migrate the **existing** dycotomic + daedalus projects from the global config into per-repo `.hermes/` + registry as part of Phase 2 (recommended), or leave global until cutover?
- Throwaway-repo deletion needs `gh auth refresh -s delete_repo` (or UI) — your call on the leftover `hermes-pipeline-test`.
```

# Spec: Proper installable Hermes plugin + per-project `.hermes/` config

## Objective
Make the daedalus a **real, installable Hermes plugin** with a **portable per-repo config**, so anyone can:
1. `hermes plugins install benmarte/daedalus --enable` — official install; **auto-provisions the roster** (post-install step).
2. In any target repo: `hermes daedalus setup` — scaffolds `<repo>/.hermes/daedalus.yaml` **and registers the repo** in a global registry.
3. Configure it (registry-aware dashboard tab or the YAML), then the native pipeline drives **issues / spec files / manual triage cards → reviewed PRs**.

**Success = a clean instance, install → configure → reviewed PR, with no manual symlink and no broken `register()`.**

## Resolved architecture (locked)
- **Config model:** per-repo `<repo>/.hermes/daedalus.yaml` (checked in, secret-free) **+ a global registry** `~/.hermes/daedalus/projects` listing managed repo paths. **One cron sweeps the registry.**
- **`.hermes/` contents:** `daedalus.yaml` only. (Spec-file trigger reads `<repo>/.hermes/pending/*.md` — path set in the config's `sources.local_specs.directory`, default `.hermes/pending/`.)
- **Triggers (per-repo, toggled in `sources`):** (a) GitHub issues, Ready-gated; (b) `spec/plan.md` files in the pending dir; (c) manual kanban triage card (always available).
- **Roster:** **bundled** — install auto-runs the idempotent provisioner (post-install), with loud prerequisite checks (default profile, agent-skills, `gh` auth).
- **`setup`:** scaffolds `.hermes/daedalus.yaml` **and** appends the repo path to the registry.
- **Dashboard tab:** **registry-aware** — lists registered repos, edits each repo's `<repo>/.hermes/daedalus.yaml`.
- **Engine stays native:** roster profiles + `hermes kanban decompose` (pin/sequence) + daemon dispatch/reaping + cron + slack. The plugin does NOT reinvent these.

## Plugin surfaces (what `register(ctx)` wires)
**No custom CLI.** The interactive trigger is NATIVE: a triage card IS the prompt —
`hermes kanban create --triage --body "<prompt>"` (or `--body "$(cat spec.md)"`), then
native `decompose` fans it to the roster, whose profiles carry the agent-skills lifecycle.
So the plugin only wires the **automation + management** layer:
- `register_auxiliary_task("daedalus", ...)` → the dispatcher tick (registry sweep) for cron.
- Dashboard tab auto-discovered from `dashboard/manifest.json` (no register call).
- **Removed:** the broken `_register_tools`/`_register_skill`/`_register_cli_commands` (imported
  deleted `.tools`/`.schemas`/`skill/`/`cli.py`). `register()` must import cleanly and never raise.

### What enforces "always follows the agent-skills lifecycle"
1. **Roster profiles** seeded with the lifecycle agent-skills (`provision_roster.sh`) — decompose
   routes work onto them, so the lifecycle is followed by construction.
2. **Ship-gate hook** — the one HARD gate: blocks `gh pr create` until the repo's quality gates pass.
3. Triage-card body prescribes the lifecycle phases. (Best-effort skills + hard ship gate = "always".)

### Onboarding/run without a CLI
- **Onboard a repo:** `hermes daedalus setup` is replaced by either a tiny `scripts/setup.sh`
  (scaffold `.hermes/daedalus.yaml` + append to registry) OR the dashboard "Add repo" action.
- **Manual run:** `python3 <plugin>/scripts/daedalus_dispatch.py [--repo PATH]`.
- **Manual one-off task:** native `hermes kanban create --triage --body "..."`.
- **Automated:** cron runs the dispatcher (registry sweep).

## Components & responsibilities
| Component | Responsibility | Status |
|---|---|---|
| `plugin.yaml` | hermes plugin manifest (makes it installable) | **NEW** |
| `__init__.py register()` | wire the auxiliary (cron) task only; import-safe, never raises | **rewrite** |
| `scripts/setup.sh` | scaffold `<repo>/.hermes/daedalus.yaml` + append to registry | **NEW** (replaces CLI `setup`) |
| `scripts/postinstall.py` (or hook) | provision roster after install, prereq-checked | **NEW** |
| `core/registry.py` | read/write `~/.hermes/daedalus/projects` | **NEW** |
| `config/ConfigLoader` | resolve `<repo>/.hermes/daedalus.yaml` (per-repo) | **adapt** |
| `scripts/daedalus_dispatch.py` | sweep registry → per repo: issues + specs + kanban triage → decompose → reconcile | **adapt** (+ spec-file source) |
| `core/source_specs.py` | turn `.hermes/pending/*.md` into triage cards | **NEW** |
| `dashboard/plugin_api.py` | registry-aware: list repos, GET/POST a repo's config, add/remove repo | **rework** |
| `dashboard/src/App.jsx` | registry-aware UI (repo list → per-repo editor) | **rework** |
| `scripts/provision_roster.sh` | 6 profiles | reuse |
| `core/kanban.py`, `core/github_project.py` | native wrappers + GH reconciliation | reuse |
| `templates/daedalus.yaml` | scaffold copied by `setup` | **NEW** |

## Commands
```
Install (official):   hermes plugins install benmarte/daedalus --enable   # auto-provisions roster
Install (local test): hermes plugins install <repo-path> --enable --force
Onboard a repo:       cd <target-repo> && bash <plugin>/scripts/setup.sh      # scaffold + register
Run once (manual):    python3 <plugin>/scripts/daedalus_dispatch.py [--repo <path>] [--dry-run]
Trigger one task:     hermes kanban create --triage --body "<prompt or $(cat spec.md)>"   # native
Automated:            hermes cron  (sweeps the registry)
Tests:                python3 tests/test_daedalus.py && pytest tests/ -q
Dashboard:            hermes dashboard → Daedalus tab (config + add repo)
```

## Per-repo `.hermes/daedalus.yaml` (shape)
```yaml
name: my-project
repo: owner/my-project
workdir: .                      # the repo root (resolved absolute by the dispatcher)
tracking:
  github_project_number: 1      # optional → omit for kanban-only
vcs:
  target_branch: dev
sources:
  github_issues: { enabled: true, ready_status: Ready }
  local_specs:   { enabled: true, directory: .hermes/pending/ }
  kanban_triage: { enabled: true }
cron:
  deliver: slack:CXXObt          # report target
```

## Project Structure
```
plugin.yaml            NEW  installable-plugin manifest
__init__.py            register(ctx): CLI + auxiliary task (no dead imports)
scripts/               postinstall.py NEW · daedalus_dispatch.py adapt · provision_roster.sh reuse
core/                  registry.py NEW · source_specs.py NEW · kanban.py · github_project.py
config/                ConfigLoader (per-repo resolution)
dashboard/             manifest.json · plugin_api.py rework · src/App.jsx rework → dist/index.js
templates/             daedalus.yaml NEW (setup scaffold)
tests/                 test_daedalus.py · test_dashboard_api.py · NEW registry/specs/register tests
tasks/                 this spec, plan, runbook, lessons
```

## Code Style
Thin native wrappers, graceful degradation, no reinvention.
```python
def register(ctx):
    """Wire surfaces. Must not raise — log failures, never crash the gateway."""
    _register_cli(ctx)             # hermes daedalus init|setup|register|status|run
    _register_auxiliary_task(ctx)  # dispatcher cron tick (sweeps the registry)
    # dashboard tab auto-discovered from dashboard/manifest.json
```

## Testing Strategy
- Keep existing suites green (27 plain + 20 pytest).
- New unit tests: registry read/write, spec-file → triage card, per-repo config resolution, and **`register()` import-safety** (loads with zero missing modules).
- **E2E (clean instance):** backup → full teardown → `hermes plugins install <local>` → roster auto-provisioned → `hermes daedalus setup` in a throwaway repo → `run` → assert triage → decompose → roster → **reviewed PR (green CI)**.

## Boundaries
- **Always:** native engine; `register()` import-safe + non-raising; secret-free repo; idempotent install/init/setup.
- **Ask first:** the backup + full teardown (incl. dycotomic board); pushing the branch; removing the global-config model from anything still using it.
- **Never:** reinvent native hermes; commit secrets; ship `hermes profile export` tarballs.

## Success Criteria
1. `register(ctx)` imports + runs with **zero missing-module errors**; `plugin.yaml` valid.
2. `hermes plugins install <repo> --enable` → `hermes plugins list` shows `daedalus` enabled; roster provisioned; dashboard tab loads **via the install** (no symlink).
3. `hermes daedalus setup` scaffolds `<repo>/.hermes/daedalus.yaml` + registers the repo; `status` reads it.
4. Clean-instance `run` drives a test issue/spec/triage → **reviewed PR with green CI** via the roster.
5. Registry-aware dashboard tab lists repos + round-trips a repo's config. Existing suites green.

## Plan preview (slices — detailed in Phase 2)
1. **Make it installable:** `plugin.yaml` + rewritten import-safe `register()` (CLI stubs + auxiliary task) → installs + loads cleanly.
2. **Per-repo config + registry:** `core/registry.py`, `templates/`, `hermes daedalus setup/register/status`, ConfigLoader per-repo resolution.
3. **Dispatcher rework:** registry sweep + per-repo run; add spec-file source (`core/source_specs.py`); keep issues + kanban triage.
4. **Post-install roster:** `scripts/postinstall.py` + prereq checks, wired to install.
5. **Registry-aware dashboard:** plugin_api.py + App.jsx rework.
6. **E2E on clean instance:** backup → teardown → install → setup → run → reviewed PR.

## Verify-during-build (technical unknowns)
- Does `hermes plugins install` support a **post-install hook**? (else `init` is invoked by a thin install wrapper / first-run.) — confirm before slice 4.
- `register_auxiliary_task` / `register_cli_command` exact signatures in this hermes version.
- `--workspace worktree:` honored? (SETUP.md says pre-create worktree + use `dir:` — keep that.)
```

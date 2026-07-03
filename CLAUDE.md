# CLAUDE.md

Quick-reference map for AI agents and human maintainers working in the Daedalus repo.
This is a map, not a tutorial — read the linked files for depth.

---

## 1. Module Responsibility Map

| Module | Path | Responsibility |
|--------|------|----------------|
| Dispatcher | `scripts/daedalus_dispatch.py` (~9200 lines) | Cron entrypoint + orchestrator. Reconciles board, creates kanban tasks, dispatches workers, runs auto-advance, posts comments/notifications |
| Auto-advance | `core/iterate.py` (~3000 lines) | CI-aware self-healing loop. `classify_blocked()` routes blocked cards → advance / qa_fix / escalate / pm_route / approve_advance / planner_decompose / reconcile_merged |
| VCS base | `core/providers/base.py` | `VCSProvider` ABC, dataclasses (`IssueSummary`, `PRSummary`, `CIStatus`), epic detection heuristics, dependency parsing |
| GitHub | `core/providers/github.py` | GitHub REST+GraphQL provider (issues, PRs, CI, boards, labels, merge, comments) |
| GitLab | `core/providers/gitlab.py` | GitLab REST provider (label-driven board columns) |
| Azure DevOps | `core/providers/azure_devops.py` | Azure DevOps REST provider (WIQL work items) |
| Provider detect | `core/providers/detect.py` | Auto-detects VCS from `git remote get-url origin` |
| HTTP client | `core/providers/http.py` | Shared HTTPS-only `httpx` wrapper with retry/backoff/pagination, token redaction |
| Kanban | `core/kanban.py` | Thin wrapper over `hermes kanban` CLI. Idempotent task creation, lifecycle, test-isolation guard |
| SQLite | `core/db.py` | WAL-mode SQLite connection helper (busy_timeout, synchronous=NORMAL) |
| Sweeper | `core/sweeper.py` | Stale-card detection: blocked >48h, running >24h. Warns, optionally archives |
| Sweeper CLI | `core/sweeper_cli.py` | Manual CLI entry for sweeper outside dispatcher tick |
| Tier promotion | `core/tier_promotion.py` | Epic dependency DAG re-evaluation. Labels next tier Ready when deps merge |
| Crash retry | `core/crash_retry.py` | Time-bounded crash retry with backoff, episode tracking, cross-provider failover |
| Provider failover | `core/provider_failover.py` | Pure resolution of coding-agent/model provider chains. Selects next provider on failure |
| Dispatch state | `core/dispatch_state.py` | File-based state persistence (JSON, atomic). Crash episodes, cooldowns, thread anchors, config fingerprints |
| File overlap | `core/file_overlap.py` | File-ref extraction + task-overlap detection for planner blocking decisions |
| Notifications | `core/notification_sender.py` | Slack/Discord webhook delivery (Block Kit / embeds) |
| Notify templates | `core/notify_templates.py` | Markdown notification templates. Silent-tick awareness |
| Thread delivery | `core/thread_delivery.py` | Per-issue platform thread mirroring (Slack/Discord) |
| Webhook dispatch | `core/webhook_dispatch.py` | Webhook entry point with HMAC verification. Spawns dispatcher |
| Webhook normalizer | `core/webhook_normalizer.py` | VCS-agnostic payload normalizer → `ReadyEvent` |
| Workspace | `core/workspace.py` | Workspace isolation (worktree/symlink/copy) for downstream agents |
| Registry | `core/registry.py` | Plain-text project registry at `~/.hermes/daedalus/projects` |
| Source specs | `core/source_specs.py` | `.hermes/pending/*.md` → triage cards |
| Util | `core/util.py` | Issue/PR number parsing, board slug, crontab conversion, env parsing |
| CLI wrapper | `core/cli.py` | `hermes_cli()` subprocess wrapper, never raises |
| Self-test | `core/dispatch_selftest.py` | Hermetic in-memory pipeline wiring smoke test |
| Config | `config/__init__.py` | `ConfigLoader` — per-repo YAML deep-merge over `templates/daedalus.yaml` |
| Agent body templates | `templates/agent_bodies/*.md` | Role prompt-body templates rendered via `string.Template` by `_render_agent_body()` in the dispatcher. 10 templates: planner, validator, pm, downstream, dev, qa, reviewer, security, docs, task_body (#1147). Golden tests in `tests/test_agent_bodies.py` lock rendered output byte-for-byte |
| Dashboard API | `dashboard/plugin_api.py` (~2000 lines) | FastAPI router at `/api/plugins/daedalus/`. Config, status, project CRUD, cron, meta pickers, lifecycle |
| Agent comment | `scripts/agent_comment.py` | GitHub PR/issue comment helper. Enforces `**Agent:**` header, uses `urllib` |
| Watchdog | `scripts/watchdog.py` | Gateway health watchdog — silent-death detection, rate-limited restart |
| Gateway watchdog | `scripts/gateway_watchdog.py` | Alt gateway watchdog via `hermes gateway status`, exponential backoff |
| Postinstall | `scripts/postinstall.py` | Prerequisite checks + roster provisioning + cron/hook/watchdog install |
| Advance hook | `scripts/register_advance_hook.py` | Registers session-end advance hook in profile config |
| Project resolver | `scripts/daedalus_resolve_project.py` | Resolves project repo-path for advance hook scoping |
| Shell scripts | `scripts/*.sh` | `daedalus-advance.sh` (session-end dispatch), `daedalus-ready.sh` (webhook), `daedalus-detect-pr.sh` (PR handshake), `daedalus-worktree-spawn.sh` (worktree isolation), `provision_roster.sh` (9-agent roster), `setup.sh` (config scaffold), `uninstall.sh` (full cleanup), `e2e_smoke_test.sh` |

---

## 2. Test & Build Commands

| Command | Purpose |
|---------|---------|
| `python -m pytest tests/ -x` | **Canonical test command** — full suite, stop on first failure |
| `python -m pytest tests/ -q` | Full suite, quiet (`make test` runs this + standalone) |
| `python -m pytest tests/ -n auto --timeout=60` | Parallel pytest (CI uses this) |
| `python tests/test_daedalus.py` | Standalone smoke test (custom `check()` runner) |
| `make test` | Full suite: standalone + `pytest tests/ -q` |
| `make lint` | Ruff lint on changed files (`origin/dev...HEAD`) |
| `make e2e` | Offline E2E regression (seeds issue, drives full pipeline) |
| `make e2e-live` | Live smoke against REAL dispatcher (needs `GITHUB_TOKEN`) |
| `python scripts/daedalus_dispatch.py --self-test` | Hermetic GitHub-free pipeline wiring check |
| `make install` | Install runtime + test deps |
| `uv sync` | Install from committed `uv.lock` |
| `bash scripts/provision_roster.sh` | Provision 9 specialist agent profiles (idempotent) |
| `bash scripts/setup.sh` | Scaffold per-repo `.hermes/daedalus.yaml` |

**Notes:**
- Python >=3.10 (CI uses 3.14). `pytest-xdist` and `pytest-timeout` are dev deps in `pyproject.toml`.
- Tests use in-memory `FakeKanban` / `FakeProvider` doubles — no network, no subprocess, no real board.
- The autouse conftest fixture `_isolate_hermes_home` forces `HERMES_HOME` to tmp dir and stubs `core.kanban._hk`. Without it, tests write to the LIVE board and trigger runaway agent spawns.
- `@pytest.mark.uses_real_hk` opts out of the `_hk` stub (still guarded by `_guard_test_isolation`).

---

## 3. Key Invariants

### Branch structure
- **`dev` is the base/integration branch.** All feature/fix PRs target `dev`.
- `main` is release-only. Never branch off `main` for feature work.
- Branch naming: `fix/issue-N-description`, `feat/...`, `docs/...`.
- Commit conventions: `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`. Only `feat:` and `fix:` appear in release changelogs.

### Plugin deployment model
- Daedalus is an **installable Hermes plugin** at `~/.hermes/plugins/daedalus/`.
- The dispatcher runs the INSTALLED plugin — fixes go live only after release + `hermes plugins update daedalus`. Edits to the repo are not live until deployed.
- **Do NOT insert the plugin dir onto the global `sys.path`** — it shadows Hermes core modules. Enforced in `__init__.py` docstring.
- `plugin.yaml` is the manifest. Do NOT add `requires_env` for per-provider tokens — it hard-disables the plugin when any var is missing.

### SOUL.md location
- **Source SOULs** live in `config/souls/<role>-daedalus.md` within this repo (version-controlled).
- **Deployed SOULs** live in `~/.hermes/profiles/<role>-daedalus/` under the user home — NOT in this repo. The dispatcher runs the installed plugin, so SOUL changes go live only after `provision_roster.sh` re-copies them (or release + `hermes plugins update daedalus`).
- 9 profiles: `validator`, `planner`, `project-manager`, `developer`, `qa`, `reviewer`, `security-analyst`, `accessibility`, `documentation` (all suffixed `-daedalus`).
- SOULs define role, workflow, completion signals, escalation. Consumed by `classify_blocked()` in `core/iterate.py` via substring matching.

### Pipeline stages (invariant order)
```
Validator → PM → Developer → QA → Reviewer + Security-Analyst + Accessibility (UI only) → Documentation
```
- Validator runs alone as Phase 1. Downstream tasks NOT created until validator emits `CONFIRMED:`.
- Six validator outcomes: `CONFIRMED`, `ALREADY_FIXED`, `DUPLICATE`, `NEEDS_MORE_INFO`, `SECURITY_THREAT`, `BLOCK_FOR_REVIEW`.
- QA gates reviewer/security/accessibility — those tasks are created with `--parent <QA_TASK_ID>`.
- `MAX_FIX_ATTEMPTS = 3` before escalation (developer, reviewer, security-analyst).
- Documentation is terminal — no fix-attempt loop. `docs posted` → APPROVE_ADVANCE.

### Human-only gates (never automate)
1. **Move issue to Ready** — only a human re-marks an issue Ready after `NEEDS_MORE_INFO` reporter response.
2. **Merge PRs** — agents never merge. `⛔ NEVER merge the PR` is enforced in task bodies (dispatch ~L2289, L2618, L2750). `auto_merge=true` in `daedalus.yaml` makes the **dispatcher** (not the agent) merge after all stages pass.
3. **Close issues** — issues stay open until the linked PR is reviewed and merged. Do NOT auto-close GitHub issues.

### VCS provider abstraction
- All VCS access through `VCSProvider` ABC (`core/providers/base.py`).
- Three implementations: GitHub, GitLab, Azure DevOps.
- Provider methods **never raise** — log warning + return falsy default.
- Tokens come ONLY from env vars, never from YAML.

### Config system
- Per-repo: `<repo>/.hermes/daedalus.yaml` (scaffolded by `setup.sh` or dashboard).
- Template: `templates/daedalus.yaml` (deep-merged — nested dicts merge, lists replaced).
- No secrets in YAML — `vcs.token_env` names the env var.

### Test isolation (critical)
- Every test gets isolated `HERMES_HOME` (tmp dir) via autouse conftest fixture.
- `core.kanban._hk` stubbed to no-op by default — no test spawns real `hermes kanban` CLI.
- `DAEDALUS_DISPATCH_LOCK` set per-xdist-worker to prevent mutex contention.
- Removing these fixtures causes runaway agent spawns from test runs.

### Epic detection thresholds (base.py:127-128)
- `_EPIC_CHECKLIST_MIN = 4` — issues with ≥4 plain checklist items route to planner.
- `_EPIC_BODY_SIZE_MIN = 2000` — issues with ≥2000 char bodies + decomposition language route to planner.
- Single-AC bugs and sub-issues are excluded.

### Dashboard
- `dashboard/dist/` is COMMITTED — needed for plugin to work without a build step. Do NOT gitignore.
- `dashboard/plugin_api.py` is the FastAPI backend. `dashboard/src/App.jsx` is the React frontend.

---

## 4. Agent-Maintainer Guide

### Where to start
1. **`CONTRIBUTING.md`** — branching, commits, PR guidelines, release process.
2. **`SETUP.md`** — roster provisioning, project connection, known gotchas.
3. **`README.md`** — comprehensive feature docs (~111K).
4. **`docs/INSTALLATION_GUIDE.md`** — full step-by-step installation.
5. **`tasks/lessons.md`** — hard-won lessons about not reinventing Hermes features.
6. **`config/souls/<role>-daedalus.md`** — each agent's system prompt.
7. **`templates/daedalus.yaml`** — full config template with comments.
8. **`core/iterate.py`** — self-healing routing (`classify_blocked` + executors).
9. **`scripts/daedalus_dispatch.py`** — the dispatcher (cron, polling, board management).

### What not to touch
- **`__init__.py` docstring warning**: Do NOT add plugin dir to global `sys.path`.
- **`plugin.yaml`**: Do NOT add `requires_env` for per-provider tokens.
- **`dashboard/dist/`**: Do NOT gitignore the committed bundle.
- **`requirements.txt`**: Runtime deps only. Test tooling (pytest, ruff) goes in `pyproject.toml` dev deps.
- **`tests/conftest.py` autouse fixtures**: `_isolate_hermes_home` and `_hk` stub are safety-critical.
- **`kanban.db`** (repo root): Empty placeholder. Real DB is `~/.hermes/kanban.db`.
- **`.hermes/`**: Gitignored runtime state. `doc_sweep_state.json` lives here but is NOT committed.
- **SOUL completion signals**: Dispatcher uses exact substring matching. Changing a signal (e.g. `docs posted`, `CONFIRMED:`) without updating `classify_blocked()` in `core/iterate.py` breaks the pipeline.

### Common pitfalls
1. **Reinventing Hermes features** — check `hermes kanban --help` before building orchestration primitives. See `tasks/lessons.md`.
2. **Tests writing to live board** — if `HERMES_HOME` isn't isolated, tests create real cards → runaway agent spawns.
3. **xdist lock contention** — `DAEDALUS_DISPATCH_LOCK` must be per-worker. Don't override it in new dispatch tests.
4. **`kanban_block` vs `kanban_complete`** — developer task must *complete* (not block) to chain to reviewer/security. Use `kanban_block("review-required: ...")` only when human review is needed.
5. **Git credentials per-profile** — worker terminals don't inherit `.env`. Credentials must live in `~/.git-credentials` (via `store` helper). `provision_roster.sh` handles this.
6. **`--workspace worktree:<path>`** — not honored precisely. Pre-create worktree from `origin/dev` and pass `--workspace dir:<path>` instead.
7. **Broken symlinks in `~/.hermes/skills/`** — aborts `profile create`. Remove the symlink first.
8. **`hermes profile create --no-skills` + `--clone`** — mutually exclusive. Provisioner clones then nukes+reseeds.
9. **Uninstall** — use `scripts/uninstall.sh`, NOT `hermes plugins uninstall daedalus` alone (leaves profiles, cron, boards, hooks behind).
10. **SOUL signal phrasing** — `docs posted` → APPROVE_ADVANCE. `documentation complete:` → PM_ROUTE (wasted round-trip). Exact substrings only.

### Code conventions
- Python >=3.10, `from __future__ import annotations` at top of every module.
- Type hints throughout. `logging.getLogger("daedalus.<module>")` for loggers.
- Provider methods never raise — log + return falsy.
- Ruff for linting (no flake8/black). `xfail_strict = true`.
- Tests co-located in `tests/` (150+ files). E2E tests separate.

### Directory layout
```
daedalus/
├── __init__.py              # Plugin entry point
├── plugin.yaml              # Plugin manifest
├── pyproject.toml           # Project config (pytest, deps)
├── requirements.txt         # Runtime deps only
├── Makefile                 # Developer targets
├── config/
│   ├── __init__.py          # ConfigLoader
│   └── souls/               # Agent SOULs per role
├── core/
│   ├── iterate.py           # Self-healing routing
│   ├── kanban.py            # Hermes kanban CLI wrapper
│   ├── crash_retry.py       # Crash retry logic
│   ├── dispatch_state.py    # State persistence
│   ├── provider_failover.py # Cross-provider failover
│   ├── sweeper.py           # Stale-card sweeper
│   ├── tier_promotion.py    # Epic DAG tier promotion
│   ├── notification_sender.py
│   ├── notify_templates.py
│   ├── thread_delivery.py
│   ├── webhook_dispatch.py
│   ├── webhook_normalizer.py
│   ├── workspace.py
│   ├── registry.py
│   ├── file_overlap.py
│   ├── source_specs.py
│   ├── db.py / util.py / cli.py
│   └── providers/
│       ├── base.py          # VCSProvider ABC
│       ├── github.py / gitlab.py / azure_devops.py
│       ├── detect.py / http.py
├── dashboard/
│   ├── plugin_api.py        # FastAPI backend
│   ├── src/                 # React frontend
│   └── dist/                # COMMITTED build output
├── docs/
├── scripts/
│   ├── daedalus_dispatch.py # Main dispatcher (~9200 lines)
│   ├── provision_roster.sh / setup.sh / uninstall.sh
│   ├── agent_comment.py / watchdog.py / postinstall.py
│   └── *.sh                 # Advance, webhook, worktree scripts
├── templates/
│   ├── daedalus.yaml          # Default config
│   └── agent_bodies/          # Agent prompt-body templates (#1147)
├── tasks/                   # Lessons, plans, specs
└── tests/                   # 150+ test files
```
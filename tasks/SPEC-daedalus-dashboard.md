# Spec — Daedalus Dashboard (Hermes dashboard plugin)

**Status:** proposed — review before build.
**Goal:** edit daedalus config, watch live pipeline status, and control the dispatch
cron from the Hermes dashboard UI. Clean-slate rebuild covering everything the
daedalus supports today.

---

## 1. Why

Today the config lives in `daedalus.yaml`, status lives in `hermes kanban`, and the
cron is driven from the CLI. Onboarding a repo or checking "what's the fleet doing right
now?" means editing YAML by hand and running three CLIs. The dashboard makes the
daedalus **operable by a human in one place** — without touching YAML or remembering
commands.

It is a **thin UI over existing surfaces** — it adds no orchestration logic. The native
dispatch (`daedalus_dispatch.py`), hooks, and cron remain the source of truth; the
dashboard only **reads and writes** them.

---

## 2. Architecture (Hermes dashboard plugin)

```
~/.hermes/plugins/daedalus/        (deployed; repo source = dashboard/)
  manifest.json     → registers the "Daedalus" tab (path /daedalus)
  plugin_api.py     → FastAPI APIRouter, mounted at /api/plugins/daedalus/
  dist/index.js     → built React bundle (esbuild; React is external/global)
```

- **Frontend:** `dashboard/src/App.jsx` → esbuild → `dist/index.js`. React is **never
  bundled** (provided by the dashboard host). `dist/` is generated — never hand-edited.
- **Backend:** `dashboard/plugin_api.py` reuses `config.ConfigLoader` for config and
  shells out to `hermes kanban` / `hermes cron` / `gh` for status. No secrets are read
  or returned.
- **Deploy/reload:** sync `dashboard/` → `~/.hermes/plugins/daedalus/`, then restart
  the **dashboard web server** (not the gateway). Edits are not live until both happen.

---

## 2.5 Source modes — GitHub Project board is OPTIONAL

The kanban board is the **universal tracker**; the GitHub Project board is an **optional
input/status adapter**. Each project runs in one of two modes, auto-detected by whether
`tracking.github_project_number` is set. **This requires a dispatcher change** (not just
the dashboard) — today, a missing board silently falls back to "dispatch every open
issue by label," which is wrong; it must fall back to the kanban board instead.

| | **GitHub-Project mode** (`github_project_number` set) | **Kanban-only mode** (no board) |
|---|---|---|
| Readiness signal | GitHub issues in the **`Ready`** column | Kanban cards a human placed in **`ready`/`triage`** |
| Who creates the card | dispatcher (from the Ready issue) | the human (dashboard "New card" or `hermes kanban create`) |
| Status tracking | mirrored to the GitHub board **and** the kanban board | the **kanban board only** |
| Issue/PR + ship-gate + decompose + auto-advance | ✅ same | ✅ same |
| merged → close | closes the linked GitHub **issue** (if the card references one) | same, if the card references an issue; otherwise just completes the card |

So GitHub Projects adds a nicer human-facing board + cross-tool sync, but is never
required: with no board, you drive everything from the kanban board (and the dashboard).

**Dispatcher change (summary):** in `run()`, when `ghproj` is `None`, source new work
from kanban cards in `ready`/`triage` (decompose + dispatch them) instead of polling
GitHub issues; skip GitHub-Project status moves; keep ship-gate, decompose, auto-advance,
and merged→close-on-issue. Add a unit test per mode.

---

## 3. API (`/api/plugins/daedalus/`)

| Method · path | Purpose | Source |
|---|---|---|
| `GET /config` | `{ defaults, projects[], meta:{ profiles[], slack_targets[], path } }` | `ConfigLoader` + `hermes send --list` + profiles dir |
| `POST /config` | validate + persist `daedalus.yaml` (full doc) | `ConfigLoader.save` |
| `POST /config/project` | add / edit / clone / remove one project | `ConfigLoader.*` |
| `GET /status?project=` | per-project: kanban cards grouped by status (**always**); GitHub board counts **only if a board is configured**; last dispatch summary; `mode: github\|kanban` | `hermes kanban … list --json`, `gh project item-list` (when board set) |
| `POST /card?project=` | **kanban-only mode:** create a `triage`/`ready` card (the human's "make this Ready") | `hermes kanban … create --triage` |
| `POST /dispatch/preview?project=` | run the dispatch `--dry-run` and return the summary (no mutations) | `daedalus-dispatch.sh --dry-run` |
| `GET /cron` | job(s): schedule, next run, last_status, active/paused | `hermes cron list` |
| `POST /cron/{run\|pause\|resume}` | control the dispatch cron | `hermes cron run/pause/resume` |

**Validation on write:** required `name`/`repo`/`workdir`; `github_project_number` numeric;
`worker_profile` ∈ roster; warn if `workdir` doesn't exist. Never accept secret fields.

---

## 4. UI layout (`App.jsx`)

```
┌ Daedalus ─────────────────────────────────────────────── [ Defaults ] [ + Project ] ┐
│                                                                                          │
│  PROJECTS                          STATUS (selected: app-one)                            │
│  ┌───────────────────────────┐     ┌──────────────────────────────────────────────────┐ │
│  │ ● app-one      board ✓     │     │  Cron: every 120m · next 14:56 · last_status ok  │ │
│  │   ORG/app-one  Ready: 2    │     │        [ Run now ] [ Pause ]                      │ │
│  ├───────────────────────────┤     │                                                  │ │
│  │   api-two      board ✓     │     │  GitHub board   Ready 2 · In progress 1 · Review │ │
│  │   ORG/api-two  Ready: 0    │     │                 3 · Done 51                       │ │
│  └───────────────────────────┘     │                                                  │ │
│                                     │  Kanban cards                                    │ │
│  EDIT: app-one                      │   ● developer  running   #329 …                  │ │
│  repo            [ORG/app-one    ]  │   ◻ reviewer   todo      Review PR …             │ │
│  workdir         [/path/to/app   ]  │   ◻ security   todo      Audit PR …              │ │
│  Project #       [1   ]             │                                                  │ │
│  worker profile  [developer  ▾]     │  Last dispatch (dry-run)   [ Preview ]           │ │
│  base branch     [dev        ▾]     │   created [] · reconciled [#344→In review] …     │ │
│  cron schedule   [every 120m    ]   └──────────────────────────────────────────────────┘ │
│  Slack deliver   [slack:dycotomic▾]                                                       │
│  labels          [bug, ready     ]   [ Save ]  [ Duplicate ]  [ Remove ]                  │
│  kanban enabled  [x]                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Left:** project list (status dot + board-exists + Ready count). Click to select/edit.
- **Edit panel:** every current field, with dropdowns populated from the roster
  (`worker_profile`) and Slack targets (`cron.deliver`). Add / Duplicate / Remove.
  `Project #` is **optional** — leaving it blank puts the project in **Kanban-only mode**
  (the panel shows a "no GitHub board — kanban is the tracker" note).
- **Defaults** modal: same fields, shared baseline.
- **Right (status):** cron line + controls, live kanban cards, and a "Preview" dry-run.
  - **GitHub-Project mode:** also shows the GitHub board counts (Ready / In progress / …).
  - **Kanban-only mode:** GitHub counts are hidden; a **[ + New card ]** button lets you
    drop a `triage`/`ready` card straight onto the board (the kanban equivalent of moving
    an issue to Ready).

---

## 5. Fields exposed (the full current surface)

Per project (and in defaults): `repo`, `workdir`, `tracking.github_project_number`,
`execution.worker_profile`, `cron.schedule`, `cron.deliver`, `vcs.target_branch`,
`issues.filters.labels`, `lifecycle.kanban.enabled`. (Ship-gate per-repo policy lives in
`agent-hooks/ship-gate.d/` — **out of scope v1**, noted below.)

---

## 6. Out of scope (v1) / risks

- **Ship-gate skip/checks editing** — lives outside `daedalus.yaml`; a later "Gate"
  tab could edit `ship-gate.d/<repo>.{skip,checks.sh}`. Not in v1.
- **Secrets** — never shown or edited; tokens stay in gitignored `.env`.
- **Auto-creating the GitHub Project board** — separate feature; dashboard assumes it
  exists and you paste its number.
- **Reload friction** — dashboard restart required after deploy (documented).
- **Status cost** — `/status` shells out to `gh`/`hermes`; cache briefly to avoid hammering.

---

## 7. Build plan (after approval)

0. **Dispatcher dual-mode** (`daedalus_dispatch.py`) — make `github_project_number`
   truly optional: kanban-only mode sources work from `ready`/`triage` cards instead of
   GitHub issues; no GitHub status moves; keep ship-gate/decompose/auto-advance/merged→
   close. Unit-test both modes. (Prereq for the dashboard's two-mode behavior.)
1. `dashboard/plugin_api.py` — `/config` (GET/POST) first; verify round-trip against a
   temp `daedalus.yaml`.
2. `dashboard/manifest.json` + minimal `App.jsx` (config CRUD) → esbuild → deploy →
   confirm the tab loads and edits persist.
3. Add `/status` + the status panel (kanban + GitHub counts + dry-run preview).
4. Add `/cron` + run/pause/resume controls.
5. Update `SETUP.md` (deploy/reload step) and `README.md` (dashboard mention).

Each step is independently verifiable in the live dashboard before the next.

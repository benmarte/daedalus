# Native Hermes autonomous issue→PR pipeline — team setup

This repo provisions a **lean roster of specialist Hermes agents** that take a GitHub Project
"Ready" issue and drive it to a reviewed, ready-to-merge PR — using **native Hermes Kanban**
(decompose → role profiles → dispatch → review), no custom plugin.

Roster: **project-manager · planner · developer · reviewer · security-analyst · documentation**
(each loads only its lifecycle agent-skills). See `tasks/RUNBOOK-native-pipeline.md` for the run flow.

## Sharing model (important)
- **Share = this git repo.** It is secret-free. Everyone reproduces the roster locally with their
  own keys via `scripts/provision_roster.sh`.
- **Do NOT share `hermes profile export` tarballs** — they bundle `config.yaml`/`.env`/`gh` tokens
  (LLM keys + GitHub PAT). Those are per-person secrets.
- Recommended: host this repo under the **`RIZQ-TECH` org** so colleagues can clone it.

## Prerequisites (each colleague, once)
1. **Hermes Agent** installed + gateway running (`hermes gateway` / `hermes gateway install`).
2. The **`agent-skills` plugin** installed (provides the lifecycle skills at
   `~/.hermes/plugins/agent-skills/skills/`).
3. A working **`default` profile** with their own LLM provider keys (model used here:
   `deepseek-v4-pro`; any capable model works — the script clones config/keys from `default`).
4. **GitHub CLI** authenticated: `gh auth login` (needs `repo`, `workflow`, `project` scopes).

## Provision the roster
```bash
git clone <this-repo> && cd daedalus
bash scripts/provision_roster.sh        # idempotent — re-run any time to reset to spec
hermes profile list                     # expect the 6 roles
```
What it does: creates the 6 profiles (cloning config/keys from `default`), seeds each with **only**
its matrix agent-skills, and authenticates `gh` **into each profile's isolated HOME**
(`gh auth login --with-token` — a profile `.env` is NOT enough; the kanban worker runs `gh` via the
`terminal` tool whose shell ignores `.env`).

## Connect a project (per repo/board)
The pipeline reads a **GitHub Project** board and writes to a **Hermes Kanban board**.
1. Find your Project's IDs: `gh project list --owner <ORG>`, then `gh project field-list <N> --owner <ORG> --format json` for the Status field + option IDs.
2. Hermes board: `hermes kanban boards` (a board slug per project).
3. Record them like `tasks/RUNBOOK-native-pipeline.md` does (board node id, Status field id, option ids).

## Project conventions the agents MUST follow (dycotomic example)
> These are repo-specific. Encode them in the triage-card body and the B4 ingestion template.
- **Branch off `dev`, open PRs into `dev`** — CI (`ci.yml`) only runs on PRs to `dev`.
  `main` is promote-only (release-please). **Never PR into `main`** (no CI runs there).
- **Create the worktree from `origin/dev`** (not `main` — `main` can be hundreds of commits stale).
- **Run the quality gates before committing**: `pre-commit run --all-files`
  (black, isort, flake8, biome lint+format, pytest, typecheck). A fresh git worktree has **no hooks
  installed**, so `git commit` skips them unless you run `pre-commit install` / `pre-commit run`.

## Run it
- **Manual / spike:** follow `tasks/RUNBOOK-native-pipeline.md` (promote an issue to Ready →
  create worktree-pinned triage card → dispatcher decomposes → roster works it → PR).
- **Automated (cron):** the B4 section of the runbook — poll Project "Ready" items, ingest, mirror
  status (one-way Hermes→GitHub). Build after the manual flow is proven on `dev`.

## Known gotchas (captured the hard way)
- `gh` auth must be **per-profile-HOME**, not `.env` (see above).
- `hermes profile create --no-skills` is **mutually exclusive with `--clone`** — the script clones
  then nukes+reseeds skills.
- A **broken symlink in `~/.hermes/skills/`** aborts every `profile create` (copytree fails) — remove it.
- `--workspace worktree:<path>` is **not honored precisely** — pre-create the worktree from `origin/dev`
  and pass `--workspace dir:<path>` instead.
- The built-in kanban worker tends to **`kanban_block` ("review-required")** instead of
  `kanban_complete`; to chain to the reviewer/security tasks, the developer task must *complete*.

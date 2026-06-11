# Native Hermes autonomous issue→PR pipeline — team setup

This repo provisions a **lean roster of specialist Hermes agents** that take a
"Ready" issue — from **GitHub, GitLab, or Azure DevOps** — and drive it to a reviewed,
ready-to-merge PR — using **native Hermes Kanban**
(decompose → role profiles → dispatch → review).

Roster: **project-manager · planner · developer · reviewer · security-analyst · documentation**
(each loads only its lifecycle agent-skills).

## Sharing model (important)
- **Share = this git repo.** It is secret-free. Everyone reproduces the roster locally with their
  own keys via `scripts/provision_roster.sh`.
- **Do NOT share `hermes profile export` tarballs** — they bundle `config.yaml`/`.env`/
  `.git-credentials` (LLM keys + VCS PATs). Those are per-person secrets.
- Recommended: host this repo under your org so colleagues can clone it.

## Prerequisites (each colleague, once)
1. **Hermes Agent** installed + gateway running (`hermes gateway` / `hermes gateway install`).
2. The **`agent-skills` plugin** installed (provides the lifecycle skills at
   `~/.hermes/plugins/agent-skills/skills/`).
3. A working **`default` profile** with their own LLM provider keys (any capable
   model works — the script clones config/keys from `default`).
4. A **VCS API token in the environment** for each provider you use:
   `GITHUB_TOKEN` (fine-grained PAT), `GITLAB_TOKEN` (`api` scope), or
   `AZURE_DEVOPS_PAT` (Work Items R&W, Code Read, PR R&W, Build Read).
   That token covers everything — dispatcher polling, dashboard pickers, worker
   `git push` (per-profile credential store), and PR/comment API calls. No
   `gh`/`glab`/`az` CLI is needed or used.

## Provision the roster
```bash
git clone <this-repo> && cd daedalus
bash scripts/provision_roster.sh        # idempotent — re-run any time to reset to spec
hermes profile list                     # expect the 6 roles
```
What it does: creates the 6 profiles (cloning config/keys from `default`), seeds each with **only**
its matrix agent-skills, writes a **per-profile git credential store** (`~/.git-credentials`,
0600, keychain-free) so `git push` works inside each isolated HOME, and drops the provider
tokens into each profile `.env` for API calls (open PR / comment via curl).

## Connect a project (per repo/board)
The easiest path is the dashboard's **“+ Add Project”** button (scaffolds config,
registers the repo, creates its kanban board + cron). From the terminal:
`cd <repo> && bash ~/.hermes/plugins/daedalus/scripts/setup.sh`, then set
`vcs.provider` and board tracking in `<repo>/.hermes/daedalus.yaml`:
- **GitHub:** `tracking.github_project_number: <N>` (Projects v2 board number).
- **GitLab:** `tracking.label_board: true` — board lists keyed to the
  `vcs.status_map` labels; self-hosted via `vcs.base_url`.
- **Azure DevOps:** `vcs.org` / `vcs.project` / `vcs.repo` — board columns map to
  work-item states.

## Project conventions the agents MUST follow (example)
> These are repo-specific. Encode them in the triage-card body (the dispatcher's
> `vcs.target_branch` drives the base branch) — the roster provisioner stays
> project-agnostic and seeds no per-project conventions.
- **Branch off your integration branch** (e.g. `dev`) and open PRs into it when
  `main` is promote-only — set `vcs.target_branch` accordingly.
- **Run the quality gates before committing**: `pre-commit run --all-files`.
  A fresh git worktree has **no hooks installed**, so `git commit` skips them
  unless you run `pre-commit install` / `pre-commit run`.

## Run it
- **Manual / spike:** promote an issue to Ready → the next cron tick creates a
  worktree-pinned triage card → dispatcher decomposes → roster works it → PR.
- **Automated (cron):** each project gets its own cron job (created by setup.sh /
  the dashboard); edits update the job in place.

## Known gotchas (captured the hard way)
- git credentials must live **per-profile-HOME** (`~/.git-credentials` via the `store`
  helper), not only in `.env` — the worker's `terminal` shell does not inherit `.env` vars.
- `hermes profile create --no-skills` is **mutually exclusive with `--clone`** — the script clones
  then nukes+reseeds skills.
- A **broken symlink in `~/.hermes/skills/`** aborts every `profile create` (copytree fails) — remove it.
- `--workspace worktree:<path>` is **not honored precisely** — pre-create the worktree from `origin/dev`
  and pass `--workspace dir:<path>` instead.
- The built-in kanban worker tends to **`kanban_block` ("review-required")** instead of
  `kanban_complete`; to chain to the reviewer/security tasks, the developer task must *complete*.

## Uninstall

To completely remove Daedalus (profiles, cron jobs, kanban boards, config, and the plugin):

```bash
bash ~/.hermes/plugins/daedalus/scripts/uninstall.sh
```

> **Do NOT use `hermes plugins uninstall daedalus` alone** — that only deletes
> the plugin directory and leaves profiles, cron jobs, boards, config, and
> hook artifacts behind. Hermes has no uninstall hook for plugins to clean up
> after themselves. Use `uninstall.sh` for a complete uninstall.

See the [README](README.md#uninstall--reset) for all options (`--keep-plugin`, `--keep-profiles`, `-y`).

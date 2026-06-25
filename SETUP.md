# Native Hermes autonomous issue→PR pipeline — team setup

This repo provisions a **lean roster of specialist Hermes agents** that take a
"Ready" issue — from **GitHub, GitLab, or Azure DevOps** — and drive it to a reviewed,
ready-to-merge PR — using **native Hermes Kanban**
(decompose → role profiles → dispatch → review).

Roster: **validator · project-manager · developer · qa · reviewer · security-analyst · accessibility · documentation**
(each loads only its lifecycle agent-skills). `accessibility` is conditional — only invoked when the issue references UI/frontend work.

The **validator** runs alone as Phase 1 on every issue — the dispatcher creates only the validator
task initially. Developer, reviewer, security-analyst, and documentation tasks are not created until
the validator completes with a `CONFIRMED:` summary. This is enforced at the infrastructure level:
downstream tasks simply don't exist until the validator decides. Six outcomes: **CONFIRMED** (triggers
Phase 2 — downstream tasks created on next tick), **ALREADY_FIXED** (closes issue), **DUPLICATE**
(closes issue), **NEEDS_MORE_INFO** (blocks, comments asking reporter), **SECURITY_THREAT** (blocks,
posts issue comment, sends security-escalation notification), **BLOCK_FOR_REVIEW** (high-privilege
request lacking verifiable context — blocks, posts comment listing missing details, sends
security-escalation notification). All blocking outcomes auto-move the VCS board card to "Blocked",
creating the column automatically if needed. All roles post a mandatory summary comment on the
GitHub issue after completing their step.

## Sharing model (important)
- **Share = this git repo.** It is secret-free. Everyone reproduces the roster locally with their
  own keys via `scripts/provision_roster.sh`.
- **Do NOT share `hermes profile export` tarballs** — they bundle `config.yaml`/`.env`/
  `.git-credentials` (LLM keys + VCS PATs). Those are per-person secrets.
- Recommended: host this repo under your org so colleagues can clone it.

## Prerequisites (each colleague, once)
1. **Hermes Agent** installed + gateway running (`hermes gateway` / `hermes gateway install`).
2. The **`agent-skills` plugin** (provides the lifecycle skills at
   `~/.hermes/plugins/agent-skills/skills/`) — installed **automatically** by
   `provision_roster.sh` if missing; no manual step needed.
3. A working **`default` profile** with their own LLM provider keys (any capable
   model works — the script clones config/keys from `default`).
4. A **VCS API token** for each provider you use:
   `GITHUB_TOKEN` (fine-grained PAT), `GITLAB_TOKEN` (`api` + `write_repository`),
   or `AZURE_DEVOPS_PAT` (Work Items R&W, Code R&W, Build Read) — exact
   permission lists in the README's
   [Creating the tokens](README.md#creating-the-tokens-pat-scopes) section.
   That token covers everything — dispatcher polling, dashboard pickers, worker
   `git push` (per-profile credential store), and PR/comment API calls. No
   `gh`/`glab`/`az` CLI is needed or used.

   **Where tokens go:**
   - Add them to **`~/.hermes/.env`** (e.g. `GITHUB_TOKEN=ghp_...`) — Hermes loads
     this file at startup, which covers the dispatcher cron and the dashboard.
     The cron wrapper also sources this file explicitly so tokens are available
     even when cron does not inherit your shell environment.
     Restart the gateway + dashboard after editing it.
   - **Export them in your shell before running `provision_roster.sh`** — the
     provisioner copies them into each worker profile's `.env` +
     `.git-credentials`, and adds them to `terminal.env_passthrough` so the
     workers' terminal shells can actually see them.

   > **Token validation:** `provision_roster.sh` validates each token before
   > writing it to any profile. It rejects values that look masked or hashed
   > (e.g. `SHA256:...` or `***`), and requires `GITHUB_TOKEN` to start with a
   > known prefix (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`). If your token is
   > sourced from a CI/CD secret store it may be redacted — always export the
   > **raw, unmasked value** before running the provisioner. Missing tokens are
   > allowed (kanban-only setups don't need them) and produce only an advisory
   > note.

## Provision the roster
```bash
git clone <this-repo> && cd daedalus
bash scripts/provision_roster.sh        # idempotent — re-run any time to reset to spec
hermes profile list                     # expect the 9 roles
```
What it does: creates the 9 profiles (cloning config/keys from `default`), seeds each with **only**
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

## Configure the coding agent (optional)
By default every role works with the **local Hermes LLM** (your `default` profile's model).
To instead delegate the implementation/review to an external CLI coding agent, add an
`execution` block to `<repo>/.hermes/daedalus.yaml`:

```yaml
execution:
  coding_agent: claude-code
  coding_agent_cmd: "CLAUDE_CONFIG_DIR=$HOME/.claude claude --dangerously-skip-permissions -p"
```

![.hermes/daedalus.yaml showing the execution block with coding_agent: claude-code and a per-role override](docs/screenshots/guide/14-coding-agent-config.png)

- Supported values: `claude-code`, `codex`, `opencode`, or `hermes` (the default).
- **Omitting `coding_agent` (or setting it to `hermes`/`none`) keeps everything on the
  local Hermes LLM** — no external agent is spawned.
- `coding_agent_cmd` is the full shell command the task body is piped into; omit it to use
  the per-agent default (`claude --dangerously-skip-permissions -p`, `codex exec
  --full-auto`, `opencode run`).
- Per-role override: set `execution.profiles.<role>.agent` (e.g. `developer: claude-code`,
  `validator: hermes`) to mix delegated and local roles.

When enabled, the dispatcher injects a `⚠️ AGENT DELEGATION` block into the delegating
role's task and auto-attaches the matching `autonomous-ai-agents/<agent>` skill, so the
local LLM pipes the task to the coding agent and relays its output as the completion signal.
See the [README](README.md#delegating-to-claude-code-or-codex) for the full reference.

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

> **If the cron silently does nothing:** the job invokes
> `~/.hermes/scripts/daedalus-cron.sh`. The plugin now (re)installs that wrapper
> automatically every time Hermes loads daedalus, so a fresh `hermes plugin add`
> + gateway restart is enough. If you hit a stale/missing wrapper on an older
> install (before this fix), re-create it immediately with:
> ```bash
> python3 ~/.hermes/plugins/daedalus/scripts/postinstall.py --check
> ```

## Pipeline lifecycle

```
Validator → PM → Developer → QA → Reviewer + Security-Analyst + Accessibility (UI only) → Documentation
```

The PM agent creates all downstream tasks directly via `hermes kanban create` with idempotency keys (`developer-N`, `qa-N`, `reviewer-N`, `security-N`, `docs-N`). QA gates the reviewer/security/accessibility stages — those tasks are created with `--parent <QA_TASK_ID>` and only become ready after QA completes.

**Validator None-summary recovery.** If a validator agent's context window fills before `kanban_complete(summary=...)` runs, its kanban summary is `None`. The dispatcher automatically:
1. Scans the GitHub issue for a comment with `**Agent: validator**` attribution and looks for `CONFIRMED` in the body — if found, advances to PM without re-running the validator (github-comment fallback).
2. If no confirming comment exists, retries the validator up to 2 times (`validator-retry-N-r1`, `validator-retry-N-r2`).
3. After 2 retries with no CONFIRMED outcome, escalates to human review.

The validator SOUL.md also instructs the agent to verify its summary was written after `kanban_complete` and retry if `latest_summary` is null.

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

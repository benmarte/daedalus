# Daedalus — autonomous issue → PR pipeline on native Hermes

Flag a GitHub issue **Ready**, and a roster of AI agents implements it, reviews it,
security-audits it, documents it, and opens a **green, mergeable PR** — with quality
gates that *cannot* be skipped, full board/issue tracking, and zero babysitting.
A single Daedalus deployment drives **many repos**.

```
GitHub issue → "Ready"
      │  (cron tick — deterministic, code)
      ▼
   triage card ──decompose──► developer ─► reviewer ─► security ─► documentation
      │                          │            │           │            │
   board: In progress       opens PR     approves     audits     posts report
                            (ship-gate                            to issue + Slack
                             enforced)
      ▼
   PR green → you merge → issue auto-closed, card → Done
```

---

## Why this exists (read this part)

AI coding agents are powerful but, left alone, **chaotic**. On a single issue this
project watched an unmanaged setup produce **nine overlapping PRs**, open PRs that
**failed CI** (lint, typecheck), **skip tests**, lose track of what was done, and
need constant human babysitting. The agents were good at *writing code* — and bad
at *running a process*.

The fix is a hard separation of concerns:

> **Deterministic code decides _what_ happens and _when_. Agents only decide _how_ the code is written.**

Everything that must be reliable — what gets worked, in what order, what quality bar
a PR must clear, how status is tracked, when an issue closes — is **plain code in this
repo**, so it can never be skipped or forgotten. The only agent-driven part is the
actual engineering inside each task. That single boundary is what turns "impressive
demo" into "a tool the team can depend on."

### What that buys you

| Without this | With this |
|---|---|
| One issue spawns 9 PRs | One issue → one tracked card → one PR |
| Agent "forgets" to lint → red PR | **Ship-gate** blocks `gh pr create` until backend **and** frontend lint/typecheck pass |
| A single agent marks its own work done | **Decompose** into developer → reviewer → security → documentation |
| You babysit every handoff | **Auto-advance**: each stage completes on green CI and flows to the next |
| Issues merged to `dev` stay open forever | Dispatcher **closes the issue + moves card to Done** on merge |
| "Works on my machine" | One config, checked in, runs on any teammate's Hermes |

These aren't aspirations — every one was a real failure this pipeline hit and then
closed off in code. The reasoning behind each is in [Design decisions](#design-decisions).

---

## How it works

1. **You** drag a GitHub issue to the **`Ready`** column on its Project board. That's
   the only manual step — nothing else moves without it.
2. A **cron tick** runs `daedalus_dispatch.py` (`--no-agent`, pure code). It:
   - selects **only `Ready`** issues (and skips any that already have a PR),
   - flips the board to **In progress**, creates a **triage card**, and **decomposes**
     it across the roster.
3. **Agents** (Hermes kanban workers) execute their tasks:
   - **developer** implements + tests, then must pass the **ship-gate** to open a PR,
   - **reviewer** reviews, **security-analyst** audits, **documentation** writes a
     completion report and posts it to the **GitHub issue and Slack**.
4. Each tick **auto-advances** any stage that's blocked on review once its PR's CI is
   green — the chain flows hands-off.
5. When you **merge** the PR, the next tick sets the card **Done** and **closes the
   issue** (GitHub doesn't auto-close on a non-default-branch merge, so the dispatcher
   does it).

The board and GitHub status are bookkept **in code on every tick**, so tracking is
deterministic — never dependent on an agent remembering to update anything.

---

## Design decisions

Each piece exists because the obvious approach failed:

- **Ready-gating** — the dispatcher works *only* issues you put in `Ready`. You stay in
  control of what the fleet touches; it never surprises you by grabbing the backlog.
- **Ship-gate** (a Hermes `pre_tool_call` hook) — blocks `gh pr create` until the repo's
  own checks pass. It's *language-agnostic* (runs the repo's `pre-commit`) plus a
  per-repo extra-checks script for things CI runs that pre-commit doesn't (e.g. a
  frontend `bun run lint && bun run typecheck`). A "remember to run pre-commit" note in
  agent memory was skipped repeatedly; a gate cannot be.
- **Triage + decompose** — real separation of concerns across specialist agents
  (developer / reviewer / security-analyst / documentation), not one agent grading its
  own homework.
- **Auto-advance** — workers *block for review* instead of completing, which stalls the
  chain. The dispatcher completes a review-required handoff once its PR's CI is green,
  so the pipeline is genuinely hands-off (the PR still waits for a human merge).
- **Self-healing loop** (`core/iterate.py`) — every blocked card is classified into one
  of 5 actions and routed to the agent that can clear it:
    - `advance` — dev PR green + review-required → complete dev card, chain flows to reviewer/security
    - `dev_fix_ci` — CI red → creates idempotent developer fix card
    - `pm_route` — reviewer/security requests changes → creates PM routing card with findings; PM decides owner (developer, security-analyst, re-spec), then fix lands. Reviewer cards are marked "awaiting-fix" and auto-unblocked when the fix completes.
    - `approve_advance` — reviewer/security approved → complete the card
    - `escalate` — cap at 3 fix attempts per PR → log + notify, set card aside (no infinite loop)
  Fix cards are idempotent per `(card, attempt)`. When a dev fix completes, awaiting
  reviewer/security cards are automatically unblocked for re-review. The loop never
  stalls — every blocked card has a deterministic path forward.
- **merged → Done + close** — a PR merged into `dev` doesn't auto-close its issue
  (GitHub only does that on the default branch), so the dispatcher closes it itself —
  and only after confirming no sibling PR is still open.

---

## Multi-repo: one daedalus, many repos

There is **one** daedalus and **one** agent roster. Every repo you want it to drive
is an entry in `daedalus.yaml`'s `projects[]`, inheriting shared `defaults` and
overriding what it needs (its own board, Slack channel, base branch, gate policy):

```yaml
defaults:
  vcs: { target_branch: dev }
  lifecycle: { kanban: { enabled: true } }
projects:
- name: app-one
  repo: ORG/app-one
  workdir: /path/to/app-one
  tracking: { github_project_number: 1 }
  cron: { deliver: slack:C0CHANNEL1 }
- name: api-two
  repo: ORG/api-two
  workdir: /path/to/api-two
  tracking: { github_project_number: 4 }
  cron: { deliver: slack:C0CHANNEL2 }
  vcs: { target_branch: main }      # overrides the default
```

One cron tick processes every project; each repo gets its **own kanban board**
automatically and its **own ship-gate policy** (keyed by the repo's origin remote).
Onboarding a repo = ~6 lines in `projects[]` + a `Ready` column on its board.

---

## Repository layout

| Path | What it is |
|------|------------|
| `scripts/daedalus_dispatch.py` | The deterministic dispatch tick (cron entrypoint, `--no-agent`). Ready-gating, reconcile, decompose, auto-advance, merged→close. |
| `core/iterate.py` | Self-healing loop: classify blocked cards into 5 actions, idempotent fix-card creation, iteration cap + escalation, reviewer re-engage after fix. |
| `scripts/provision_roster.sh` | Provisions the 6-agent Hermes roster. |
| `core/github_project.py` | GitHub Projects v2 status tracking + PR/CI state, all via `gh`. |
| `core/kanban.py` | Thin, idempotent wrapper over `hermes kanban` (triage, decompose, complete). |
| `config/` | `ConfigLoader` (defaults + `projects[]` merge) and the config template. |
| `tests/` | Unit tests for the config loader. |
| `tasks/RUNBOOK-native-pipeline.md` | Deeper operational reference. |

The **ship-gate hook**, **cron wrapper**, and **roster profiles** live in the Hermes
home (`$HERMES_HOME`), not here — see [`SETUP.md`](SETUP.md) for how they're deployed
and shared across a team.

---

## Prerequisites

Hermes (installed + model auth), `gh` (authed, with **Projects v2** scope — ideally a
shared bot token so PRs are authored consistently), `bun`, `pre-commit`,
`python3` + `pyyaml`. Each target repo needs a GitHub Project board with a `Ready`
column and its own `.pre-commit-config.yaml` / CI.

## Troubleshooting

**macOS "Keychain Not Found" prompt during install?** It's a benign interaction
between git's `osxkeychain` credential helper and a public-repo clone — no
credentials are needed and nothing is exposed. Click **Cancel** (NOT "Reset To
Defaults", which resets your login keychain). To suppress it, either unlock your
login keychain or set a non-keychain helper:
`git config --global credential.helper ""`.

## Quickstart

**1. Install the plugin** (official Hermes plugin):
```bash
hermes plugins install benmarte/daedalus --enable
hermes gateway restart            # load the plugin
```

> **macOS note:** on macOS without launchd management, `hermes gateway restart` falls
> back to running the gateway as a **background process**. It works, but does NOT
> auto-start at login or auto-restart on crash.

**2. Provision the agent roster** (the 6 specialist profiles — fails loudly if a
prerequisite is missing, e.g. no `default` profile / `agent-skills` / `gh` auth):
```bash
python3 ~/.hermes/plugins/daedalus/scripts/postinstall.py
hermes profile list               # expect: developer reviewer security-analyst documentation planner project-manager
```

**3. Onboard a target repo** — scaffolds `<repo>/.hermes/daedalus.yaml` and
registers the repo so the dispatcher sweeps it:
```bash
cd /path/to/your/repo
bash ~/.hermes/plugins/daedalus/scripts/setup.sh
# then edit .hermes/daedalus.yaml (tracking, sources, cron) — repo/workdir are fixed
```

**4. Trigger work** — any of:
- **Prompt / spec file:** `hermes kanban create --triage --workspace dir:$PWD --body "$(cat spec.md)"`
- **Spec drop:** put a `*.md` in `<repo>/.hermes/pending/` (when `sources.local_specs.enabled`)
- **GitHub issue:** move an issue to **Ready** on the repo's GitHub Project (GitHub-Projects mode)

The triage card decomposes across the roster → developer opens a PR → reviewer + security
gate it → CI-aware auto-advance → documentation posts the resolution **on the PR** + Slack.
You merge (agents never merge `main`).

**5. Visual config + status** — `hermes dashboard` → the **Daedalus** tab: a card per
project with live status (kanban counts, open PRs + CI, needs-attention, cron), and an
editor for each project's config (`repo`/`workdir` are read-only).

**6. Automate** — schedule the dispatcher so advancing/onboarding run unattended:
```bash
hermes cron add daedalus --schedule "every 3m" \
  --script "python3 ~/.hermes/plugins/daedalus/scripts/daedalus_dispatch.py"
```

> Per-project conventions and the legacy native-roster setup are in **[`SETUP.md`](SETUP.md)**.


## Uninstall / reset

To completely remove Daedalus and all its host-side state:

```bash
# HERMES_HOME defaults to ~/.hermes — set it if yours is elsewhere
# 1. Clean up host-side artifacts (config, registry, hooks, cron jobs, boards, profiles)
#    Shows a data-loss summary first — review it, then confirm, or use -y for scripting:
bash "$HERMES_HOME/plugins/daedalus/scripts/uninstall.sh"

# Use --keep-profiles to keep the 6 agent profiles:
bash "$HERMES_HOME/plugins/daedalus/scripts/uninstall.sh" --keep-profiles

# 2. Remove the plugin package itself
hermes plugins uninstall daedalus
```

The uninstall script is idempotent — safe to re-run; absent items are skipped without error.

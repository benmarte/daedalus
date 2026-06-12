# Daedalus — Installation & Usage Guide

**Daedalus** is a [Hermes](https://herm.es) plugin that automates the journey from a GitHub, GitLab, or Azure DevOps issue all the way to a reviewed, mergeable pull request — without you having to babysit the process. You mark an issue **Ready**, and a team of six AI agents handles the implementation, code review, security audit, and documentation. When the PR is merged, Daedalus closes the issue and moves the board card to **Done**.

This guide walks you through every step: installing the plugin, provisioning the agents, adding your first project, understanding the dashboard, and keeping everything up to date.

---

> **Tip:** If you're setting up Daedalus for an entire engineering team (shared repo, multiple machines), see [SETUP.md](../SETUP.md) after completing this guide.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Install the Plugin](#2-install-the-plugin)
3. [Provision the Agent Roster](#3-provision-the-agent-roster)
4. [Add Your First Project](#4-add-your-first-project)
5. [Explore the Dashboard](#5-explore-the-dashboard)
6. [Configure a Project](#6-configure-a-project)
7. [How the Kanban Board Works](#7-how-the-kanban-board-works)
8. [How the Cron Job Works](#8-how-the-cron-job-works)
9. [Add Your VCS Token](#9-add-your-vcs-token)
10. [Triggering Work](#10-triggering-work)
11. [Update the Plugin](#11-update-the-plugin)
12. [Remove a Project](#12-remove-a-project)
13. [Uninstall Daedalus](#13-uninstall-daedalus)
14. [Troubleshooting](#14-troubleshooting)
15. [What's Next](#15-whats-next)

---

## 1. Prerequisites

Before installing Daedalus, make sure these tools are ready:

| Requirement | Why it's needed |
|---|---|
| [Hermes](https://herm.es) installed with a working default profile | The runtime that runs agents and the dashboard |
| [agent-skills](https://github.com/addyosmani/agent-skills) Hermes plugin | Provides the base skills each agent role loads — **must be installed first** |
| Python 3 + `pyyaml` | Runs the dispatcher (the scheduling engine) |
| `pre-commit` | Enforced before any PR can be opened (the "ship gate") |
| A VCS API token for each platform you use | Lets Daedalus poll issues and open PRs on your behalf |

> **No `gh`, `glab`, or `az` CLI needed.** Daedalus talks directly to your VCS platform's HTTPS API using your token. No CLI tools are installed on your machine.

**Install agent-skills first** — Daedalus's roster depends on it:

```bash
hermes plugins install addyosmani/agent-skills --enable
hermes gateway restart
```

---

## 2. Install the Plugin

```bash
hermes plugins install benmarte/daedalus --enable
hermes gateway restart
```

The `gateway restart` is required so Hermes loads the new plugin's API and dashboard tab.

![Hermes Plugins page showing Daedalus installed](../screenshots/guide/00-plugins-page.png)

You should now see **Daedalus** listed on the Hermes Plugins page.

> **macOS note:** On macOS without launchd management, `hermes gateway restart` falls back to a background process. Everything works, but the gateway won't auto-start at login or restart itself if it crashes. This is a Hermes limitation, not Daedalus's.

---

## 3. Provision the Agent Roster

Daedalus runs six specialist AI agents. Before they can work, their **Hermes profiles** need to be created. A Hermes profile is like a dedicated workspace for one agent role — it has its own AI model configuration, set of skills, and (once you add projects) its own git credentials for pushing branches.

Run the provisioner:

```bash
python3 ~/.hermes/plugins/daedalus/scripts/postinstall.py
```

Then verify the profiles were created:

```bash
hermes profile list
```

You should see all six roles:

```
developer-daedalus
reviewer-daedalus
security-analyst-daedalus
documentation-daedalus
planner-daedalus
project-manager-daedalus
```

![Hermes Profiles page showing the 6 provisioned agent profiles](../screenshots/guide/02-profiles-roster.png)

### The Six Agent Roles

Each agent has a single, focused job. Here's what they each do in plain English:

| Role | What it does |
|---|---|
| **project-manager** | The coordinator. Triages incoming work, routes it to the right agents, and steps in when something gets stuck (e.g. a reviewer has requested changes — the PM decides who should address them). |
| **planner** | Breaks an issue down into a concrete plan: what to build, in what order, with what acceptance criteria. The developer works from this plan. |
| **developer** | Writes the code, runs tests, and passes the **ship gate** (your repo's pre-commit checks) before the PR can be opened. If CI turns red, the developer gets a fix card. |
| **reviewer** | Reviews the PR for correctness, style, and logic. If changes are requested, the PM routes the feedback back to the developer. |
| **security-analyst** | Audits the PR for security issues (secrets, injection risks, over-permissioned code, etc.). Runs in parallel with the reviewer. |
| **documentation** | Writes a completion report once the PR is approved. Posts it directly on the PR as a comment and sends it to any configured notification channels (Slack, Discord, etc.). |

> **Why separate roles instead of one agent doing everything?** An agent reviewing its own work is the same as no review at all. The hard separation means each stage is independently verified — the developer can't skip the reviewer, and the reviewer can't merge without the security analyst's sign-off.

---

## 4. Add Your First Project

Open the Hermes dashboard, go to the **Daedalus** tab, and click **+ Add Project**.

![Empty Daedalus dashboard — fresh install, no projects](../screenshots/guide/01-empty-dashboard.png)

### Step 1 — Enter the repository path

![Add Project modal, step 1, empty](../screenshots/guide/03-add-project-empty.png)

Type (or paste) the full path to the repository you want Daedalus to manage, then click anywhere outside the field. Daedalus will read the repository's `origin` remote and **auto-detect**:

- The **provider** (GitHub, GitLab, or Azure DevOps)
- The **repo slug** (e.g. `myorg/my-app`)
- A default **project name**

![Add Project modal with path filled in — name, repo, and provider auto-detected](../screenshots/guide/04-add-project-autodetected.png)

You can leave auto-detected values as-is or override them.

### Step 2 — (Optional) Set a cron schedule and notification channel

Scroll down in the modal to set how often Daedalus should poll your issues. The default is every 60 minutes.

![Add Project modal showing cron schedule and notify fields](../screenshots/guide/05-add-project-cron-section.png)

You can also pick a **notification channel** — any Slack, Discord, or other platform already configured in Hermes. Daedalus will send status summaries and documentation reports to that channel.

![Add Project modal bottom with Next: Configure button](../screenshots/guide/05b-add-project-bottom.png)

Click **Next: Configure** when you're ready.

### Step 3 — Project created

Daedalus will:
1. Scaffold a config file at `<your-repo>/.hermes/daedalus.yaml`
2. Register the project in its internal registry
3. Create a dedicated **kanban board** for tracking work on this repo
4. Create a **cron job** that polls on your chosen schedule

![Modal at step 2 after project created — configure step](../screenshots/guide/06-project-created.png)

---

## 5. Explore the Dashboard

After adding a project, the Daedalus tab shows a **card per project** with live status.

![Dashboard showing the newly added project card](../screenshots/guide/07-dashboard-with-project.png)

Each card shows:

- **Project name and repo** slug
- **Kanban summary** — counts of cards by status (In progress, In review, Done, etc.)
- **Open PRs** — how many PRs are currently open, with their CI status (green/red/pending)
- **Needs attention** — any cards that are blocked or have given up, so you know where human input is needed
- **Cron status** — the schedule and when it last ran

---

## 6. Configure a Project

Click the gear icon (or the project name) on a dashboard card to open the **config modal**.

![Project config modal — VCS, identity, and sources section](../screenshots/guide/08-config-modal.png)

The top of the modal lets you configure:

- **VCS provider** — GitHub, GitLab, or Azure DevOps (auto-detected; you can override)
- **Target branch** — the branch PRs are opened against (e.g. `dev`, `main`)
- **Tracking** — link to a GitHub Project v2 board number, a GitLab label board, or Azure DevOps work-item states
- **Sources** — which triggers are enabled (VCS issues, local spec files, manual kanban cards)

Scroll down to see the schedule and notification settings:

![Project config modal scrolled — cron schedule, notifications, and tracking](../screenshots/guide/08b-config-cron-notify.png)

- **Cron schedule** — how often to poll (e.g. `every 60m`, `every 2h`, or a cron expression like `0 9 * * *`)
- **Notify via** — a single delivery target for summaries and doc reports
- **Notifications (multi-target)** — configure multiple channels with per-channel event filters (`doc-report`, `dispatch-summary`, `pipeline-failure`, `pr-ready`)

After saving, Daedalus updates the cron job in place — it never creates a duplicate.

> **Note:** `repo` and `workdir` are read-only in the config modal. They are set when the project is first created and cannot be changed here. This is intentional — changing the repo path would orphan the kanban board and cron job.

---

## 7. How the Kanban Board Works

Every Daedalus project gets its own **kanban board** inside Hermes. If you haven't used kanban before: a kanban board is a visual way to track work in progress. Cards (representing tasks) move through columns from left to right — typically from **Ready** → **In progress** → **In review** → **Done**.

Daedalus creates and moves these cards automatically as the agents work:

1. An issue is moved to **Ready** on your VCS board.
2. The next cron tick creates a **triage card** and decomposes it into tasks for each agent role.
3. Cards advance through the board as work is completed — developer opens a PR, reviewer approves, security clears it, documentation posts the report.
4. When you **merge the PR**, the card moves to **Done** and the original issue is closed.

You can view the board at any time from `hermes dashboard` → navigate to the Hermes Kanban view for your project.

![Hermes Kanban board for the project](../screenshots/guide/09-kanban-board.png)

> **Why does Daedalus maintain its own kanban board?** Because tracking must be deterministic. If an agent "remembers" to update the board, it might forget. By having the dispatcher (plain Python code, not an agent) maintain the board on every tick, the board always reflects reality — you always know exactly where each issue is in the pipeline.

---

## 8. How the Cron Job Works

A **cron job** is a task that runs automatically on a schedule — like an alarm clock for code. Every time the cron job fires, Daedalus runs its dispatch loop:

1. Polls your VCS platform for any issues in the **Ready** state.
2. Skips any issue that already has an open PR (no duplicate work).
3. Kicks off the agent pipeline for new Ready issues.
4. Auto-advances any pipeline stage that's unblocked (e.g. CI turned green on a PR that was waiting for review).
5. Closes issues and marks cards **Done** when their PRs are merged.

You can view the cron job for any project in the Hermes Cron page:

![Hermes Cron page showing the daedalus-daedalus cron job](../screenshots/guide/10-cron-job.png)

Each project gets its own cron job, named `<project-name>-daedalus`. You can pause, edit, or remove it from there, or adjust the schedule in the Daedalus config modal (which updates the job in place).

---

## 9. Add Your VCS Token

Daedalus needs a personal access token (PAT) so it can poll your issues and open PRs on your behalf. Tokens are **never stored in config files** — they live only in environment variables.

**Add your token to `~/.hermes/.env`:**

```
# GitHub
GITHUB_TOKEN=ghp_your_token_here

# GitLab
GITLAB_TOKEN=glpat_your_token_here

# Azure DevOps
AZURE_DEVOPS_PAT=your_pat_here
```

After editing the file, restart the gateway:

```bash
hermes gateway restart
```

> **Why `~/.hermes/.env`?** Hermes loads this file at startup and injects its variables into the gateway process. This means both the Daedalus dashboard and the dispatcher cron job can see your tokens without you having to export them each time.

Also export the token in your shell before re-running the provisioner, so the worker agent profiles get it seeded into their own credentials:

```bash
export GITHUB_TOKEN=ghp_your_token_here
python3 ~/.hermes/plugins/daedalus/scripts/postinstall.py
```

### Token Permissions Required

**GitHub — Fine-grained PAT** (GitHub → Settings → Developer settings → Fine-grained tokens):

| Permission | Level | Used for |
|---|---|---|
| Contents | Read and write | Workers push branches |
| Pull requests | Read and write | Open PRs, post the documentation report |
| Issues | Read and write | Poll Ready issues, close on merge |
| Commit statuses + Checks | Read | CI-green gating |
| Metadata | Read | Required baseline |
| Projects *(organization permission)* | Read and write | Projects v2 board sync |

> If your organization hasn't enabled fine-grained PATs, use a **classic PAT** with `repo` + `project` scopes (add `workflow` only if agents will edit `.github/workflows/` files).

**GitLab — Personal Access Token** (GitLab → Preferences → Access tokens):
- `api` — covers issues, boards/labels, MRs, notes, pipelines
- `write_repository` — workers push branches over HTTPS

Same scopes apply for self-hosted GitLab instances.

**Azure DevOps — PAT** (dev.azure.com → User settings → Personal access tokens):

| Scope | Level |
|---|---|
| Work Items | Read & Write |
| Code | Read & Write |
| Build | Read |

> **Security tip:** Use a dedicated bot or machine account so PRs and comments are attributed to it, not your personal account. Set a token expiry date and rotate regularly.

---

## 10. Triggering Work

Once a project is added and configured, Daedalus picks up work from three sources (all enabled by default):

**1. VCS Issues (the main path)**
Move an issue to the **Ready** state on your board:
- **GitHub:** Move the issue card to the `Ready` column in your Projects v2 board.
- **GitLab:** Apply the `Ready` label to the issue.
- **Azure DevOps:** Set the work item state to `Ready`.

The next cron tick will pick it up automatically.

**2. Spec file drop**
Drop a Markdown file into `<your-repo>/.hermes/pending/`. Daedalus will pick it up on the next tick and treat it as a spec to implement.

**3. Manual kanban triage card**
Create a triage card directly via the Hermes kanban:

```bash
hermes kanban create --triage --workspace dir:$PWD --body "$(cat spec.md)"
```

---

## 11. Update the Plugin

When a new version of Daedalus is available, the dashboard footer shows an **Update Plugin** button.

![Dashboard footer showing "Update Plugin" button — version 1.0.0 to 1.1.0-beta.1](../screenshots/guide/11-update-available.png)

Click it to update. The dashboard will run `hermes plugins update daedalus` in the background and report back when done.

After updating, restart the gateway so the new plugin code takes effect:

```bash
hermes gateway restart
```

Then reload the Hermes dashboard browser tab.

> **Important:** The Hermes dashboard loads each plugin's backend code once at startup and does not hot-reload. Skipping the gateway restart means the dashboard will still be running the old version's API code even though the files on disk are new.

---

## 12. Remove a Project

To stop Daedalus from managing a project (without uninstalling Daedalus itself):

1. Open the Daedalus dashboard.
2. Find the project card.
3. Click the **Remove** (trash) icon.

This will:
- Remove the project's cron job
- Archive the project's kanban board (it's recoverable via `hermes kanban boards restore`)
- Remove the project from the Daedalus registry

The project's `.hermes/daedalus.yaml` config file is intentionally **left on disk** — so you can re-add the project at any time using **+ Add Project** and Daedalus will adopt the existing config without overwriting it.

---

## 13. Uninstall Daedalus

**Option A — Dashboard button (recommended):**

Scroll to the bottom of the Daedalus dashboard tab. Click **Uninstall Daedalus**.

A confirmation dialog will show you exactly what will be removed before anything is deleted.

![Uninstall confirmation modal](../screenshots/guide/12-uninstall-confirm.png)

The uninstall removes: all cron jobs, all six agent profiles, all kanban boards, the project registry, and the plugin package itself.

**Option B — Terminal:**

```bash
bash ~/.hermes/plugins/daedalus/scripts/uninstall.sh
```

Options:
```bash
# Keep the plugin installed but reset all host state:
bash ~/.hermes/plugins/daedalus/scripts/uninstall.sh --keep-plugin

# Keep the 6 agent profiles:
bash ~/.hermes/plugins/daedalus/scripts/uninstall.sh --keep-profiles

# Non-interactive (for scripting):
bash ~/.hermes/plugins/daedalus/scripts/uninstall.sh -y
```

> **Do NOT run `hermes plugins uninstall daedalus` alone.** That command only deletes the plugin directory and leaves all profiles, cron jobs, kanban boards, and config files behind. Use the dashboard button or `uninstall.sh` for a complete removal.

---

## 14. Troubleshooting

### "Failed to load" or "No such API endpoint" in the dashboard

The gateway needs to be restarted after installing or updating Daedalus. The Hermes dashboard loads each plugin's backend code once at startup and does not hot-reload.

```bash
hermes gateway restart
```

Then reload the browser tab. If the error persists, check that Daedalus is enabled:

```bash
hermes plugins list
```

It should appear as `daedalus [enabled]`.

### CI badge shows the wrong state, or a PR seems stuck

The dispatcher auto-advances stages once CI turns green. If a PR has been green for a while and nothing moved:

1. Wait for the next cron tick (check the Cron page to see when it last ran).
2. If the cron job isn't running, check the Hermes Cron page — the job may be paused.
3. Check the **Needs attention** section of the dashboard card — if a card is `blocked` or `gave_up`, it needs human input.

### Labels not loading in the config modal

The label picker calls your VCS provider's API. If it shows as empty:
- Check that your token is in `~/.hermes/.env` and has the correct permissions (Issues: Read and write for GitHub; `api` scope for GitLab).
- Restart the gateway after editing `.env`.
- Verify the token is valid by checking your VCS provider's token settings page.

### Project not showing after adding it

The dashboard fetches the project list from the backend. Try:
1. Refreshing the browser tab.
2. Restarting the gateway (`hermes gateway restart`) if you just installed the plugin.
3. Checking that the `workdir` path you entered actually exists and is an absolute path.

### macOS "Keychain Not Found" prompt during install

This is a benign interaction between git's `osxkeychain` credential helper and a public-repo clone. Click **Cancel** (not "Reset To Defaults" — that resets your login keychain). To suppress it permanently:

```bash
git config --global credential.helper ""
```

---

## 15. What's Next

- **Multi-user team setup:** If you're rolling Daedalus out to a team, see [SETUP.md](../SETUP.md) for how to share the configuration without sharing tokens, and how each colleague provisions their own roster from the same repo.

- **Notifications:** Configure Slack, Discord, Telegram, Signal, WhatsApp, or any other platform supported by Hermes in the project config modal's **Notifications** section. Use the **Send test message** button to verify connectivity before the first real dispatch.

- **Custom board column names:** If your board uses different column names (e.g. `To do` instead of `Ready`), edit `vcs.status_map` in `.hermes/daedalus.yaml` or via the config modal.

- **Multiple repos:** Add as many projects as you like — each gets its own kanban board, cron job, and notification config. There's one Daedalus plugin and one shared agent roster driving all of them.

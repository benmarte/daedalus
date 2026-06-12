# Daedalus — Installation & Usage Guide

**Daedalus** is a [Hermes](https://herm.es) plugin that automates the journey from a GitHub, GitLab, or Azure DevOps issue all the way to a reviewed, mergeable pull request. You mark an issue **Ready**, and a team of six AI agents handles the implementation, code review, security audit, and documentation. When the PR is merged, Daedalus closes the issue and moves the board card to **Done**.

This guide walks you through every step: installing the plugin, provisioning the agents, adding your first project, and keeping everything up to date.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Install the Plugin](#2-install-the-plugin)
3. [Provision the Agent Roster](#3-provision-the-agent-roster)
4. [Add Your First Project — Step 1](#4-add-your-first-project--step-1)
5. [Configure the Project — Step 2](#5-configure-the-project--step-2)
6. [Dashboard Overview](#6-dashboard-overview)
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

| Requirement | Why it's needed |
|---|---|
| [Hermes](https://herm.es) installed with a working default profile | The runtime that runs agents and the dashboard |
| A VCS API token for the platform you use | Lets Daedalus poll issues and open PRs on your behalf |

> **Everything else is automatic.** When you click **Install Agents**, Daedalus installs the [agent-skills](https://github.com/addyosmani/agent-skills) plugin automatically if it is missing. No manual setup required.

> **No `gh`, `glab`, or `az` CLI needed.** Daedalus talks directly to your VCS platform's HTTPS API. No additional CLIs required.

---

## 2. Install the Plugin

```bash
hermes plugins install benmarte/daedalus --enable
hermes gateway restart
```

The `gateway restart` is required so Hermes registers Daedalus's API routes and dashboard tab.

![Hermes Plugins page showing Daedalus installed and enabled](screenshots/guide/00-plugins-page.png)

You should now see **Daedalus** listed as enabled on the Plugins page.

> **macOS note:** On macOS without launchd management, `hermes gateway restart` falls back to a background process. Everything works, but the gateway won't auto-start at login or restart itself if it crashes. This is a Hermes limitation.

---

## 3. Provision the Agent Roster

Open the Hermes dashboard and go to the **Daedalus** tab. On a fresh install, you'll see a **Worker Agents not provisioned** banner:

![Empty Daedalus dashboard showing the Install Agents banner](screenshots/guide/01-install-agents-banner.png)

Click **Install Agents**. Daedalus runs its provisioner and creates six specialist profiles. This takes about 10–20 seconds.

Verify the profiles by going to **Profiles** in Hermes:

![Hermes Profiles page showing the 6 Daedalus agent profiles](screenshots/guide/02-profiles-page.png)

### The Six Agent Roles

| Role | What it does |
|---|---|
| **project-manager** | Coordinates work, routes issues to agents, unblocks stalled pipelines |
| **planner** | Breaks an issue into a concrete plan with acceptance criteria |
| **developer** | Writes code, runs tests, auto-detects and runs the project's lint/format tools before opening a PR |
| **reviewer** | Reviews the PR for correctness, style, and logic |
| **security-analyst** | Audits for secrets, injection risks, and over-permissioned code — runs in parallel with the reviewer |
| **documentation** | Writes a completion report, posts it on the PR, and sends it to notification channels |

> **Why separate roles?** An agent reviewing its own work is the same as no review. Hard role separation ensures each stage is independently verified.

After provisioning, the Daedalus tab shows a clean empty dashboard ready for your first project:

![Empty Daedalus dashboard — agents installed, no projects yet](screenshots/guide/03-empty-dashboard.png)

---

## 4. Add Your First Project — Step 1

Click **+ Add Project** in the top right corner. Adding a project is a two-step process. Step 1 collects the basics.

![Add Project modal — Step 1 of 2, empty form](screenshots/guide/04-add-project-step1-empty.png)

### Enter the repository path

Type (or paste) the full absolute path to the repository you want Daedalus to manage, then click outside the field or press Tab. Daedalus reads the repository's `origin` remote and **auto-detects**:

- The **VCS provider** (GitHub, GitLab, or Azure DevOps)
- The **repo slug** (e.g. `myorg/my-app`)
- A default **project name**

You can also click **Browse…** to pick a directory from a native folder picker.

![Add Project Step 1 — workdir filled in, provider and repo auto-detected, Sources section visible](screenshots/guide/05-add-project-step1-filled.png)

### Source toggles

The **Sources** section controls what triggers Daedalus to pick up work:

| Source | What it does |
|---|---|
| **VCS Issues** | Polls your GitHub/GitLab/Azure board for issues in the Ready state |
| **Local Specs** | Watches `.hermes/pending/*.md` for spec files to implement |
| **Kanban Triage** | Picks up manual triage cards created directly on the Hermes kanban board |

All three are enabled by default. Click **Next: Configure →** to create the project and move to Step 2.

---

## 5. Configure the Project — Step 2

Step 2 opens automatically after Step 1. This is where you set branches, boards, cron schedule, and notifications:

![Add Project Step 2 — VCS provider, target branch, and GitHub Project Board settings](screenshots/guide/06-add-project-step2-top.png)

### Fields at a glance

**VCS section:**
- **Provider** — auto-detected; change if needed
- **Target Branch** — the branch PRs are opened against (default: `main`)
- **Branch Prefix** — prefix added to branches created by the developer agent (default: `fix/`)
- **PR Title Prefix** — prefix added to PR titles (default: `fix:`)

**GitHub / GitLab / Azure Board:**
- Link to a Projects v2 board number (GitHub), GitLab label board, or Azure DevOps board. Daedalus syncs card status to/from your VCS board.

**Cron section:**
- How often the dispatch loop runs (default: every 60 minutes). Pick minutes, hours, or enter a raw cron expression.

![Step 2 scrolled — cron frequency settings](screenshots/guide/07-add-project-step2-cron.png)

**Notifications:**
- Configure one or more delivery targets (Slack, Discord, etc.) for dispatch summaries, documentation reports, and pipeline failure alerts.
- Click **+ Add notification target** to add a channel. Each target can be filtered to specific event types (`doc-report`, `dispatch-summary`, `pipeline-failure`, `pr-ready`).

![Step 2 notifications section — add notification targets and configure event filters](screenshots/guide/08-add-project-step2-notify.png)

Click **Finish Setup** to save. Daedalus will:
1. Write a config file at `<your-repo>/.hermes/daedalus.yaml`
2. Create a dedicated **kanban board** for this repo
3. Create a **cron job** that polls on your chosen schedule

---

## 6. Dashboard Overview

After adding a project, the Daedalus tab shows a card for each project:

![Daedalus dashboard showing the newly added project card](screenshots/guide/09-dashboard-with-project.png)

Each card shows:

- **Project name and repo** slug
- **Cron** — the schedule and last-run time
- **Kanban summary** — counts of cards by status once the dispatcher starts running
- **Open PRs** — how many PRs are currently open with their CI status
- **Needs attention** — any cards that are blocked or gave up (needs human input)

Click the **gear icon** on any card to open its config modal, where you can change branches, boards, cron schedule, and notifications.

---

## 7. How the Kanban Board Works

Every Daedalus project gets its own **kanban board** inside Hermes. Cards move through columns as the agents work:

1. An issue is moved to **Ready** on your VCS board.
2. The next cron tick creates a triage card and decomposes it into tasks per agent role.
3. Cards advance as work is completed — developer opens a PR, reviewer approves, security clears it, documentation posts the report.
4. When you **merge the PR**, the card moves to **Done** and the original issue is closed.

View the board at any time from the Hermes **Kanban** page:

![Hermes Kanban board for the Daedalus project — empty columns ready for work](screenshots/guide/10-kanban-board.png)

> **Why does Daedalus maintain its own kanban board?** Because tracking must be deterministic. The dispatcher (plain Python, not an agent) updates the board on every tick, so the board always reflects reality — you always know exactly where each issue is in the pipeline.

---

## 8. How the Cron Job Works

Every time the cron job fires, Daedalus runs its dispatch loop:

1. Polls your VCS platform for issues in the **Ready** state.
2. Skips issues that already have an open PR (no duplicate work).
3. Kicks off the agent pipeline for new Ready issues.
4. Auto-advances any pipeline stage that's unblocked (e.g. CI turned green).
5. Closes issues and marks cards **Done** when their PRs are merged.

View the cron job for your project in the Hermes **Cron** page:

![Hermes Cron page — daedalus-daedalus job for the installed project](screenshots/guide/11-cron-job.png)

Each project gets its own cron job named `<project-name>-daedalus`. You can pause, edit, or delete it here, or adjust the schedule in the project config modal (which updates the job in place — no duplicate is created).

---

## 9. Add Your VCS Token

Daedalus needs a personal access token (PAT) to poll your issues and open PRs. Tokens are **never stored in config files** — they live only in environment variables.

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

> **Why `~/.hermes/.env`?** Hermes loads this file at startup and injects its variables into the gateway process. Both the dashboard and the dispatcher cron job can see your tokens without you re-exporting them each session.

### Token Permissions Required

**GitHub — Fine-grained PAT** (Settings → Developer settings → Fine-grained tokens):

| Permission | Level | Used for |
|---|---|---|
| Contents | Read and write | Workers push branches |
| Pull requests | Read and write | Open PRs, post documentation reports |
| Issues | Read and write | Poll Ready issues, close on merge |
| Commit statuses + Checks | Read | CI-green gating |
| Metadata | Read | Required baseline |
| Projects *(org permission)* | Read and write | Projects v2 board sync |

> Use a **classic PAT** with `repo` + `project` scopes if your org hasn't enabled fine-grained PATs.

**GitLab — Personal Access Token** (Preferences → Access tokens):
- `api` — issues, boards/labels, MRs, notes, pipelines
- `write_repository` — workers push branches over HTTPS

**Azure DevOps — PAT** (User settings → Personal access tokens):

| Scope | Level |
|---|---|
| Work Items | Read & Write |
| Code | Read & Write |
| Build | Read |

> **Security tip:** Use a dedicated bot or machine account. Set a token expiry date and rotate regularly.

---

## 10. Triggering Work

Daedalus picks up work from three sources (all enabled by default):

**1. VCS Issues (the main path)**

Move an issue to the **Ready** state on your board:
- **GitHub:** Move the issue card to the `Ready` column in your Projects v2 board.
- **GitLab:** Apply the `Ready` label to the issue.
- **Azure DevOps:** Set the work item state to `Ready`.

The next cron tick picks it up automatically.

**2. Spec file drop**

Drop a Markdown file into `<your-repo>/.hermes/pending/`. Daedalus picks it up on the next tick and treats it as a spec to implement.

**3. Manual kanban triage card**

Create a triage card directly via the Hermes kanban:

```bash
hermes kanban create --triage --workspace dir:$PWD --body "$(cat spec.md)"
```

---

## 11. Update the Plugin

When a new version is available, the dashboard footer shows an **Update Plugin** button:

![Dashboard footer showing Update Plugin button and version](screenshots/guide/12-update-available.png)

Click it to update in place. After the update, restart the gateway so the new code takes effect:

```bash
hermes gateway restart
```

Then reload the browser tab.

> **Important:** Hermes loads each plugin's backend code once at startup. Skipping the restart means the dashboard will keep running the old version's API code even though the files on disk are updated.

---

## 12. Remove a Project

To stop Daedalus from managing a project without uninstalling the plugin:

1. Open the Daedalus dashboard.
2. Find the project card.
3. Click the **Remove** (trash) icon.

This removes the project's cron job, archives its kanban board, and removes it from the Daedalus registry.

The project's `.hermes/daedalus.yaml` config file is intentionally **left on disk** — you can re-add the project at any time and Daedalus will adopt the existing config without overwriting it.

---

## 13. Uninstall Daedalus

**Option A — Dashboard button (recommended):**

Scroll to the bottom of the Daedalus dashboard tab and click **Uninstall**. A confirmation dialog shows exactly what will be removed before anything is deleted:

![Uninstall Daedalus confirmation modal](screenshots/guide/13-uninstall-confirm.png)

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

> **Do NOT run `hermes plugins uninstall daedalus` alone.** That command deletes the plugin directory but leaves all profiles, cron jobs, kanban boards, and config files behind. Use the dashboard button or `uninstall.sh` for a complete removal.

---

## 14. Troubleshooting

### "Plugin not active — restart the Hermes gateway" in the dashboard

The gateway loads plugin API routes once at startup. After installing or updating Daedalus:

```bash
hermes gateway restart
```

Then reload the browser tab. If the error persists, verify Daedalus is enabled:

```bash
hermes plugins list
```

It should appear as `daedalus [enabled]`.

### CI badge shows the wrong state, or a PR seems stuck

The dispatcher auto-advances stages once CI turns green. If a PR has been green for a while and nothing moved:

1. Check when the cron job last ran (Hermes Cron page).
2. Make sure the cron job is active, not paused.
3. Check the **Needs attention** section on the dashboard card — a `blocked` or `gave_up` card needs human input.

### Labels not loading in the config modal

The label picker calls your VCS provider's API. If it shows empty:
- Confirm your token is in `~/.hermes/.env` with the correct permissions.
- Restart the gateway after editing `.env`.
- Verify the token is still valid at your VCS provider's token settings page.

### Project not showing after adding it

1. Refresh the browser tab.
2. Restart the gateway if you just installed the plugin.
3. Confirm the working directory path exists and is an absolute path.

### macOS "Keychain Not Found" prompt during install

A benign interaction between git's `osxkeychain` helper and a public-repo clone. Click **Cancel**. To suppress it permanently:

```bash
git config --global credential.helper ""
```

---

## 15. What's Next

- **Multi-user team setup:** See [SETUP.md](../SETUP.md) for sharing configuration across teammates without sharing tokens, and for how each person provisions their own roster from the same repo.

- **Notifications:** Configure Slack, Discord, Telegram, or any Hermes-supported platform in the project config modal's **Notifications** section. Use **Send test message** to verify connectivity before the first dispatch.

- **Custom board column names:** If your board uses different column names (e.g. `To do` instead of `Ready`), edit `vcs.status_map` in `.hermes/daedalus.yaml` or via the config modal.

- **Multiple repos:** Add as many projects as you like — each gets its own kanban board, cron job, and notification config. One Daedalus plugin drives all of them.

- **Re-run screenshots:** The screenshot script lives at `scripts/take_screenshots.py`. Run it any time from a fresh state to regenerate the guide images:
  ```bash
  python3 scripts/take_screenshots.py
  ```

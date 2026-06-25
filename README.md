# Daedalus — autonomous issue → PR pipeline on native Hermes

Flag an issue **Ready** — on **GitHub**, **GitLab**, or **Azure DevOps** — and a
roster of AI agents implements it, reviews it, security-audits it, documents it,
and opens a **green, mergeable PR** — with quality gates that *cannot* be skipped,
full board/issue tracking, and zero babysitting. A single Daedalus deployment
drives **many repos**, each with its own provider, kanban board, cron job, and
notification channels (Slack, Discord, Telegram, Signal, WhatsApp, …).

```mermaid
flowchart TD
    A([🏁 Issue marked Ready\nGitHub · GitLab · Azure]) -->|cron tick or\ncompletion trigger| B[Dispatcher\ndaedalus_dispatch.py]

    B --> C["⚡ Phase 1\nValidator task created\n— only agent active —"]
    C --> V{Validator\nOutcome}

    V -->|"CONFIRMED: &lt;note&gt;"| E["📋 Phase 2\nPM decomposes work\nacross team roster"]
    V -->|"ALREADY_FIXED\nor DUPLICATE"| AF(["✅ Issue closed\nPipeline ends"])
    V -->|NEEDS_MORE_INFO| NI(["⏸ Card blocked\nComment posted\nAwaits reporter response"])
    V -->|"SECURITY_THREAT\nor BLOCK_FOR_REVIEW"| ST(["🔒 Card blocked\nIssue comment posted\nsecurity-escalation fired"])

    E --> Dev["👨‍💻 Developer\nImplement · test\nShip-gate · open PR"]
    Dev --> QA["🧪 QA\nTest suite · coverage\nqa-passed / qa-failed"]
    QA --> CI{CI}
    CI -->|green| Rev["🔍 Reviewer\nCode review\nApprove / request changes"]
    CI -->|green| A11y["♿ Accessibility\nWCAG 2.1 AA audit\n(conditional on UI work)"]
    CI -->|red| Fix["🔧 Fix card created\nidempotent · capped at 3"]
    Fix --> Dev
    Rev -->|approved| Sec["🛡 Security Analyst\nOWASP audit\nSecrets · injection · authz"]
    Sec -->|cleared| Doc["📝 Documentation\nADRs · changelog\nReport → PR + chat channels"]
    A11y -->|cleared| Doc
    Doc --> Merge(["🔀 You merge the PR"])
    Merge --> Done(["✅ Issue closed\nCard → Done"])

    style A fill:#1976D2,color:#fff,stroke:#0D47A1
    style AF fill:#757575,color:#fff,stroke:#424242
    style NI fill:#F57C00,color:#fff,stroke:#E65100
    style ST fill:#C62828,color:#fff,stroke:#B71C1C
    style Merge fill:#388E3C,color:#fff,stroke:#1B5E20
    style Done fill:#2E7D32,color:#fff,stroke:#1B5E20
```

![Daedalus dashboard — one card per managed project, showing kanban counts, open PRs with CI status, and cron schedule](docs/screenshots/guide/09-dashboard-with-project.png)

---

## Table of contents

- [Why this exists](#why-this-exists-read-this-part)
- [How it works](#how-it-works)
- [Agent roster](#agent-roster)
- [Customizing agents](#customizing-agents)
  - [Custom profiles](#custom-profiles)
  - [Skills per agent](#skills-per-agent)
  - [Delegating to Claude Code (or Codex)](#delegating-to-claude-code-or-codex)
  - [Profile fallback behavior](#profile-fallback-behavior)
  - [Comment attribution template](#comment-attribution-template)
- [Autonomous pipeline advancement](#autonomous-pipeline-advancement)
- [Webhook configuration](#webhook-configuration)
- [Self-healing loop](#self-healing-loop)
- [Design decisions](#design-decisions)
- [Multi-repo: one Daedalus, many repos](#multi-repo-one-daedalus-many-repos)
- [Repository layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [VCS providers](#vcs-providers)
  - [Creating the tokens (PAT scopes)](#creating-the-tokens-pat-scopes)
- [Notifications](#notifications)
- [Quickstart](#quickstart)
- [Team setup](#team-setup)
- [Uninstall / reset](#uninstall--reset)
- [Known limitations](#known-limitations)

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
| Agent starts coding a bug that was fixed last week | **Validator** confirms the issue is real, unaddressed, and has enough detail before any code is written |
| Agent "forgets" to lint → red PR | **Ship-gate** detects and runs the project's lint/format tools before the PR is opened — no tool mandated |
| A single agent marks its own work done | **Decompose** into validator → developer → QA → reviewer → security → accessibility (UI only) → documentation |
| You babysit every handoff | **Auto-advance**: each stage completes on green CI and flows to the next |
| Issues merged to `dev` stay open forever | Dispatcher **closes the issue + moves card to Done** on merge |
| "Works on my machine" | One config, checked in, runs on any teammate's Hermes |

These aren't aspirations — every one was a real failure this pipeline hit and then
closed off in code. The reasoning behind each is in [Design decisions](#design-decisions).

---

## How it works

1. **You** move an issue to **`Ready`** — a GitHub Project column, a GitLab `Ready`
   board label, or an Azure DevOps work-item state. That's the only manual step —
   nothing else moves without it.
2. A **cron tick** runs `daedalus_dispatch.py` (`--no-agent`, pure code). It:
   - selects **only `Ready`** issues (and skips any that already have a PR),
   - flips the board to **In progress** and creates **one validator task** (Phase 1).
     No developer, reviewer, or other downstream task is created yet — this is
     enforced at the infrastructure level, not by instructions alone.
3. **Agents** (Hermes kanban workers) execute their tasks:
   - **validator** (Phase 1) checks that the issue is real, not already fixed, not a duplicate,
     and has enough detail. Also scans for security threats (prompt injection, social engineering,
     auth bypass, backdoor patterns, supply-chain attacks) and for high-privilege requests lacking
     verifiable context (BLOCK_FOR_REVIEW). On SECURITY_THREAT or BLOCK_FOR_REVIEW it blocks the
     pipeline and fires a `security-escalation` notification. The validator posts a summary comment
     on the GitHub issue and completes with a `CONFIRMED: <note>` summary — the dispatcher detects
     this exact prefix to trigger Phase 2. If the outcome is anything other than CONFIRMED, Phase 2
     never runs.
   - **developer/reviewer/security-analyst/accessibility/documentation** (Phase 2) — tasks are created by the
     dispatcher only after it detects the validator's `CONFIRMED:` summary. The dispatcher creates
     these atomically on the next tick: a triage card is decomposed across all roles with QA gating
     the reviewer/security/accessibility stages.
   - **developer** implements + tests, then must pass the **ship-gate** to open a PR.
   - **qa** runs the test suite, analyzes coverage, and reports a verdict (`qa-passed` or
     `qa-failed`). The pipeline advances to reviewer/security/accessibility only after QA passes.
   - **reviewer** reviews, **security-analyst** audits, **accessibility** audits the PR for
     WCAG 2.1 AA compliance (only when the issue references UI/frontend work), and
     **documentation** writes a completion report and posts it to the **PR and your chat
     channels**. All roles post a summary comment on the GitHub issue after completing
     their step.
4. Each tick **auto-advances** any stage that's blocked on review once its PR's CI is
   green. When `_execute_advance()` completes the developer card, it also calls
   `_create_downstream_review_tasks()` — a safety net that auto-creates reviewer,
   security-analyst, and documentation tasks (idempotency keys `reviewer-{n}`,
   `security-{n}`, `docs-{n}`) if they don't already exist on the board. This handles
   the edge case where the initial Phase 2 decompose didn't propagate to all four roles.
5. When you **merge** the PR, the next tick sets the card **Done** and **closes the
   issue** (GitHub doesn't auto-close on a non-default-branch merge, so the dispatcher
   does it).

The kanban board and VCS board status are bookkept **in code on every tick**, so tracking is
deterministic — never dependent on an agent remembering to update anything.

---

## Agent roster

Clicking **Install Agents** provisions 9 specialist Hermes profiles. Each is a
separate agent with its own context, credentials, and curated skill set — no
profile can see another's in-progress work. The separation enforces the
"no grading your own homework" principle: every handoff is a different agent with
a different perspective.

| Profile | Role | Writes code? |
|---|---|---|
| `validator-daedalus` | **Phase 1 — runs alone before any other agent.** Validates the issue: reproduces the bug, checks git history, detects duplicates. Scans for security threats (prompt injection, social engineering, credential exfiltration, auth-bypass, backdoor patterns, supply-chain attacks). Six possible outcomes: **CONFIRMED** (proceeds; summary prefix `CONFIRMED:` triggers Phase 2), **ALREADY_FIXED** (closes issue, pipeline ends), **DUPLICATE** (closes issue), **NEEDS_MORE_INFO** (blocks, comments asking reporter), **SECURITY_THREAT** (blocks, posts issue comment, sends `security-escalation` notification), **BLOCK_FOR_REVIEW** (high-privilege request lacks verifiable context — blocks, posts comment listing missing details, sends `security-escalation` notification). Posts a summary comment to the GitHub issue regardless of outcome. | No |
| `project-manager-daedalus` | Scope, acceptance criteria, decomposition, pre-ship checklist. Coordinates the team. Creates the conditional accessibility task when the issue references UI/frontend work. | No |
| `planner-daedalus` | Task graph, interface contracts, architecture decisions. | No |
| `developer-daedalus` | Implementation, tests, ship-gate, PR open. | Yes |
| `qa-daedalus` | **Runs after Developer, before Reviewer and Security-Analyst.** Runs the test suite, analyzes coverage gaps, and reports a QA verdict (`qa-passed` or `qa-failed`). | No |
| `reviewer-daedalus` | Code review — correctness, quality, performance. Approves or blocks with actionable findings. Runs after QA passes. | No |
| `security-analyst-daedalus` | Security audit — OWASP, injection, secrets, authn/z. Blocks on risk with severity-rated findings. Runs after QA passes, parallel to reviewer. | No |
| `accessibility-daedalus` | **Runs after QA passes, parallel to reviewer/security. Conditional — only created for UI/frontend issues.** Audits the PR against WCAG 2.1 AA and posts a findings table. Blocks with `approved` or `changes requested`. | No |
| `documentation-daedalus` | READMEs, ADRs, changelogs, completion report posted to the PR and chat channels. Waits for reviewer, security-analyst, and accessibility (when assigned) to clear. | No |

### Skills per profile

Each profile installs only the [agent-skills](https://github.com/addyosmani/agent-skills)
workflows relevant to its phase. Skills are curated process templates — an agent
follows the skill's checklist rather than winging the approach, which is what
makes the pipeline repeatable rather than demo-quality.

**`validator-daedalus`**

| Skill | What it governs |
|---|---|
| `debugging-and-error-recovery` | Reproduces the reported issue to confirm it still exists |
| `context-engineering` | Loads the minimal codebase context needed for accurate validation |
| `source-driven-development` | Verifies the issue description against the actual code state |
| `security-and-hardening` | Recognizes threat patterns: prompt injection, social engineering, auth bypass, backdoor requests, supply-chain attacks |
| `git-workflow-and-versioning` | Searches commit history for evidence the problem is already fixed |
| `using-agent-skills` | Meta-skill |

**`project-manager-daedalus`**

| Skill | What it governs |
|---|---|
| `idea-refine` | Structured divergent → convergent thinking; turns vague requests into buildable scopes |
| `spec-driven-development` | Requirements and acceptance criteria before any code exists |
| `planning-and-task-breakdown` | Decomposes a spec into ordered, verifiable work chunks |
| `shipping-and-launch` | Pre-launch checklist: risk review, rollback plan, monitoring |
| `using-agent-skills` | Meta-skill: skill discovery and invocation rules |

**`planner-daedalus`**

| Skill | What it governs |
|---|---|
| `spec-driven-development` | Requirements and acceptance criteria |
| `planning-and-task-breakdown` | Task graph with stable inter-task interface contracts |
| `context-engineering` | Loads the right context at the right time; avoids token waste |
| `source-driven-development` | Verifies assumptions against official docs before committing to an approach |
| `api-and-interface-design` | Stable interface definitions with clear contracts and evolution rules |
| `using-agent-skills` | Meta-skill |

**`developer-daedalus`**

| Skill | What it governs |
|---|---|
| `context-engineering` | Scoped context loading |
| `source-driven-development` | Docs-first verification before implementing |
| `incremental-implementation` | Thin vertical slices: implement → test → verify, one slice at a time |
| `test-driven-development` | Failing test first, then make it pass |
| `frontend-ui-engineering` | Production-quality UI with accessibility |
| `api-and-interface-design` | Interface-stable implementation |
| `debugging-and-error-recovery` | Reproduce → localize → fix → guard |
| `git-workflow-and-versioning` | Atomic commits, clean branch history |
| `using-agent-skills` | Meta-skill |

**`qa-daedalus`**

| Skill | What it governs |
|---|---|
| `test-driven-development` | Analyzes coverage gaps against the test pyramid; decides which scenarios are missing |
| `debugging-and-error-recovery` | Triages failing tests and flaky-test signals before reporting qa-failed |
| `git-workflow-and-versioning` | Inspects the diff to scope the test surface accurately |
| `using-agent-skills` | Meta-skill |

**`reviewer-daedalus`**

| Skill | What it governs |
|---|---|
| `code-review-and-quality` | Five-axis review: correctness, readability, architecture, security, performance |
| `code-simplification` | Identifies complexity that can be reduced without behavior change |
| `performance-optimization` | Measure first; optimize only what evidence shows matters |
| `test-driven-development` | Verifies test coverage is adequate |
| `debugging-and-error-recovery` | Traces potential failure paths in the diff |
| `git-workflow-and-versioning` | Reviews commit history quality |
| `using-agent-skills` | Meta-skill |

**`security-analyst-daedalus`**

| Skill | What it governs |
|---|---|
| `security-and-hardening` | OWASP prevention, input validation, least-privilege, secrets audit, injection/SSRF |
| `code-review-and-quality` | Quality gate alongside the security findings |
| `source-driven-development` | Verifies security claims against authoritative references |
| `debugging-and-error-recovery` | Traces exploit paths and edge-case failure modes |
| `using-agent-skills` | Meta-skill |

**`accessibility-daedalus`**

| Skill | What it governs |
|---|---|
| `frontend-ui-engineering` | DOM structure, form field semantics, and the production-quality UI patterns that underlie WCAG 2.1 AA compliance |
| `debugging-and-error-recovery` | Triages audit findings (contrast ratios, missing alt text, focus order) and reproduces keyboard-navigation failures |
| `using-agent-skills` | Meta-skill |

**`documentation-daedalus`**

| Skill | What it governs |
|---|---|
| `documentation-and-adrs` | READMEs, Architecture Decision Records, changelogs |
| `source-driven-development` | Verifies documentation accuracy against the actual code |
| `context-engineering` | Loads only the relevant merged changes into context |
| `using-agent-skills` | Meta-skill |

---

## Customizing agents

Every aspect of the agent roster is configurable per project in `.hermes/daedalus.yaml`
under `execution.profiles`. You can swap any role to your own Hermes profile, add extra
skills to any agent, or mix both. Only the keys you specify are overridden — unspecified
roles continue to use the built-in defaults.

### Custom profiles

Point any pipeline role at your own Hermes agent profile with the simple (name-only) form:

```yaml
execution:
  profiles:
    developer: my-senior-dev-profile
    reviewer:  my-code-reviewer-profile
```

Or use the dict form when you also want to add skills:

```yaml
execution:
  profiles:
    developer:
      profile: my-senior-dev-profile
    reviewer:
      profile: my-code-reviewer-profile
```

Both forms accept any role key: `validator`, `pm`, `developer`, `qa`, `reviewer`, `security`,
`accessibility`, `documentation`.

Built-in defaults (used for any unspecified role):

| Role | Default profile |
|---|---|
| `validator` | `validator-daedalus` |
| `pm` | `project-manager-daedalus` |
| `developer` | `developer-daedalus` |
| `qa` | `qa-daedalus` |
| `reviewer` | `reviewer-daedalus` |
| `security` | `security-analyst-daedalus` |
| `accessibility` | `accessibility-daedalus` |
| `documentation` | `documentation-daedalus` |

### Skills per agent

Attach extra Hermes skills to any agent without replacing its profile. Skills are passed
to the worker at task-creation time via `hermes kanban create --skill <name>`, so the
agent has them pre-loaded without needing to call `skill_view()` itself:

```yaml
execution:
  profiles:
    validator:
      profile: validator-daedalus   # can also omit to keep the default profile
      skills:
        - security-and-hardening
        - my-custom-threat-model
    developer:
      profile: my-senior-dev-profile
      skills:
        - incremental-implementation
        - my-project-conventions
```

You can mix simple (name-only) and dict forms in the same `profiles` block:

```yaml
execution:
  profiles:
    reviewer: my-reviewer            # simple form — just swap the profile
    developer:                       # dict form — profile + extra skills
      profile: my-senior-dev-profile
      skills:
        - incremental-implementation
```

The built-in profile skills installed by `postinstall.py` are always present.
`skills:` in the config adds **on top of** those — it never removes the built-in set.

### Delegating to Claude Code (or Codex)

By default every pipeline role does its own work using the **local Hermes LLM** (whatever
model your `default` profile is configured with). For coding-heavy roles you can instead
**delegate the actual work to an external CLI coding agent** — Claude Code, Codex, or
OpenCode — while Hermes stays in charge of orchestration (decompose → dispatch → review →
PR). This is the primary differentiator of running Daedalus **with** Claude Code versus
Daedalus alone: Hermes runs the pipeline, the coding agent writes the code.

**When to use it:** delegate when you want a frontier coding agent (Claude Code) doing the
implementation and review, but you still want Hermes managing the issue→PR lifecycle,
kanban board, gates, and notifications. Skip it (use the default `hermes`) when your
`default` profile's model is already strong enough and you'd rather keep everything in one
process.

**Supported values** for `execution.coding_agent`:

| Value | Behavior |
|---|---|
| `hermes` | **(default)** No external delegation. The role works directly with the local Hermes LLM. Used whenever `coding_agent` is unset, empty, or invalid. |
| `claude-code` | Delegate to the Claude Code CLI (one-shot `-p` mode). |
| `codex` | Delegate to the OpenAI Codex CLI (`exec` mode). |
| `opencode` | Delegate to the OpenCode CLI (`run` mode). |
| `none` | No delegation — same as `hermes`, the role codes directly. |

**Enable it project-wide** in `.hermes/daedalus.yaml`:

```yaml
execution:
  coding_agent: claude-code
  coding_agent_cmd: "CLAUDE_CONFIG_DIR=$HOME/.claude claude --dangerously-skip-permissions -p"
```

![.hermes/daedalus.yaml showing the execution block with coding_agent: claude-code and a per-role override (developer delegates to Claude Code, validator stays on the local Hermes LLM)](docs/screenshots/guide/14-coding-agent-config.png)

- `coding_agent_cmd` is the **full shell command** the agent pipes the task body into (not a
  shell alias). Use the absolute binary path + flags. When omitted, sensible per-agent
  defaults are used (`claude --dangerously-skip-permissions -p`, `codex exec --full-auto`,
  `opencode run`).
- Optional: `coding_agent_model` passes through to the agent's `--model` flag, and
  `coding_agent_max_turns` (default `10`) caps runaway loops.

**How it works:**

1. The **dispatcher** reads `execution.coding_agent`. When it resolves to a CLI agent
   (anything other than `hermes`/`none`), it **injects a `⚠️ AGENT DELEGATION` block** into
   each delegating role's task body with the exact steps to pipe the task into the agent.
2. The matching skill is **auto-attached** to the role's profile —
   `autonomous-ai-agents/claude-code` for `claude-code`, `…/codex` for `codex`,
   `…/opencode` for `opencode`. The role doesn't need to call `skill_view()` itself.
3. The role's **local Hermes LLM** loads that skill, writes the task body to a temp file,
   and **pipes it to the coding agent** via `nohup bash -c '...' &` (fully daemonized so
   the spawned agent survives the Hermes session exit).
4. The coding agent does the work (writes code, opens the PR), and its output is **relayed
   back as the role's completion signal** so the pipeline advances to the next phase.

**Per-role override.** Each role can choose its own agent via
`execution.profiles.<role>.agent`, which takes precedence over the global
`execution.coding_agent`. This lets you, e.g., have the developer delegate to Claude Code
while the validator stays on the local Hermes LLM:

```yaml
execution:
  coding_agent: hermes            # default for every role…
  profiles:
    developer:
      profile: my-senior-dev-profile
      agent: claude-code          # …but the developer delegates to Claude Code
      skills:
        - incremental-implementation
    reviewer:
      agent: codex                # the reviewer uses Codex
    validator:
      agent: hermes               # the validator stays on the local LLM
```

Any role key works: `validator`, `pm`, `developer`, `qa`, `reviewer`, `security`,
`accessibility`, `documentation`.

### Profile fallback behavior

When a configured profile does not exist in Hermes (checked via
`~/.hermes/profiles/<name>/` directory or `<name>.yaml` file), daedalus can either
fall back or skip:

```yaml
execution:
  profile_fallback_behavior: "fallback"   # default
  # profile_fallback_behavior: "skip"
```

| Value | Behavior |
|---|---|
| `fallback` | (default) Log a warning, use the built-in default profile for that role. Dispatching continues normally. |
| `skip` | Log a warning and drop the role entirely — no tasks are created for that role until the profile exists. |

### Comment attribution template

Every comment any agent or the dispatcher posts to a VCS issue or PR begins with a one-line attribution header so it's always clear which pipeline role wrote it:

```
**Agent: developer**
```

You can change the format project-wide in `.hermes/daedalus.yaml`:

```yaml
execution:
  comment_header_template: "**Agent: {role}**"   # default
```

**Available placeholders:**

| Placeholder | Value |
|---|---|
| `{role}` | Pipeline role name — `validator`, `project-manager`, `developer`, `qa`, `reviewer`, `security-analyst`, `accessibility`, `documentation`, `daedalus` |
| `{profile}` | Hermes profile name for the role (empty if using the built-in default) |
| `{issue}` | Issue reference, e.g. `#42` (empty when not applicable) |
| `{pr}` | PR reference, e.g. `#7` (empty when not applicable) |

**Examples:**

```yaml
# Include the profile name alongside the role
comment_header_template: "**Agent: {role}** | {profile}"

# Emoji style
comment_header_template: "🤖 _{role} agent_"

# Plain text (no markdown bold)
comment_header_template: "Agent: {role}"
```

The template is applied to all dispatcher-posted comments (PR size warnings, forbidden-file alerts, staleness notices) and is embedded in each agent's task instructions so agent-authored comments follow the same pattern. The default `**Agent: {role}**` is consistent with the `**Agent: documentation**` sentinel the dispatcher uses internally to detect doc reports.

---

## Autonomous pipeline advancement

Each phase transition is triggered by a **completion hook** in every agent's SOUL.md.
When an agent reaches any terminal state — marking its task **done**, blocking with
**review-required**, blocking with **awaiting-fix**, or any other blocked state — it
immediately runs:

```bash
bash ~/.hermes/scripts/daedalus-cron.sh
```

This means each phase transition starts within seconds rather than waiting for the
hourly cron tick. For example, as soon as the developer blocks with `review-required`,
the dispatcher fires, detects CI green, and promotes the reviewer task — all within
seconds.

**Error recovery:** If the state-transition call itself fails ("already terminal" —
a known platform bug where Hermes marks tasks done prematurely), agents are instructed
to run the dispatcher anyway. The pipeline never waits for a human to manually trigger
recovery.

The cron job is still present as a last-resort safety net (in case an agent crashes
before reaching its final step), but it is no longer the primary advancement mechanism.

The result is a fully autonomous pipeline: once an issue is marked Ready, the entire
validator → PM → developer → QA → reviewer + security-analyst + accessibility chain runs
end-to-end without any human or scheduler intervention between phases.

```
issue marked Ready
      │
      ▼
validator runs → CONFIRMED: <note>
      │   └─ agent runs daedalus-cron.sh on any terminal state
      ▼
PM / project-manager runs → SPEC: <note>
      │   └─ agent runs daedalus-cron.sh on any terminal state
      ▼
developer → review-required
      │   └─ agent runs daedalus-cron.sh → dispatcher detects CI green → QA starts
      ▼
QA → qa-passed (or qa-failed → dev fix card)
      │   └─ agent runs daedalus-cron.sh → dispatcher creates reviewer + security-analyst
      │       + accessibility (only when UI/frontend keywords present in issue)
      ▼
reviewer → approved
security-analyst → cleared
accessibility → approved (or accessibility-na if no frontend files changed)
      │   └─ all three run in parallel; each agent runs daedalus-cron.sh on its terminal state
      ▼
documentation → done → report posted
```

---

## Webhook configuration

The dispatcher can advance instantly when an issue is marked **Ready** via an
inbound webhook — no waiting for the next cron tick. The webhook normalizer
(`core/webhook_normalizer.py`) parses payloads from **GitHub**, **GitLab**,
**Azure DevOps**, and **Hermes Kanban**, extracting a `ReadyEvent` when an item
transitions to the configured ready status.

### 1. Enable the webhook server

The Hermes gateway hosts the webhook endpoint. Enable it in `~/.hermes/config.yaml`:

```yaml
platforms:
  webhook:
    host: "0.0.0.0"        # bind address (default: 0.0.0.0)
    port: 8644             # HTTP port (default: 8644)
    secret: "your-hmac-secret"  # shared secret for HMAC-SHA256 signature verification
```

The gateway must be restarted after changing webhook config: `hermes gateway restart`.

### 2. Expose the gateway to the internet

VCS providers need a public URL to deliver webhooks. Options:

| Method | Command | Notes |
|---|---|---|
| **Hermes portal** (built-in) | `hermes portal` | Free, auto-provisioned Cloudflare tunnel. Easiest. |
| **Cloudflared** (manual) | `cloudflared tunnel --url http://localhost:8644` | Free tunnel; use when you need a stable URL. |
| **ngrok** | `ngrok http 8644` | Free tier available; good for quick local testing. |

All three expose port 8644 to the internet. Copy the public URL they provide — that's your webhook base URL.

### 3. Register the webhook with your VCS provider

Set the **Payload URL** to `<your-public-url>/webhook/daedalus` and use the
**secret** from step 1 for HMAC-SHA256 signature verification:

- **GitHub:** Settings → Webhooks → Add webhook. Set Payload URL, content type
  to `application/json`, **Secret** to the shared secret, and subscribe to
  `projects_v2_item` events.
- **GitLab:** Settings → Webhooks. Set the URL, **Secret token** to the shared
  secret, and enable `Issue` events.
- **Azure DevOps:** Project Settings → Service hooks → Web Hooks. Trigger on
  `Work item updated`. (Azure doesn't support HMAC — use the `on_session_end`
  fallback for Azure, or poll via the cron tick.)

### 4. Dead-man's-switch: local-only setups still work

**Webhooks are a latency optimization, not a requirement.** The dispatcher's
ready-polling logic runs on every cron tick regardless of webhook state, and
the `on_session_end` fallback catches any session that finishes without a
webhook. If the tunnel drops, the secret is wrong, or webhooks are disabled
entirely, issues still get dispatched — just with cron-tick latency instead of
instant.

```
issue marked Ready
      │
      ├─► webhook delivered instantly ──► normalizer ──► dispatch (fast path)
      │
      └─► cron tick (next interval) ──► poll ──► dispatch (fallback)
      │
      └─► on_session_end ──► dispatch (last-resort fallback)
```

Both paths are idempotent: an issue dispatched via webhook won't be
re-dispatched by the next polling tick (the card already exists on the board).

---

## Self-healing loop

`core/iterate.py` runs on every cron tick after the main dispatch. It scans every
blocked card and routes it to the agent that can clear it — the pipeline never
stalls waiting for a human unless it has already retried 3 times.

**Validator None-summary recovery.** When a validator agent's context window fills before `kanban_complete(summary=...)` runs, its kanban summary is `None`. Without recovery this causes the entire downstream pipeline to ghost-complete with no code written (all downstream agents hit a HARD STOP checking for `CONFIRMED:`). The dispatcher handles this in two stages:

1. **GitHub-comment fallback** — scans the issue for a comment with the mandatory `**Agent: validator**` attribution header and looks for `CONFIRMED` in the body. If found, advances directly to PM without re-running the validator.
2. **Validator retry** — if no confirming comment exists, re-queues the validator with a per-run idempotency key (`validator-retry-N-r1`, `validator-retry-N-r2`), capped at 2 retries before human escalation.

The validator SOUL.md also instructs the agent to call `hermes kanban show <tid>` after `kanban_complete` and re-issue the complete call if `latest_summary` is null.

**PM stale-task recovery.** If the Hermes platform prematurely marks a PM task done
before the agent finishes (a known platform-level bug), the task is left with no
`assigned:` summary. The dispatcher detects this "stale" state on the next tick and
re-creates the PM task with a new idempotency key (`pm-{n}-r1`, `pm-{n}-r2`),
capped at 3 retries. Previously this stalled the pipeline indefinitely.

```
blocked card detected
        │
        ├─ developer card + CI green + review-required?
        │       └──► advance
        │             complete the developer card
        │             _create_downstream_review_tasks() fires:
        │               creates reviewer, security-analyst, docs tasks
        │               idempotency keys: reviewer-{n}, security-{n}, docs-{n}
        │               skips any whose key already exists on the board
        │
        ├─ developer card + CI red?
        │       └──► dev_fix_ci
        │             create an idempotent fix card assigned to developer-daedalus
        │             key: fix-ci-{card_id}-attempt-{N}
        │             only fires when CI is definitively RED (not UNKNOWN/PENDING)
        │
        ├─ reviewer or security-analyst card + changes requested?
        │       └──► pm_route
        │             create a PM routing card assigned to project-manager-daedalus
        │             PM reads the findings and decides the fix owner:
        │               developer-daedalus, security-analyst-daedalus, or re-spec
        │             the reviewer/security card is marked "awaiting-fix"
        │
        ├─ reviewer or security-analyst card + approved?
        │       └──► approve_advance
        │             complete the card; next stage starts
        │
        └─ any action, attempt count > 3?
                └──► escalate
                      post a comment to the card
                      leave it blocked for a human
                      no new fix cards are ever created beyond this cap
```

**Idempotency.** Fix cards are keyed `fix-ci-{card_id}-attempt-N` and
`pm-route-{card_id}-attempt-N`. Before creating one, the loop cross-checks the
live board for a card with that key — multiple dispatcher instances (or a restart
mid-tick) never double-create fix cards. Attempt counts survive across ticks in
`.hermes/daedalus-fix-attempts.json`.

**Awaiting-fix unblock.** When a developer fix card completes, the loop
automatically unblocks any reviewer or security-analyst cards that were marked
"awaiting-fix" for that issue. They re-enter the queue without human intervention.

**Escalation cap.** `MAX_FIX_ATTEMPTS = 3`. After three attempts the loop posts a
comment, leaves the card blocked, and stops. The pipeline never runs away — every
blocked card has a finite ceiling and exactly one deterministic path forward.

---

## Design decisions

Each piece exists because the obvious approach failed:

- **Two-phase dispatch with hard validator gate** — the dispatcher creates *only* the
  `validator-daedalus` task in Phase 1. Developer, reviewer, security-analyst, and
  documentation tasks do not exist yet — they cannot be dispatched, period. On every cron tick,
  the dispatcher scans done validator tasks for a `CONFIRMED:` summary prefix; only then does it
  create the Phase 2 downstream triage and decompose it. This enforcement is structural (no tasks
  to dispatch), not instructional (agent is told to check). Six validator outcomes:
  **CONFIRMED** (`CONFIRMED: <note>` summary triggers Phase 2),
  **ALREADY_FIXED** (closes issue, no Phase 2),
  **DUPLICATE** (closes issue, no Phase 2),
  **NEEDS_MORE_INFO** (blocks, comments asking reporter; pipeline waits for a human to re-Ready),
  **SECURITY_THREAT** (posts issue comment, fires `security-escalation` notification, blocks),
  **BLOCK_FOR_REVIEW** (high-privilege action lacking verifiable context — identity, business justification,
  approval ticket; posts comment listing exactly what's missing, fires `security-escalation`, blocks).
  All blocking outcomes also auto-move the VCS board card to a "Blocked" column, creating it
  automatically if it doesn't exist.
  Every role posts a mandatory summary comment on the GitHub issue after completing — the issue
  history always reflects the current pipeline state, not just the internal kanban board.
- **Ready-gating** — the dispatcher works *only* issues you put in `Ready`. You stay in
  control of what the fleet touches; it never surprises you by grabbing the backlog.
- **Ship-gate** — before pushing, the developer agent detects the project's configured
  lint and format tools and runs them: `.pre-commit-config.yaml` → `pre-commit run --all-files`;
  `package.json` lint/format scripts → `npm run lint && npm run format`;
  `pyproject.toml` ruff config → `ruff check --fix && ruff format`;
  `Makefile` lint target → `make lint`. Skips gracefully when nothing is configured.
  Auto-fixes are committed before the PR is opened. A "remember to run linting" note
  in agent memory was skipped repeatedly; a gate in the task instructions cannot be.
- **Triage + decompose** — real separation of concerns across specialist agents
  (developer / reviewer / security-analyst / documentation), not one agent grading its
  own homework.
- **Auto-advance** — workers *block for review* instead of completing, which stalls the
  chain. The dispatcher completes a review-required handoff once its PR's CI is green,
  so the pipeline is genuinely hands-off (the PR still waits for a human merge).
- **Post-developer handoff safety net** — when `_execute_advance()` completes a developer
  card, it calls `_create_downstream_review_tasks()` as a guard. If the initial Phase 2
  decompose (triage → developer + reviewer + security + docs) failed to create any of the
  downstream tasks, this path creates them with idempotent keys (`reviewer-{n}`,
  `security-{n}`, `docs-{n}`). Any task that already exists on the board (any status) is
  skipped — re-runs never duplicate. This closed a production gap where reviewer, security,
  and docs tasks had to be manually created by the human operator (issue #21).
- **Self-healing loop** (`core/iterate.py`) — every blocked card is classified into one
  of 5 actions and routed to the agent that can clear it:
    - `advance` — dev PR green + review-required → complete dev card, then `_create_downstream_review_tasks()` creates reviewer/security/docs tasks (idempotent keys `reviewer-{n}`, `security-{n}`, `docs-{n}`; skips any that already exist)
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
carries its own checked-in `<repo>/.hermes/daedalus.yaml` (scaffolded by the
dashboard's **+ Add Project** or `scripts/setup.sh`) and is listed in the
registry at `~/.hermes/daedalus/projects`. Each project picks its own provider,
board, base branch, schedule, and channels:

```yaml
# app-one/.hermes/daedalus.yaml
name: app-one
repo: ORG/app-one
workdir: /path/to/app-one
vcs: { provider: github, target_branch: dev }
tracking: { github_project_number: 1 }
cron:
  schedule: "every 60m"
  notifications:
    - { platform: Slack, target: "slack:C0CHANNEL1", events: [doc-report, pipeline-failure] }

# api-two/.hermes/daedalus.yaml
name: api-two
repo: group/api-two
vcs: { provider: gitlab, target_branch: main }
tracking: { label_board: true }
cron: { schedule: "every 2h", deliver: "discord:#api-two" }
```

Each repo gets its **own kanban board**, its **own cron job** (edits update it in
place), and its **own ship-gate policy** (keyed by the repo's origin remote).
Onboarding a repo = one dashboard click (or `setup.sh`) + a `Ready` column/label
on its board.

---

## Repository layout

| Path | What it is |
|------|------------|
| `scripts/daedalus_dispatch.py` | The deterministic dispatch tick (cron entrypoint, `--no-agent`). Ready-gating, reconcile, decompose, auto-advance, merged→close. |
| `core/iterate.py` | Self-healing loop: classify blocked cards into 5 actions, idempotent fix-card creation, iteration cap + escalation, reviewer re-engage after fix. |
| `core/notify_templates.py` | Rich markdown notification templates (dispatch summary, doc report envelope, PR-ready, pipeline-failure) with clickable issue/PR links for every Hermes messaging platform. |
| `scripts/provision_roster.sh` | Provisions the 9-agent Hermes roster. |
| `core/providers/` | VCS provider layer: GitHub (REST + GraphQL Projects v2), GitLab (REST), Azure DevOps (REST/WIQL) — token-authenticated HTTPS APIs, extensible via `register_provider()`. |
| `core/kanban.py` | Thin, idempotent wrapper over `hermes kanban` (triage, decompose, complete). |
| `config/` | `ConfigLoader` (defaults + per-repo merge), `validate_vcs`, and the config template. |
| `dashboard/` | Dashboard tab: project grid, add/edit project modals, notifications editor (`plugin_api.py` + React `src/App.jsx`). |
| `tests/` | Unit tests — config, providers (mocked HTTP), dispatcher, dashboard API, installers. |

The **ship-gate hook**, **cron wrapper**, and **roster profiles** live in the Hermes
home (`$HERMES_HOME`), not here — see [`SETUP.md`](SETUP.md) for how they're deployed
and shared across a team.

---

## Prerequisites

| Requirement | Why |
|---|---|
| [Hermes](https://herm.es) installed + model auth | The runtime everything runs on |
| `bun` | Dashboard build (only needed if you modify `dashboard/src/`) |

**Everything else is automatic.** Clicking **Install Agents** in the dashboard (or running `postinstall.py`) auto-installs [agent-skills](https://github.com/addyosmani/agent-skills) if it is missing. `pyyaml` ships inside the Hermes venv. The developer agent auto-detects the project's lint/format tooling at ship time — no specific tool is required up front.

**No VCS CLIs needed — ever.** Everything (dispatcher, dashboard, AND worker
agents) talks to your VCS host via its **HTTPS API** with a token from the
environment. Worker `git push` authenticates through a per-profile credential
store written by the roster provisioner; PRs and comments go through the
provider API with the token already in each worker's env.

## VCS providers

The provider is **auto-detected from the repo's `origin` remote** (github.com,
gitlab hosts — incl. self-hosted `base_url`, dev.azure.com / *.visualstudio.com
— incl. org/project/repo) by both `setup.sh` and the dashboard's Add Project
(leave the provider on "Auto-detect" and the repo field empty). You can always
pin it manually in `.hermes/daedalus.yaml` (`vcs.provider`) or the dropdown. Tokens are read **only from environment
variables** — never from config files — and are redacted from all errors/logs.
Override the env var name per project with `vcs.token_env`.

**Where to put the tokens:** add them to **`~/.hermes/.env`**
(e.g. `GITHUB_TOKEN=ghp_...`) — Hermes loads that file at startup, which covers
the dispatcher cron and the dashboard (restart the gateway + dashboard after
editing). Also export them in your shell before running
`scripts/provision_roster.sh` so each worker profile gets them seeded into its
own `.env` / `.git-credentials` / `terminal.env_passthrough`.

| Provider | `vcs.provider` | Token env (default) | Minimal token scopes | Board model |
|---|---|---|---|---|
| GitHub | `github` | `GITHUB_TOKEN` / `GH_TOKEN` | see [Creating the tokens](#creating-the-tokens-pat-scopes) | Projects v2 (`tracking.github_project_number`) |
| GitLab | `gitlab` | `GITLAB_TOKEN` | `api` + `write_repository` | Issue-Board labels (`tracking.label_board: true`; lists keyed to `vcs.status_map` labels). Self-hosted via `vcs.base_url` |
| Azure DevOps | `azuredevops` | `AZURE_DEVOPS_PAT` | Work Items R&W, Code R&W, Build Read | Work-item states (`vcs.org` + `vcs.project` + `vcs.repo`; `vcs.work_item_type`, default `Issue`) |

### Creating the tokens (PAT scopes)

One token per provider covers everything daedalus does with it: dispatcher
polling + board moves, dashboard pickers, worker `git push` (via the
per-profile credential store), and PR create/comment API calls.

**GitHub — fine-grained PAT** (github.com → Settings → Developer settings →
Fine-grained tokens). Grant access to the repos daedalus drives, with:

| Permission | Level | Used for |
|---|---|---|
| Contents | **Read and write** | workers push branches |
| Pull requests | **Read and write** | open PRs, post the doc report |
| Issues | **Read and write** | poll Ready issues, close on merge |
| Commit statuses + Checks | Read | CI-green gating |
| Metadata | Read | (mandatory baseline) |
| Projects *(organization permission)* | **Read and write** | Projects v2 board sync |

> Org-owned Projects v2 boards require your org to allow fine-grained PATs.
> If that's not enabled, use a **classic PAT** with `repo` + `project` scopes
> (+ `workflow` only if agents will edit `.github/workflows/`).

**GitLab — personal access token** (GitLab → Preferences → Access tokens):
`api` (covers issues, boards/labels, MRs, notes, pipelines) and
`write_repository` (workers push over HTTPS). Same scopes on self-hosted.

**Azure DevOps — PAT** (dev.azure.com → User settings → Personal access
tokens), scoped to your organization:
- **Work Items: Read & Write** — poll/close/move work items
- **Code: Read & Write** — list PRs/branches, create PRs + threads, worker pushes
- **Build: Read** — CI status on PRs

**Security tips:** prefer a dedicated bot/machine account so PRs and comments
are attributed to it (pass its token as `ROSTER_GH_TOKEN` when provisioning);
set an expiry and rotate; if you want the dispatcher even more locked down,
give it its own read-mostly token via `vcs.token_env` and keep the write
token only in the worker profiles.

The canonical pipeline statuses (`ready` / `in_progress` / `in_review` / `done`)
map to your board's column/label/state names via `vcs.status_map` — defaults are
`Ready` / `In progress` / `In review` / `Done`.

Other trackers (Jira, Linear, Gitea, Bitbucket, …) plug in by implementing the
`core/providers/base.py` interface and calling `register_provider()` — the
dispatcher and dashboard never need to change.

## Notifications

Reports and tick summaries go to **any configured Hermes messaging platform**
via `hermes send` — Slack, Discord, Telegram, Signal, WhatsApp, SMS, etc. Two
modes per project:

- **Single target** (`cron.deliver: "slack:C123"`) — the cron delivers the
  dispatcher's summary; doc reports go to the same target.
- **Multi-target** (`cron.notifications`) — a list of `{platform, target,
  events}` entries; each channel picks which events it receives
  (`doc-report`, `dispatch-summary`, `pipeline-failure`, `pr-ready`,
  `security-escalation`; omit `events` to receive everything).
  Route `security-escalation` to a high-visibility channel (e.g. `#security-alerts`)
  — it fires on SECURITY_THREAT and BLOCK_FOR_REVIEW for immediate human review.
  Configure it in the dashboard's **Notifications** editor — channels are discovered
  from `hermes send --list`, with manual entry as fallback.

All notifications are rendered as **rich structured markdown** with clickable
links to issues and PRs — `[#15](url)` links that render as hyperlinks on Slack,
Teams, Discord, and every other Hermes-supported platform. The dispatch summary
includes per-project sections for dispatched issues, completions, advanced PRs,
auto-remediation actions, and delivered doc reports. Documentation reports are
wrapped in a structured envelope with a header, navigation links, and issue
cross-reference before delivery.

## Troubleshooting

**macOS "Keychain Not Found" prompt during install?** It's a benign interaction
between git's `osxkeychain` credential helper and a public-repo clone — no
credentials are needed and nothing is exposed. Click **Cancel** (NOT "Reset To
Defaults", which resets your login keychain). To suppress it, either unlock your
login keychain or set a non-keychain helper:
`git config --global credential.helper ""`.

## Quickstart

**1. Install the plugin:**
```bash
hermes plugins install benmarte/daedalus --enable
hermes gateway restart            # load the plugin
```

![Hermes Plugins page — Daedalus listed as installed and enabled](docs/screenshots/guide/00-plugins-page.png)

> **macOS note:** on macOS without launchd management, `hermes gateway restart` falls
> back to running the gateway as a **background process**. It works, but does NOT
> auto-start at login or auto-restart on crash.

**2. Provision the agent roster** — open `hermes dashboard` → **Daedalus** tab →
click **Install Agents**. This auto-installs agent-skills if missing and creates the
9 specialist profiles (takes ~10–20 s). Or from the terminal:
```bash
python3 ~/.hermes/plugins/daedalus/scripts/postinstall.py
hermes profile list               # expect: validator developer reviewer security-analyst documentation planner project-manager qa accessibility
```

![Daedalus dashboard on fresh install — Install Agents banner prompts provisioning](docs/screenshots/guide/01-install-agents-banner.png)

**3. Onboard a target repo** — either click **”+ Add Project”** in the dashboard
(scaffolds the config, registers the repo, creates its kanban board + cron), or
from the terminal:
```bash
cd /path/to/your/repo
bash ~/.hermes/plugins/daedalus/scripts/setup.sh
# then edit .hermes/daedalus.yaml (vcs provider, tracking, sources, cron) — repo/workdir are fixed
```

![Add Project Step 1 — paste the repo path and Daedalus auto-detects the provider and repo slug](docs/screenshots/guide/05-add-project-step1-filled.png)

Prefer hand-writing the config? That works too: copy
[`templates/daedalus.yaml`](templates/daedalus.yaml) to `<repo>/.hermes/daedalus.yaml`,
edit it, then run `setup.sh` once to register the repo (it never overwrites an
existing config without `--force`) — or skip the registry entirely and run a
single repo with `daedalus_dispatch.py --repo /path/to/repo`.

Export the provider token for the dispatcher's environment (see
[VCS providers](#vcs-providers)), e.g. `GITHUB_TOKEN`, `GITLAB_TOKEN`, or
`AZURE_DEVOPS_PAT`.

**4. Trigger work** — all three sources are **enabled by default** (toggle any
off in the config):
- **Prompt / spec file:** `hermes kanban create --triage --workspace dir:$PWD --body "$(cat spec.md)"`
- **Spec drop:** put a `*.md` in `<repo>/.hermes/pending/` (when `sources.local_specs.enabled`)
- **VCS issue:** move an issue/work item to **Ready** — GitHub Project column,
  GitLab `Ready` board label, or Azure DevOps work-item state

The triage card decomposes across the roster → developer opens a PR → reviewer + security
gate it → CI-aware auto-advance → documentation posts the resolution **on the PR** and your
configured chat channels. You merge (agents never merge `main`).

**5. Visual config + status** — `hermes dashboard` → the **Daedalus** tab: a card per
project with live status (kanban counts, open PRs + CI, needs-attention, cron), and an
editor for each project's config (`repo`/`workdir` are read-only).

![Hermes kanban board for a Daedalus project showing a live pipeline — cards spread across Todo, Blocked, and Done columns](docs/screenshots/guide/10-kanban-board.png)

**6. Automate** — schedule the dispatcher so advancing/onboarding run unattended:
```bash
hermes cron add daedalus --schedule "every 3m" \
  --script "python3 ~/.hermes/plugins/daedalus/scripts/daedalus_dispatch.py"
```

---

## Team setup

See **[`SETUP.md`](SETUP.md)** for everything beyond the single-machine quickstart:

- **Sharing across teammates** — the repo is secret-free; each person runs `provision_roster.sh` locally with their own LLM keys and VCS tokens. No shared profile exports (those bundle credentials).
- **Per-project conventions** — custom `status_map` column names, branch prefixes, bot identity (`ROSTER_GH_TOKEN` / `ROSTER_BOT_NAME`), and how to pin a dedicated read-mostly dispatcher token.
- **Multi-user cron** — running the dispatcher on a shared server so the pipeline advances without anyone's laptop being open.
- **Full installation guide** — step-by-step with screenshots: [`docs/INSTALLATION_GUIDE.md`](docs/INSTALLATION_GUIDE.md).

---

## Uninstall / reset

**Option A — dashboard button (easiest):** open `hermes dashboard` → Daedalus tab →
scroll to the footer → click **Uninstall Daedalus**. It removes profiles, cron jobs,
kanban boards, config, and the plugin package in one go, with a confirmation dialog
before anything is deleted.

**Option B — terminal:**
```bash
# HERMES_HOME defaults to ~/.hermes — set it if yours is elsewhere
bash "$HERMES_HOME/plugins/daedalus/scripts/uninstall.sh"
```

This single command removes profiles, cron jobs, kanban boards, config, AND the
plugin package in one go. It shows a data-loss summary first so you can review
what will be removed before confirming (or use `-y` for scripting).

> **Do NOT use `hermes plugins uninstall daedalus` alone** — that only deletes
> the plugin directory and leaves profiles, cron jobs, boards, config, and
> hook artifacts behind. Hermes has no uninstall hook for plugins to clean up
> after themselves. Use the dashboard button or `uninstall.sh` for a complete uninstall.

```bash
# Skip the plugin removal (keep daedalus installed, reset host state only):
bash "$HERMES_HOME/plugins/daedalus/scripts/uninstall.sh" --keep-plugin

# Keep the 9 agent profiles:
bash "$HERMES_HOME/plugins/daedalus/scripts/uninstall.sh" --keep-profiles

# Both — keep profiles AND the plugin, reset everything else:
bash "$HERMES_HOME/plugins/daedalus/scripts/uninstall.sh" --keep-profiles --keep-plugin

# Non-interactive (scripting / CI):
bash "$HERMES_HOME/plugins/daedalus/scripts/uninstall.sh" -y
```

The uninstall script is idempotent — safe to re-run; absent items are skipped
without error.

---

## Known limitations

- **Restart the dashboard server after install/update.** The Hermes dashboard loads
  each plugin's `plugin_api.py` once at startup and does NOT hot-reload. After
  `hermes plugins install/update daedalus`, you must restart the dashboard server
  **and** reload the browser tab for backend changes (e.g. saving/creating a cron job)
  to take effect. Restarting the gateway alone is not enough.

- **Uninstall with `scripts/uninstall.sh`, not `hermes plugins uninstall` alone.**
  Core Hermes has no plugin-uninstall hook — `hermes plugins uninstall daedalus` only
  deletes the plugin folder and leaves roster profiles, cron jobs, kanban boards, and
  config behind. Use [`scripts/uninstall.sh`](scripts/uninstall.sh) for a complete
  uninstall (see [Uninstall / reset](#uninstall--reset)).

- **macOS gateway: no launchd in some setups.** `hermes gateway restart` can't use
  launchd on some macOS versions and falls back to a background process. It works, but
  won't auto-restart on crash or auto-start at login.

- **Agents can't message chat platforms directly.** Notifications and reports are
  delivered by the deterministic dispatcher (root cron context), not by individual
  agents. Set channels via the config modal's Notify Via dropdown or the multi-target
  Notifications editor, and use the **Send test message** button to verify connectivity.

- **GitLab/Azure worker flows are less battle-tested than GitHub.** The dispatcher
  and dashboard are fully provider-backed (mocked-API test suites for all three),
  but worker agents pushing branches/opening MRs on GitLab/Azure need their own
  credentials in the profile environment (`GITLAB_TOKEN` / `AZURE_DEVOPS_PAT`) and
  have not been dogfooded end-to-end yet — beta feedback welcome.

- **Single-machine validation.** This beta has been dogfooded on one machine.
  Cross-machine and multi-user behavior is exactly what beta feedback should surface —
  please report anything unexpected.

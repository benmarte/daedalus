# Daedalus — autonomous issue → PR pipeline on native Hermes

Flag an issue **Ready** — on **GitHub**, **GitLab**, or **Azure DevOps** — and a
roster of AI agents implements it, reviews it, security-audits it, documents it,
and opens a **green, mergeable PR** — with quality gates that *cannot* be skipped,
full board/issue tracking, and zero babysitting. A single Daedalus deployment
drives **many repos**, each with its own provider, kanban board, cron job, and
notification channels (Slack, Discord, Telegram, Signal, WhatsApp, …).

```mermaid
flowchart TD
    subgraph Reliability["🛡 Pipeline reliability layer"]
        direction LR
        WD["🐕 Gateway watchdog\ndetects stalled dispatcher\n· 3 restarts/hr cap\n· exponential backoff"]
        MX["🔒 FileLock mutex\n· concurrency-safe\n· 300s stale timeout"]
        ID["🔑 Idempotency keys\n· per-role per-stage\n· prevents duplicate cards"]
    end

    A([🏁 Issue marked Ready\nGitHub · GitLab · Azure]) -->|cron tick · webhook\nor completion signal| MX

    MX --> B["Dispatcher\ndaedalus_dispatch.py"]
    B --> Epic{"Epic-sized?\n≥4 checklist items\nepic label · body ≥2000 chars"}
    Epic -->|yes| P["🗺 Phase 3 — Planner\nScopes work · defines interfaces\nDecomposes into sub-issues"]
    Epic -->|no| C

    P -->|"PLANNING COMPLETE"| SubI(["📋 Sub-issues created\nEach follows the full pipeline"])
    P -->|"NOT SUITABLE\nfor decomposition"| C

    C["⚡ Phase 1 · Validator\ntask created\n(idempotency key enforced)"]
    C --> V{Validator\nOutcome}

    V -->|"CONFIRMED: <note>"| E["📋 Phase 2\nPM decomposes work\nacross team roster"]
    V -->|"ALREADY_FIXED\nor DUPLICATE"| AF(["✅ Issue closed\nPipeline ends"])
    V -->|NEEDS_MORE_INFO| NI(["⏸ Card blocked\nComment posted\nAwaits reporter response"])
    V -->|"SECURITY_THREAT\nor BLOCK_FOR_REVIEW"| ST(["🔒 Card blocked\nIssue comment posted\nsecurity-escalation fired"])
    V -->|"None summary"| Reco["🔄 Validator retry\nGitHub-comment fallback\nthen retry capped at 2"]
    Reco --> V

    E --> Dev["👨‍💻 Developer\nImplement · test\nShip-gate · open PR"]
    Dev --> QAGate{{"🧪 QA GATE checkpoint\ntest suite · coverage\ncard summary: qa-passed\nor qa-failed"}}
    QAGate --> CI{CI}
    CI -->|green| Rev["🔍 Reviewer\nCode review\nApprove / request changes"]
    CI -->|green| A11y["♿ Accessibility\nWCAG 2.1 AA audit\n(conditional on UI work)"]
    CI -->|red| Fix["🔧 Fix card created\nidempotent · capped at 3\nunique retry key pm-{n}-r{k}"]
    Fix --> Dev
    Rev -->|approved| Sec["🛡 Security Analyst\nOWASP audit\nSecrets · injection · authz"]
    Sec -->|cleared| Doc["📝 Documentation\nADRs · changelog\nReport → PR + chat channels"]
    A11y -->|cleared| Doc
    Doc --> AutoMerge{{"🚦 QA auto-merge gate\nQA-passed signal present\nOR skip-qa label?"}}
    AutoMerge -->|"yes"| Merge(["🔀 Auto-merge fires\nPR merged automatically"])
    AutoMerge -->|"no signal yet"| Wait(["⏳ Monitor polls\nuntil qa-passed\nor skip-qa label appears"])
    Wait --> AutoMerge
    Merge --> Done(["✅ Issue closed\nCard → Done"])
    Merge --> Reconcile["🩹 reconcile_merged\nheals cards closed\noutside the pipeline"]

    WD -. monitors .-> B
    Reconcile -.-> Done

    style A fill:#1976D2,color:#fff,stroke:#0D47A1
    style P fill:#6A1B9A,color:#fff,stroke:#4A148C
    style SubI fill:#1565C0,color:#fff,stroke:#0D47A1
    style AF fill:#757575,color:#fff,stroke:#424242
    style NI fill:#F57C00,color:#fff,stroke:#E65100
    style ST fill:#C62828,color:#fff,stroke:#B71C1C
    style Merge fill:#388E3C,color:#fff,stroke:#1B5E20
    style AutoMerge fill:#FF8F00,color:#fff,stroke:#E65100
    style QAGate fill:#FF6F00,color:#fff,stroke:#E65100
    style Wait fill:#5D4037,color:#fff,stroke:#3E2723
    style Done fill:#2E7D32,color:#fff,stroke:#1B5E20
    style Reco fill:#AD1457,color:#fff,stroke:#880E4F
    style Reconcile fill:#0277BD,color:#fff,stroke:#01579B
    style WD fill:#37474F,color:#fff,stroke:#263238
    style MX fill:#37474F,color:#fff,stroke:#263238
    style ID fill:#37474F,color:#fff,stroke:#263238
```

![Daedalus dashboard — one card per managed project, showing kanban counts, open PRs with CI status, and cron schedule](docs/screenshots/guide/09-dashboard-with-project.png)

---

> **New here?** The step-by-step setup guide with live screenshots is in
> [`docs/INSTALLATION_GUIDE.md`](docs/INSTALLATION_GUIDE.md).

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
- [Dashboard REST API](#dashboard-rest-api)
- [Development references](#development-references)
- [Prerequisites](#prerequisites)
- [VCS providers](#vcs-providers)
  - [Creating the tokens (PAT scopes)](#creating-the-tokens-pat-scopes)
- [Notifications](#notifications)
  - [Comment threading](#comment-threading)
- [Dispatch history](#dispatch-history)
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
   - selects **only `Ready`** issues, **skipping any that still have open blockers**
     (dependency-aware ready-gating — see below), and skips any that already have a PR,
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
     channels**. The dispatcher mirrors every role's kanban completion summary as a
     comment on the GitHub issue after each tick — agents don't post to VCS themselves.
4. Each tick **auto-advances** any stage that's blocked on review once its PR's CI is
   green. When `_execute_advance()` completes the developer card, it also calls
   `_create_downstream_review_tasks()` — a safety net that auto-creates reviewer,
   security-analyst, and documentation tasks (idempotency keys `reviewer-{n}`,
   `security-{n}`, `docs-{n}`) if they don't already exist on the board. This handles
   the edge case where the initial Phase 2 decompose didn't propagate to all four roles.
   This fallback **also enforces the QA gate**: it creates a `qa-{n}` task and parents
   the reviewer/security/docs roles to it, so they only run once QA has passed. A prior
   version of this path bypassed the gate by parenting every downstream role directly to
   the developer card (fixed in #955).
5. Once the documentation card completes and CI is green, the dispatcher's
   **auto-merge monitor** checks for an explicit QA-passed signal (the QA card's
   `latest_summary` contains `qa-passed`, OR the issue has a `skip-qa` label for
   docs-only / emergency hotfixes). If the signal is present, the PR is merged
   automatically. If not, the monitor polls on each tick until the signal arrives.
   After merge, the next tick sets the card **Done** and **closes the issue**
   (GitHub doesn't auto-close on a non-default-branch merge, so the dispatcher
   does it). See [`docs/qa-gate-design.md`](docs/qa-gate-design.md) for the full
   QA gate design specification.

The kanban board and VCS board status are bookkept **in code on every tick**, so tracking is
deterministic — never dependent on an agent remembering to update anything.

### Epic decomposition (Phase 3)

When an issue is flagged as epic-sized (≥4 checklist items, an `epic` label, or body ≥2000
chars), the dispatcher routes it to the planner agent for scoping instead of splitting it
across the team directly. The planner confirms readiness by completing its kanban card with
`PLANNING COMPLETE:` — the dispatcher records a decomposed-mark
(`<!-- daedalus:decomposed -->`, tolerant of an optional suffix like
`<!-- daedalus:decomposed:1719630000 -->`) on the parent so subsequent ticks never
re-trigger decomposition even if the planner's summary is replayed. The dispatcher then decomposes the
epic into sub-issues:

- **Case A** (parent has checklist items): one sub-issue per item, capped at 10.
- **Case B** (no checklist): three default sub-issues — Research & Scoping,
  Implementation, Testing & Documentation.

Each sub-issue inherits the parent's labels (minus `epic`) and adds `subtask`, uses a
standard body template with a backlink to the parent, and gets its own triage card so it
enters the validator pipeline independently. An idempotency marker comment
(`<!-- daedalus:sub-issues:[N1,N2,...] -->`) is posted on the parent to prevent
re-creation on subsequent dispatcher ticks. The `epic` label is applied to the parent
issue (GitHub only in Phase 3; no-op on GitLab/Azure DevOps).

**Source file context injection.** When the dispatcher detects a planner task completion
with the `PLANNING COMPLETE:` prefix (which triggers the decompose), the dispatcher reads
up to 10 source files from the codebase (hardcoded limits: max 10 files, max 50KB per file)
and analyzes their contents to derive per-sub-issue context (file paths and symbols). This
gives the planner concrete context about existing implementations when scoping sub-issues.
The dispatcher scans the repo's source tree and picks the most relevant files (config files,
entry points, modules matching the epic's keywords). Sub-issue bodies always include an
explicit `depends_on:` metadata line (even when empty), making tier-graph parsing consistent.

**Sub-issue file &amp; symbol references.** Each auto-generated sub-issue body includes a
`### Affected files &amp; symbols` block listing up to 50 file paths and 50 function/class
identifiers extracted from its scope text by the per-sub-issue `EpicContext`. Identifiers
are pulled from `def` and `class` statements; file paths from explicit references in the
scope; component names from `load_known_components(workdir)` cross-referenced against the
scope text. This gives downstream agents concrete starting points without re-reading the
whole repo.

**Project board enrollment.** After each sub-issue is created, the dispatcher automatically
enrolls it on the configured project board (when present) with dependency-aware status:
tier-0 sub-issues (no dependencies) land in **Ready**; dependent sub-issues land in **Todo**.
Enrollment failures are logged and non-fatal so sibling sub-issues still get processed.
This ensures sub-issues are visible on the board immediately after decomposition, not just
after they pick up the `Ready` label via tier promotion.

**Not-suitable fallback.** When the planner completes its card but concludes the parent
issue is not suitable for decomposition (e.g., already small, blocked on a dependency),
it signals `NOT SUITABLE FOR DECOMPOSITION` instead of `PLANNING COMPLETE:`. The
dispatcher detects this via a case-insensitive regex, skips the planner's normal
`PLANNING COMPLETE:` handler, looks up the parent issue, and creates a validator task
for it — routing the issue through the standard validator → PM → developer flow rather
than leaving it stuck In Progress with no active child task. Idempotency is enforced
via a `planner-fallback-validator-{n}` idempotency key so re-runs on subsequent ticks
return the existing task instead of creating duplicates. The handler scans **both
`done` and `blocked` planner cards** for the signal (defense in depth — the planner
soul instructs completion, but if the planner blocks instead the handler still detects
the signal and routes correctly, preventing the stuck-In-Progress failure mode that
the original #931 handler had). Diagnostic logging is added at each skip point so
empty/unrelated summaries no longer fail silently.

### Tier promotion: dependency-aware sub-issue Ready-gating

Phase-3 sub-issues can declare `Depends on: #N` (or the planner emits `depends_on:` in its
spec). The dispatcher uses that DAG to roll sub-issues out in **tiers** rather than marking
them all `Ready` at once — only dependency-free issues go to the board immediately;
downstream tiers wait for their blockers to close and then get promoted automatically.


Sub-issues are linked to their parent epic via a body-reference convention. The discovery regex is aligned between `VCSProvider.sub_issues_of()` (`core/providers/base.py`) and `EPIC_REF_RE` (`core/tier_promotion.py`) so both code paths agree on what counts as a parent-epic reference. Recognised formats (case-insensitive, with or without colon):

- `Epic: #N`, `Epic #N`
- `Part of: #N`, `Part of #N`
- `Part of epic: #N`, `Part of epic #N`
- `Part-of #N`, `Part-of-epic #N` (hyphenated variants)

A single issue reference matches regardless of which format the author chose. Providers with native sub-issue links (GitHub's dependency API) override `sub_issues_of` to prefer the API, then fall back to the regex scan for portability.

Promotion is **idempotent**: `promote_waiting_tiers` queries `provider.has_label(n, "Ready")` before adding the label, so each promotable issue is labeled and commented exactly once. `VCSProvider.has_label()` defaults to returning `False`; the GitHub provider implements it via the issue's `labels` field (never raises).

**Three tiers of gating** combine to scope what dispatches when:

1. **Phase-1 detection** — `VCSProvider.is_epic()` flags the parent epic via the three
   heuristics above (≥4 checklist items, `epic` label, body ≥2000 chars).
2. **Conditional Ready labeling** — on decomposition, sub-issues whose DAG tier is **0**
   (no blockers) get the `Ready` label immediately and feed into the normal dispatch flow
   on the next tick. Tier > 0 sub-issues skip the label; they stay off the dispatch queue
   until promoted.
3. **Tier promotion on merge** — every dispatch tick, after the dispatcher archives merged
   PRs (completed issues), it calls `tier_promotion.promote_waiting_tiers(provider,
   just_closed)`. That scans every epic whose sub-issues are still open, re-computes the
   DAG tiers, and relabels issues whose tier level is now 0 (all their blockers closed)
   with `Ready`. They enter the validator pipeline on the next tick.

Tiers are computed as longest-path through the declared DAG via DFS with cycle detection —
cycles are logged and those issues are skipped rather than wedging the whole epic.
External references (to issues outside the epic) are dropped so they don't perturb tier
levels. The planner can emit `depends_on: [...]` metadata in sub-issue bodies; the
dispatcher parses them via the portable `Depends on: #N` convention so the tier graph works
on any provider, not only GitHub's native dependencies.

**Visual flow — epic decomposition and tier promotion:**

```mermaid
flowchart TD
    Issue([Issue marked Ready]) --> Detect{is_epic?}

    Detect -->|yes| Planner[Phase 3: Planner\ndecomposes into sub-issues]
    Detect -->|no| Validator[Phase 1: Validator]

    Planner -->|PLANNING COMPLETE| SubI[Sub-issues created\neach follows full pipeline]
    Planner -->|NOT SUITABLE| Validator

    SubI --> ParentEach[For each sub-issue:\ninherit labels\ndepend_on metadata computed]

    ParentEach --> TierCheck{Tier level?}

    TierCheck -->|tier 0| Ready[Apply Ready label immediately\nnext tick dispatches]
    TierCheck -->|tier > 0| WaitForBlocker[Blocked — wait for tier-1 blockers]

    WaitForBlocker -->|blocker merges| Promote[promote_waiting_tiers\nrelabel with Ready]
    Promote --> Ready

    Ready --> NormalPipeline[validator → PM → developer → QA → reviews → auto-merge]
```

### Dependency-aware ready-gating

Marking an issue `Ready` is necessary but **not sufficient** — daedalus also checks
that the issue has **no open blockers** before dispatching it. Blockers are resolved
per-provider:

| Provider | Source |
|----------|--------|
| **GitHub** | Native issue dependencies via `GET /issues/{n}/dependencies/blocked_by` (sub-issues / task-list refs, GA Aug 2025), merged with the portable body fallback. |
| **GitLab** | Issue links with `link_type: is_blocked_by` from `GET /issues/{iid}/links`, merged with the body fallback. |
| **Azure DevOps** | Work-item `Predecessor` links (`System.LinkTypes.Dependency-Reverse`), merged with the body fallback. |
| **Portable fallback** | A `Depends on: #N, #M` (or `Blocked by:` / `Depends-on:`) line anywhere in the issue body — works on any provider. |

While any blocker is open, the issue is skipped and reported under
**⛓ Waiting on Dependencies** in the dispatch summary. The next tick re-evaluates;
once the last blocker closes, the dependent auto-dispatches with no human
re-labeling. Unknown/unresolvable blocker references are treated as
**not-blocking** so a dependent is never permanently wedged on a stale link.

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
| `planner-daedalus` | Task graph, interface contracts, architecture decisions. **Phase 3:** reviews epic-sized issues and signals readiness with `PLANNING COMPLETE:`, triggering automated sub-issue decomposition. | No |
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

The dispatcher runs **automatically** when each agent's session ends. When an agent
reaches any terminal state — marking its task **done**, blocking with **review-required**,
blocking with **awaiting-fix**, or any other blocked state — the dispatcher fires at the
end of the session, triggering the next phase within seconds rather than waiting for
the next cron tick.

For example, as soon as the developer blocks with `review-required`, the session ends,
the dispatcher fires, detects CI green, and promotes the reviewer task.

This end-of-session trigger is the `daedalus-advance.sh` hook, wired into each profile's
`hooks.on_session_end` in its `config.yaml`. Registration is **per profile** — a profile
that never gets it wired silently stalls until the next hourly cron tick. `provision_roster.sh`
now registers the hook for **every** role, including `planner-daedalus`, which previously
lacked it: a planner that finished scoping an epic and signalled `PLANNING COMPLETE:` would
sit idle for up to 60 minutes waiting on cron before the dispatcher decomposed it into
sub-issues. With the hook registered, the planner's session end triggers immediate
advancement just like every other role (fixed in #962).

The cron job is still present as a last-resort safety net (in case an agent crashes
before reaching its terminal state), but it is no longer the primary advancement mechanism.

The result is a fully autonomous pipeline: once an issue is marked Ready, the entire
validator → PM → developer → QA → reviewer + security-analyst + accessibility chain runs
end-to-end without any human or scheduler intervention between phases.

```
issue marked Ready
      │
      ▼
validator runs → CONFIRMED: <note>
      │   └─ session ends → dispatcher triggers next phase
      ▼
PM / project-manager runs → SPEC: <note>
      │   └─ session ends → dispatcher triggers next phase
      ▼
developer → review-required
      │   └─ session ends → dispatcher detects CI green → QA starts
      ▼
QA → qa-passed (or qa-failed → dev fix card)
      │   └─ session ends → dispatcher creates reviewer + security-analyst
      │       + accessibility (only when UI/frontend keywords present in issue)
      ▼
reviewer → approved
security-analyst → cleared
accessibility → approved (or accessibility-na if no frontend files changed)
      │   └─ all three run in parallel; each session end triggers dispatcher on its terminal state
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

**Dispatcher mutex: FileLock.** The dispatcher acquires a process-level file lock
(`<scripts-dir>/.daedalus_dispatch.lock`) on every tick. This prevents concurrent dispatcher
instances from racing on the same board — a common failure mode when cron ticks overlap
or when the dispatcher is invoked both by webhook and cron simultaneously. The mutex
guarantees that only one dispatcher runs at a time, so no duplicate task creation, no
double-decomposing epics, and no race conditions in the self-healing loop. If another
dispatcher is already running, the new tick exits immediately (no-op) rather than queuing.
The lock auto-releases after 300 seconds (5 minutes) even on crash, preventing stale locks
from wedging future ticks.

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
        │               creates a qa-{n} task and parents reviewer,
        │                 security-analyst, docs to it — QA gate enforced
        │                 (downstream roles run only after qa-passed)
        │               idempotency keys: qa-{n}, reviewer-{n}, security-{n}, docs-{n}
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

**Approve-signal precision.** `approve_signals` (the set the loop matches a card's
handoff text against to detect `approve_advance`) deliberately omits the bare token
`pass` — it false-triggered on phrases like "all tests pass" and "password", silently
advancing a reviewer/security card that hadn't actually been approved. The set now
carries only explicit, role-prefixed signals (`approved`, `lgtm`, `qa-passed`,
`a11y-passed`, `security-approved`, …), so a QA pass note can describe its run without
being misread as an approval (fixed in #956).

**Merged-PR guard on Done sync.** When an issue reaches **Done** on the VCS board, the
dispatcher bulk-closes the issue's remaining kanban cards. For a developer card it first
asks the provider `is_pr_open(pr)`; only when the PR is **not** open does it call
`is_pr_merged(pr)`. A PR that is *closed but not merged* (`is_pr_merged=False`) no longer
triggers the sync — previously such a state orphaned every remaining pipeline card by
closing them as if the work had landed. Unverifiable states (provider lacks the
capability or errors) fall back to prior behaviour; only an affirmative "not merged"
holds the cards (fixed in #957).

**Escalation cap.** `MAX_FIX_ATTEMPTS = 3`. After three attempts the loop posts a
comment, leaves the card blocked, and stops. The pipeline never runs away — every
blocked card has a finite ceiling and exactly one deterministic path forward.

**Stale-blocked sweeper.** After diagnostics but before the self-healing loop,
the dispatcher calls `core/sweeper.py` on every tick. The sweeper scans all
`blocked` cards on the board via `hermes kanban ls --json --status blocked`,
enriches each with a `last_heartbeat_at` timestamp via a direct SQLite read (to
sidestep the CLI's omission of heartbeat columns), and compares against a
configurable threshold (default **48 hours**). Stale cards are logged with their
age for operational visibility and — when `archive: true` is set — archived off
the active board via `hermes kanban archive`. The sweeper degrades gracefully:
any failure logs a warning and the tick continues.

**Stale-running detector.** A separate pass (also in `core/sweeper.py`) scans
cards still marked `running` with no heartbeat update for more than 24 hours
(configurable via `tracking.stale_running.hours`). Unlike blocked cards, running
cards are never auto-archived — the detector only logs a warning with the
card id, assignee, and hours elapsed. Use it to spot dead workers that the
self-healing loop can't classify.

Configure per-project in `.hermes/daedalus.yaml`:

```yaml
tracking:
  stale_blocked:
    hours: 48          # age threshold (default 48)
    archive: false     # set true to archive stale cards instead of just warning
```

The sweeper also runs as a standalone CLI for manual invocation (e.g., scheduled jobs, ad-hoc cleanup):

```bash
python -m core.sweeper_cli <board_slug> [--threshold-hours 48] [--archive] [--dry-run]
```

**Retry-cap exhaustion notifications.** Per-attempt counters for validator retries
(`validator-retry-N-r*`) and PM stale-task recovery (`pm-{n}-r*`) each cap at a
maximum (2 for validator, 3 for PM). When an attempt counter exhausts the cap,
the dispatcher fires a one-time `retry-cap-exhausted` notification — idempotently
deduped on the issue via an `<!-- daedalus:retry-cap-notified -->` marker comment
so the same cap-exhaustion is never messaged twice per issue — and posts a
comment on the card instructing a human to investigate. Route this event to a
high-visibility channel in the Notifications editor alongside
`security-escalation`. The `MAX_FIX_ATTEMPTS = 3` cap for CI/routing fix cards
is unchanged: it posts a per-card comment and stops escalating, but does not
fire a chat notification. See
[`design-retry-cap-notification.md`](design-retry-cap-notification.md) for the
design rationale.

**Intermediate retry-attempt notifications.** When a validator or PM retry is
actually about to happen (retry_count < max_retries), the dispatcher first fires
a distinct `retry-attempt` notification — separate from the cap-exhaustion event —
so operators see each intermediate retry in real time. At the boundary
(retry_count >= max_retries), retry-attempt is suppressed and `retry-cap-exhausted`
fires on the next tick instead, preventing duplicate notifications. Both events
are routed to the same configured channels.

**Gateway watchdog (`daedalus-cron.sh`).** The per-project cron wrapper script
(`~/.hermes/scripts/daedalus-cron.sh`, installed by `postinstall.py`) detects a
silently-dead Hermes gateway (`hermes gateway status` reporting "not running")
and attempts a `hermes gateway restart` before exec-ing the dispatcher.
Prevents the common failure mode where the gateway crashes overnight and the
dispatcher's cron continues to fire but its messages never deliver. Two layers
of protection:

1. **Shell-level check** — the wrapper parses `hermes gateway status` (which
   always exits 0) for the `not running` marker and does a basic restart attempt.
2. **Enhanced watchdog** (`scripts/gateway_watchdog.py`, invoked after the
   shell check if installed) adds safeguards: a **STOP marker**
   (`~/.hermes/gateway-stop`) that inhibits all restarts for manual maintenance,
   **rate limiting** (max 3 restarts per 3600s window to prevent restart storms),
   **exponential backoff** (10s → 20s → 40s, capped at 300s) between restart
   attempts, **persistent state** (`~/.hermes/gateway-watchdog-state.json`) so
   restart history survives across ticks, and **crash log detection** (scans
   `~/.hermes/logs/` for `gateway*.log` or `hermes.*.log` files within the
   lookback window). If the watchdog is rate-limited or the restart fails, it
   logs to stderr and the dispatch tick still runs — self-heal, never blocking
   the run.

Additionally, `scripts/watchdog.py` provides an HTTP health-probe mode for
daemon-style deployments: it probes the gateway's health endpoint and checks
dispatch-staleness (zombie detection — process alive but dispatcher goroutine
stuck). Configured via `DAEDALUS_GW_*` environment variables (health port,
timeouts, rate limits). Both watchdogs degrade gracefully: any failure logs
and never blocks dispatch.

**Fetch-limit ceiling.** `_fetch_issues()` defaults to a page limit of **100**
per call rather than 20. Boards with more than 20 open issues were silently
truncated under the previous limit, causing validator and sweep scans to miss
work. The increase covers most real boards; very large boards can still paginate
further.

**`issues_map` miss fallback.** When a worker agent reads a freshly-created
issue that the dispatcher's sweep cached before its body was fully loaded
(race between a new issue and the sweep), the dispatcher's `get_issue(number)`
call retries once with a short backoff on transient HTTP failures before
raising. Prevents one flaky API response from stalling a tick.

**Stop-handler split.** Previously, a developer agent reporting `stop:` in its
summary could fall through a dead-code branch and leave the card blocked with no
closure signal. The blocked/stop handlers are now separate: `stop:` routes to a
dedicated auto-close path that archives the task and comments on the issue;
`blocked` routes to the PM consultation loop. Both paths are exercised by
dedicated tests.

### Self-healing behaviors (epic #180)

Five concrete behaviors make the pipeline recover from agent failures without
manual intervention. Each one is implemented in `core/iterate.py` — verified
on `origin/dev` at commit `70c1340`.

1. **`awaiting-fix:` auto-unblock.** When developer QA/tests fail or a reviewer
   flags changes, a dedicated fix card is created and assigned to
   `developer-daedalus`. The reviewer/security card is blocked with
   `awaiting-fix: <fix_card_id>`. When the fix card completes,
   `_execute_advance()` in `core/iterate.py` (lines 424–433, within
   `def _execute_advance` starting at line 394) scans all blocked cards and
   automatically unblocks any whose block reason contains both `awaiting-fix:`
   and the completing fix card's task ID. No human action needed.

2. **Crash-marker silent no-op.** If a developer agent crashes with
   infrastructure-failure markers (`coding-agent-failed:`, `permission-error:`,
   `coding_agent_died`, `coding_agent_timeout`, `exited with code`,
   `agent crash`) in the block reason, `classify_blocked()` (lines 180–188)
   returns empty string instead of routing to PM. This prevents the infinite
   PM consultation loop where every cron tick would spawn `PM_ROUTE` → PM
   completes as "no-op" → next tick spawns another `PM_ROUTE` → repeat. A
   human must fix the environment and manually unblock.

3. **`awaiting-fix:` concurrency guard.** Reviewer and security-analyst are
   handled in one combined branch of `classify_blocked()` (lines 192–205:
   `if assignee in (reviewer, security)`). When the card is already blocked
   with `awaiting-fix:` in the block reason, the guard at lines 199–200
   returns empty string. This prevents concurrent dispatcher ticks from
   spawning duplicate fix cards for the same reviewer card. The first tick
   that annotates the card with `awaiting-fix:` wins; subsequent ticks see the
   marker and skip.

4. **`PENDING_PR` VCS search.** When a developer card blocks with
   `review-required: awaiting-pr`, the dispatcher has not yet seen a GitHub
   PR. Every cron tick calls `_execute_pending_pr()` (lines 601–657), which
   searches open PRs via `provider.list_prs()` and matches them against the
   issue number in the PR title/body/branch. Once a PR appears, the block
   reason is updated to `review-required: PR #N — awaiting CI` so CI checks
   can drive the next stage. This eliminates the race where the agent opens a
   PR but the dispatcher keeps classifying the card as "no PR found."

5. **PM `awaiting-fix:` silent no-op.** The project-manager profile's
   classifier branch (lines 162–165) returns empty string when the PM's own
   block reason contains `awaiting-fix:`. This happens when a PM routing card
   dispatches a developer fix — the PM is then blocked waiting for the fix
   card to complete, which is a legitimate wait, not a real escalation.
   Without this guard the dispatcher would escalate the PM to a human every
   time a developer fix was in flight.

> **Deferred epic #180 behaviors.** The original epic spec listed three
> additional behaviors that are *not yet implemented* as of this commit —
> the README intentionally does not document them as shipped:
>
> - **`MAX_PENDING_PR_TICKS` timeout (8 cron ticks / ~8h) with a human
>   warning comment.** `PENDING_PR` currently keeps searching silently until
>   a PR appears; there is no timeout constant or escalation path yet.
>   Tracked in epic #180.
> - **QA/accessibility `PENDING_CI` escalation after `MAX_FIX_ATTEMPTS` (3)
>   silent ticks.** `classify_blocked()` returns `PENDING_CI` for
>   non-canonical QA/a11y signals (anything that isn't `qa-passed:` /
>   `qa-failed:` / `approved:` / `a11y-na:` / `a11y-skipped:` /
>   `changes requested`), but there is no fix-attempt counter or escalation
>   path for the `PENDING_CI` case itself.
> - **Empty-summary developer skip.** Developers that block with no
>   recognizable signal still route to `PM_ROUTE` for downstream review —
>   the "skip PM consult when the developer has no signal" branch was not
>   added.

### What breaks self-healing

Three scenarios require human intervention because the self-healing loop cannot
resolve them:

- **Infrastructure crashes** (coding-agent-failed, permission-error, etc.). The
  dispatcher returns silent no-op. A human must fix the gateway/OS/agent binary
  and unblock the card manually.

- **`awaiting-fix:` blocks that never complete.** If the fix card is stuck (the
  developer agent keeps crashing, or the fix task itself blocks with a
  non-terminal signal), the reviewer card stays blocked forever. A human must
  investigate the fix card and either complete it or escalate.

- **Non-canonical QA/a11y signals.** QA must block with `qa-passed:` or
  `qa-failed:` (lowercase, with the colon). Accessibility must use
  `approved:`, `a11y-approved:`, `a11y-na:`, `a11y-skipped:`, or
  `a11y-changes-requested:`. Any other phrasing (e.g., `qa-blocked:`,
  `a11y-failed:`) returns `PENDING_CI` from `classify_blocked()`, which stays
  in the pending queue indefinitely. The `PENDING_CI` retry cron eventually
  resolves when a proper signal arrives, but the `MAX_FIX_ATTEMPTS` escalation
  does not apply to `PENDING_CI` cards — so a QA/a11y agent that keeps posting
  ambiguous signals will sit in the queue with no automatic human alert.
  Operators should watch the board and re-queue or cancel cards that never
  reach a canonical signal.

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
  The dispatcher mirrors every role's kanban completion summary as an issue comment after
  each tick (agents do not post to VCS themselves — see PR #897). The issue
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
    - `advance` — dev PR green + review-required → complete dev card, then `_create_downstream_review_tasks()` creates a `qa-{n}` task and parents reviewer/security/docs to it so they run only after QA passes (idempotent keys `qa-{n}`, `reviewer-{n}`, `security-{n}`, `docs-{n}`; skips any that already exist)
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
- **QA auto-merge gate** (`core/iterate.py`) — the dispatcher's auto-merge path checks for
  an explicit `qa-passed` signal from the QA agent before merging. The helper
  `_qa_passed_for_issue()` examines the QA card's `latest_summary` (matched by idempotency
  key `qa-{issue_number}`) via case-insensitive substring match. If the gate finds QA still
  running (no `qa-passed` signal yet), it skips the merge with a warning log: _"Skipping merge:
  QA has not passed for PR #N (issue #M). Wait for QA card to report 'qa-passed'."_ The check
  is fail-closed: any kanban DB error or missing card also blocks the merge. PRs with green CI
  but no QA pass signal wait indefinitely — the dispatcher re-checks every tick, so QA
  completion automatically triggers the merge on the next cycle. A `skip-qa` label on a PR
  bypasses the gate entirely for low-risk changes (docs-only, config, dependency bumps). See
  [`qa-gate-design.md`](qa-gate-design.md) for the full design spec, edge cases, and signal
  format reference.
- **Tier promotion** — epic decomposition produces multiple sub-issues, but marking
  all of them `Ready` at once overwhelms the validator pipeline and bypasses dependency
  semantics. Instead, sub-issues with no declared blockers get the `Ready` label on
  decomposition; dependents stay unlabeled until a blocking sub-issue's PR merges and
  the dispatcher's `tier_promotion.promote_waiting_tiers()` pass re-evaluates the DAG.
  Cycles are detected and logged rather than wedging the epic. This makes epic
  execution genuinely incremental and dependency-aware on any provider.
- **Stale-blocked sweeper** — a blocked card that no agent can classify sits on the
  board forever, polluting the queue and confusing humans. The sweeper detects this
  silently (via heartbeat timestamps), logs it for visibility, and optionally
  archives so humans can inspect without disrupting the live queue. It runs inside
  the tick so it never drifts from the dispatcher's view of the board.
- **Gateway watchdog** — the cron tick continues to fire even after the Hermes
  gateway crashes, giving the appearance of a healthy pipeline until a human
  notices no agents have run. A two-layer defense: the `daedalus-cron.sh`
  wrapper parses `hermes gateway status` for the `not running` marker, and the
  enhanced `gateway_watchdog.py` adds rate limiting (max 3 restarts per hour),
  exponential backoff (10s → 300s), a STOP marker for manual maintenance, and
  persistent state so restart history survives across ticks. The pipeline
  self-heals and the tick still delivers its nudge. Rate limiting prevents a
  flapping gateway from exhausting the cron slot with restart attempts every
  three minutes.
- **Retry-cap notifications** — validator and PM retry caps exist so a broken issue
  doesn't loop forever. But a cap with no notification means the operator only
  learns about it days later by scrolling the board. A one-time `retry-cap-exhausted`
  notification (deduped per issue via a marker comment) surfaces the wedge the
  moment it happens, routed to the same channels as `security-escalation`.
- **Fetch limit raised to 100** — the original page limit of 20 silently truncated
  boards with more than 20 open issues: validator sweep missed work, merged-PR
  archival missed completions, and the board looked healthy while issues rotted in
  `Ready`. The raised limit matches real-board sizes without breaking the API.
- **Stop-handler split** — a developer agent reporting `stop:` (human-driven abort)
  and an agent stuck `blocked` (waiting for input) used to share a code path; a
  dead-code branch let `stop:` fall through and leave the card blocked. Separate
  paths — `stop:` auto-closes, `blocked` PM-consults — prevent the regression.
- **`issues_map` miss fallback** — a freshly-created issue can race with the
  dispatcher's sweep such that the sweep has the issue's number but not its body.
  A one-shot `get_issue()` retry on transient failure prevents a single API flake
  from stalling the entire tick.

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
  schedule: "0 * * * *"
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
| `scripts/daedalus_dispatch.py` | The deterministic dispatch tick (cron entrypoint, `--no-agent`). Ready-gating, reconcile, decompose, auto-advance, merged→close. Flags: `--history [N]`, `--repo <path>`, `--plugin-dir <path>`, `--dry-run`, `--self-test` (offline hermetic smoke test — seeds in-memory doubles, drives the real handoff functions, asserts state transitions with zero network/GitHub access). |
| `core/iterate.py` | Self-healing loop: classify blocked cards into 5 actions, idempotent fix-card creation, iteration cap + escalation, reviewer re-engage after fix. |
| `core/dispatch_state.py` | Dispatch state persistence (`daedalus_dispatch_state.json`) — threads, retry counters, idempotency keys. |
| `core/notification_sender.py` | Structured webhook payloads + Slack/Discord/Telegram/Signal/WhatsApp `send()` with per-platform formatting. |
| `core/notify_templates.py` | Rich markdown notification templates (dispatch summary, doc report envelope, PR-ready, pipeline-failure) with clickable issue/PR links for every Hermes messaging platform. |
| `core/registry.py` | Project registry read/write — `projects.yaml` CRUD for multi-repo onboarding. |
| `core/source_specs.py` | `.hermes/pending/*.md` spec-file trigger — SHA-256 idempotency keys, lifecycle prefix injection, title from filename. |
| `core/sweeper_cli.py` | Standalone sweeper CLI (`python -m core.sweeper_cli`) for manual stale-card cleanup. |
| `core/thread_delivery.py` | Per-target thread anchors + comment mirroring for notification threading. |
| `core/tier_promotion.py` | DAG tier computation + Ready-label promotion — rolls sub-issues out in dependency tiers, not all at once. |
| `core/webhook_normalizer.py` | Inbound GitHub/GitLab/Azure DevOps/Hermes webhook → `ReadyEvent` normalizer. |
| `scripts/provision_roster.sh` | Provisions the 9-agent Hermes roster. |
| `scripts/agent_comment.py` | Helper for posting agent comments with mandatory `**Agent: <name>**` attribution header. |
| `scripts/gateway_watchdog.py` | Enhanced gateway watchdog with rate limiting/backoff/STOP-marker detection. |
| `scripts/watchdog.py` | HTTP health-probe mode for daemon deployments. |
| `core/providers/` | VCS provider layer: GitHub (REST + GraphQL Projects v2), GitLab (REST), Azure DevOps (REST/WIQL) — token-authenticated HTTPS APIs, extensible via `register_provider()`. |
| `core/kanban.py` | Thin, idempotent wrapper over `hermes kanban` (triage, decompose, complete). |
| `config/` | `ConfigLoader` (defaults + per-repo merge), `validate_vcs`, and the config template. |
| `dashboard/` | Dashboard tab: project grid, add/edit project modals, notifications editor (`plugin_api.py` + React `src/App.jsx`). |
| `tests/` | Unit tests — config, providers (mocked HTTP), dispatcher, dashboard API, installers. |

The **ship-gate hook**, **cron wrapper**, and **roster profiles** live in the Hermes
home (`$HERMES_HOME`), not here — see [`SETUP.md`](SETUP.md) for how they're deployed
and shared across a team.

---

## Dashboard REST API

`dashboard/plugin_api.py` exposes a REST surface the dashboard UI (and any
external tool) uses to manage projects. All endpoints live under
`/api/plugins/daedalus/`:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/projects` | GET | List all registered projects with live kanban/PR/cron status. |
| `/project/create` | POST | Onboard a new repo (creates config, cron job, kanban board). |
| `/project/{name}/config` | GET | Read merged config. **Secrets are redacted** — keys matching `secret`, `api_key`, `password`, `token` are replaced with `***`. |
| `/project/{name}/config` | POST | Persist config changes. |
| `/meta/branches` | GET | Picker data: available branches for the project. |
| `/meta/labels` | GET | Picker data: available labels (GitHub/GitLab/Azure). |
| `/meta/boards` | GET | Picker data: available kanban boards. |
| `/meta/statuses` | GET | Picker data: available status columns. |
| `/meta/notifications` | GET | Picker data: configured notification channels. |
| `/meta/channels` | GET | Picker data: discovered Slack/Discord/Telegram channels. |

The `/meta/*` endpoints power the dashboard's dropdown selectors when editing
project config. All config reads pass through `_strip_secrets()` to ensure
tokens are never echoed back to the UI.

---

## Development references

Standalone documents that describe design rationale, internals, or developer
conventions:

| Document | Purpose |
|----------|---------|
| [`SPEC.md`](SPEC.md) | Detailed specification of the pipeline's behavior — what each phase does, how agents interact, what the quality gates are. The README is an overview; SPEC.md is the reference. |
| [`design-retry-cap-notification.md`](design-retry-cap-notification.md) | Design rationale for retry-cap exhaustion and intermediate retry-attempt notifications. |
| [`qa-gate-design.md`](docs/qa-gate-design.md) | Full QA gate design specification — how the auto-merge gate validates the QA signal, edge cases, and the `skip-qa` label bypass. |
| [`ci-plugin-lifecycle.md`](docs/ci-plugin-lifecycle.md) | CI integration patterns and plugin lifecycle hooks for pipeline automation. |
| [`e2e-smoke-test.md`](docs/e2e-smoke-test.md) | End-to-end smoke testing procedures and regression test suites. |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How to contribute: branch naming, commit conventions, PR process, code review. |
| [`CHANGELOG.md`](CHANGELOG.md) | Release notes and notable changes per version. |

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
  `security-escalation`, `comment-mirror`; omit `events` to receive everything).
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

### Comment threading

Since **beta.30**, every daedalus-managed issue gets **one persistent thread per
notification target**. Agent comments — spec posts, progress updates, review
feedback — on the issue *and* its linked PR are mirrored into that thread as
replies, alongside PR-open and merge events. The whole pipeline conversation is
readable in chat without opening GitHub.

> ⚠️ **Behavior change for existing users.** Threading is delivered as a new
> `comment-mirror` event, and any **catch-all** notification entry (one with no
> `events` filter, or `events: []`) receives it **automatically — no opt-in**.
> If you have existing Slack/Discord targets without an `events` filter, they
> will start receiving threaded comment mirrors. On an active board this can be a
> noticeable jump in message volume. To exclude it, list the events you *do* want
> on that entry and leave `comment-mirror` out.

How it works:

- **No new config keys.** Threading runs automatically against your existing
  `cron.notifications` entries (and the legacy single `deliver` target).
- **Per-platform anchor.** The first event for a target posts a *root* message;
  every later event replies under it. Slack anchors on `thread_ts`, Discord on
  `message_id` — both captured automatically via `hermes send --json`.
- **`thread_broadcast` (Slack only).** Slack supports a `reply_broadcast` flag
  that mirrors a threaded reply into the parent channel as well. The dispatcher
  honors this per-target via the boolean `thread_broadcast` field on each
  `cron.notifications` entry — when `true` (the default), replies are also
  broadcast; when `false`, they stay thread-only. Discord has no equivalent and
  ignores the field.

  ```yaml
  cron:
    notifications:
      - platform: Slack
        target: "slack:C0ABC"
        events: [doc-report, dispatch-summary]
        thread_broadcast: false   # keep it thread-only
  ```
- **Cross-tick dedup.** Each event has a stable key; once mirrored to a target it
  is never resent, so repeated cron ticks don't repost the same comment.
- **Self-healing anchor.** If a thread's root message is deleted, the next event
  posts a fresh root and updates the stored anchor.
- **Agent header.** Mirrored comments always begin with the mandatory
  `**Agent: <name>**` header (enforced in beta.30), so it's clear which agent
  spoke — handy if you parse or filter the thread.

State lives in `daedalus_dispatch_state.json`, which gains a `threads` key per
issue:

```json
{
  "127": {
    "threads":       { "slack:C0CHANNEL1": "1718900000.001200" },
    "thread_events": { "slack:C0CHANNEL1": ["root", "comment:issue:456", "pr-opened:99"] }
  }
}
```

**Caveat — per-tick API cost.** On every tick, each open issue's issue and PR
comments are fetched before dedup decides what to mirror. This is fine for small
boards; on large boards with many open issues it adds VCS API calls per tick.
See [docs/notification-threading.md](docs/notification-threading.md) for the full
reference.

## Dispatch history

Every dispatch tick appends a one-line JSON record to a rotating log at
`~/.hermes/plugins/daedalus/history.jsonl` (capped at 1000 lines, oldest-first).
Each record captures the tick's UTC timestamp, project name, and summary counters
(issues_seen, created, reconciled, completed, advance_prs, spec_created, blocked,
error). Use the `--history` flag to print the last N entries as a fixed-width
table — no log tailing needed:

```bash
python3 ~/.hermes/plugins/daedalus/scripts/daedalus_dispatch.py --history 20
```

The table columns (in order): `TIMESTAMP`, `PROJECT`, `MODE`, `ISSUES`, `CREATED`,
`RECON`, `DONE`, `PRS`, `SPEC`, `BLOCKED`, `ERROR`. List-valued fields are shown
as counts; `--history` without an argument defaults to the last 10 entries.
History is best-effort auditing — a write failure is logged but never breaks the
dispatch tick.

---

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
- **Spec drop:** put a `*.md` in `<repo>/.hermes/pending/` (when `sources.local_specs.enabled`). Title comes from the filename stem; body is prefixed with the project's lifecycle instruction and target branch. A SHA-256 of the file path is embedded in the card's idempotency key, so the same file never creates duplicate cards. Empty files are silently skipped; the directory need not exist.
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

# Lessons

## Don't reinvent native hermes-kanban orchestration (2026-06-09)

**Mistake:** When a decompose worker wandered into the wrong checkout and reviewer/
security spawned before a PR existed, I started building a custom deterministic
`_fan_out` (+254 lines): role cards, parent edges, workspace pins. The user flagged
it: *"doesn't hermes kanban already have these safeguards natively?"*

**Reality (verified on the board):**
- `decompose` **inherits** the root triage's `--workspace dir:<checkout>` to every
  child — pinning the triage pins the whole roster. Unpinned triage = `scratch` =
  worker wanders.
- `decompose` **sequences roles natively** via `parents=[developer]` edges.
- The premature spawn was self-inflicted: I **archived the running developer card**,
  which released its dependents' gate.

**Rule:** Before building orchestration primitives, check what `hermes kanban`
already does (`<cmd> --help`, inspect `show --json` edges/workspace). Prefer one
native flag over custom code. Never archive/kill a prerequisite card mid-run. The
real fix here was +34/-3: a `workspace` param on `create_triage`.

## Audit the platform's native surface BEFORE designing integrations (2026-06-11)

**Mistake:** While planning the multi-VCS refactor I designed a hand-rolled urllib
HTTP client and kept the dashboard's remove+create cron reconcile, assuming neither
had platform support. The user pushed: *"make sure we leverage hermes native
features... no reinventing the wheel."* A docs sweep then found: **httpx is a core
Hermes dependency** (plugins just import it), **`hermes cron edit <id>` updates jobs
in place** (list/create/edit/pause/resume/run/remove all exist), and `hermes kanban
create --idempotency-key` is native. The plan changed in three places.

**Other platform facts verified this session (Hermes + VCS APIs):**
- `requires_env` in plugin.yaml HARD-DISABLES the plugin when any var is missing —
  never declare per-provider-optional tokens there; resolve from env at runtime.
- GitHub Projects v2 has NO REST API — boards are GraphQL-only (same token/host).
- The REST issues endpoint returns PRs too — filter `"pull_request" in item`.
- GitLab boards are label-driven; Azure boards are work-item-state-driven — a
  uniform `status_map` (canonical → provider name) absorbs all three.
- `hermes send -t <platform>:<channel>` is already platform-agnostic — Slack,
  Discord, Telegram, Signal, WhatsApp all flow through it; `--list` enumerates.

**Rule:** Before designing any integration layer, inventory the host platform's
native CLI/API surface (docs sweep + `--help`) and write a "reuse vs build" table
into the plan. Custom code is only justified for gaps the table proves exist.

## Resolve "this" to the user's deliverable, not my latest side-thread (2026-07-04)

**Mistake:** User asked "make a story for this so we can have daedalus work on it."
The conversation's main deliverable was a Hermes-native migration analysis, but I had
just started a knowledge-extraction side task, so I bound "this" to the side task —
twice (filed it in the wrong repo, then filed the wrong story in the right repo).
Three round-trips to land benmarte/daedalus#1276.

**Rule:** When the user says "this/it" after a multi-topic exchange, resolve the
referent to the *primary deliverable they asked for*, not the most recent thing I
did on my own initiative (hook-driven side tasks, memory writes, evaluations).
If two candidates genuinely fit, name the referent in one sentence before acting:
"Filing a story for X — say if you meant Y."

## Never chain merge after checks; validate review tweaks in CI's env (2026-07-04)

**Mistake 1:** Ran `gh pr checks 1281; gh pr merge 1281` as ONE compound command.
The checks output showed `test fail` but the merge had already fired — merged a
red PR onto dev. The `--fail-fast` background watcher exiting 0 had lulled me.

**Mistake 2:** The failure itself was a review finding I applied without
re-validating: tightening the route-count assertion to `>=10` encoded my
full-Hermes-install machine (23 routes) — CI's minimal env mounts 4 via the
graceful-degradation imports. "Tighter" assertions can smuggle in environment
assumptions; the reviewer proposed it and I pattern-matched "stricter = better".

**Rules:**
- Merging is ALWAYS its own gated step: read the checks result, THEN merge.
  Never `checks && merge` or `checks; merge` in one command.
- Before applying a reviewer's "tighten this assertion" suggestion, ask what
  environment the new bound assumes. Anything counting mounted routes, plugins,
  providers, or optional-dep behavior differs between dev machines and CI.
- After any accidental red-dev merge: fix forward within minutes (small PR,
  CI-gated), announce it in the program thread — don't quietly rewrite history.

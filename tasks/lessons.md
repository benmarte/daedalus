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

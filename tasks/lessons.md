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

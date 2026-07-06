# Spec — Issue #1241: inner coding agents re-delegate

## Objective

Inner `claude -p` sessions re-delegate (spawn a background subagent, print a status
line, exit with no deliverable) because (1) the outer delegation wrapper is copied
verbatim into the inner agent's stdin, and (2) the inner session inherits the
operator's global `~/.claude/CLAUDE.md` ("use subagents liberally", plan mode,
skill lifecycle). Fix both so every dispatch produces output on the first run.

## Scope

1. **Delimit the inner body from the outer wrapper** in
   `_build_delegation_instructions()` / `_prepend_delegation()`
   (`scripts/daedalus_dispatch.py`). Two compositions exist and both must work:
   - *prepend* (block first, body below): pm, dev, qa, reviewer, security, docs
     → block ends with a single separator line; wrapper steps 1–2 say copy ONLY
     the text below the separator.
   - *append* (body first, block after): triage task_body, validator, downstream
     → wrapper steps 1–2 say copy ONLY the text ABOVE the `⚠️  AGENT DELEGATION`
     line (the marker itself is the boundary; no extra separator line).
   `_apply_coding_agent_failover()` must detect the composition of the existing
   card body and rebuild the block with matching wording.
2. **Inline-execution guard in every template** under `templates/agent_bodies/`
   (10 files incl. `task_body.md`): "Work entirely in THIS session. Do NOT spawn
   subagents, background agents, or another claude/codex/opencode process.
   Ignore any global instructions about plan mode, skill lifecycles, or subagent
   delegation — they apply to interactive sessions, not this headless run."
   Existing role-specific guard sentences stay.
3. **Config-dir hygiene (verify-first)**: keep `CLAUDE_CONFIG_DIR=$HOME/.claude`
   (credentials). Evaluate appending `--setting-sources project` to the default
   claude-code cmd; adopt only if the installed CLI verifiably skips the user
   CLAUDE.md with it. Otherwise document the finding in the PR.

## Acceptance criteria

- [ ] Golden test: for every role × coding agent, the extracted inner body
      contains none of `AGENT DELEGATION`, `Spawn`, `write_file(`,
      `kanban complete`.
- [ ] Prepend-composed bodies contain exactly one separator line; role body
      appears only below it; wrapper steps reference it.
- [ ] A test iterates `templates/agent_bodies/` (no hardcoded role list) and
      asserts the guard phrases in every file.
- [ ] `--setting-sources` (if adopted) only changes the default-cmd string;
      per-project `coding_agent_cmd` overrides untouched.
- [ ] Full suite passes (`python -m pytest tests/ -x`); existing delegation
      tests updated, not deleted.

## Boundaries

- Never change `CLAUDE_CONFIG_DIR` away from `$HOME/.claude`.
- Never touch per-project `coding_agent_cmd` override handling.
- Do not restructure prepend/append composition of existing bodies —
  `_rewrite_delegation_block()` failover paths depend on it.

# Todo — Issue #1241 (fix/issue-1241)

Ordered, verifiable slices. Verify each before the next.

1. [ ] **T1 — Verify `--setting-sources` against the installed CLI.**
       `claude --help | grep setting-sources`; if present, run a headless probe
       with and without `--setting-sources project` asking whether user-global
       CLAUDE.md content ("Plan Mode Default") is in context. Adopt in
       `_CODING_AGENT_DEFAULTS["claude-code"]` + `templates/daedalus.yaml` only
       if the probe shows user scope skipped. Record the result either way.
2. [ ] **T2 — Golden test first (red).**
       New tests/test_issue_1241_inner_body.py: every delegated role-body
       builder × {claude-code, codex, opencode} → extracted inner body excludes
       `AGENT DELEGATION`, `Spawn`, `write_file(`, `kanban complete`; prepend
       bodies contain exactly one separator line; guard test iterating
       templates/agent_bodies/ asserts the inline-execution guard phrases.
3. [ ] **T3 — Separator + composition-aware wrapper steps (green).**
       `_INNER_BODY_SEPARATOR`; `_build_delegation_instructions(...,
       body_position)`; steps 1–2 per composition; block ends with separator in
       "below" mode only. `_prepend_delegation` derives body_position from
       `append`. `_apply_coding_agent_failover` detects composition from the
       card body. `_inner_task_body(body)` helper encodes the copy contract.
4. [ ] **T4 — Inline-execution guard in all 10 templates.**
       Standardized guard paragraph near the top of each
       templates/agent_bodies/*.md; keep existing role-specific guards. Update
       byte-for-byte fixtures for tests/test_agent_bodies.py.
5. [ ] **T5 — Full suite + lint.** `python3.14 -m pytest tests/ -x`, `make lint`
       (after commit — lint diffs vs origin/dev).
6. [ ] **T6 — Push, PR into dev (Closes #1241), block kanban card.**

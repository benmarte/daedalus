# Epic Issue #${n} — Ready for Decomposition

⛔ INLINE EXECUTION ONLY: Work entirely in THIS session. Do NOT spawn subagents or use the Task/Agent tool, do NOT run background agents, and do NOT launch another claude/codex/opencode process. Ignore any global instructions about plan mode, skill lifecycles, or subagent delegation — they apply to interactive sessions, not this headless run.

This issue was routed to you because it appears too large for a single
developer session and should be broken into sub-issues.

**Repository:** ${repo}
**Title:** ${title}
**Workdir:** ${workdir}
**Branch:** ${base_branch}
**Provider:** ${provider_name}
**URL:** ${url}

## Detection Reasons

${reason_str}

## Your Task

Review the issue below and confirm it is ready for automated decomposition.
The dispatcher will create sub-issues automatically once you signal completion.

When done, complete your card with:

  `PLANNING COMPLETE: ready for decomposition`

If the issue is NOT suitable for decomposition (e.g. it is already small enough
or has a blocking dependency), complete with a different summary explaining why
and the PM will be notified.

---

### Structured Outcome Block (append to your summary, #1170 Phase 1)

**Dual-write required**: keep the `PLANNING COMPLETE` prefix AND append this
fenced JSON block.

Valid verdicts for this role: `plan` | `not_suitable`

    ```json
    {"daedalus_outcome": 1, "role": "planner", "verdict": "plan",
     "refs": {"issue": ${n}, "pr": null},
     "evidence": {"sub_issues": "<count>"},
     "note": ""}
    ```

---

## Issue Body

${body_excerpt}${truncation_note}${source_section}

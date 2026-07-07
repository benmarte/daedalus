# Spec — #1350 gate cards flap/duplicate on local-LLM runs (guard_prefix churn)

## Problem
`_guard_prefix_on_done` (core/dispatch/checks.py) uses a narrow colon-terminated
prefix whitelist (`_DONE_GUARD_PREFIXES`) to decide whether a done gate card is
well-formed. Local models emit reasonable verdict variance the whitelist rejects:
`security: passed`, `SECURITY: no vulnerabilities found`, `SECURITY REVIEW: approved`,
`security-analyst: cleared`. The guard then archives + recreates a blocked card;
F11 end-of-tick dispatch re-runs it → duplicate gate cards + churn/latency.

Also: `_get_task_summary` already strips `<role>:` labels (`security-analyst:`),
but the whitelist tokens still embed the role name (`security-approved:`), so a
stripped `cleared` never matches — inconsistent, guard mis-fires even on canonical
signals.

## Fix
1. Broad per-role completion-signal vocabulary `_ROLE_COMPLETION_SIGNALS`
   (superset of the canonical prefixes AND the advance-gate vocab in
   core/iterate/executors.py) — pass/fail/neutral verdict tokens.
2. Guard-local label stripping `_GUARD_LABEL_RE` for the bare labels the global
   `_strip_role_label` intentionally leaves (`security:`, `security review:`,
   `security analysis:`). Matched against raw AND label-stripped summary.
3. Per-(issue, role) dedup pass: at most one active/done guarded card survives;
   extra done duplicates are archived (no recreate) so they never accumulate.

## Acceptance criteria
- AC1 Local-model gate completions with reasonable prefix variance accepted (no
  archive+recreate). Cases: `security: passed`, `SECURITY: no vulnerabilities found`,
  `SECURITY REVIEW: approved`, `security-analyst: cleared`, `lgtm ...`.
- AC2 No duplicate gate cards accumulate for a role on a clean run.
- AC3 Dedup: at most one active+done card per (issue, role).
- AC4 Genuinely botched completions (empty / no verdict / narration) still fire
  the guard (regression protection preserved).

## Out of scope
Advance-gate routing vocabulary (core/iterate) is unchanged; guard vocab is a
superset so nothing advance would route is rejected by the guard. Stalls (guard
accepts but advance can't route) are pre-existing and handled by the sweeper.

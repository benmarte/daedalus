# Spec — Issue #1161: auto-adopt spec comment when PM card lacks SPEC: summary

Adopted from the PM spec posted on GitHub issue #1161.

## Objective

A hermes premature-completion bug can complete the PM kanban card with an empty
summary even though the PM agent posted a valid `## Implementation Spec` comment
on the GitHub issue (observed live on #1160). The dispatcher's SPEC gate reads
only the card summary, classifies the card `'stale'`, retries PM up to
`max_pm_retries`, then notifies and stalls. Make the stale path self-healing by
auto-adopting the spec comment — codifying the operator's manual recovery
(`hermes kanban edit <task-id> --result --summary 'SPEC: ...'`).

## Changes

1. **`_pm_spec_comment(provider, issue_number, pm_profile)`** in
   `scripts/daedalus_dispatch.py`, mirroring `_validator_github_comment_outcome`:
   - Guard `provider is None` / exceptions → return `""`.
   - Scan `provider.get_issue_comments(issue_number)` in reverse for a comment
     whose body contains an `## Implementation Spec` heading (case-insensitive)
     AND the PM agent attribution marker in the first ~300 chars.
   - Attribution marker derivation must NOT use `profile.split("-")[0]`
     (yields `project` for `project-manager-daedalus`); strip the trailing
     profile suffix so the marker is `agent: project-manager`.
   - Return a short synthesized head (first non-empty content line after the
     heading, truncated ~200 chars) or `""`.
2. **`edit_summary(slug, task_id, summary)`** in `core/kanban.py` wrapping
   `hermes kanban edit <task-id> --result --summary <text>` via `_hk()`.
3. **Auto-adopt at both stale decision points** (~4091 confirmed-secondary and
   ~4397 primary): when `pm_state == 'stale'`, BEFORE the retry-cap check and
   before any intermediate retry, call `_pm_spec_comment`. If it returns a head:
   edit the newest stale done PM card's summary to
   `SPEC: (adopted from issue comment) <head>`, log the rescue at warning level,
   and `continue` — no retry card, no notification. `_check_completed_pm` (later
   the same tick) then sees `spec:` and fans out normally. In `dry_run`, log
   only, do not edit.
4. **Retry-cap exhaustion only when no spec comment exists** — existing retry /
   cap-notification / GitHub-comment logic unchanged when `_pm_spec_comment`
   returns `""`.

Scope: `scripts/daedalus_dispatch.py`, `core/kanban.py`, new regression test
file. No changes to SOUL.md, profiles, or the notification format.

## Acceptance Criteria

- [ ] Done PM card with empty summary + attributed `## Implementation Spec`
      comment on the issue → next tick edits the card summary to start with
      `SPEC: (adopted from issue comment)`, fan-out proceeds, and NO retry PM
      card or retry-cap notification is created.
- [ ] Same state but NO spec comment → behavior unchanged: PM retried up to
      `max_pm_retries`, then retry-cap notification + GitHub cap comment fire
      exactly once (idempotency marker preserved).
- [ ] Both stale call sites perform the adoption check.
- [ ] `dry_run=True` never edits the kanban card (logs the would-adopt instead).
- [ ] Attribution marker matches `agent: project-manager` (not `agent: project`)
      — unit test covers the profile-suffix derivation.
- [ ] Regression tests in `tests/test_issue_1161_*.py` pass under
      `python3.14 -m pytest`; full existing suite stays green.

## Branch / PR

`fix/issue-1161-adopt-spec-comment` → `dev`

# Spec: Global Agent Delegation for All Pipeline Roles

## Objective

Expand `execution.coding_agent` from developer-only delegation to a **global default** that routes ALL pipeline roles (PM, developer, QA, reviewer, security, docs) to the configured cloud agent. Individual profiles can override to stay on the local LLM.

## Problem Being Solved

Currently, `execution.coding_agent: claude-code` only injects delegation instructions into the developer task body. All other roles (PM, QA, reviewer, security, docs) always run on the local LLM. Users who configure a cloud agent expect the entire pipeline to use it.

## Desired Behavior

### Global delegation (no per-profile override)
```yaml
execution:
  coding_agent: claude-code
  coding_agent_cmd: "claude --dangerously-skip-permissions -p"
```
→ All 6 roles (PM, developer, QA, reviewer, security, docs) get delegation instructions. Each role's task body begins with a role-specific delegation block telling the local Hermes agent to spawn Claude Code via `terminal()` and wait for results.

### Per-profile override
```yaml
execution:
  coding_agent: claude-code
  profiles:
    qa:
      name: qa-daedalus
      agent: hermes   # this role stays on local LLM
```
→ Only QA uses the local LLM; all other roles delegate to Claude Code.

### No delegation (unchanged)
```yaml
execution:
  coding_agent: hermes  # or omitted / "none"
```
→ All roles run on local LLM. No behavior change.

## Acceptance Criteria

- [ ] When `coding_agent` is a cloud agent (`claude-code`, `codex`, `opencode`), all 6 role task bodies begin with a role-specific delegation block
- [ ] Per-profile `agent: hermes` override disables delegation for that specific role only
- [ ] Per-profile `agent: claude-code` override can re-enable delegation even if global is `hermes`
- [ ] Each role's delegation block contains role-appropriate instructions (PM posts spec comment; developer writes code + PR; QA checks criteria; reviewer reviews diff; security audits; docs posts report)
- [ ] `coding-agents` skill is injected for ALL delegated roles (not just developer)
- [ ] When `coding_agent` is `none` or `hermes`, zero behavior change from current code
- [ ] All existing 70 tests still pass
- [ ] New tests cover: global delegation for each role, per-role override, mixed config

## Implementation Plan

### 1. New function: `_resolve_agent_for_role(execution, role)`

```python
def _resolve_agent_for_role(execution: Dict[str, Any], role: str) -> str:
    """Per-role agent: checks profiles[role].agent, falls back to global coding_agent."""
    profiles = (execution or {}).get("profiles") or {}
    entry = profiles.get(role)
    if isinstance(entry, dict):
        role_agent = (entry.get("agent") or "").strip().lower()
        if role_agent in ("hermes", "claude-code", "codex", "opencode", "none"):
            return role_agent
    return _resolve_coding_agent(execution)
```

### 2. Extend `_build_delegation_instructions(agent, cmd, role)`

Add `role` parameter with role-specific instructions:

| Role | Delegation instruction summary |
|------|-------------------------------|
| `pm` | Write spec, post GitHub comment via urllib, complete with `"spec: ..."` |
| `developer` | Write code, run tests, open PR, block with `"review-required"` |
| `qa` | Read files/PR, check each criterion, complete with `"qa-passed"` or `"qa-failed"` |
| `reviewer` | Review diff, complete with `"reviewed:approved"` or `"changes-requested"` |
| `security` | Audit for vulnerabilities, complete with `"security:cleared"` or `"security:flagged"` |
| `docs` | Read PR/issue, post completion report comment, complete the card |

### 3. Update all body functions

- `_pm_body(...)` — inject delegation block when `coding_agent` is cloud agent
- `_qa_task_body(...)` — inject delegation block
- `_reviewer_task_body(...)` — inject delegation block
- `_security_task_body(...)` — inject delegation block
- `_docs_task_body(...)` — inject delegation block
- `_dev_task_body(...)` — already has delegation; use `_resolve_agent_for_role` instead of global

### 4. Update `_check_completed_pm()` call sites

Pass `_resolve_agent_for_role(execution, role)` per role instead of the global `coding_agent`.

### 5. Update skill injection

`coding-agents` skill should be appended for ALL roles where the resolved agent is a cloud agent (not just developer).

### 6. Update `_pm_body()` call in the main dispatch loop

When the PM task is first created, `_pm_body()` needs the per-role agent resolved for `"pm"`.

## Files to Change

- `scripts/daedalus_dispatch.py` — all logic changes
- `tests/test_dispatch.py` — new tests (target: ~85 total)

## Files NOT to Change

- `scripts/provision_roster.sh` — profiles unchanged, skill injection is dynamic
- `config/souls/*.md` — SOUL.md files unchanged; delegation is task-body driven
- `templates/daedalus.yaml` — comments only, update after implementation

## Out of Scope

- Changing how `coding_agent_cmd` resolves (still global, applies to all roles)
- Adding per-role `agent_cmd` overrides (future work)
- Changing the PM SOUL.md template
- Any UI/dashboard changes

## Testing Strategy

New tests to add (in `tests/test_dispatch.py`):

```
test_resolve_agent_for_role_uses_global_when_no_override
test_resolve_agent_for_role_uses_profile_override
test_resolve_agent_for_role_rejects_invalid_override
test_pm_body_has_delegation_when_global_claude_code
test_pm_body_no_delegation_when_global_hermes
test_pm_body_no_delegation_when_profile_override_hermes
test_qa_body_has_delegation_when_global_claude_code
test_qa_body_no_delegation_when_profile_override_hermes
test_reviewer_body_has_delegation_when_global_claude_code
test_security_body_has_delegation_when_global_claude_code
test_docs_body_has_delegation_when_global_claude_code
test_skill_injection_all_delegated_roles
test_skill_injection_skips_overridden_hermes_role
```

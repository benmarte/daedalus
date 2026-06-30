# Spec: Beta.32 Audit + Feature Additions

**Branch:** `dev` → `main` (PR #225)
**Release target:** v1.0.0-beta.33

---

## 1. Objective

Audit the full Daedalus dispatcher codebase across five quality axes (correctness,
readability, architecture, security, performance), apply all findings, then add two
operator-facing features surfaced by the audit. Target users are Daedalus pipeline
operators and CI-automation consumers.

---

## 2. Acceptance Criteria

### Bug fixes (must pass)
- AC1: `qa_failed_cards` is only populated when the fix-card executor returns `ok=True` — prevents notification when kanban is down
- AC2: `enrollment_failures` key present in kanban-only dispatch summary — prevents `KeyError` for callers
- AC3: `merge_pr()` fallback GET failure emits a combined warning with both errors — observable in logs
- AC4: `_redact()` covers URL-percent-encoded token variants — tokens don't leak through transport errors
- AC5: `ensure_labels()` calls `list_labels()` exactly once — no redundant API round-trip
- AC6: `_resolve_web_path()` in GitLab provider: lazy-fetches `path_with_namespace` only when needed; raw API response sanitized before logging
- AC7: `VCSProvider.enrollment_failures` defined as instance attribute on base class — no `getattr` fallbacks needed
- AC8: `_execute_dev_fix_ci` docstring documents `True`/`False` return semantics for callers

### New features (must pass)
- AC9: `max-fix-attempts` is a subscribable NOTIFY_EVENT; `_notify_max_fix_attempts()` sends to configured targets when QA fix card escalates after exhausting `MAX_FIX_ATTEMPTS`
- AC10: `run_iterate()` returns a 5-tuple; 5th slot `escalated_cards` is populated for QA cards that hit the escalation path (file counter `>= MAX_FIX_ATTEMPTS`)
- AC11: `_notify_qa_failed()` is deduplicated per `(issue_n, pr)` within the process lifetime — no per-tick spam while a QA card stays blocked
- AC12: `_notify_max_fix_attempts()` is deduplicated per `(issue_n, pr)` within the process lifetime

### E2E regression tests (must pass)
- AC13: 5-scenario smoke test suite covers QA gate (happy path, mutex, no-QA-signal block, qa-passed merge, skip-qa bypass)
- AC14: Scenario 2 mutex test is deterministic — uses `threading.Event` barriers, not timing

### Test quality (must pass)
- AC15: `ok=False` path (kanban down) does not populate `qa_failed_cards`
- AC16: `ensure_labels()` dedup verified: `list_labels()` called exactly once
- AC17: `enrollment_failures` present in kanban-only dispatch summary tests
- AC18: Combined PUT+GET failure warning asserted in `test_merge_pr_already_merged.py`
- AC19: `qa-failed` and `max-fix-attempts` dedup: second call for same (issue, pr) does not send

---

## 3. Tech Stack / Constraints

- Python 3.14 + pytest
- No new dependencies
- `run_iterate()` return extended to 5-tuple; all callers updated to use `*_` suffix
- Module-level dedup sets use plain `set()` (not TTL/LRU) — reset on process restart is acceptable
- Security: tokens never appear in logs; raw API responses sanitized before logging

---

## 4. Project Structure (changed files)

```
core/
  iterate.py                      — 5-tuple return, escalated_cards, ok=True gate
  providers/
    base.py                       — enrollment_failures on __init__
    github.py                     — combined warning, _redact() encoded variants
    gitlab.py                     — _resolve_web_path(), _existing dedup, Set[str] annotation
    http.py                       — _redact() URL-encoded token variant
scripts/
  daedalus_dispatch.py            — max-fix-attempts event, dedup sets, enrollment_failures cap
tests/
  test_e2e_qa_gate_filelock_smoke.py  — 5-scenario suite, barrier fix, liveness guards
  test_qa_fail_notification.py        — ok=False test, dedup tests, max-fix-attempts tests
  test_gitlab_provider.py             — ensure_labels() single-call tests
  test_merge_pr_already_merged.py     — combined warning log test
  test_daedalus.py                    — enrollment_failures in kanban summary tests
  test_iterate.py / test_pipeline_scenarios.py / … — 5-tuple unpack updates
```

---

## 5. Testing Strategy

- Unit tests for each AC using `FakeKanban` / `FakeProvider` fixtures
- `setup_method` clears module-level dedup sets to prevent cross-test pollution
- `threading.Event` barriers in Scenario 2 for deterministic concurrent test
- Full suite: `python3.14 -m pytest` — must pass 2551+ tests with no regressions

---

## 6. Boundaries

- **Always**: run full test suite before committing; gate `qa_failed_cards` on `ok=True`
- **Ask first**: changing the `run_iterate()` return arity (downstream callers affected)
- **Never**: automate human gates (merge PRs, move to Ready, unblock issues); skip security audit role; use class-level mutable defaults

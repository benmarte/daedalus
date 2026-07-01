# Documentation Gap Analysis — Pipeline & QA Gate Coverage

**Task:** t_d231de66  
**Scope:** Review README, CHANGELOG, USER_GUIDE, and Mermaid diagrams for pipeline/QA gate coverage  
**Date:** 2026-06-29  

---

## Summary

The documentation is broadly comprehensive for the core pipeline flow, but has **3 stale facts**, **4 missing sections**, and **3 Mermaid diagrams that need creation or update**. The QA gate is partially documented (mentioned in CHANGELOG + design spec), but not surfaced in the README or USER_GUIDE as a user-facing behavior. The recent process-level mutex/FileLock fix has no documentation at all.

---

## 1. Sections That Mention Pipeline/QA but Are Outdated

### Finding 1.1: Epic detection threshold mismatch

**Severity:** HIGH (users will miscount epics)

| File | Line(s) | Current text | What source says | Fix |
|------|---------|--------------|------------------|-----|
| `docs/INSTALLATION_GUIDE.md` | 80 | `"≥5 subtasks or labeled "epic""` | `core/providers/base.py:162` — `≥4 checklist items` (`- [ ]` or `* [ ]`) | Change to "≥4 checklist items (`- [ ]` or `* [ ]`)" |
| `docs/INSTALLATION_GUIDE.md` | 80 | `"epic label"` (correct but vague) | Exact heuristic: `≥4 checklist items` OR `epic` label (case-insensitive) OR body `≥2000 chars` | Add the three heuristics explicitly |
| `README.md` | 14 | `"Epic-sized?\n≥4 checklist items\nepic label · body ≥2000 chars"` | Matches source | **Already correct** — no fix needed |
| `docs/USER_GUIDE_NEW_BEHAVIORS.md` | 62 | `"≥4 markdown checklist items"` | Matches source | **Already correct** — no fix needed |

**Action:** Fix INSTALLATION_GUIDE line 80.

### Finding 1.2: CHANGELOG lacks many merged PRs

**Severity:** MEDIUM (users can't see what changed)

The CHANGELOG's "Unreleased" section (lines 134–231) stops at issues #220, #236, #2075, #183, #378, #203, #185. Many merged PRs since then have no CHANGELOG entries:

| Missing PR | Issue | What was fixed |
|------------|-------|----------------|
| #954 | #953 | QA races developer mid-edit on shared working tree |
| #952 | #916 | Validator completes with summary=None when Claude Code delegation fails |
| #950 | #936 | Add advance hook to postinstall |
| #941/#943 | #931 | Dispatcher handler for planner NOT SUITABLE |
| #937 | #915 | Auto-advance sub-issues to Ready after planner decomposition |
| #917 | #902 | E2E regression assertions for #891, #894 |
| #914 | #900 | Dry-run mode flag for dispatcher |
| #913 | #901 | Multi-tick pipeline harness |
| #895 | #894 | Agents silently fail when GITHUB_TOKEN not set |
| #893 | #891 | Concurrent dispatcher ticks re-decompose epics |
| #981 | #962 | Advance hook doesn't fire after planner session |
| #983 | #956 | `pass` substring false-trigger on approve_signals |
| #984 | #957 | Orphaned kanban cards on board Done sync |
| #985 | #955 | `_create_downstream_review_tasks` bypasses QA gate |
| #989 | #986 | Docs update for pipeline reliability fixes |
| #1006 | #998 | QA pass signal auto-merge gate |
| #1027 | #1008 | Process-level mutex — eliminate dispatcher race conditions |
| #1028 | #1015 | FileLock acquired at `main()` with timeout=0 |

**Action:** Add CHANGELOG entries for these PRs.

### Finding 1.3: QA auto-merge gate not in USER_GUIDE

**Severity:** MEDIUM (users don't know QA gates auto-merge)

The QA signal gating (`_qa_passed_for_issue()`) is fully implemented and tested (13 tests in `tests/test_qa_gate_auto_merge.py`), and documented in `docs/qa-gate-design.md`. However, it is **not mentioned** in:
- `docs/USER_GUIDE_NEW_BEHAVIORS.md` (no section covers it)
- `README.md` (not in the pipeline section, not in the self-healing section)

The README *does* mention the QA gate in the agent roster table and briefly in the "How it works" section (lines 158-159), but not as a standalone documented behavior.

**Action:** Add a USER_GUIDE section (e.g., section 6.5 or a new "7. QA Gate" section) explaining the qa-passed signal, the skip-qa label bypass, and the re-check-on-next-tick behavior.

---

## 2. Sections That Should Exist but Don't

### Gap 2.1: Process-level mutex / dispatcher concurrency

**Priority:** HIGH (critical reliability fix)

The dispatcher now acquires a `FileLock` at `main()` entry with `timeout=0` (`scripts/daedalus_dispatch.py:5153-5165`). If another instance holds the lock, the dispatcher exits cleanly with a log message. This eliminates all race conditions from concurrent dispatcher ticks (fixes #988, #993, #994, #995, #996, #997).

**No documentation exists for this.** Users and operators should know:
- The dispatcher is now safe to run from multiple cron entries or overlapping invocations
- The lock path is `~/.hermes/daedalus_dispatch.lock` (or similar)
- A second invocation logs "FileLock already held" and exits 0 (not an error)

**Recommendation:** Add a "Concurrency safety" subsection to the "Autonomous pipeline advancement" or "Self-healing loop" section in README.md, and mention it in the CHANGELOG.

### Gap 2.2: QA gate as a user-facing behavior

**Priority:** HIGH (users need to understand the skip-qa bypass)

The QA gate prevents auto-merge until `_qa_passed_for_issue()` returns `True` (i.e., the QA card's `latest_summary` contains `qa-passed`, case-insensitive). A `skip-qa` label on the PR bypasses the gate entirely.

This behavior should be documented:
1. In USER_GUIDE as a new section (e.g., "6.5 QA Auto-Merge Gate")
2. In README under "How it works" or "Self-healing loop"
3. In CHANGELOG under "Unreleased → Added"

### Gap 2.3: `skip-qa` label documentation

**Priority:** MEDIUM (users won't know this escape hatch exists)

The `skip-qa` label is mentioned in the README mermaid diagram (line 41: `"yes OR skip-qa label"`) and in a passing comment in the README (line 1123), but there is no explicit documentation of:
- What `skip-qa` does (bypasses the QA gate entirely)
- When to use it (docs-only changes, config bumps, dependency updates)
- How to apply it (add the label to the PR on GitHub)

**Recommendation:** Add a "Skipping QA for low-risk changes" subsection to the USER_GUIDE or README.

### Gap 2.4: Dispatcher dry-run mode

**Priority:** LOW (developer-facing, but useful for operators)

The dispatcher accepts a `--dry-run` flag (added in PR #912 for issue #900) that seeds test issues/tasks without touching real GitHub. It also accepts `--self-test` for an offline hermetic smoke test (mentioned in README line 1207).

Neither flag is documented beyond the README's repository layout table. A "Testing & Debugging" section in the README or a standalone doc should explain:
- `--dry-run`: what it does, when to use it, limitations
- `--self-test`: what it asserts, what it does NOT access
- `--history [N]`: viewing past dispatch decisions (already briefly documented in README lines 1446–1463)

**Recommendation:** Create a `docs/testing-and-debugging.md` or add a section to the README.

---

## 3. Mermaid Diagrams That Need Updating or Creation

### Finding 3.1: Main pipeline flowchart — needs QA gate update

**File:** `README.md`, lines 10–54

The existing Mermaid `flowchart TD` shows:
```
QA --> CI{CI}
CI -->|green| Rev[...] 
CI -->|green| A11y[...]
CI -->|red| Fix[...]
```

This is **structurally incorrect** after PR #985 / #1006:
- QA should gate reviewer/security/accessibility, not CI
- The current diagram shows CI as the gate, but now QA runs *before* reviewer/security
- The AutoMerge gate is shown correctly (lines 39–43) but doesn't show `skip-qa` label bypass explicitly

**Required fix:** Update the flow so that:
- Dev → QA → CI → (QA passed gates) → Rev + Sec + A11y → Doc → AutoMerge
- Add `skip-qa` label as a bypass label on AutoMerge

### Finding 3.2: Missing — Self-healing loop diagram

**File:** None exists. The self-healing loop is documented in text only (README lines 771–888).

**Required:** Create a Mermaid `flowchart TD` showing the 5 classify_blocked actions:
- Developer card + CI green + review-required → advance → `_create_downstream_review_tasks()` with QA parenting
- Developer card + CI red → dev_fix_ci
- Reviewer/security + changes requested → pm_route
- Reviewer/security + approved → approve_advance
- Retry cap exhausted → escalate

### Finding 3.3: Missing — Epic decomposition + tier promotion diagram

**File:** None exists. Epic decomposition (Phase 3) and tier promotion are documented in text only (README lines 182–281).

**Required:** Create a Mermaid `flowchart TD` showing:
1. Epic detection (3 heuristics) → planner task
2. Planner signals `PLANNING COMPLETE:` OR `NOT SUITABLE FOR DECOMPOSITION`
3. Case A (checklist → N sub-issues) vs Case B (3 default sub-issues)
4. Tier 0 → Ready label immediately; Tier > 0 → wait for blocker closure → promote
5. Sub-issue enters validator pipeline independently

### Finding 3.4: Missing — QA gate decision tree

**File:** None exists. `docs/qa-gate-design.md` has code snippets but no diagram.

**Required:** Create a simple Mermaid `flowchart TD` showing:
- `_qa_passed_for_issue()` called before auto-merge
- Returns True → merge
- Returns False (no card / empty summary / qa-failed / DB error) → skip merge, re-check next tick
- `skip-qa` label → bypass gate entirely

---

## 4. Additional Minor Gaps

### Gap 4.1: `docs/qa-gate-design.md` not linked from main docs

The QA gate design spec exists at `docs/qa-gate-design.md` but is not linked from:
- README.md (no reference in the "Development references" section, lines 1259–1270)
- CHANGELOG.md (no link in the PR #1006 changelog entry, which doesn't exist yet)
- USER_GUIDE_NEW_BEHAVIORS.md (no mention at all)

**Action:** Add a link to the README's "Development references" table and to USER_GUIDE.

### Gap 4.2: `docs/e2e-smoke-test.md` and `docs/ci-plugin-lifecycle.md` not linked

These docs exist but are not referenced in the README's "Development references" table (lines 1259–1270). They should be linked for developers running CI or smoke tests.

### Gap 4.3: USER_GUIDE line-count drift

The USER_GUIDE references line numbers in source files (e.g., "line 126", "line 380"). These drift over time. A note at the top of USER_GUIDE should warn that line numbers are approximate and users should search for the function name if the line has shifted.

---

## Priority-ordered Action Plan

| # | Action | Effort | Impact |
|---|--------|--------|--------|
| 1 | Fix INSTALLATION_GUIDE line 80 (≥5 → ≥4 checklist items) | 2 min | HIGH |
| 2 | Update README Mermaid diagram to show QA as gate (not CI) | 15 min | HIGH |
| 3 | Add QA gate section to USER_GUIDE | 30 min | HIGH |
| 4 | Document process-level mutex in README + CHANGELOG | 20 min | HIGH |
| 5 | Add CHANGELOG entries for 18 merged PRs | 30 min | MEDIUM |
| 6 | Create self-healing loop Mermaid diagram | 20 min | MEDIUM |
| 7 | Create epic decomposition + tier promotion Mermaid diagram | 20 min | MEDIUM |
| 8 | Create QA gate decision Mermaid diagram | 10 min | MEDIUM |
| 9 | Add `skip-qa` label escape-hatch docs | 10 min | MEDIUM |
| 10 | Link `qa-gate-design.md`, `e2e-smoke-test.md`, `ci-plugin-lifecycle.md` from README | 5 min | LOW |
| 11 | Add dispatcher dry-run / self-test section | 20 min | LOW |
| 12 | Add line-number-drift warning to USER_GUIDE | 2 min | LOW |

---

## Verification Against Source

All findings verified against:
- `core/providers/base.py:is_epic()` — epic heuristics (≥4 checklist items, epic label, ≥2000 chars body)
- `core/iterate.py:_qa_passed_for_issue()` — QA signal gate implementation
- `scripts/daedalus_dispatch.py:5153-5165` — FileLock mutex with timeout=0
- `scripts/daedalus_dispatch.py:_is_epic()` — thin wrapper over `core.providers.base.is_epic()`

---

## Deliverable

This document (`docs/gap-analysis.md`) is the deliverable for task t_d231de66. It enumerates all gaps with file paths, line numbers, and specific remediation actions. The next task (t_12ce2770 or its children) should consume this analysis and produce the actual doc updates.

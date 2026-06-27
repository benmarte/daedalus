# Plan: Phase 3 — Epic Sub-Issue Creation

**Spec:** SPEC.md  
**Issue:** #151  
**Branch:** `feat/issue-151-subissue-creation` → `dev`

---

## Dependency Graph

```
Task 1: add_label() stub on base provider
    └── Task 2: add_label() impl on GitHub provider
            └── Task 3: PLANNER_DECOMPOSE + _execute_planner_decompose() in iterate.py
                    └── Task 5: tests/test_subissue_creation.py

Task 4: update _planner_body() in dispatch script  (independent)
```

---

## Critical Finding: provider not passed to executors

The `_ACTION_EXECUTORS` dispatch loop in `run_iterate` (iterate.py:1001) does NOT
pass `provider` in its kwargs. `_execute_planner_decompose` needs it to call
`create_issue`, `post_issue_comment`, `add_label`, and `get_issue_comments`.

Fix: add `provider=provider` to the executor kwargs call in `run_iterate`. Existing
executors already use `**_kwargs` so they silently ignore it — zero regression risk.

---

## Tasks

### Task 1 — `add_label()` stub on base provider
**File:** `core/providers/base.py`

Add after `create_issue()`:
```python
def add_label(self, issue_number: int, label_name: str) -> bool:
    """Apply a label to an issue. Returns True on success. Default no-op."""
    return False
```

**Verify:** grep confirms the method exists; existing tests still pass.

---

### Task 2 — `add_label()` on GitHub provider
**File:** `core/providers/github.py`

Override `add_label()` using the GitHub Labels API:
```
POST /repos/{owner}/{repo}/issues/{n}/labels
body: {"labels": [label_name]}
```
- Log success/failure via `self._log`
- Return True on 200/201, False otherwise
- Guard against HTTP errors with try/except

**Verify:** unit-testable with a mock `_api` call.

---

### Task 3 — PLANNER_DECOMPOSE in iterate.py  *(core task)*
**File:** `core/iterate.py`

**3a. Pass `provider` to executors.**  
In `run_iterate`, add `provider=provider` to the executor kwargs block (line ~1001).

**3b. Add constant.**  
```python
PLANNER_DECOMPOSE = "planner_decompose"
```

**3c. Update `_classify_blocked_card`.**  
Replace the current PM_ROUTE stub:
```python
if assignee == "planner-daedalus":
    if "PLANNING COMPLETE" in (handoff_text or "").upper():
        return PLANNER_DECOMPOSE
    return PM_ROUTE   # unexpected planner output → escalate to PM
```

**3d. Add `_extract_sub_issues_from_body(body)`.**  
Pure helper, no I/O:
- Regex: `r"^\s*[-*+]\s*\[[ xX]\]"` (re-use `_EPIC_CHECKLIST_RE` pattern)
- Strip `- [ ] ` / `* [x] ` prefix, strip whitespace, skip empty
- Cap at 10 items
- Returns `List[str]` — empty list triggers Case B (3 defaults)

**3e. Add `_default_sub_issue_titles(parent_n, parent_title)`.**  
Returns the 3 fixed defaults when no checklist items found.

**3f. Add `_execute_planner_decompose()`.**  
Signature:
```python
def _execute_planner_decompose(
    slug: str, card: dict, repo: str, handoff_text: str,
    *, workdir: str = "", dry_run: bool = False,
    provider=None, **_kwargs
) -> bool:
```

Logic:
1. Extract parent issue number from card title via `_extract_issue_number_from_card`
2. If `provider is None`: log warning, return False (kanban-only: no VCS, skip gracefully)
3. Fetch parent: `provider.get_issue(parent_n)` — if None, log + return False
4. **Idempotency check**: `provider.get_issue_comments(parent_n)` → scan for
   `<!-- daedalus:sub-issues:` — if found, log "already decomposed" + return True
5. Parse checklist items → build sub-issue list (Case A or B)
6. If dry_run: log planned sub-issues + return True
7. Create each sub-issue: `provider.create_issue(title, body, labels=[...])`
   - Collect returned issue numbers; skip on None (log warning)
8. Post idempotency marker: 
   `provider.post_issue_comment(parent_n, f"<!-- daedalus:sub-issues:{created_numbers} -->")`
9. Apply `epic` label: `provider.add_label(parent_n, "epic")`
10. Create kanban triage card per sub-issue:
    `kanban.create_triage(slug, sub_n, sub_title, body=sub_body, idempotency_key=f"epic-sub-{sub_n}", workspace=...)`
11. `kanban.decompose(slug, tid)` for each triage card
12. Complete the planner card: `kanban.complete(slug, card["id"], summary=f"Decomposed epic #{parent_n} into {len(created_numbers)} sub-issues")`
13. Return True

**3g. Register in `_ACTION_EXECUTORS` and add to counts init.**

**Verify:** `python -m pytest tests/test_subissue_creation.py -v` passes.

---

### Task 4 — Update `_planner_body()` in dispatch script  *(independent)*
**File:** `scripts/daedalus_dispatch.py`

Update the planner agent's task instructions to:
- Tell the agent its job is to analyze the epic issue and confirm it's ready for
  automated decomposition
- Output: `PLANNING COMPLETE: ready for decomposition`
- Remove the Phase 1-only "detection stub" language

**Verify:** `_planner_body()` output contains `PLANNING COMPLETE` in the instructions.

---

### Task 5 — Test suite
**File:** `tests/test_subissue_creation.py`

Tests (all unit, provider mocked):

| Test | Description |
|---|---|
| `test_checklist_case_creates_sub_issues` | Parent with 5 checklist items → 5 sub-issues created |
| `test_checklist_capped_at_10` | Parent with 15 checklist items → only 10 sub-issues |
| `test_no_checklist_creates_3_defaults` | Label/size-detected epic → 3 default sub-issues |
| `test_idempotency_no_duplicate_on_retick` | Comment with `<!-- daedalus:sub-issues:` already present → no provider.create_issue calls |
| `test_provider_none_returns_false` | `provider=None` → returns False, no crash |
| `test_dry_run_no_vcs_calls` | `dry_run=True` → no provider mutations, no kanban calls |
| `test_epic_label_applied` | `provider.add_label` called with `"epic"` |
| `test_marker_comment_posted` | `provider.post_issue_comment` called with marker |
| `test_kanban_triage_created_per_subissue` | `kanban.create_triage` called N times |
| `test_planner_card_completed` | `kanban.complete` called on planner card after decomposition |
| `test_planning_complete_prefix_routes_to_decompose` | `classify_blocked` returns PLANNER_DECOMPOSE for "PLANNING COMPLETE:" handoff |
| `test_other_planner_handoff_routes_to_pm` | `classify_blocked` returns PM_ROUTE for unexpected planner output |
| `test_non_epic_path_unchanged` | validator/developer cards still route through unchanged path |

---

## Checkpoints

- [ ] After Task 1+2: `python -m pytest tests/ -v -k "not test_subissue"` — all existing tests pass
- [ ] After Task 3: `python -m pytest tests/test_subissue_creation.py -v` — all 13 tests pass
- [ ] After Task 4: `_planner_body()` contains `PLANNING COMPLETE` in output
- [ ] After Task 5: full suite `python -m pytest tests/ -v` — all pass, no regressions

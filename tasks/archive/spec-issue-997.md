# SPEC — Issue #997: reconcile-merged-PR bulk-closes downstream cards that never ran

**Branch:** `fix/issue-997-skip-unstarted-review-cards`
**PR target:** `dev`
**Status:** Validator-confirmed, ready to implement.

---

## 1. Objective

Stop the reconcile/close path from silently marking QA, reviewer, security, documentation, and
accessibility cards as **done** when those cards never ran (`started_at is None`). When a PR merges
quickly, downstream review agents must still be dispatched (or, for an abandoned issue, explicitly
recorded as *never dispatched* rather than counted as completed reviews).

## 2. Root Cause

`core/kanban.close_issue_tasks` (`core/kanban.py:426-496`) completes **every** non-done card
matching `#issue_number` with no `started_at` check:

- **First pass (lines 445-460):** filters only on `status` (`done/complete/completed/cancelled`).
  Any matching card in `todo/running/ready` is completed regardless of whether it ever ran.
- **Second pass (lines 462-494):** walks blocked children with `latest_summary` starting
  `review-required:` and completes them unconditionally when a `summary` is supplied.

`_execute_reconcile_merged` (`core/iterate.py:638`) calls it with a summary on every PR merge, so the
five downstream review-role cards (`started_at=None`, empty run logs) are closed before any agent
dispatches — code review, security audit, QA, docs, and accessibility checks are silently skipped.
Reproduced in the issue #986 dispatch log (2026-06-29 07:50): "closed 6 card(s)" including
`t_28f029e2` (qa), `t_88c45405` (reviewer), `t_74cc509e` (security), `t_45e3424d` (docs),
`t_a93156a6` (accessibility) — all with `started=None`.

## 3. Fix Strategy

Add an explicit `skip_unstarted` flag to `close_issue_tasks` and apply a `started_at` guard in **both
passes**. Behaviour differs by caller intent:

1. **Signature change** (`core/kanban.py:426`):
   ```python
   def close_issue_tasks(slug, issue_number, *, summary="", dry_run=False,
                         skip_unstarted=True) -> List[str]:
   ```
   Default `skip_unstarted=True` makes the function safe-by-default.

2. **First pass guard (lines 445-460):** before completing a matching card, read its
   `started_at` (top-level field). If `started_at` is falsy/`None`:
   - When `skip_unstarted=True` → **do NOT complete**; leave the card active so the normal
     dispatcher flow runs it. Emit a `logger.warning` naming the card id + assignee
     (e.g. `"close_issue_tasks: leaving unstarted card %s (%s) active for dispatch"`).
   - When `skip_unstarted=False` → complete it, but with a **distinct summary prefix**
     `"skipped-never-dispatched: "` (instead of the merge/close summary) so the board/metrics
     show the review was never actually performed, not legitimately completed.

3. **Second pass guard (lines 462-494):** apply the same `started_at` check to each child
   (`child_task.get("started_at")`, since children come from `show_card(...)["task"]`). A blocked
   `review-required:` child that never started is the *primary* leak — honour `skip_unstarted` the
   same way as the first pass.

4. **Caller wiring:**
   - `core/iterate.py:638` (`_execute_reconcile_merged`, merged PR): keep the default
     `skip_unstarted=True` → unstarted review cards are left active and dispatched. **This is the
     core bug fix.**
   - `scripts/daedalus_dispatch.py:4154 / 4162 / 4388 / 4392 / 4451 / 4456` (issue externally
     *closed*, i.e. abandoned/wontfix — not merged): pass `skip_unstarted=False`. Work is
     abandoned, so cards are still closed, but unstarted review cards get the
     `"skipped-never-dispatched:"` summary for audit rather than being recorded as completed.

Rationale for the flag over an unconditional skip: a GitHub-closed (abandoned) issue legitimately
wants its idle cards cleared, whereas a *merged* PR must let review run. One parameter expresses both
intents without duplicating the helper.

## 4. Acceptance Criteria

- [ ] **AC1:** When a PR merges (`_execute_reconcile_merged`), matching cards with `started_at=None`
      are NOT marked done — `close_issue_tasks(..., skip_unstarted=True)` leaves them active and logs
      a warning per card.
- [ ] **AC2:** QA, reviewer, security, documentation, and accessibility cards remain dispatchable
      (or, on the abandoned-issue path, are completed with a `"skipped-never-dispatched:"` summary, not
      the generic merge summary).
- [ ] **AC3:** New test in `tests/test_dispatch.py`: given a board with a started developer card and
      unstarted review cards for `#42`, `close_issue_tasks(slug, 42)` (default `skip_unstarted=True`)
      completes only the started card and leaves the unstarted review cards untouched
      (`complete` not called for them). A second test asserts `skip_unstarted=False` completes the
      unstarted cards with a `skipped-never-dispatched` summary.
- [ ] **AC4:** No regression — existing tests
      (`test_close_issue_tasks_completes_non_done_and_skips_done`,
      `test_close_issue_tasks_walks_children_with_review_required_summary`,
      `test_close_issue_tasks_dry_run_does_not_call_complete`,
      `test_close_issue_tasks_empty_board_returns_empty`) still pass. NOTE: existing fixtures define
      tasks without a `started_at` key — under the new `skip_unstarted=True` default those cards count
      as unstarted, so the existing fixtures must add `"started_at": <ts>` to cards that are expected
      to be completed, or those tests must pass `skip_unstarted=False`. Update fixtures accordingly so
      intent stays explicit.
- [ ] **AC5:** `dry_run=True` still performs no completions and reports the would-complete ids under
      the new guard semantics.

## 5. Testing Strategy

- Unit tests in `tests/test_dispatch.py` following the existing `mock.patch("core.kanban.list_tasks"/
  "core.kanban.complete"/"core.kanban.show_card")` + `check(...)` convention.
- Fixtures must include `started_at` on cards (top-level for first-pass tasks; under `"task"` for
  children) to exercise both started and unstarted paths.
- Run: `python3.14 -m pytest tests/test_dispatch.py -q` (per repo convention; isolate work in a git
  worktree and commit early to avoid the concurrent-dispatch trampling hazard).

## 6. Boundaries

- **Do:** modify `core/kanban.close_issue_tasks`, wire the three dispatch call sites + the iterate
  call site, add/adjust tests, update CHANGELOG.
- **Ask first:** any change to the dispatcher's downstream-card *creation* logic (out of scope —
  the dispatcher already creates these cards).
- **Never:** auto-merge PRs, move cards to Done via side channels, or weaken the `done/cancelled`
  status short-circuit (AC4 regression guard).

## 📋 Implementation Spec — Issue #961

**Title:** perf: dispatcher processes validator retry logic per-task instead of per-issue, burning O(N) API calls

**Branch:** `fix/issue-961-dedup-validator-issue-fetch`
**PR target:** `dev`

---

### Root Cause

`_check_confirmed_validators()` in `scripts/daedalus_dispatch.py` (loop starts at line ~2667) iterates over **every** done validator kanban task. For each task whose summary is empty/unrecognized (the runaway-loop case), the **empty-summary fallback branch** (lines ~2795–2806) makes two GitHub API calls *per task*:

```python
# line ~2800 — one get_issue (with retry) per TASK
fetched = _fetch_issue_with_retry(provider, n_nr)
# line ~2806 — one get_issue_comments per TASK
gh_outcome = _validator_github_comment_outcome(provider, n_nr, p["validator"])
```

There is **no grouping or memoization by issue number**. When a single issue has 13 done validator tasks (from the runaway retry loop fixed in #959), the dispatcher makes `13 × 2 = 26` GitHub API calls for that one issue, every tick. With several cap-exhausted issues this exhausts the rate limit before the dispatcher ever reaches the actual Ready issues (#955, #956, #957).

Note: the *outcome* of those two calls is identical for every task that shares an issue number — `n_nr` is the only input — so the repeated calls are pure waste. The retry-cap accounting (`validator_tasks`, `retry_count`, `cap_count`) already operates per-issue and produces the same verdict for every task of that issue; the idempotency key `validator-retry-{n_nr}-r{retry_count}` already prevents duplicate retry-task creation. Only the API calls are unbounded.

### Fix Strategy

Eliminate the redundant per-task GitHub calls so each unique issue number is fetched **at most once per tick**. Preferred approach (minimal-diff, surgical, low risk):

**Per-tick memoization cache** keyed by issue number for the two API-calling helpers, scoped to a single `_check_confirmed_validators` invocation:

1. Initialize two local dicts at the top of the function, e.g. `_issue_fetch_cache: Dict[int, Optional[Dict]] = {}` and `_gh_outcome_cache: Dict[int, Optional[str]] = {}`.
2. Wrap the call site at line ~2800: only call `_fetch_issue_with_retry(provider, n_nr)` when `n_nr not in _issue_fetch_cache`; otherwise reuse the cached value (cache the result whether the fetch succeeded or returned `None`).
3. Wrap the call site at line ~2806: only call `_validator_github_comment_outcome(provider, n_nr, p["validator"])` when `n_nr not in _gh_outcome_cache`; otherwise reuse.
4. Apply the same cache to the BLOCKED branch fetch at line ~2687 (`_fetch_issue_with_retry`) so it shares the cache and benefits identically.

This achieves the issue's goal (`O(unique issues)` API calls instead of `O(tasks)`) without restructuring the multi-branch loop body (CONFIRMED / BLOCKED / STOP / ESCALATE / empty-summary), which keeps blast radius minimal and preserves all existing branch behavior and notification idempotency.

> Alternative (explicitly **not** preferred): pre-grouping all done validator tasks by issue number before the loop. This forces restructuring every branch and increases regression risk for no additional benefit, since memoization already collapses the API calls to one-per-issue.

The cache lives only for the duration of one tick (one function call), so freshness across ticks is unchanged — each new dispatch tick re-fetches.

### Acceptance Criteria

1. **API-call dedup:** For a kanban with N done validator tasks all sharing one issue number and all in the empty-summary branch, `_check_confirmed_validators` calls `provider.get_issue` / `_fetch_issue_with_retry` **at most once** and `get_issue_comments` / `_validator_github_comment_outcome` **at most once** for that issue per tick (assert via a mock provider that counts calls). Target: `5 issues × 13 tasks` → ≤ `5×2 = 10` provider calls, not `130`.
2. **Behavior preserved:** All existing branches (CONFIRMED github-comment fallback, BLOCKED consultation, STOP auto-close, ESCALATE skip, empty-summary retry, retry-cap-exhausted notification + GitHub comment) produce identical outcomes and the same set of `triggered` issue numbers as before the change.
3. **Idempotency preserved:** Retry-task creation still uses `validator-retry-{n_nr}-r{retry_count}`; no duplicate retry tasks or duplicate cap-exhausted notifications are created.
4. **Multi-issue correctness:** With done validator tasks spanning multiple distinct issue numbers, each issue is fetched exactly once and routed to its correct branch (cache must key on `n_nr`, never leak one issue's fetched data to another).
5. **Regression test:** Add a test in `tests/test_dispatch.py` (and/or e2e in `tests/test_e2e_full_pipeline.py`) using a `FakeProvider` (see `tests/conftest.py`) that records `get_issue`/`get_issue_comments` call counts, asserting the per-issue dedup. Test must run under both pytest and the standalone `__main__` runner.
6. **No new behavior past the cap:** cap-exhausted issues (`cap_count >= max+1` or `retry_count >= absolute_max`) still emit exactly one notification per issue via the existing `_RETRY_CAP_MARKER` guard.
7. Full existing dispatcher test suite (`pytest tests/test_dispatch.py`) remains green.

### Out of Scope

- The runaway-loop / absolute-ceiling logic (already fixed by #959/#960).
- The PM-task scan below the validator loop (line ~2950+) — not part of this perf issue.
- Any change to GitHub fetch retry semantics inside `_fetch_issue_with_retry`.

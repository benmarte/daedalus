# Issue #1199 — auto-merge sweep re-runs failed CI (bounded), then escalates

## Spec / Acceptance criteria
- Pipeline-complete PR (`docs-<n>` done, issue open, open `fix/issue-<n>` PR) + genuinely **RED** CI
  (never PENDING/UNKNOWN) → sweep re-runs failed CI, bounded to **N=2** per PR head SHA.
- CI passes after a re-run → existing merge path takes over (no change).
- CI still RED after N re-runs → **escalate** (PR comment w/ failing-run URL + logged warning), no loop.
- Idempotent: markers persisted on the PR ⇒ same-tick re-invocation never exceeds N.
- No regression in existing `sweep_deferred_merges` / `_try_merge_if_gates_pass` tests.

## Design
- Natural inter-tick backoff: issuing a re-run flips CI to PENDING, so the sweep only
  acts again once it settles back to RED — no timer needed.
- Idempotency: per-SHA marker comments on the PR:
  - re-run:   `<!-- daedalus:ci-rerun:<sha>:<n> -->`  (count ⇒ attempts used)
  - escalated:`<!-- daedalus:ci-escalated:<sha> -->`  (presence ⇒ stop, no loop)
  - New head SHA ⇒ fresh budget (new code deserves fresh attempts).

## Tasks
1. [x] Provider capability — base.py: `supports_ci_rerun=False`, default `get_pr_head_sha`,
       `rerun_failed_ci`, `failed_ci_run_url` no-ops.
2. [x] GitHub impl — github.py: `supports_ci_rerun=True`, `get_pr_head_sha`,
       `_latest_failed_run`, `rerun_failed_ci` (POST rerun-failed-jobs), `failed_ci_run_url`.
3. [x] iterate.py: `_rerun_or_escalate_red_ci` helper + marker counting; wire into
       `sweep_deferred_merges` on RED CI.
4. [x] Tests — tests/test_issue_1199_ci_rerun.py (18 tests: rerun→merge, exhaust→escalate, idempotent).
5. [x] Full suite: 2938 passed (-n auto); ruff clean on changed files.

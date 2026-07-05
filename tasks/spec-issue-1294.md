# Spec — Issue #1294: Native planner decompose + QA swarm fan-out

**Phase 5 of #1276.** Predecessor Phase 2 (`pipeline.upfront_dag`, #1290/#1300) is **landed** in dev. Branch: `feat/issue-1294-native-decompose` → PR target: **dev**. Flag: default-off `planner.native_decompose`.

> **CLI verified against the installed `hermes kanban` binary (2026-07-05):**
> - `hermes kanban decompose [task_id] [--all] [--tenant] [--author] [--json]` — **no `--description` flag.** The decomposer (`auxiliary.kanban_decomposer`) routes a triage card's work to roles **by each profile's `description`** (a profile property), not a call-time argument. The issue's "profile routing by `--description`" is imprecise — routing is controlled by the triage-card body + profile descriptions.
> - `hermes kanban swarm --worker PROFILE:TITLE[:SKILL,…] --verifier VERIFIER --synthesizer SYNTHESIZER [--idempotency-key KEY] [--json] <goal>` — Kanban Swarm v1 graph: **parallel workers → verifier → synthesizer**, single root card, dedup via `--idempotency-key`.

> **Code anchors (verified against tree):**
> - Planner decompose: `_trigger_planner_decompose` `scripts/daedalus_dispatch.py:5630`; `_execute_planner_decompose` / `_inner` `core/iterate.py:2096`–`2335`; per-sub-issue triage+decompose loop `core/iterate.py:2307`–`2329` (already calls `kanban.decompose`).
> - QA fan-out (two sites): primary dispatch `scripts/daedalus_dispatch.py:6200`–`6280` (qa parented to dev; reviewer/security parented to qa; docs to dev+reviewer+security); iterate advance `_create_downstream_review_tasks` `core/iterate.py:819`, roles `_DOWNSTREAM_REVIEW_ROLES` L784, edges `_downstream_parents` L789.
> - `kanban.decompose` `core/kanban.py:190`; `decompose_all_triage` `core/kanban.py:282`. **No `swarm` wrapper exists** — must be added.
> - Flag-read pattern (from #1290): `bool((resolved.get("<section>") or {}).get("<flag>", False))`.

---

## Goal

Behind default-off `planner.native_decompose`, replace two custom-orchestration paths with native Hermes kanban primitives, **byte-identical to today when the flag is off**:

1. **Planner epic decomposition → `hermes kanban decompose`.** Instead of the custom checklist-extraction → GitHub-sub-issue-creation → per-issue triage loop, route the epic through a single triage card and let the Hermes decomposer fan it out into role-routed child cards.
2. **QA-gated reviewer/security/accessibility fan-out → `hermes kanban swarm`.** Instead of hand-creating parented reviewer/security/accessibility (+docs) cards, emit one swarm graph rooted when QA completes.

Epic-detection thresholds unchanged. Completion-signal substring matching + `classify_blocked()` routing intact.

## Design decisions (confirmed with owner 2026-07-05)

**D1 — Full native (child cards). CONFIRMED.**
Under the flag, epic decomposition routes through a single triage card + `hermes kanban decompose`, producing **role-routed kanban child cards** (no GitHub sub-issues). The custom checklist-extraction + `provider.create_issue()` path becomes the flag-off `else` branch. Matches #1276's "migrate to native primitives" thesis. Consequence: flag-on epics no longer emit trackable GitHub sub-issues (accepted).

**D2 — Swarm the post-QA review+docs stage; preserve the QA-gate invariant. CONFIRMED (owner delegated the call).**
`swarm` requires exactly one `--verifier` and one `--synthesizer`, with the verifier running **after** the parallel workers. Mapping:
- **Root the swarm when the QA gate card completes** — preserves the documented "QA gates reviewer/security/accessibility" invariant. (Rejected alternative: rooting at dev-completion, which would move QA after the reviews and waste review effort on PRs QA would reject.)
- workers = `reviewer-daedalus`, `security-analyst-daedalus`, `accessibility-daedalus` (UI issues only, same predicate as today)
- `--verifier` = `qa-daedalus` — a post-review consolidation pass. The gate-QA card is already **complete** when the swarm roots, so at most one active `qa` card exists at a time → no `classify_blocked` assignee-substring collision.
- `--synthesizer` = `documentation-daedalus` (docs = the terminal synthesis output; natural fit)
- `--idempotency-key` = `swarm-{issue_number}`
- `<goal>` = "Review + document PR for issue #N"
(Rejected alternative: "reviews-only" swarm — impossible, `swarm` mandates a verifier + synthesizer.)

## Structure decision — split by part (behind the one flag)

Both parts are gated by `planner.native_decompose` (default `False`) → flag-off is inert.
- **Part B — native QA swarm fan-out** (`kanban.swarm()`/`kanban.link()` + `_create_downstream_swarm` in `core/iterate/executors.py`). **Shipped: PR #1312 (merged).**
- **Part A — native planner epic decomposition via `hermes kanban decompose`** (D1a: one epic triage card + `decompose` → role-routed kanban child cards, replacing the `provider.create_issue` sub-issue loop in `_execute_planner_decompose_inner`). Flag threaded through both entry points: the `classify_blocked` executor path (`core/iterate/__init__.py`) and the per-tick trigger `core/dispatch/checks.py::check_planner_decompose_trigger`. **Shipped: this PR.** With both parts landed, **#1294 closes.**

## Acceptance criteria (each testable)

1. **`kanban.swarm(...)` — additive wrapper.** New `core/kanban.py::swarm(slug, goal, workers, verifier, synthesizer, idempotency_key=..., ...)` builds `hermes kanban --board <slug> swarm --worker … --verifier … --synthesizer … --idempotency-key … <goal>` argv. Follows the never-raise contract (log + return falsy on failure). Test asserts exact argv incl. repeated `--worker`.
2. **Flag read, threaded once.** `native_decompose = bool((resolved.get("planner") or {}).get("native_decompose", False))` resolved once per tick, threaded to the planner-decompose path and the QA-fan-out path. Never read env. Test asserts flag present→true, absent→false.
3. **Native planner decompose (flag ON).** When on, `_execute_planner_decompose` routes the epic through one triage card + `kanban.decompose` (per D1a) instead of custom sub-issue creation. Idempotency preserved (existing `daedalus:decomposed:` marker + `_acquire_decompose_lock` still guard against re-decompose). Epic-detection thresholds (`_EPIC_CHECKLIST_MIN`, `_EPIC_BODY_SIZE_MIN`) unchanged. Test: flag on → one triage card + one `decompose` call, no `provider.create_issue`; marker still written; re-tick creates zero duplicates.
4. **Native QA swarm fan-out (flag ON).** When the QA card completes with the flag on, the reviewer/security/accessibility(+docs) fan-out is created via one `kanban.swarm(...)` call (per D2 mapping) instead of individual `create_task` calls. Accessibility worker included only for UI issues. Idempotent via `swarm-{n}`. Test asserts the swarm argv and that no per-role `create_task` fires when flag on.
5. **Completion signals + classify_blocked intact.** The role cards the swarm/decompose create still carry summaries whose completion substrings (`docs posted`, `CONFIRMED:`, review/security sign-off phrases) are matched by `classify_blocked()`. Test drives a swarm-created card to completion and asserts the dispatcher routes it identically to a legacy card.
6. **Flag-OFF is byte-identical.** With `planner.native_decompose` absent/false: planner path creates GitHub sub-issues exactly as today; QA fan-out creates individual parented cards exactly as today; no `swarm`/native-decompose call fires. Existing suite passes unchanged; a regression test diffs the flag-off argv/call sequence against current behavior.
7. **QA-gate invariant preserved.** Even under the flag, reviewer/security/accessibility never run before QA completes (the swarm is rooted at QA completion). Test asserts the swarm is not emitted while the QA card is still open.
8. **Full suite + self-test/e2e green.** `python tests/test_daedalus.py` and `python -m pytest tests/ -q` pass. Add `tests/test_native_decompose_1294.py` covering ACs 1–7. `python scripts/daedalus_dispatch.py --self-test` still passes. An e2e run with the flag ON drives an epic → decompose → dev → QA → swarm → docs.

## Files to change (explicit)

- `core/kanban.py` — add `swarm(...)` wrapper (beside `decompose` L190).
- `core/iterate.py` — thread `native_decompose`; branch `_execute_planner_decompose_inner` (L2096) on it (D1a); branch `_create_downstream_review_tasks` (L819) to emit a swarm when on.
- `scripts/daedalus_dispatch.py` — resolve the flag once; thread to `_trigger_planner_decompose` (L5630) and the primary QA fan-out site (L6200–6280).
- `templates/daedalus.yaml` — add commented `planner:` section with `native_decompose: false` + doc comment.
- `config/__init__.py` — optional `validate_planner(resolved)` type-check (bool) if the section is present; no hard requirement.
- `tests/test_native_decompose_1294.py` (new) — ACs 1–7.
- `CHANGELOG.md` — `feat:` entry.

## Design notes

- **Flag-off coexistence (byte-identical):** all new code lives behind `if native_decompose:` branches; the existing sub-issue-creation and per-role fan-out paths are the untouched `else`. No shared statement reordered → flag-off argv/call sequence unchanged (same guarantee #1290 used).
- **Never-raise:** `swarm()` mirrors `decompose()` — on non-zero rc, log a warning and return `False`/`""`; the caller falls back to the legacy path so a swarm failure degrades to today's behavior rather than stranding the pipeline.
- **Idempotency:** planner decompose keeps the `daedalus:decomposed:` marker + lock; swarm uses `--idempotency-key swarm-{n}` so a re-tick of a completed-QA card re-roots zero duplicate swarms.
- **Decomposer routing:** we do NOT pass `--description`; instead the triage-card body describes the full lifecycle (mirroring the existing `_triage_body_for_decompose` at `core/iterate.py:1193`), and the Hermes decomposer routes to roles by profile description.

## Out of scope (build ON this later)

- #1298 — native webhook intake (blocked on upstream `hermes webhook subscribe --script`).
- Flipping `native_decompose` to default-on (separate decision after soak).
- Removing the legacy sub-issue/fan-out code paths (kept as the flag-off branch until native soaks).

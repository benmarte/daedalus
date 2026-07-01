# Spec — Issue #1053: Trigger profile resync when the config fingerprint changes

**Epic:** #1051 — sync agent profile models when `coding_agent` or global model changes
**Depends on:** #1052 (compute + store config fingerprint per tick — ✅ merged to `dev`)
**Branch:** `fix/issue-1053-fingerprint-resync-trigger`
**PR target:** `dev`

---

## 1. Objective

When the dispatcher detects that the config fingerprint (`coding_agent` + global
`model.default`) has changed since the last resync, it must trigger a one-time
profile resync for that project and persist the new fingerprint so the resync
does not re-fire on subsequent unchanged ticks. State is per-project (keyed by
`workdir`), so a change in one project never resyncs or mutates another's state.

## 2. Root cause

The dispatcher's `run()` loop (`scripts/daedalus_dispatch.py`) computes and
stores the config fingerprint (issue #1052) but does **not** drive a resync from
it. Two concrete gaps:

1. **The resync fingerprint is never persisted.** The intended wiring reads
   `dispatch_state.get_resync_fingerprint(workdir)` to decide whether to resync,
   but no code path calls `dispatch_state.set_resync_fingerprint(workdir, fp)`.
   Result: `get_resync_fingerprint` is always `None`, every tick is treated as a
   "first tick", and `_resync_profiles_to_model()` is never invoked from `run()`.
2. **The change log references the wrong identifier.** The fingerprint-change log
   line must name the project (`resolved["name"]` / repo), not the raw `workdir`
   path, so operators can see which project resynced.

This is the single behavioral defect behind the 4 failing integration tests
(`tests/test_profile_resync_integration.py`):
`test_fingerprint_change_triggers_resync`,
`test_first_tick_seeds_resync_fingerprint_without_resync`,
`test_unrelated_project_not_affected`,
`test_resync_logs_fingerprint_change`.

## 3. Dependency note (read before implementing)

On `dev` today, **only** the #1052 helpers exist
(`compute_config_fingerprint`, `set_config_fingerprint`, `get_config_fingerprint`).
The pieces this issue wires together —
`set_resync_fingerprint` / `get_resync_fingerprint`,
`set_config_values` / `get_config_values` (in `core/dispatch_state.py`),
and `_resync_profiles_to_model()` / `_log_resync()`
(in `scripts/daedalus_dispatch.py`) — currently exist **only as uncommitted
working-tree/concurrent sibling-issue work**, not on `dev`.

**The PR must be self-contained and green on `dev`.** Branch from `dev` and, for
each supporting symbol the run() wiring calls, confirm it is already on `dev`;
if not, include it in this PR so CI passes. Do not assume sibling PRs land first.
(Reference implementations for these helpers already exist in the current
working tree and in the untracked test files — reuse them rather than
reinventing.)

## 4. Fix strategy

In `run()`, inside the existing `if workdir and not dry_run:` block (right after
the fingerprint is computed and `set_config_fingerprint` is called):

1. Resolve old values: `old_resync_fp = get_resync_fingerprint(workdir)`;
   read `old_config_vals = get_config_values(workdir)` → `old_coding_agent`,
   `old_model` (empty strings when absent).
2. **First tick** (`old_resync_fp is None`):
   - `set_resync_fingerprint(workdir, _config_fp)`  ← **the missing call**
   - `set_config_values(workdir, coding_agent, new_model)`
   - **Do not** call `_resync_profiles_to_model()`.
   - Log at INFO that the resync fingerprint was seeded (no resync).
3. **Changed** (`old_resync_fp != _config_fp`):
   - Log at INFO a "fingerprint changed" line that includes the **project name**
     (`resolved.get("name")` or `repo`) and the word "resync"/"triggering",
     plus `old→new` coding_agent and model.
   - Call `_resync_profiles_to_model(workdir, new_coding_agent=coding_agent,
     new_model=new_model, old_coding_agent=old_coding_agent, old_model=old_model)`.
   - `set_resync_fingerprint(workdir, _config_fp)`  ← **the missing call**
   - `set_config_values(workdir, coding_agent, new_model)`
4. **Unchanged** (`old_resync_fp == _config_fp`): no-op (no resync, no writes
   needed beyond the already-stored config fingerprint).
5. **`dry_run`**: the whole block is already gated by `not dry_run` — keep it
   that way. No fingerprint, no resync, no state writes in dry-run.

`new_model` = `active_model.get("model") or ""` (already resolved via
`_resolve_active_model_provider()`).

Keep the change minimal and surgical — this is wiring + one log-target fix, not a
redesign of the resync function itself.

## 5. Acceptance criteria

- [ ] First tick seeds the resync fingerprint (`get_resync_fingerprint(workdir)
      == get_config_fingerprint(workdir)`) and does **not** call
      `_resync_profiles_to_model`.
- [ ] A subsequent tick whose fingerprint differs from the stored resync
      fingerprint calls `_resync_profiles_to_model` exactly once, then updates the
      resync fingerprint to the new value.
- [ ] A tick whose fingerprint matches the stored resync fingerprint does **not**
      call `_resync_profiles_to_model` (idempotent / deduped across ticks).
- [ ] `dry_run=True` performs no resync and writes no fingerprint/config-value
      state.
- [ ] The fingerprint-change event is logged at INFO and the message contains the
      project name (or repo) and "resync"/"triggering".
- [ ] State is per-project: triggering a resync for project A leaves project B's
      resync fingerprint untouched until B itself ticks.
- [ ] `tests/test_profile_resync_integration.py` passes (all 8, especially the 4
      previously failing).
- [ ] `tests/test_resync_log.py` and the `dispatch_state` resync/config-value unit
      tests pass.
- [ ] Full suite green; PR opened against `dev` and passing CI.

## 6. Testing strategy

- Run with `python3.14` (per project convention for the dispatcher tests).
- Primary contract: `pytest tests/test_profile_resync_integration.py -v` (the
  end-to-end fingerprint-change → resync behavior).
- Supporting: `pytest tests/test_resync_log.py tests/test_profile_resync.py
  tests/test_profile_model_fallback.py -v`.
- No new test files required — the failing integration tests already encode the
  contract. Add a test only if a behavior in §4 is not already covered.

## 7. Boundaries

- **Always:** branch from `dev`; keep the diff minimal and confined to the
  `run()` resync wiring (and any supporting helper that isn't yet on `dev`);
  commit early in an isolated worktree (concurrent-dispatch hazard).
- **Ask first:** before changing the *signature* or *behavior* of
  `_resync_profiles_to_model` / `_log_resync` / the `dispatch_state` helpers —
  this issue only wires them in.
- **Never:** resync during `dry_run`; mutate another project's state; resync on
  an unchanged fingerprint; widen scope to file-overlap/merge logic (unrelated to
  this epic).

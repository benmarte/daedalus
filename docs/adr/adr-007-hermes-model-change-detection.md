# ADR-007: Hermes Model-Change Detection & Profile Sync

**Status:** Accepted
**Date:** 2026-07-08
**Issue:** #1367
**Follow-up:** #1368

---

## Context

Daedalus runs its worker agents from dedicated Hermes profiles
(`<role>-daedalus`). Each profile has its own `config.yaml` whose `model` block
must track the operator's active Hermes model selection — otherwise a profile
keeps running a stale/wrong model after the operator switches models via
`hermes model`, the Config web page, or `hermes config set model.default …`.

Issue #1367 asked whether Hermes exposes an API / event / webhook / signal we
can subscribe to so Daedalus detects **when** the model/provider changes and
**what** the new values are, in real time — and whether the current custom sync
code can then be retired.

### What Hermes exposes today (investigation findings)

Verified against the installed Hermes at `~/.hermes/hermes-agent/` on 2026-07-08.

- **Plugin hook registry** (`hermes_cli/plugins.py`, the authoritative
  `_ALLOWED_HOOK_EVENTS` set): lifecycle (`pre/post_tool_call`, `pre_verify`,
  `pre/post_api_request`, `api_request_error`), session (`on_session_start`,
  `on_session_end`, `on_session_finalize`, `on_session_reset`), subagent
  (`subagent_start/stop`), gateway (`pre_gateway_dispatch`), approval
  (`pre_approval_request`, `post_approval_response`), and kanban-task
  (`kanban_task_claimed`, `kanban_task_completed`, `kanban_task_blocked`).
  **There is no `on_model_change` (or equivalent) hook.**
- **Model mutation paths** all write `model.default` / `model.provider` into
  `~/.hermes/config.yaml` and emit **no** event: the `hermes model` picker, the
  Config web page save path (`web_server.py` → `_infer_provider_on_model_change`
  → `_apply_main_model_assignment`), and `hermes config set model.*`.
- **No** event stream, SSE endpoint, Unix socket, or file-watch API for config
  changes. `webhook_normalizer._normalize_hermes` (`core/webhook_normalizer.py`)
  is a forward-looking stub for kanban **ready** events, not model changes.
- **Pollable fallbacks:** `~/.hermes/config.yaml` is the durable source of truth
  (`hermes config path` locates it; `hermes config show` renders the `model`
  block in human format). Reading the file directly is cheaper and more
  parseable than shelling out to `hermes config show`.

**Conclusion:** Hermes offers no push signal for model changes today. Detection
must be either (a) event-adjacent — react on a hook that fires around the time a
profile is about to be used — or (b) poll `config.yaml`.

### Daedalus's three existing detection paths

| # | Path | Location | Trigger | Scope | Respects per-profile override? |
|---|------|----------|---------|-------|-------------------------------|
| 1 | **`kanban_task_claimed` JIT hook** | `__init__.py:_on_kanban_task_claimed` | event: fires in the dispatcher right before each worker subprocess spawns | the one profile about to run | yes (`_daedalus_model_override`) |
| 2 | **Poll-fingerprint** | `core/dispatch/resolvers.py:_resync_profiles_to_model` + `core/dispatch_state.py` `resync_fingerprint` | poll: each dispatch tick, when the SHA-256 of `(coding_agent, model.default)` changes | all `*-daedalus` profiles | yes (skips explicit overrides) |
| 3 | **Standalone force-sync** | `core/sync_profiles.py` (`--sync-profiles-model` CLI, dashboard admin endpoint) | manual/operator | all (or selected) profiles | no — `force=True` overrides locks |

Path 1 is genuinely event-driven and always reads current config at the exact
moment a profile is about to run, so a worker never starts on a stale model. It
also syncs more than the poll path: `model`, `providers`, `fallback_providers`,
`custom_providers`, and strips the unregistered `messaging` toolset. Paths 2 and
3 predate the hook and now overlap it for model sync.

## Decision

1. **`kanban_task_claimed` (path 1) is the canonical automatic sync mechanism.**
   It is event-driven, always-fresh at spawn time, needs no polling state, and
   already syncs the full model/provider surface. Its docstring is annotated as
   canonical (see ADR reference in `__init__.py`).

2. **Demote the poll-fingerprint path (path 2) to a fallback.** Its model-sync
   duty is now redundant with path 1 (any profile that actually runs is synced
   at claim time). It is retained only because it (a) also detects
   `coding_agent` changes and (b) covers profiles that never get claimed within
   a tick. Its docstring is annotated to say so and to point here. No behavior
   change — it still runs.

3. **Keep the standalone force-sync (path 3) as the operator escape hatch.** It
   is semantically distinct: `force=True` deliberately overrides per-profile
   `_daedalus_model_override` locks, which neither hook nor poll path do. It
   backs the dashboard "sync profiles" button and the `--sync-profiles-model`
   CLI. Its module docstring is annotated to say it is manual-only, not part of
   the automatic path.

4. **Propose an upstream `on_model_change` Hermes hook** (follow-up #1368) so
   Daedalus can resync **all** profiles the instant the model changes, instead
   of lazily at claim time or on the next poll tick. Payload contract:
   `old_model`, `new_model`, `old_provider`, `new_provider`, `source`
   (`cli`/`web`/`config_set`); observer-only, fired after the write is durable —
   mirroring the kanban-task hook contract.

### Can the custom sync code be retired?

Not yet, and not wholesale:

- **Path 1** stays — it is the recommended mechanism.
- **Path 2's model-sync responsibility** becomes retireable **once #1368 lands**
  (an `on_model_change` consumer resyncs all profiles eagerly). Its
  `coding_agent`-change detection is unique and must be rehomed or kept as a
  thin fallback before the poll path can be deleted. Until then it stays as a
  fallback — hence "demote", not "delete".
- **Path 3** stays regardless — force-sync overriding locks has no substitute in
  the hook/poll paths.

## Consequences

**Good:**
- One clear canonical path; the other two have documented, non-overlapping roles.
- Zero behavior change now — this ADR + docstring annotations only; no code path
  is added or removed, so the pipeline is unaffected.
- A concrete, decoupled integration contract (#1368) that lets Daedalus consume
  a real signal without reaching into Hermes internals.

**Trade-off:**
- Until #1368 lands, model sync remains reactive (claim-time) rather than eager
  (change-time); a profile that never runs can hold a stale model until its next
  claim or the next poll tick. This is acceptable — a stale profile only matters
  when it actually runs, and path 1 guarantees freshness at that moment.
- Three paths remain in the tree during the soak; the annotations keep their
  roles unambiguous so they don't silently rot or get duplicated.

## Alternatives Considered

- **Poll `hermes config show` / `config.yaml` on a timer** — path 2 already does
  the file-poll; adding a dedicated timer is redundant and adds a moving part.
- **File-watch `~/.hermes/config.yaml`** — a watcher process is more machinery
  than the JIT hook, and still races the moment of use; the hook already gives
  point-of-use freshness.
- **Delete paths 2 and 3 now** — rejected: path 2 uniquely detects
  `coding_agent` changes and path 3 uniquely overrides locks; deleting either is
  a behavior regression. Retirement is gated on #1368.
- **Reuse `on_session_end`** — fires too late (after a worker ran on the stale
  model) and is per-session, not per-model-change.

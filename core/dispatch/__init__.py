"""core.dispatch — sub-package for dispatcher leaf modules.

Functions extracted from scripts/daedalus_dispatch.py to keep the main
dispatcher file navigable.  Each module holds a cohesive, low-fan-in
cluster of helpers:

  resolvers         — pure config/execution-dict extractors, repo-path resolvers
                      (PR 1/4)
  dedup             — kanban comment-marker deduplication helpers (PR 1/4)
  history           — dispatch history JSONL I/O (PR 1/4)
  delivery          — hermes-send wrappers and notification delivery helpers (PR 1/4)
  bodies            — agent body template engine, delegation building blocks,
                      body-inspection helpers (_DELEGATION_MARKER, _ROLE_TMP_PREFIX,
                      _role_from_card, _inner_task_body, _rewrite_delegation_block)
                      (PR 2/4, PR 4/4)
  validator_comment — GitHub comment scanners for validator/PM outcome fallback (PR 2/4)
  housekeeping      — issue fetch, follow-up extraction, orphan/worktree sweepers (PR 2/4)
  stages            — stage-check auxiliary helpers: consultation markers,
                      downstream probe, planner-fallback key, validator
                      block enforcement (PR 3/4)
  cli_helpers       — CLI-layer utilities: _sweep_exit_code (PR 4/4)
  checks            — stage-check family: _check_confirmed_validators,
                      _check_completed_*, _get_task_summary,
                      _guard_prefix_on_done and supporting helpers
                      (issue #1262 PR 2/2)

  # NOTE: notifications.py not created — those functions call _hermes_send/_notify_targets
  # which are sibling-patched by tests via disp.*; they stay in daedalus_dispatch.py

The dispatcher re-exports every moved symbol so the public surface
(and test monkeypatching against the ``disp`` module) is unchanged.
"""

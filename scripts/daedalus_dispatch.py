#!/usr/bin/env python3
"""Deterministic daedalus dispatch — the cron entrypoint (run with --no-agent).

Each tick, for the project whose workdir matches the cwd:
  1. For every in-scope issue, reconcile its GitHub Project status from PR state
     (open PR -> In review, merged -> Done).
  2. For new issues (no PR, no existing kanban task): set the card to In progress
     and create a Hermes-kanban task carrying the issue + lifecycle instructions.
  3. Dispatch the board so Hermes workers execute the tasks — Hermes tracks their
     status/runs/heartbeat live, so tracking is deterministic, not agent-dependent.

The ONLY agent-driven part is the code each kanban worker writes. All board and
status bookkeeping happens here, in code, so it can never be skipped.

── Architecture (after issue #1153 + #1262 refactor) ────────────────────────

This file is now a THIN ORCHESTRATOR. Stateless helpers have been progressively
extracted into the ``core/dispatch/`` package to make the codebase navigable
without losing the single-module public surface that tests rely on:

  core/dispatch/resolvers.py     — pure config/execution-dict extractors, repo
                                   path resolution
  core/dispatch/dedup.py         — kanban comment-marker deduplication helpers
  core/dispatch/history.py       — dispatch history JSONL I/O
  core/dispatch/delivery.py      — hermes-send wrappers, notification delivery
  core/dispatch/bodies.py        — agent task-body constants, template engine,
                                   delegation building blocks, body-inspection
                                   helpers (_DELEGATION_MARKER, _role_from_card,
                                   _inner_task_body, _rewrite_delegation_block)
  core/dispatch/validator_comment.py — GitHub comment scanners for validator/PM
  core/dispatch/housekeeping.py  — issue fetch, follow-up, orphan/worktree sweep
  core/dispatch/stages.py        — stage-check auxiliaries: consultation markers,
                                   downstream probe, planner-fallback key,
                                   validator block enforcement
  core/dispatch/cli_helpers.py   — CLI-layer utilities (_sweep_exit_code)
  core/dispatch/checks.py        — stage-check family: _check_confirmed_validators,
                                   _check_completed_*, _get_task_summary,
                                   _guard_prefix_on_done and supporting helpers
                                   (issue #1262 PR 2/2)

Every moved symbol is re-exported here (see ``# noqa: F401`` imports below) so
the public surface and all test monkeypatching against this ``disp`` module are
unchanged.

What INTENTIONALLY STAYS here:

  _validate_profiles — internally calls _hermes_profile_exists; tests rebind
  ``disp._hermes_profile_exists`` to stub it.  If moved to resolvers.py, the
  internal call would resolve against resolvers._hermes_profile_exists and the
  test stub would be silently bypassed.

  _build_delegation_instructions, _prepend_delegation — read _CODING_AGENT_MAX_WAIT
  (a mutable module global set at import time); extracting them would sever the
  mutable-global link without adding value.

  _apply_coding_agent_failover, _build_failover_context — call
  _build_delegation_instructions, creating a circular import if moved.

  run(), _run_tick(), main(), _main_inner() — the orchestration core.

  _maybe_redirect_dev_mode — uses ``__file__`` to compare paths; semantics
  change if moved to a sub-module.

  Rerun-contention helpers (_rerun_marker_path, _drain_rerun_requests, …) —
  _drain_rerun_requests calls _main_inner(), so the group cannot move without a
  cycle; splitting the group would fracture cohesion.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import signal
import subprocess  # noqa: F401 — kept for test compatibility: mock.patch.object(disp.subprocess, "run")
import sys
import threading
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional

from filelock import FileLock, Timeout


# Make the plugin's modules importable. This script may run in place (plugin/
# scripts/) OR be COPIED into ~/.hermes/scripts/ (Hermes --script rejects symlinks
# that escape that dir), so locate the plugin root robustly by looking for core/.
def _find_plugin_root() -> Path:
    for c in (
        Path(__file__).resolve().parent.parent,
        Path.home() / ".hermes" / "plugins" / "daedalus",
    ):
        if (c / "core").is_dir():
            return c
    return Path(__file__).resolve().parent.parent


_PLUGIN_ROOT = _find_plugin_root()
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from config import ConfigLoader  # noqa: E402
from core import crash_retry  # noqa: E402
from core import goal_mode  # noqa: E402
from core import native_bounds  # noqa: E402
from core import dispatch_state  # noqa: E402
from core import provider_failover  # noqa: E402
from core import iterate  # noqa: E402
from core import providers  # noqa: E402
from core import kanban  # noqa: E402
from core.dispatch.direct_dispatch import direct_dispatch as _direct_dispatch  # noqa: E402
from core import registry  # noqa: E402
from core import source_specs  # noqa: E402
from core import sweeper  # noqa: E402
from core import notify_templates  # noqa: E402
from core import thread_delivery  # noqa: E402
from core.notification_sender import (  # noqa: E402
    NotificationPayload,
    send as send_webhook_notification,
)
from core.providers.base import (  # noqa: E402
    _DECOMP_LANGUAGE_RE,
    _SUB_ISSUE_CHECKLIST_RE,
    ensure_closing_keyword,
)
from core import tier_promotion  # noqa: E402
from core.util import board_slug as _board_slug  # noqa: E402
from core.util import extract_issue_number  # noqa: E402
from core.util import extract_pr_number_from_summary  # noqa: E402, F401

# ── Leaf-module re-exports (moved in issue #1153 PR 1/4) ─────────────────────
# All symbols are imported into this module's namespace so existing tests that
# monkeypatch ``disp.<symbol>`` continue to work without modification.
from core.dispatch.resolvers import (  # noqa: F401, E402
    NOTIFY_EVENTS,
    _CLAUDE_MODEL_PREFIXES,
    _CODING_AGENT_DEFAULTS,
    _DEFAULT_CODING_AGENT_MAX_TURNS,
    _DEFAULT_CODING_AGENT_MAX_WAIT,
    _DEFAULT_PROFILES,
    _DEFERRED_LINE_RE,
    _FOLLOW_UP_LINE_PATTERNS,
    _FOLLOW_UP_SECTION_RE,
    _HISTORY_MAX_LINES,
    _SUB_ISSUE_NUM_RE,
    _THRESHOLD_DEFAULTS,
    _apply_coding_agent_max_turns,
    _delimit_issue_content,
    _extract_sub_issue_numbers,
    _get_target_broadcast,
    _hermes_profile_exists,
    _inject_model_into_coding_agent_cmd,
    _is_epic,
    _is_model_compatible_with_coding_agent,
    _log_resync,
    _normalize_model_name,
    _notify_targets,
    _parse_follow_ups,
    _preflight_local_model_capability,
    _resolve_active_model_provider,
    _resolve_agent_for_role,
    _resolve_checklist_threshold,
    _resolve_coding_agent,
    _resolve_coding_agent_cmd,
    _resolve_coding_agent_max_turns,
    _resolve_coding_agent_max_wait,
    _resolve_epic_config,
    _resolve_follow_up_scan_limit,
    _resolve_github_api_issue_limit,
    _resolve_github_api_pr_limit,
    _resolve_github_issue_limit,
    _resolve_history_max_lines,
    _resolve_max_developer_retries,
    _resolve_max_dispatch,
    _resolve_max_fix_attempts,
    _resolve_max_planner_retries,
    _resolve_max_pm_retries,
    _resolve_max_validator_retries,
    _resolve_profiles,
    _resolve_repo_arg,
    _resolve_repo_from_cwd,
    _resolve_role_skills,
    _resolve_stall_minutes,
    _resolve_thresholds,
    _resync_profiles_to_model,
    _unpack_issue,
)
from core.dispatch.dedup import (  # noqa: F401, E402
    _ESCALATION_MARKER,
    _RETRY_CAP_MARKER,
    _RETRY_CAP_NOTIFICATION_MARKER,
    _has_notified_block,
    _mark_notified_block,
    _retry_cap_marker_for_role,
)
from core.dispatch.history import (  # noqa: F401, E402
    _HISTORY_COLUMNS,
    _append_history,
    _format_history,
    _history_cell,
    _history_path,
    _read_history,
)
from core.dispatch.delivery import (  # noqa: F401, E402
    _PROJECT_SUMMARY_ANCHOR,
    _build_security_notify_cmds,
    _find_doc_comment,
    _format_completion_comment,
    _hermes_send,
    _human_summary,
    _parse_pr_from_card,
    _resolve_pr_from_parents,
    _send_via_hermes,
    _summary_events,
    _validator_summary_burns_cap,
)
from core.dispatch.bodies import (  # noqa: F401, E402
    _AGENT_BODY_CACHE,
    _AGENT_BODY_TEMPLATE_DIR,
    _AGENT_COMMENT_NOTE,
    _AGENT_FAILED_NOTE,
    _CLOSE_ISSUE_HOWTO,
    _CLOUD_AGENT_LABELS,
    _INNER_BODY_SEPARATOR,
    _PR_COMMENT_HOWTO,
    _PR_CREATE_HOWTO,
    _ROLE_AFTER_SPAWN,
    _load_agent_body_template,
    _pm_consultation_body,
    _render_agent_body,
    _resolve_howtos,
    _spawn_step3,
    _wait_for_agent_cmd,
)
from core.dispatch.validator_comment import (  # noqa: F401, E402
    _pm_spec_comment,
    _validator_github_comment_outcome,
)
from core.dispatch.housekeeping import (  # noqa: F401, E402
    _FOLLOWUP_MARKER,
    _FOLLOWUP_MARKER_RE,
    _GENERIC_TO_ROLE,
    _GET_ISSUE_RETRY_DELAYS,
    _WT_BRANCH_ISSUE_RE,
    _WT_PATH_ISSUE_RE,
    _WT_TERMINAL_STATUSES,
    _check_follow_ups_from_reviewer_prs,
    _check_stalled_in_progress,
    _extract_follow_ups_from_pr_comment,
    _fetch_issue_with_retry,
    _fetch_issues,
    _find_issue_n_from_parents,
    _global_reconcile_orphan_cards,
    _is_issue_closed_cached,
    _maybe_reset_brain_to_primary,
    _remap_generic_role_assignees,
    _sweep_orphan_worktrees,
)
from core.dispatch.stages import (  # noqa: F401, E402
    _BLOCKER_CARD_COMMENT_PREFIX,
    _CONSULT_RESOLVED_MARKER_PREFIX,
    _CONSULT_RESOLVED_MARKER_TMPL,
    _PLANNER_FALLBACK_KEY_RE,
    _PLANNER_FALLBACK_TERMINAL_STATUSES,
    _arbitrate_validator_outcome,
    _compute_planner_fallback_idempotency_key,
    _downstream_tasks_running_or_done,
    _enforce_validator_blocks,
    _is_consult_resolved,
    _stamp_resolved_consultations,
)
from core.dispatch.bodies import (  # noqa: F401, E402
    _DELEGATION_MARKER,
    _ROLE_BODY_MARKER,
    _ROLE_TMP_PREFIX,
    _inner_task_body,
    _rewrite_delegation_block,
    _role_from_card,
)
from core.dispatch.cli_helpers import (  # noqa: F401, E402
    _sweep_exit_code,
)
from core.dispatch.checks import (  # noqa: F401, E402
    _DONE_GUARD_PREFIXES,
    _GUARD_PREFIX_IK_PREFIX,
    _NOT_SUITABLE_RE,
    _check_completed_developer,
    _check_completed_planner,
    _check_completed_pm,
    _check_confirmed_validators,
    _check_planner_not_suitable,
    _developer_task_state,
    _get_task_summary,
    _guard_prefix_on_done,
    _has_active_pm_consultation,
    _has_downstream_tasks,
    _has_pm_tasks,
    _planner_not_suitable_validator_body,
    _pm_task_state,
    _retry_cap_stage_recovered,
    _retry_or_escalate_planner_stall,
    _try_adopt_developer_pr,
    _try_open_missing_developer_pr,
    _try_adopt_pm_spec_comment,
)
# ── End leaf-module re-exports ────────────────────────────────────────────────

logger = logging.getLogger("daedalus.dispatch")

# Host-global process mutex for the dispatcher (issue #1011). Honors the
# ``DAEDALUS_DISPATCH_LOCK`` env override so the test suite can point each
# pytest-xdist worker at a unique lock file — otherwise concurrent workers
# calling main() contend on this one host-global lock and the loser returns
# early without dispatching, flaking any test that asserts on dispatch side
# effects (issue #1198). Unset in production → the stable default below.
_MUTEX_LOCK_PATH = os.environ.get(
    "DAEDALUS_DISPATCH_LOCK",
    str(Path(__file__).resolve().parent / ".daedalus_dispatch.lock"),
)

# Maximum seconds the dispatcher may hold the process-level FileLock before the
# watchdog force-exits. Prevents a stuck tick from starving queued advance-hook
# invocations for hours (issue #1115).
_LOCK_WATCHDOG_SECS = 30 * 60  # 30 minutes

# Rerun-on-contention (issue #1160): a dispatch that loses the FileLock race
# records its intended scope in a marker file next to the lock instead of being
# silently dropped; the lock HOLDER consumes the marker and runs one extra pass
# per recorded scope before releasing.
#
# The holder drains the marker until it is EMPTY (issue #1235) so a burst of
# near-simultaneous completions — e.g. all five gate agents for a PR finishing at
# once, each firing an on_session_end advance that loses the race — is fully
# handed off in this lock cycle instead of stalling until the next */15 cron
# tick. A previous fixed 3-round cap abandoned the rerun queue under sustained
# load, deferring handoffs by up to a full cron interval.
#
# Draining is bounded by a wall-clock budget (a fraction of the #1115 watchdog
# window, so we release gracefully before the watchdog force-exits) and a hard
# round cap as a non-Unix safety net (SIGALRM watchdog is Unix-only). Whichever
# trips first leaves any residual marker for the next tick.
_RERUN_MAX_PASSES = 200
_RERUN_DRAIN_BUDGET_SECS = int(_LOCK_WATCHDOG_SECS * 0.8)
# Marker line meaning "unscoped sweep requested" (a dropped invocation whose
# cwd matched no registered project).
_RERUN_GLOBAL_SCOPE = "*"

# Environment variable that signals we are already running the dev-mode
# redirect copy of the dispatcher (infinite-loop guard for dev re-exec).
_DEV_MODE_ENV = "DAEDALUS_DEV"

_LIFECYCLE = "Triage → Spec → Plan → Build → Test → Review → Code-Simplify → Ship"


# Priority label ordering — P0 dispatched before P1 before P2 before unlabeled.
_PRIORITY = {"p0": 0, "P0": 0, "p1": 1, "P1": 1, "p2": 2, "P2": 2}

# Default forbidden-file patterns (agents may never touch these without human review).
_DEFAULT_FORBIDDEN = [
    ".env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    ".env.*",
    "*.secrets",
    "secrets.*",
]








_CODING_AGENT_MAX_WAIT = _DEFAULT_CODING_AGENT_MAX_WAIT


_TH: Dict[str, float] = _THRESHOLD_DEFAULTS.copy()




# _ROLE_TMP_PREFIX → moved to core/dispatch/bodies.py (issue #1153 PR 4/4)

# Shared instruction injected after every role's wait step. If the guarded wait
# (see ``_wait_for_agent_cmd``) reports the agent died or timed out, the worker
# must move its card OUT of ``running`` (block it) so the dispatcher retries per
# kanban.failure_limit instead of leaving a zombie ``running`` card (issue #141).

# _AGENT_FAILED_NOTE, _wait_for_agent_cmd, _ROLE_AFTER_SPAWN, _CLOUD_AGENT_LABELS,
# _INNER_BODY_SEPARATOR, _spawn_step3
# → moved to core/dispatch/bodies.py (issue #1153 PR 2/4)
def _build_delegation_instructions(
    agent: str,
    cmd: str = "",
    role: str = "developer",
    issue_number: int = 0,
    base_branch: str = "dev",
    body_position: str = "below",
) -> str:
    """Return delegation instruction text to inject into any role's task body.

    ``cmd`` is the full CLI command from coding_agent_cmd.
    ``role`` selects role-specific post-spawn steps (what to do with the output).
    ``issue_number`` scopes the /tmp task/out filenames so concurrent tasks for
    different issues never clobber each other's files (issue #114).
    ``base_branch`` is the branch the developer's isolated worktree forks off.
    ``body_position`` says where the role body sits relative to this block
    ("below" for prepended blocks, "above" for appended ones) so steps 1–2 can
    tell the outer agent to copy ONLY the inner task body — never this block —
    into the inner agent's stdin file (#1241). "below" blocks end with the
    ``_INNER_BODY_SEPARATOR`` line; "above" blocks use the delegation marker
    line itself as the boundary.
    """
    effective_cmd = cmd or _CODING_AGENT_DEFAULTS.get(agent, "")
    pfx = _ROLE_TMP_PREFIX.get(role, role)
    # Only the developer opens a PR, so only it gets provider-side PR detection
    # (issue #146). Other roles run ON an existing PR branch, where detection
    # would false-fire and kill their agent prematurely.
    wait_cmd = _wait_for_agent_cmd(
        pfx, issue_number, _CODING_AGENT_MAX_WAIT, detect_pr=(role == "developer")
    )
    after = _ROLE_AFTER_SPAWN.get(role, _ROLE_AFTER_SPAWN["developer"]).format(
        pfx=pfx,
        issue_number=issue_number,
        wait_cmd=wait_cmd,
        failed_note=_AGENT_FAILED_NOTE,
    )
    label = _CLOUD_AGENT_LABELS.get(agent, agent)

    # Spawn captures the agent PID (for the liveness check) and sends stderr to
    # its own ``-err.txt`` log so a crash reason survives even when nothing is
    # written to stdout/``-out.txt`` (issue #141).
    # Warning prepended to all delegation blocks. The full body (including this block)
    # is piped via stdin to the inner coding agent, so the inner agent reads it too.
    # This prevents inner agents from calling hermes kanban complete/block directly,
    # which would mark the card done with summary:None and trigger infinite retries.
    _inner_agent_prohibition = (
        "⛔ IF YOU ARE THE INNER CODING AGENT (reading this via stdin):\n"
        "   DO NOT call `hermes kanban complete` or `hermes kanban block` — those kanban\n"
        "   write commands are reserved for the OUTER orchestrator agent only.\n"
        "   Your ONLY deliverable is output to stdout. The outer agent reads your stdout\n"
        "   and calls hermes kanban complete on your behalf.\n\n"
    )
    # Steps 1–2 name the boundary of the INNER task body so the delegation
    # wrapper itself never reaches the inner agent's stdin — an inner agent
    # that reads "Spawn Claude Code via terminal" re-delegates and exits with
    # no output (#1241).
    if body_position == "above":
        copy_steps = (
            "  Steps:\n"
            "  1. Copy ONLY the inner task body: the text ABOVE the '⚠️  AGENT DELEGATION' line\n"
            "     (everything from the top of this card down to that line). NEVER copy this\n"
            "     delegation block or these steps — the inner agent re-delegates if it reads them.\n"
            f'  2. write_file("/tmp/{pfx}-{issue_number}-task.txt", "<text above the delegation line ONLY>")\n'
        )
        separator_tail = ""
    else:
        copy_steps = (
            "  Steps:\n"
            "  1. Copy ONLY the inner task body: the text BELOW the '━━━ INNER TASK BODY' separator\n"
            "     line at the end of this block. NEVER copy this delegation block or these steps —\n"
            "     the inner agent re-delegates if it reads them.\n"
            f'  2. write_file("/tmp/{pfx}-{issue_number}-task.txt", "<text below the separator ONLY>")\n'
        )
        separator_tail = _INNER_BODY_SEPARATOR + "\n"
    if agent == "claude-code":
        # Bare fallback keeps the plugin/MCP bypass flags so a headless worker never
        # hangs on plugin/MCP init even when no coding_agent_cmd is configured (daedalus#1323).
        run_cmd = effective_cmd or (
            "claude --dangerously-skip-permissions --strict-mcp-config --setting-sources project -p"
        )
        return (
            f"\n⚠️  AGENT DELEGATION — USE {label.upper()}:\n"
            f"  Do NOT do this work yourself. Spawn {label} via terminal.\n\n"
            + _inner_agent_prohibition
            + copy_steps
            + _spawn_step3(pfx, issue_number, run_cmd, role, base_branch)
            + after
            + separator_tail
        )
    if agent == "codex":
        run_cmd = effective_cmd or "codex exec --full-auto"
        return (
            f"\n⚠️  AGENT DELEGATION — USE {label.upper()}:\n"
            f"  Do NOT do this work yourself. Spawn {label} via terminal.\n\n"
            + _inner_agent_prohibition
            + copy_steps
            + _spawn_step3(pfx, issue_number, run_cmd, role, base_branch)
            + after
            + separator_tail
        )
    if agent == "opencode":
        run_cmd = effective_cmd or "opencode run"
        return (
            f"\n⚠️  AGENT DELEGATION — USE {label.upper()}:\n"
            f"  Do NOT do this work yourself. Spawn {label} via terminal.\n\n"
            + _inner_agent_prohibition
            + copy_steps
            + _spawn_step3(pfx, issue_number, run_cmd, role, base_branch)
            + after
            + separator_tail
        )
    return ""


def _prepend_delegation(
    body: str,
    coding_agent: str,
    coding_agent_cmd: str,
    role: str = "developer",
    issue_number: int = 0,
    *,
    append: bool = False,
    trailing: str = "\n\n",
    base_branch: str = "dev",
) -> str:
    """Inject the agent-delegation block into a role body.

    Guards uniformly with ``not in ("none", "hermes")`` — this fixes the latent
    inconsistency where ``_task_body``/``_downstream_body``/``_validator_body``
    used ``!= "none"`` (which let the no-op "hermes" path append stray output).
    ``_build_delegation_instructions`` returns ``""`` for "hermes" anyway, so the
    only effect is that ``_validator_body`` no longer appends a trailing blank
    line in the "hermes" case.

    By default the block is prepended (``block + trailing + body``); pass
    ``append=True`` to append (``body + block + trailing``). Returns ``body``
    unchanged when no external coding agent is configured.
    """
    if coding_agent in ("none", "hermes"):
        return body
    block = _build_delegation_instructions(
        coding_agent,
        coding_agent_cmd,
        role=role,
        issue_number=issue_number,
        base_branch=base_branch,
        body_position="above" if append else "below",
    )
    if append:
        return body + block + trailing
    return block + trailing + body
























# ── Provider failover (issue #1207) ──────────────────────────────────────────
# Ordered provider chains (primary first) for the two layers Daedalus consumes
# AI through: the external coding agent and the orchestration brain. The
# crash-retry reconciler decides WHEN to fail over (core/crash_retry.py +
# core/provider_failover.py); these helpers supply the chains and HOW a switch
# is applied — rewriting a card's delegation block to the fallback coding
# agent, or resyncing the *-daedalus profiles to the fallback brain provider.

# _DELEGATION_MARKER, _ROLE_BODY_MARKER → moved to core/dispatch/bodies.py (issue #1153 PR 4/4)
# _role_from_card, _inner_task_body, _rewrite_delegation_block → moved to core/dispatch/bodies.py (PR 4/4)


def _apply_coding_agent_failover(
    slug: str,
    card: Dict[str, Any],
    entry: Dict[str, Any],
    execution: Dict[str, Any],
    base_branch: str,
) -> bool:
    """Rewrite *card*'s delegation block to the fallback coding agent (#1207).

    Applies the same cmd transforms as first-time injection (``--max-turns``,
    compatible ``--model``) so the fallback runs with the project's knobs.
    Returns False (logged) on any failure — the reconciler retries next tick.
    """
    tid = str(card.get("id") or card.get("task_id") or "")
    if not tid:
        return False
    agent = str(entry.get("name") or "").strip().lower()
    cmd = str(entry.get("cmd") or "")
    cmd = _apply_coding_agent_max_turns(agent, cmd, execution)
    active = _resolve_active_model_provider()
    if active.get("model"):
        cmd = _inject_model_into_coding_agent_cmd(cmd, agent, active["model"])
    body = kanban.get_body(slug, tid)
    if body is None:
        logger.warning("failover: cannot read body of card %s — skipped", tid)
        return False
    block = ""
    if agent not in ("none", "hermes"):
        # Match the card's existing composition so the rebuilt block's copy
        # steps point at the right boundary (#1241): a block after the role
        # body means the inner body sits ABOVE the delegation marker.
        role_idx = body.find(_ROLE_BODY_MARKER)
        marker_idx = body.find(_DELEGATION_MARKER)
        body_position = (
            "above" if role_idx != -1 and marker_idx > role_idx else "below"
        )
        block = _build_delegation_instructions(
            agent,
            cmd,
            role=_role_from_card(card),
            issue_number=extract_issue_number(card.get("title") or "") or 0,
            base_branch=base_branch,
            body_position=body_position,
        )
    new_body = _rewrite_delegation_block(body, block)
    if new_body is None:
        logger.warning("failover: unrecognized body shape on card %s — skipped", tid)
        return False
    return kanban.edit_body(slug, tid, new_body)


def _build_failover_context(
    slug: str,
    resolved: Dict[str, Any],
    execution: Dict[str, Any],
    workdir: str,
) -> Dict[str, Any]:
    """Build the cross-provider failover context for ``crash_retry.reconcile``.

    Resolves both chains (legacy single-value keys become one-element chains,
    disabling failover structurally) and binds the apply callbacks. Cheap when
    nothing is configured — reconcile treats <2-element chains as no-ops.
    """
    model_cfg = resolved.get("model") or {}
    fcfg = provider_failover.resolve_failover_config(execution, model_cfg)
    coding_chain = provider_failover.resolve_coding_agent_chain(
        execution, _CODING_AGENT_DEFAULTS
    )
    brain_chain = provider_failover.resolve_model_provider_chain(
        model_cfg, _resolve_active_model_provider()
    )
    base_branch = (resolved.get("vcs") or {}).get("target_branch") or "dev"

    brain_names = [e["provider"] for e in brain_chain]
    brain_idx = dispatch_state.get_brain_active_index(workdir)
    if brain_idx >= len(brain_names):
        brain_idx = 0
    brain_current = brain_names[brain_idx] if brain_names else ""

    # The active coding agent is the chain's primary; the legacy single-value
    # key only wins when it names an entry of the chain (it IS the chain in
    # the back-compat one-element case). Identity, not bare name, so a
    # multi-account chain (#1227) tracks the specific account.
    coding_current = provider_failover.entry_identity(coding_chain[0])
    legacy_agent = _resolve_coding_agent(execution)
    for e in coding_chain:
        if e["name"] == legacy_agent:
            coding_current = provider_failover.entry_identity(e)
            break

    def _apply_coding(card: Dict[str, Any], entry: Dict[str, Any]) -> bool:
        return _apply_coding_agent_failover(slug, card, entry, execution, base_branch)

    def _apply_brain(card: Dict[str, Any], entry: Dict[str, Any]) -> bool:
        # old_values must reflect the model the profiles are CURRENTLY synced
        # to (the active chain entry), not the stored global default —
        # otherwise a failed-over profile looks like a manual override and the
        # resync back to primary would skip it.
        prev_idx = dispatch_state.get_brain_active_index(workdir)
        prev_default = (
            brain_chain[prev_idx]["default"] if prev_idx < len(brain_chain) else ""
        ) or (dispatch_state.get_config_values(workdir) or {}).get("model_default", "")
        try:
            idx = brain_chain.index(entry)
        except ValueError:
            idx = 0
        count = _resync_profiles_to_model(
            workdir,
            entry.get("default") or None,
            entry.get("provider") or None,
            {"model_default": prev_default, "coding_agent": ""},
        )
        dispatch_state.set_brain_active_index(workdir, idx)
        logger.info(
            "failover: resynced %d profile(s) to brain provider=%s model=%s",
            count,
            entry.get("provider"),
            entry.get("default"),
        )
        return True

    return {
        "cfg": fcfg,
        "chains": {"coding_agent": coding_chain, "brain": brain_chain},
        "apply": {"coding_agent": _apply_coding, "brain": _apply_brain},
        "current": {
            "coding_agent": coding_current,
            "brain": brain_current,
        },
    }



# _maybe_reset_brain_to_primary → moved to core/dispatch/housekeeping.py (issue #1153 PR 2/4)
def _validate_profiles(
    profiles: Dict[str, str],
    *,
    fallback_behavior: str = "fallback",
) -> Dict[str, str]:
    """Validate that every resolved profile name exists in Hermes.

    For each missing profile, logs a warning naming the role and the missing
    profile so the user knows exactly what to fix.  Behavior depends on
    ``fallback_behavior``:

    * ``"fallback"`` (default) — replace the missing profile with the built-in
      default for that role, so dispatching continues with a known-good assignee.
    * ``"skip"`` — drop the role entirely so no tasks are created for it until
      the profile is configured.

    The check is a plain filesystem lookup — no subprocess calls, no external
    I/O — so it is safe in the hot path but only invoked once per dispatch tick.

    NOTE: stays in daedalus_dispatch.py (not extracted to core/dispatch/resolvers)
    because tests rebind ``disp._hermes_profile_exists`` to stub it; moving this
    function to resolvers.py would sever that rebinding since internal calls would
    resolve against resolvers._hermes_profile_exists instead.
    """
    missing: Dict[str, str] = {}
    for role, name in profiles.items():
        if not _hermes_profile_exists(name):
            missing[role] = name

    if not missing:
        return profiles

    for role, name in missing.items():
        default_name = _DEFAULT_PROFILES.get(role, "?")
        logger.warning(
            "Hermes profile %r for role %r does not exist "
            "(checked ~/.hermes/profiles/%s/ and ~/.hermes/profiles/%s.yaml). "
            "Create it with `hermes profile create %s` or remove the override. "
            "%s",
            name,
            role,
            name,
            name,
            name,
            (
                f"Falling back to default profile {default_name!r}."
                if fallback_behavior != "skip"
                else f"Skipping dispatch for role {role!r} until the profile exists."
            ),
        )

    if fallback_behavior == "skip":
        return {k: v for k, v in profiles.items() if k not in missing}

    return {
        role: (
            profiles[role]
            if role not in missing
            else _DEFAULT_PROFILES.get(role, profiles[role])
        )
        for role in profiles
    }











# _fetch_issues → moved to core/dispatch/housekeeping.py (issue #1153 PR 2/4)

# _AGENT_COMMENT_NOTE, _PR_COMMENT_HOWTO, _CLOSE_ISSUE_HOWTO, _PR_CREATE_HOWTO
# → moved to core/dispatch/bodies.py (issue #1153 PR 2/4)
def _check_epic_qa_ready(
    slug: str,
    issue_number: int,
    issue: Optional[Dict[str, Any]],
    kanban_mod,
    epic_config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Pre-dispatch guard: return True if QA may be dispatched for this issue.

    For non-epic issues, always returns True — the guard is a no-op for
    single-issue flows.

    For epic issues (issues with sub-issue decomposition), returns True only
    when at least one sub-issue developer card has a ``review-required: PR #``
    signal in its latest summary (i.e., at least one sub-issue PR exists).
    Otherwise returns False — the QA card should be skipped this tick.

    The guard never raises — any error in inspecting sub-issue cards returns
    True (fail-open) so a transient kanban error does not permanently block QA.
    """
    if issue is None:
        return True  # can't determine epic status — fail open

    if not _is_epic(issue, epic_config=epic_config):
        return True  # not an epic — always dispatch QA

    sub_issue_numbers = _extract_sub_issue_numbers(issue.get("body") or "")
    if not sub_issue_numbers:
        return True  # epic with no parseable sub-issues — fail open

    # Check if any sub-issue developer card has a review-required: PR # signal.
    try:
        all_tasks = kanban_mod.list_tasks(slug)
    except Exception:
        return True  # fail open on kanban errors

    dev_profile = _DEFAULT_PROFILES["developer"]
    for task in all_tasks:
        title = task.get("title") or ""
        # Look for sub-issue developer cards
        if (task.get("assignee") or "").strip() != dev_profile:
            continue
        # Check if this card's title references any sub-issue number
        task_issue_num = extract_issue_number(title)
        if task_issue_num is None or task_issue_num not in sub_issue_numbers:
            continue
        # Check for review-required: PR # signal in the card summary
        summary = _get_task_summary(task, slug)
        if (
            "review-required:" in (summary or "").lower()
            and "pr #" in (summary or "").lower()
        ):
            return True  # at least one sub-issue has a PR

    return False


def _gate_epic_qa_tasks(
    slug: str,
    issues_map: Dict[int, Dict[str, Any]],
    kanban_mod,
    *,
    epic_config: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
) -> int:
    """Block ready/runnable QA cards for epics with no sub-issue PRs yet.

    Scans the board for QA cards (assignee = qa-daedalus) whose issue is an epic
    and for which no sub-issue developer card has signalled ``review-required:
    PR #``.  Each such card is moved to ``blocked`` with reason
    ``qa-deferred: no sub-issue PRs open yet for epic #{N}`` so the dispatcher
    does not spawn a QA agent prematurely.

    Returns the count of QA cards that were blocked (deferred) this tick.
    On the next cron tick, if a sub-issue PR has appeared, the guard re-checks;
    the card is unblocked and dispatched normally.

    This is idempotent — a card already blocked with ``qa-deferred:`` is left
    as-is (not re-blocked) and will be re-evaluated by
    ``_maybe_undefer_epic_qa_tasks`` on the next tick.
    """
    qa_profile = _DEFAULT_PROFILES["qa"]
    deferred = 0

    try:
        # Check both "ready" and "running" statuses — "running" cards that
        # were spawned before the guard existed, and "ready"/"todo" cards
        # that haven't been dispatched yet.
        tasks = kanban_mod.list_tasks(slug)
    except Exception:
        return 0

    for task in tasks:
        if (task.get("assignee") or "").strip() != qa_profile:
            continue
        status = (task.get("status") or "").lower()
        # Skip already-blocked or done cards (includes qa-deferred cards —
        # those are re-evaluated by _maybe_undefer_epic_qa_tasks)
        if status in ("blocked", "done", "complete", "completed", "archived"):
            continue

        n = extract_issue_number(task.get("title") or "")
        if n is None:
            continue
        issue = issues_map.get(n)
        if issue is None:
            continue  # issue not in current window — can't check, fail open

        if _check_epic_qa_ready(slug, n, issue, kanban_mod, epic_config=epic_config):
            continue  # QA is ready (not an epic, or sub-issue PR exists)

        # Block the QA card — it will be re-evaluated next tick
        reason = f"qa-deferred: no sub-issue PRs open yet for epic #{n}"
        tid = str(task.get("id") or task.get("task_id") or "")
        if not tid:
            continue
        if dry_run:
            logger.info(
                "[dry-run] would block QA card %s for #%s — no sub-issue PRs yet",
                tid,
                n,
            )
        else:
            if kanban_mod.block_task(slug, tid, reason):
                deferred += 1
                logger.debug(
                    "dispatch: deferred epic QA for #%s — no sub-issue PRs open yet",
                    n,
                )

    return deferred


def _maybe_undefer_epic_qa_tasks(
    slug: str,
    issues_map: Dict[int, Dict[str, Any]],
    kanban_mod,
    *,
    epic_config: Optional[Dict[str, Any]] = None,
) -> int:
    """Unblock QA cards previously deferred with ``qa-deferred:`` when ready.

    Scans blocked QA cards for the ``qa-deferred:`` sentinel.  For each, re-
    evaluates ``_check_epic_qa_ready``.  When a sub-issue PR now exists, unblocks
    the card so the dispatcher can spawn it on the next ``kanban.dispatch()``.

    Returns the count of QA cards unblocked this tick.
    """
    qa_profile = _DEFAULT_PROFILES["qa"]
    unblocked = 0

    try:
        blocked_tasks = kanban_mod.list_blocked(slug)
    except Exception:
        return 0

    for task in blocked_tasks:
        if (task.get("assignee") or "").strip() != qa_profile:
            continue
        summary = _get_task_summary(task, slug) or ""
        if "qa-deferred" not in summary.lower():
            continue  # not a deferred QA card

        n = extract_issue_number(task.get("title") or "")
        if n is None:
            continue
        issue = issues_map.get(n)
        if issue is None:
            continue

        if _check_epic_qa_ready(slug, n, issue, kanban_mod, epic_config=epic_config):
            tid = str(task.get("id") or task.get("task_id") or "")
            if tid and kanban_mod.unblock_task(slug, tid):
                unblocked += 1
                logger.info(
                    "dispatch: unblocked deferred epic QA for #%s — sub-issue PR now open",
                    n,
                )

    return unblocked


# ── agent-body template rendering (issue #1147) ─────────────────────────────
#
# Each ``_*_body()`` builder renders its prose from a markdown template in
# ``templates/agent_bodies/<name>.md`` via ``string.Template`` ($placeholder)
# substitution.  Unlike f-strings, ``string.Template`` treats literal ``{`` and
# ``}`` as ordinary characters, so a stray brace in a prompt no longer silently
# breaks rendering.  Templates are cached per-process after the first read.
#
# ``_render_agent_body(name, **vars)`` is the single entry point used by every
# builder.  It raises ``FileNotFoundError`` when the template file is missing
# (never silently yields an empty prompt).


# _AGENT_BODY_TEMPLATE_DIR, _AGENT_BODY_CACHE, _load_agent_body_template, _render_agent_body
# → moved to core/dispatch/bodies.py (issue #1153 PR 2/4)
def _planner_body(
    repo: str,
    issue: Dict[str, Any],
    workdir: str,
    base_branch: str,
    provider_name: str,
    epic_config: Optional[Dict[str, Any]] = None,
) -> str:
    """Task body for the planner role — Phase 3: confirm epic is ready for decomposition.

    When ``epic_config`` is provided, uses its thresholds for detection reasons.
    Otherwise uses legacy hardcoded values (1000 / 5 / 'epic').
    """
    from core.iterate import (
        identify_relevant_files,
        read_source_files,
        build_sub_issue_context,
    )

    n = issue.get("number", "?")
    title = issue.get("title", "")
    body = issue.get("body") or ""
    url = issue.get("url", "")

    # Get thresholds from config or use legacy defaults
    if epic_config:
        size_threshold = int(epic_config.get("size_threshold", 1000))
        min_checklist = int(epic_config.get("min_deliverables", 5))
        epic_label = str(epic_config.get("epic_label", "epic"))
    else:
        size_threshold = 1000
        min_checklist = 5
        epic_label = "epic"

    reasons = []
    # Semantic: decomposition language (e.g. "Phase 1", "decompose into", "split into")
    if _DECOMP_LANGUAGE_RE.search(body):
        reasons.append("semantic: decomposition language")
    # Semantic: sub-issue checklist (items referencing issue numbers like #NNN)
    sub_issue_count = len(_SUB_ISSUE_CHECKLIST_RE.findall(body))
    if sub_issue_count >= 2:
        reasons.append(f"semantic: sub-issue checklist ({sub_issue_count} refs)")
    if len(body) > size_threshold:
        reasons.append(f"body size ({len(body)} chars)")
    checklist_count = len(re.findall(r"^\s*[-*+]\s*\[[ xX]\]", body, re.MULTILINE))
    if checklist_count >= min_checklist:
        reasons.append(f"checklist ({checklist_count} items)")
    for lbl in issue.get("labels") or []:
        name = (
            lbl
            if isinstance(lbl, str)
            else (lbl.get("name", "") if isinstance(lbl, dict) else "")
        )
        if isinstance(name, str) and name.strip().lower() == epic_label:
            reasons.append("epic-label")
            break
    reason_str = ", ".join(reasons) if reasons else "unknown heuristic"

    body_excerpt = body[:size_threshold]
    truncation_note = (
        "\n\n(Body truncated — see full issue for remainder)"
        if len(body) > size_threshold
        else ""
    )

    # Inject source context from relevant files (design spec: issue #386)
    source_context = ""
    if workdir:
        try:
            scope_text = f"{title}\n{body}"
            file_paths, _meta = identify_relevant_files(
                scope_text, workdir, max_files=10
            )
            if file_paths:
                file_contents = read_source_files(file_paths, workdir, max_size=50_000)
                if file_contents:
                    source_context = build_sub_issue_context(file_contents)
                    # Enforce 100KB total context cap (measure in bytes, not chars)
                    encoded = source_context.encode("utf-8")
                    if len(encoded) > 100_000:
                        # Truncate by bytes, decode back to string
                        source_context = encoded[:100_000].decode(
                            "utf-8", errors="ignore"
                        )
        except Exception as exc:  # noqa: BLE001
            logger.warning("_planner_body: source context injection failed: %s", exc)
            source_context = ""

    source_section = f"\n\n{source_context}" if source_context else ""

    return _render_agent_body(
        "planner",
        n=n,
        title=title,
        repo=repo,
        workdir=workdir,
        base_branch=base_branch,
        provider_name=provider_name,
        url=url,
        reason_str=reason_str,
        body_excerpt=body_excerpt,
        truncation_note=truncation_note,
        source_section=source_section,
    )


# _resolve_howtos → moved to core/dispatch/bodies.py (issue #1153 PR 2/4)





# _get_task_summary — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


def _post_completion_comments(
    slug: str,
    provider,
    profiles: Dict[str, str],
    workdir: str,
    *,
    dry_run: bool = False,
) -> List[int]:
    """Mirror each completed pipeline role's kanban summary to its GitHub issue.

    Replaces the old agent-side comment posting (#894): agents no longer
    authenticate to GitHub themselves (``GITHUB_TOKEN`` is not exported into the
    cron worker env, so the old ``urllib`` snippets raised ``KeyError`` and the
    comment was silently dropped). On every tick the dispatcher scans DONE cards
    and, for each pipeline role's card, posts the card's summary to the issue via
    its already-authenticated ``provider`` — exactly once per ``(issue, role)``,
    tracked with a ``dispatch_state`` flag so a comment is never re-posted.

    Already-closed issues are skipped (and flagged) so a backlog of historical
    DONE cards can't spam old/closed issues on first run after deploy.

    Returns the issue numbers a comment was posted to (for the human summary).
    """
    if provider is None:
        return []
    role_by_assignee = {
        (assignee or "").strip(): role
        for role, assignee in (profiles or {}).items()
        if (assignee or "").strip()
    }
    posted: List[int] = []
    for card in kanban.list_tasks(slug, status="done"):
        assignee = (card.get("assignee") or "").strip()
        role = role_by_assignee.get(assignee)
        if not role:
            continue
        n = extract_issue_number(card.get("title") or "")
        if n is None:
            continue
        flag = f"completion_comment_{role}"
        if dispatch_state.has_pr_flag(workdir, n, flag):
            continue
        # Don't spam historical/closed issues. Mark handled so the closed-state
        # lookup isn't repeated for this card on every subsequent tick.
        if (
            hasattr(provider, "get_issue_state")
            and provider.get_issue_state(n) == "closed"
        ):
            dispatch_state.set_pr_flag(workdir, n, flag)
            continue
        body = _format_completion_comment(
            role,
            card.get("title") or "",
            _get_task_summary(card, slug),
        )
        # The documentation report must land on the PR, not the issue (#1325):
        # ``_deliver_doc_reports`` mirrors the ``**Agent: documentation**`` *PR*
        # comment to Slack/Discord, so posting it on the issue would silently break
        # doc delivery. Every other role posts to the issue. If no PR is resolvable
        # yet, fall back to the issue so the report is never dropped (retried next
        # tick once the PR exists).
        target_pr = None
        if role == "documentation":
            target_pr = _parse_pr_from_card(card) or _resolve_pr_from_parents(
                slug, provider, card
            )
        target_num = target_pr if target_pr is not None else n
        target_kind = "PR" if target_pr is not None else "issue"
        if dry_run:
            logger.info(
                "dispatch: [dry-run] would post %s completion comment on %s #%s",
                role,
                target_kind,
                target_num,
            )
            posted.append(n)
            continue
        post_comment = (
            provider.post_pr_comment
            if target_pr is not None
            else provider.post_issue_comment
        )
        try:
            if post_comment(target_num, body):
                dispatch_state.set_pr_flag(workdir, n, flag)
                posted.append(n)
                logger.info(
                    "dispatch: posted %s completion comment on %s #%s",
                    role,
                    target_kind,
                    target_num,
                )
            else:
                logger.warning(
                    "dispatch: post comment on %s #%s (%s) returned falsy — will retry next tick",
                    target_kind,
                    target_num,
                    role,
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "dispatch: post comment on %s #%s (%s) raised %s — will retry next tick",
                target_kind,
                target_num,
                role,
                exc,
            )
    return posted


def _task_body(
    repo: str,
    issue: Dict[str, Any],
    iterations: int,
    workdir: str,
    notify_target: str = "",
    base_branch: str = "dev",
    provider_name: str = "github",
    security_notify_targets: Optional[List[str]] = None,
    coding_agent: str = "none",
    coding_agent_cmd: str = "",
) -> str:
    """Triage body for decompose(): describes the FULL lifecycle so the decomposer
    fans it out across the roster (validator → developer → reviewer → security-analyst →
    documentation). Each role's instructions are spelled out so routing is clean."""
    n, title, body, issue_url = _unpack_issue(issue)
    _h = _resolve_howtos(provider_name, repo, n)
    comment_howto = _h["comment"]
    pr_create_howto = _h["pr_create"]
    close_howto_completed = _h["close_completed"]
    close_howto_wontfix = _h["close_wontfix"]
    security_notify_cmds = _build_security_notify_cmds(
        repo, n, title, security_notify_targets or []
    )
    _doc_template = notify_templates.DOC_COMMENT_TEMPLATE.replace(
        "<issue_number>", str(n)
    ).replace("<issue_url>", issue_url)
    _body = _render_agent_body(
        "task_body",
        repo=repo,
        n=n,
        title=title,
        workdir=workdir,
        base_branch=base_branch,
        comment_howto=comment_howto,
        security_notify_cmds=security_notify_cmds,
        close_howto_completed=close_howto_completed,
        close_howto_wontfix=close_howto_wontfix,
        lifecycle=_LIFECYCLE,
        iterations=iterations,
        pr_create_howto=pr_create_howto,
        doc_template=_doc_template,
    ) + _delimit_issue_content(n, body)
    return _prepend_delegation(
        _body,
        coding_agent,
        coding_agent_cmd,
        issue_number=n,
        append=True,
        trailing="",
        base_branch=base_branch,
    )


def _validator_body(
    repo: str,
    issue: Dict[str, Any],
    workdir: str,
    base_branch: str,
    provider_name: str,
    security_notify_targets: Optional[List[str]] = None,
    coding_agent: str = "none",
    coding_agent_cmd: str = "",
) -> str:
    """Phase-1 task body: VALIDATOR only. No other agent sees this task."""
    n, title, body, _ = _unpack_issue(issue)
    _h = _resolve_howtos(provider_name, repo, n)
    comment_howto = _h["comment"]
    close_howto_completed = _h["close_completed"]
    close_howto_wontfix = _h["close_wontfix"]
    security_notify_cmds = _build_security_notify_cmds(
        repo, n, title, security_notify_targets or []
    )
    # When a coding agent (e.g. claude-code) is configured, the task body is piped to
    # an inner subprocess. That inner agent must NOT call hermes kanban complete/block —
    # only the outer validator-daedalus agent calls those after reading inner stdout.
    # (Issue #1121: inner agent called kanban complete with no summary → infinite retry.)
    _is_delegated = coding_agent not in ("none", "hermes")
    if _is_delegated:
        _kanban_constraint = (
            "DO NOT call hermes kanban complete or hermes kanban block — "
            "kanban writes are FORBIDDEN for inner agents. "
            "Your ONLY deliverable is printing the verdict to stdout "
            "(the outer agent reads your stdout and calls kanban complete for you)."
        )
        _progress_note = (
            "📋 POST A GITHUB COMMENT as described below for each verdict outcome. "
            "Then print your verdict to stdout on the LAST LINE of your output "
            "(e.g. 'CONFIRMED: <reason>'). "
            "The outer agent reads your stdout and calls kanban complete for you."
        )
        _action_security = (
            "→ Print to stdout: 'ESCALATE: security threat — <one-line desc>'"
        )
        _action_block_review = (
            "→ Print to stdout: 'BLOCKED: needs human verification — "
            "<one-line description of what is missing>'"
        )
        _action_confirmed = (
            "→ Print to stdout: 'CONFIRMED: <one-line reproduction note>' "
            "(e.g., 'CONFIRMED: reproduced on main at commit abc1234'). "
            "This EXACT prefix is what the outer agent uses as the kanban summary to trigger the PM phase."
        )
        _action_cannot_repro = (
            "→ Print to stdout: 'STOP: cannot reproduce — <one-line description>'"
        )
        _action_already_fixed = (
            "→ Print to stdout: 'STOP: already fixed — <commit/PR reference>'"
        )
        _action_duplicate = "→ Print to stdout: 'STOP: duplicate of #<N>'"
        _action_needs_info = (
            "→ Print to stdout: 'BLOCKED: needs more info — <what is missing>'"
        )
    else:
        _kanban_constraint = (
            "The only kanban write allowed is completing or blocking YOUR OWN card. "
            "Your ONLY deliverable is a classification decision written as your kanban card summary."
        )
        _progress_note = (
            f"📋 PROGRESS COMMENTS ARE AUTOMATIC: Do NOT post GitHub comments yourself. "
            f"When you complete (or block) your kanban card, the dispatcher mirrors your "
            f"completion summary to GitHub issue #{n} automatically. "
            f"Make that summary clear: role (VALIDATOR), findings/decision, and next steps."
        )
        _action_security = "→ Block your card with summary starting 'ESCALATE: security threat — ' + one-line desc."
        _action_block_review = (
            "→ Block your card with summary starting 'BLOCKED: needs human verification — ' "
            "followed by a one-line description of what is missing."
        )
        _action_confirmed = (
            "→ Complete your card with summary starting 'CONFIRMED: ' followed by a 1–2 sentence "
            "reproduction note (e.g., 'CONFIRMED: reproduced on main at commit abc1234, test_login fails'). "
            "The dispatcher detects this EXACT prefix to trigger the PM phase."
        )
        _action_cannot_repro = "→ Complete your card with summary starting 'STOP: cannot reproduce — ' + one-line description."
        _action_already_fixed = (
            "→ Complete your card with summary starting 'STOP: already fixed — '."
        )
        _action_duplicate = (
            "→ Complete your card with summary starting 'STOP: duplicate of #<N>'."
        )
        _action_needs_info = (
            "→ Block your card with summary starting 'BLOCKED: needs more info'."
        )

    _vbody = _render_agent_body(
        "validator",
        repo=repo,
        n=n,
        title=title,
        workdir=workdir,
        base_branch=base_branch,
        kanban_constraint=_kanban_constraint,
        progress_note=_progress_note,
        comment_howto=comment_howto,
        security_notify_cmds=security_notify_cmds,
        action_security=_action_security,
        action_block_review=_action_block_review,
        action_confirmed=_action_confirmed,
        action_cannot_repro=_action_cannot_repro,
        close_howto_wontfix=close_howto_wontfix,
        close_howto_completed=close_howto_completed,
        action_already_fixed=_action_already_fixed,
        action_duplicate=_action_duplicate,
        action_needs_info=_action_needs_info,
    ) + _delimit_issue_content(n, body)
    return _prepend_delegation(
        _vbody,
        coding_agent,
        coding_agent_cmd,
        role="validator",
        issue_number=n,
        append=True,
    )


def _pm_body(
    repo: str,
    issue: Dict[str, Any],
    validator_summary: str,
    workdir: str,
    base_branch: str,
    provider_name: str,
    profiles: Optional[Dict[str, str]] = None,
    coding_agent: str = "none",
    coding_agent_cmd: str = "",
) -> str:
    """Phase-2 task body: PM writes the spec. Dispatcher creates all downstream tasks."""
    n, title, body, _ = _unpack_issue(issue)
    comment_howto = _resolve_howtos(provider_name, repo, n)["comment"]
    _body = _prepend_delegation(
        _render_agent_body(
            "pm",
            repo=repo,
            n=n,
            title=title,
            workdir=workdir,
            base_branch=base_branch,
            validator_summary=validator_summary,
            comment_howto=comment_howto,
        )
        + _delimit_issue_content(n, body),
        coding_agent,
        coding_agent_cmd,
        role="pm",
        issue_number=n,
    )
    return _body


# Upfront-DAG stage key → profile/skills/bounds role name (#1290). The DAG uses
# "docs" as its terminal stage key; the roster profile for it is "documentation".
_DAG_STAGE_ROLE: Dict[str, str] = {
    "validator": "validator",
    "pm": "pm",
    "developer": "developer",
    "qa": "qa",
    "reviewer": "reviewer",
    "security": "security",
    "accessibility": "accessibility",
    "docs": "documentation",
}


def _dag_stage_body(role: str, n: int, title: str, workdir: str) -> str:
    """Concise body for an upfront-DAG stage card (#1290).

    Used for the intermediate stages whose rich, phase-specific bodies depend on
    upstream output that does not exist yet at Ready-time (e.g. the PM spec, the
    developer PR). The card is created dependency-blocked and only dispatches once
    its parents complete; a follow-up will thread the upstream summaries into
    these bodies. Kept deliberately generic so the flag-off path is unaffected —
    this helper is only ever reached when ``pipeline.upfront_dag`` is on.
    """
    return (
        f"Pipeline stage **{role}** for issue #{n} ({title}).\n\n"
        f"This card was created as part of the upfront pipeline DAG (#1290): it "
        f"sat dependency-blocked from Ready-time and auto-promoted once every "
        f"upstream stage completed. Perform your role's work per your SOUL and "
        f"emit the usual completion signal / structured outcome.\n\n"
        f"Workspace dir: {workdir}\n"
    )


def _pipeline_dag_role_specs(
    repo: str,
    issue: Dict[str, Any],
    workdir: str,
    base_branch: str,
    provider_name: str,
    profiles: Dict[str, str],
    execution: Dict[str, Any],
    resolved: Dict[str, Any],
    role_skills: Dict[str, List[str]],
    bounds: Dict[str, Any],
    coding_agent_cmd: str,
    security_notify_targets: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Assemble the per-stage creation specs for :func:`iterate.build_pipeline_dag`.

    Returns ``{stage_key: {assignee, body, workspace, skills, extra}}`` for every
    stage of the upfront DAG. The validator and PM stages reuse their real body
    builders; the remaining stages use a concise generic body (see
    :func:`_dag_stage_body`) because their rich bodies depend on upstream output
    that does not exist at Ready-time. Only reached when the flag is on.
    """
    n = issue["number"]
    title = issue.get("title", "")
    ws = f"dir:{workdir}" if workdir else ""

    def _extra(stage_key: str) -> Dict[str, Any]:
        return dict(native_bounds.bounds_kwargs(bounds, _DAG_STAGE_ROLE[stage_key]))

    def _skills(stage_key: str) -> Optional[List[str]]:
        return role_skills.get(_DAG_STAGE_ROLE[stage_key]) or None

    specs: Dict[str, Dict[str, Any]] = {}
    for stage_key in (
        "validator", "pm", "developer", "qa",
        "reviewer", "security", "accessibility", "docs",
    ):
        role = _DAG_STAGE_ROLE[stage_key]
        if stage_key == "validator":
            body = _validator_body(
                repo, issue, workdir, base_branch, provider_name,
                security_notify_targets,
                coding_agent=_resolve_agent_for_role(execution, "validator"),
                coding_agent_cmd=coding_agent_cmd,
            )
        elif stage_key == "pm":
            body = _pm_body(
                repo, issue, "", workdir, base_branch, provider_name,
                profiles=profiles,
                coding_agent=_resolve_agent_for_role(execution, "pm"),
                coding_agent_cmd=coding_agent_cmd,
            )
        else:
            body = _dag_stage_body(role, n, title, workdir)
        specs[stage_key] = {
            "title": f"#{n} {title}" if stage_key == "validator"
                     else f"#{n} {role.title()} — {title}",
            "assignee": profiles.get(role, ""),
            "body": body,
            "workspace": ws,
            "skills": _skills(stage_key),
            "extra": _extra(stage_key),
        }
    return specs


def _downstream_body(
    repo: str,
    issue: Dict[str, Any],
    iterations: int,
    workdir: str,
    notify_target: str,
    base_branch: str,
    provider_name: str,
    security_notify_targets: Optional[List[str]] = None,
    label_overrides: Optional[Dict[str, Any]] = None,
    profiles: Optional[Dict[str, str]] = None,
    coding_agent: str = "none",
    coding_agent_cmd: str = "",
) -> str:
    """Phase-3 triage body: DEVELOPER → REVIEWER → SECURITY-ANALYST → DOCUMENTATION.

    ``label_overrides`` (from ``execution.label_overrides`` in config) can suppress
    or customise roles per issue label. Example config::

        execution:
          label_overrides:
            documentation: {skip_developer: true}
            security: {skip_developer: false, security_first: true}

    Only created after the validator completes with a 'CONFIRMED:' summary.
    """
    n, title, body, issue_url = _unpack_issue(issue)
    issue_labels = [
        (lbl["name"] if isinstance(lbl, dict) else lbl).lower()
        for lbl in (issue.get("labels") or [])
    ]
    p = profiles or _DEFAULT_PROFILES
    _h = _resolve_howtos(provider_name, repo, n)
    comment_howto = _h["comment"]
    pr_create_howto = _h["pr_create"]

    # Resolve label-driven overrides: merge all matching label configs.
    merged_override: Dict[str, Any] = {}
    for lbl in issue_labels:
        cfg = (label_overrides or {}).get(lbl) or {}
        merged_override.update(cfg)
    skip_developer = merged_override.get("skip_developer", False)
    security_first = merged_override.get("security_first", False)

    # Build role list respecting overrides.
    roles: List[str] = []
    if security_first:
        roles.append(
            f"1. SECURITY-ANALYST — this issue is security-sensitive (label: {issue_labels}). "
            f"Audit the issue and verify it's safe to implement before any code is written. "
            f"Block your card with 'BLOCKED: security risk' if human intervention is required.\n"
        )
    if not skip_developer:
        role_num = len(roles) + 1
        roles.append(
            f"{role_num}. DEVELOPER — implement the fix/feature. Follow the agent-skills lifecycle "
            f"({_LIFECYCLE}). "
            f"⛔ NEVER merge the PR — merging is a human-only action. Do NOT run `gh pr merge`, "
            f"`git merge`, or any merge command. Do NOT invoke the /ship skill. "
            f"Your job ends at opening the PR and blocking your kanban card with 'review-required: PR #N'. "
            f"BRANCH SETUP (mandatory): `git checkout {base_branch} && git pull && "
            f"git checkout -b fix/issue-{n}-<slug>` — always branch off `{base_branch}`, "
            f"never off main or any other branch. "
            f"Write code + tests, iterate up to {iterations}x if review fails. "
            f"Before pushing, run the project's configured lint and format tools "
            f"(use whatever is present, skip gracefully if nothing is configured): "
            f".pre-commit-config.yaml → `pre-commit run --all-files`; "
            f"package.json lint/format scripts → `npm run lint && npm run format`; "
            f"pyproject.toml ruff config → `ruff check --fix && ruff format`; "
            f"Makefile lint target → `make lint`. "
            f"Commit any auto-fixes before pushing. "
            f"Push the branch (git credentials are pre-configured) and open a PR "
            f"into {base_branch} via {pr_create_howto} — no gh/glab/az CLI is installed. "
            f"CRITICAL: The PR body MUST include `Closes #{n}` (or `Fixes #{n}`) on its own line. "
            f"(REQUIRED: GitHub only auto-closes issues on default-branch merges. Since this PR "
            f"targets '{base_branch}', the Daedalus dispatcher relies on this exact keyword to "
            f"automatically close the issue and mark the Kanban task Done upon merge.) Also include "
            f"sections for: Problem, Fix, How to test, and Manual testing.\n"
        )
    roles.append(
        f"{len(roles) + 1}. REVIEWER — review the developer's PR for correctness, quality, and performance; "
        f"request changes or approve.\n"
    )
    if not security_first:
        roles.append(
            f"{len(roles) + 1}. SECURITY-ANALYST — audit the PR diff for vulnerabilities (authz, secrets, injection, "
            f"input validation); flag findings or sign off.\n"
        )
    roles_text = "".join(roles)

    doc_num = len(roles) + 1
    doc_role = (
        f"{doc_num}. DOCUMENTATION — after the PR is open and reviewed, write a detailed completion report "
        f"and post it as a comment on the PR ({comment_howto}). "
        f"Use the PR number from the chain above (developer/reviewer cards carry it). "
        f"The comment MUST follow this exact structure:\n\n"
        f"```\n{notify_templates.DOC_COMMENT_TEMPLATE.replace('<issue_number>', str(n)).replace('<issue_url>', issue_url)}\n```\n\n"
        f"Replace every <placeholder> with the real value. "
        f"NOTE: messaging-platform delivery is handled automatically by the dispatcher — do NOT "
        f"attempt to send the report yourself.\n"
    )
    _body = (
        _render_agent_body(
            "downstream",
            repo=repo,
            n=n,
            title=title,
            workdir=workdir,
            base_branch=base_branch,
            dev_profile=p.get("developer", _DEFAULT_PROFILES["developer"]),
            qa_profile=p.get("qa", _DEFAULT_PROFILES["qa"]),
            reviewer_profile=p.get("reviewer", _DEFAULT_PROFILES["reviewer"]),
            security_profile=p.get("security", _DEFAULT_PROFILES["security"]),
            docs_profile=p.get("documentation", _DEFAULT_PROFILES["documentation"]),
            roles_text=roles_text,
            doc_role=doc_role,
        )
        + "\n"
        + _delimit_issue_content(n, body)
    )
    return _prepend_delegation(
        _body,
        coding_agent,
        coding_agent_cmd,
        issue_number=n,
        append=True,
        trailing="",
        base_branch=base_branch,
    )


def _dev_task_body(
    repo: str,
    issue: Dict[str, Any],
    iterations: int,
    workdir: str,
    base_branch: str,
    provider_name: str,
    coding_agent: str = "none",
    coding_agent_cmd: str = "",
    profiles: Optional[Dict[str, str]] = None,
    label_overrides: Optional[Dict[str, Any]] = None,
) -> str:
    """Developer task body. Delegation block always comes first when coding_agent is set."""
    n, title, body, _ = _unpack_issue(issue)
    _h = _resolve_howtos(provider_name, repo, n)
    pr_create_howto = _h["pr_create"]
    _body = _prepend_delegation(
        _render_agent_body(
            "dev",
            repo=repo,
            n=n,
            title=title,
            workdir=workdir,
            base_branch=base_branch,
            iterations=iterations,
            pr_create_howto=pr_create_howto,
        )
        + _delimit_issue_content(n, body),
        coding_agent,
        coding_agent_cmd,
        issue_number=n,
        base_branch=base_branch,
    )
    return _body


def _qa_task_body(
    repo: str,
    issue: Dict[str, Any],
    workdir: str,
    provider_name: str,
    profiles: Optional[Dict[str, str]] = None,
    coding_agent: str = "none",
    coding_agent_cmd: str = "",
) -> str:
    n, title, _, _ = _unpack_issue(issue)
    comment_howto = _resolve_howtos(provider_name, repo, n)["comment"]
    _body = _prepend_delegation(
        _render_agent_body(
            "qa",
            repo=repo,
            n=n,
            title=title,
            workdir=workdir,
            comment_howto=comment_howto,
        ),
        coding_agent,
        coding_agent_cmd,
        role="qa",
        issue_number=n,
    )
    return _body


def _reviewer_task_body(
    repo: str,
    issue: Dict[str, Any],
    workdir: str,
    provider_name: str,
    profiles: Optional[Dict[str, str]] = None,
    coding_agent: str = "none",
    coding_agent_cmd: str = "",
) -> str:
    n, title, _, _ = _unpack_issue(issue)
    comment_howto = _resolve_howtos(provider_name, repo, n)["comment"]
    _body = _prepend_delegation(
        _render_agent_body(
            "reviewer",
            repo=repo,
            n=n,
            title=title,
            workdir=workdir,
            comment_howto=comment_howto,
        ),
        coding_agent,
        coding_agent_cmd,
        role="reviewer",
        issue_number=n,
    )
    return _body


def _security_task_body(
    repo: str,
    issue: Dict[str, Any],
    workdir: str,
    provider_name: str,
    profiles: Optional[Dict[str, str]] = None,
    coding_agent: str = "none",
    coding_agent_cmd: str = "",
) -> str:
    n, title, _, _ = _unpack_issue(issue)
    comment_howto = _resolve_howtos(provider_name, repo, n)["comment"]
    _body = _prepend_delegation(
        _render_agent_body(
            "security",
            repo=repo,
            n=n,
            title=title,
            workdir=workdir,
            comment_howto=comment_howto,
        ),
        coding_agent,
        coding_agent_cmd,
        role="security",
        issue_number=n,
    )
    return _body


def _docs_task_body(
    repo: str,
    issue: Dict[str, Any],
    workdir: str,
    provider_name: str,
    notify_target: str,
    profiles: Optional[Dict[str, str]] = None,
    coding_agent: str = "none",
    coding_agent_cmd: str = "",
) -> str:
    n, title, _, issue_url = _unpack_issue(issue)
    comment_howto = _resolve_howtos(provider_name, repo, n)["comment"]
    _doc_template = notify_templates.DOC_COMMENT_TEMPLATE.replace(
        "<issue_number>", str(n)
    ).replace("<issue_url>", issue_url)
    _body = _prepend_delegation(
        _render_agent_body(
            "docs",
            repo=repo,
            n=n,
            title=title,
            workdir=workdir,
            comment_howto=comment_howto,
            doc_template=_doc_template,
        ),
        coding_agent,
        coding_agent_cmd,
        role="documentation",
        issue_number=n,
    )
    return _body








# _CONSULT_RESOLVED_MARKER_TMPL, _CONSULT_RESOLVED_MARKER_PREFIX,
# _BLOCKER_CARD_COMMENT_PREFIX — moved to core/dispatch/stages.py (PR 3/4)



# _validator_github_comment_outcome, _pm_spec_comment
# → moved to core/dispatch/validator_comment.py (issue #1153 PR 2/4)

# _try_adopt_pm_spec_comment — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


# _try_adopt_developer_pr — moved to core/dispatch/checks.py (issue #1262 PR 2/2)






# _downstream_tasks_running_or_done — moved to core/dispatch/stages.py (PR 3/4)


# _retry_cap_stage_recovered — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


# _has_downstream_tasks — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


# _pm_task_state — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


# _has_pm_tasks — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


# _developer_task_state — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


# _has_active_pm_consultation — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


# _is_consult_resolved, _stamp_resolved_consultations — moved to core/dispatch/stages.py (PR 3/4)


# ── follow-up extraction ─────────────────────────────────────────────────────




# Marker embedded in the summary comment for idempotency.

# _FOLLOWUP_MARKER, _FOLLOWUP_MARKER_RE, _extract_follow_ups_from_pr_comment,
# _check_follow_ups_from_reviewer_prs
# → moved to core/dispatch/housekeeping.py (issue #1153 PR 2/4)
# agent-header used in follow-up comment: "**Agent: dispatcher**\n\n"

# _GENERIC_TO_ROLE, _remap_generic_role_assignees, _find_issue_n_from_parents,
# _global_reconcile_orphan_cards
# → moved to core/dispatch/housekeeping.py (issue #1153 PR 2/4)


def _count_active_issue_tasks(slug: str, issue_number: int) -> int:
    """Count ACTIVE tasks for issue #N.

    A task "belongs" to an issue when its title references ``#<issue_number>``.
    Used to guard orphaned-issue cleanup: an issue closed on VCS while active
    kanban tasks remain is likely an accidental close (bot mis-fire, manual
    mis-click mid-pipeline), so the dispatcher must NOT bulk-complete its tasks.

    The tasks are filtered to exclude terminal states (done, complete, completed,
    cancelled, canceled, archived), consistent with the status-blind principle from
    epic #1008.

    NOTE: stays in dispatcher (not housekeeping.py) because tests replace
    ``disp.kanban = fk`` and this function reads ``kanban.list_tasks``; moving
    it would break the fake-kanban injection in test_issue_1008 and
    test_concurrent_dispatch (issue #1153 PR 2/4).
    """
    terminal_statuses = {
        "done",
        "complete",
        "completed",
        "cancelled",
        "canceled",
        "archived",
    }
    count = 0
    for t in kanban.list_tasks(slug):
        num = extract_issue_number(t.get("title") or "")
        if num != issue_number:
            continue
        status = (t.get("status") or "").strip().lower()
        if status not in terminal_statuses:
            count += 1
    return count

def _repair_orphan_tasks(
    slug: str,
    profiles: Dict[str, str],
    *,
    dry_run: bool = False,
) -> int:
    """Auto-repair orphan kanban tasks caused by PM creation bugs.

    Fixes two classes of orphan on todo/ready tasks:
      1. Generic role assignees (developer/qa/reviewer/etc.) → Daedalus profiles
      2. Child task titles missing the parent issue #N → auto-prefix from body/parents

    Both repairs are idempotent (re-running on already-fixed tasks is a no-op) and
    logged at INFO so the operator sees exactly what was auto-fixed.
    Returns the count of individual repairs applied.
    """
    profile_values = set(profiles.values())
    repaired = 0

    for task in kanban.list_tasks(slug):
        if (task.get("status") or "").lower() not in ("todo", "ready"):
            continue
        task_id = (task.get("id") or task.get("task_id") or "").strip()
        if not task_id:
            continue
        title = (task.get("title") or "").strip()
        assignee = (task.get("assignee") or "").strip()

        # ── Bug 1: generic assignee → Daedalus profile ────────────────────
        if assignee and assignee not in profile_values:
            role = _GENERIC_TO_ROLE.get(assignee)
            if role:
                new_assignee = profiles.get(role)
                if new_assignee:
                    if dry_run:
                        logger.info(
                            "[dry-run] repair: would reassign %s: %s → %s (title=%r)",
                            task_id,
                            assignee,
                            new_assignee,
                            title[:60],
                        )
                        repaired += 1
                    elif kanban.reassign_task(slug, task_id, new_assignee):
                        logger.info(
                            "dispatch: repair: reassigned %s: %s → %s title=%r",
                            task_id,
                            assignee,
                            new_assignee,
                            title[:60],
                        )
                        repaired += 1
            else:
                logger.debug(
                    "dispatch: repair: unknown assignee %r on %s — skipping",
                    assignee,
                    task_id,
                )

        # ── Bug 2: missing issue-number prefix in title ────────────────────
        if extract_issue_number(title) is not None:
            continue  # already has issue number, nothing to do

        # Body not included in list_tasks output — fetch via show_card.
        issue_n: Optional[str] = None
        card = kanban.show_card(slug, task_id) or {}
        body_text = (card.get("body") or "").strip()
        _num = extract_issue_number(body_text)
        if _num is not None:
            issue_n = str(_num)

        if not issue_n:
            issue_n = _find_issue_n_from_parents(slug, task_id)

        if issue_n:
            new_title = f"#{issue_n} {title}"
            if dry_run:
                logger.info(
                    "[dry-run] repair: would prefix title on %s: %r → %r",
                    task_id,
                    title[:60],
                    new_title[:80],
                )
                repaired += 1
            elif kanban.rename_task(slug, task_id, new_title):
                logger.info(
                    "dispatch: repair: prefixed title on %s: %r → %r",
                    task_id,
                    title[:60],
                    new_title[:80],
                )
                repaired += 1

    if repaired > 0:
        logger.info("dispatch: repaired %d orphan task(s) on board %s", repaired, slug)
    return repaired


# _WT_BRANCH_ISSUE_RE, _WT_PATH_ISSUE_RE, _WT_TERMINAL_STATUSES, _sweep_orphan_worktrees
# → moved to core/dispatch/housekeeping.py (issue #1153 PR 2/4)




def _native_send_enabled(resolved: Dict[str, Any]) -> bool:
    """Whether native ``hermes send`` fully replaces the legacy webhook path (#1293).

    When ``notify.native_send`` is true, the redundant raw-webhook calls
    (``send_webhook_notification`` → ``SLACK_WEBHOOK_URL`` / ``DISCORD_WEBHOOK_URL``,
    Block Kit / embeds) are skipped because the native ``_hermes_send`` path already
    delivers the same notifications through ``hermes send``. Defaults to ``False``
    (byte-identical legacy behaviour: both transports fire). Never raises.

    Tradeoff when enabled: ``hermes send`` delivers plain markdown only (no Block
    Kit / Discord embeds), and delivery follows ``cron.notifications`` targets
    rather than the ``SLACK_WEBHOOK_URL`` / ``DISCORD_WEBHOOK_URL`` env webhooks.
    """
    try:
        notify = resolved.get("notify") or {}
        return bool(notify.get("native_send", False))
    except Exception:
        return False


def _send_retry_cap_notification(
    *,
    role: str,
    issue_number: int,
    retry_count: int,
    max_retries: int,
    resolved: Dict[str, Any],
    dry_run: bool,
) -> None:
    """Send notification when PM/validator retry cap is exhausted.

    Routes through ``hermes send`` to every target subscribed to the
    ``retry-cap-exhausted`` event (or catch-all targets with no ``events``
    filter). When no targets are configured, returns silently. Failures are
    logged but not raised.
    """
    # Fire webhook notification asynchronously (non-blocking) — independent of
    # whether ``hermes send`` targets are configured. Skipped when
    # ``notify.native_send`` is on: the native ``_hermes_send`` path below already
    # delivers this notification, so the legacy webhook is redundant (#1293).
    if not _native_send_enabled(resolved):
        _fire_webhook_notification(
            role=role,
            issue_number=issue_number,
            retry_count=retry_count,
            max_retries=max_retries,
            dry_run=dry_run,
        )

    targets = _notify_targets(resolved, "retry-cap-exhausted")
    if not targets:
        return

    body = (
        f"⚠️ **Retry Cap Exhausted: {role.upper()}**\n\n"
        f"Issue #{issue_number} has failed {retry_count} times (max: {max_retries}).\n\n"
        f"**Role**: {role}\n"
        f"**Retry count**: {retry_count}/{max_retries}\n"
        f"**Status**: Manual intervention required\n\n"
    )
    if role == "pm":
        body += (
            "**Likely cause**: PM agent completed without `SPEC:` summary.\n"
            "**Recovery**: `hermes kanban edit <task-id>` and add `SPEC:` "
            "summary, or manually requeue with fresh context."
        )
    elif role == "developer":
        body += (
            "**Likely cause**: Developer agent completed without opening a PR "
            "(context window overflow, agent crash, or silent failure).\n"
            "**Recovery**: Check agent logs, verify issue context, then manually "
            "requeue developer or escalate to human review."
        )
    else:  # validator
        body += (
            "**Likely cause**: Validator agent completed without `CONFIRMED` "
            "(context window overflow, agent crash, or silent failure).\n"
            "**Recovery**: Check agent logs, verify issue context, then manually "
            "requeue validator or escalate to human review."
        )

    for target in targets:
        if dry_run:
            logger.info(
                "[dry-run] would send retry-cap notification to %s for #%s",
                target,
                issue_number,
            )
            continue
        ok, _anchor = _hermes_send(target, body)
        if ok:
            logger.info(
                "sent retry-cap notification to %s for #%s (role=%s)",
                target,
                issue_number,
                role,
            )
        else:
            logger.warning(
                "failed to send retry-cap notification to %s for #%s",
                target,
                issue_number,
            )


def _fire_webhook_notification(
    *,
    role: str,
    issue_number: int,
    retry_count: int,
    max_retries: int,
    dry_run: bool,
) -> None:
    """Fire webhook notification in background thread (non-blocking).

    Constructs a `NotificationPayload` with retry-cap context and dispatches
    it via `send_webhook_notification` in a daemon thread so the caller does
    not block on HTTP latency or webhook failures.
    """
    if dry_run:
        return

    def _fire():
        try:
            # Match _send_retry_cap_notification format (issue #283)
            if role == "validator":
                diagnosis = "validator completed without CONFIRMED summary"
                recovery = "check agent logs, verify issue context, then manually requeue validator or escalate to human review"
            elif role == "developer":
                diagnosis = "developer completed without opening a PR"
                recovery = "check agent logs, verify issue context, then manually requeue developer or escalate to human review"
            else:  # pm
                diagnosis = "PM completed without SPEC: summary"
                recovery = "manually requeue with fresh context or add SPEC: summary via comment"

            body = (
                f"Issue #{issue_number} has failed {retry_count}/{max_retries} retries.\n"
                f"Manual intervention required.\n\n"
                f"Likely cause: {diagnosis}\n"
                f"Recovery: {recovery}"
            )

            payload = NotificationPayload(
                title=f"Retry Cap Exhausted: {role.upper()}",
                body=body,
                severity="critical",
                context={
                    "issue": f"#{issue_number}",
                    "role": role,
                    "retry_count": f"{retry_count}/{max_retries}",
                    "max_retries": str(max_retries),
                    "recovery": recovery,
                },
            )
            send_webhook_notification(payload)
        except Exception as exc:
            logger.warning(
                "webhook notification failed for #%s (%s): %s", issue_number, role, exc
            )

    thread = threading.Thread(target=_fire, daemon=True)
    thread.start()


def _send_crash_retries_exhausted_notification(
    *,
    action: Dict[str, Any],
    resolved: Dict[str, Any],
    dry_run: bool,
) -> None:
    """Notify humans that a card's crash retries are exhausted (#1205).

    ``action`` is the ``escalated`` dict returned by ``crash_retry.reconcile``
    (already deduped there — this fires at most once per episode). Routes to
    targets subscribed to ``crash-retries-exhausted``, falling back to
    ``retry-cap-exhausted`` / catch-all targets so existing configs get the
    escalation without edits. Also fires the webhook sender in a background
    thread. Failures are logged, never raised.
    """
    issue_n = action.get("issue")
    task_id = action.get("task_id") or "?"
    attempts = action.get("attempt")
    max_attempts = action.get("max_attempts")
    elapsed = action.get("elapsed_minutes")
    last_error = (action.get("summary") or "no failure details")[:300]
    provider_history = (action.get("provider_history") or "").strip()
    body = (
        "⚠️ **Crash Retries Exhausted**\n\n"
        f"Issue #{issue_n} (card `{task_id}`, {action.get('assignee') or 'unknown'}) "
        f"crashed through {attempts}/{max_attempts} automatic re-dispatches "
        f"over {elapsed} min.\n\n"
        f"**Last failure**: {last_error}\n"
        + (
            f"**Per-provider history** (#1207):\n{provider_history}\n"
            if provider_history
            else ""
        )
        + "**Status**: hard-blocked (`crash-retries-exhausted`) — manual "
        "intervention required\n"
        f"**Recovery**: fix the underlying cause, then `hermes kanban unblock "
        f"{task_id}` (resets the crash-retry counter)."
    )

    # Legacy raw-webhook path — skipped when ``notify.native_send`` is on: the
    # native ``_hermes_send`` fan-out below already delivers this notification to
    # ``crash-retries-exhausted`` (fallback ``retry-cap-exhausted``) targets (#1293).
    if not dry_run and not _native_send_enabled(resolved):

        def _fire():
            try:
                send_webhook_notification(
                    NotificationPayload(
                        title="Crash Retries Exhausted",
                        body=body,
                        severity="critical",
                        context={
                            "issue": f"#{issue_n}",
                            "task_id": str(task_id),
                            "attempts": f"{attempts}/{max_attempts}",
                            "elapsed_minutes": str(elapsed),
                            "last_error": last_error,
                            **(
                                {"provider_history": provider_history}
                                if provider_history
                                else {}
                            ),
                        },
                    )
                )
            except Exception as exc:
                logger.warning(
                    "crash-retry webhook notification failed for #%s: %s",
                    issue_n,
                    exc,
                )

        threading.Thread(target=_fire, daemon=True).start()

    targets = _notify_targets(resolved, "crash-retries-exhausted") or _notify_targets(
        resolved, "retry-cap-exhausted"
    )
    for target in targets:
        if dry_run:
            logger.info(
                "[dry-run] would send crash-retries-exhausted notification "
                "to %s for #%s",
                target,
                issue_n,
            )
            continue
        ok, _anchor = _hermes_send(target, body)
        if ok:
            logger.info(
                "sent crash-retries-exhausted notification to %s for #%s (card %s)",
                target,
                issue_n,
                task_id,
            )
        else:
            logger.warning(
                "failed to send crash-retries-exhausted notification to %s for #%s",
                target,
                issue_n,
            )


def _send_retry_attempt_notification(
    *,
    role: str,
    issue_number: int,
    retry_count: int,
    max_retries: int,
    resolved: Dict[str, Any],
    dry_run: bool,
) -> None:
    """Send notification when a retry attempt is being made (before cap exhaustion).

    Distinct from _send_retry_cap_notification (which fires at cap): different title,
    different event type ('retry-attempt'), different status message.  Routes through
    ``hermes send`` to every target subscribed to the ``retry-attempt`` event.
    Failures are logged but not raised.
    """
    targets = _notify_targets(resolved, "retry-attempt")
    if not targets:
        return

    body = (
        f"\U0001f504 **Retry Attempt: {role.upper()}**\n\n"
        f"Issue #{issue_number} has been retried (run {retry_count} of {max_retries}).\n\n"
        f"**Role**: {role}\n"
        f"**Retry count**: {retry_count}/{max_retries}\n"
        f"**Status**: Retry queued — dispatcher will spawn another attempt\n\n"
    )
    if role == "pm":
        body += (
            "**Context**: PM agent completed without `SPEC:` summary. "
            "Retrying with a fresh PM task to generate proper specification."
        )
    elif role == "developer":
        body += (
            "**Context**: Developer agent completed without opening a PR "
            "(context window overflow, agent timeout, or silent failure). "
            "Retrying with a fresh developer task to produce a PR."
        )
    else:  # validator
        body += (
            "**Context**: Validator agent completed without `CONFIRMED` "
            "summary (context window overflow, agent timeout, or silent failure). "
            "Retrying with a fresh validator task to obtain confirmation."
        )

    for target in targets:
        if dry_run:
            logger.info(
                "[dry-run] would send retry-attempt notification to %s for #%s",
                target,
                issue_number,
            )
            continue
        ok, _anchor = _hermes_send(target, body)
        if ok:
            logger.info(
                "sent retry-attempt notification to %s for #%s (role=%s)",
                target,
                issue_number,
                role,
            )
        else:
            logger.warning(
                "failed to send retry-attempt notification to %s for #%s",
                target,
                issue_number,
            )


def _notify_validator_blocked(
    issue_number: int,
    issue_title: str,
    blocker_text: str,
    block_number: int,
    resolved: Dict[str, Any],
    *,
    dry_run: bool = False,
) -> None:
    """Notify human channels that a validator blocked an issue (#994).

    Fires on every validator block — including repeat blocks after a PM
    resolution — so a stalled issue surfaces on Slack/Discord instead of
    sitting silently on the kanban board. Routes through ``hermes send`` to
    every ``validator-blocked`` subscriber (mirrors ``_notify_retry_cap_exhausted``).
    When no targets are configured, returns silently. Failures are logged,
    never raised.
    """
    targets = _notify_targets(resolved, "validator-blocked")
    if not targets:
        return

    ordinal = (
        "first"
        if block_number == 1
        else "second"
        if block_number == 2
        else f"#{block_number}"
    )
    body = (
        f"🚧 **Validator Blocked: #{issue_number}**\n\n"
        f"Issue #{issue_number} ({issue_title}) was blocked by the validator "
        f"for the {ordinal} time.\n\n"
        f"**Blocker**: {blocker_text}\n\n"
        f"A PM consultation has been created to unblock it. If this keeps "
        f"recurring, the issue likely needs human intervention."
    )

    for target in targets:
        if dry_run:
            logger.info(
                "[dry-run] would send validator-blocked notification to %s for #%s",
                target,
                issue_number,
            )
            continue
        ok, _anchor = _hermes_send(target, body)
        if ok:
            logger.info(
                "sent validator-blocked notification to %s for #%s (block #%s)",
                target,
                issue_number,
                block_number,
            )
        else:
            logger.warning(
                "failed to send validator-blocked notification to %s for #%s",
                target,
                issue_number,
            )


# Per-process dedup sets: prevent repeat notifications within the same dispatcher
# lifetime for events that can fire every tick while the card stays blocked.
# Sets reset on process restart — acceptable for cron mode (one restart per tick).
_QA_FAILED_NOTIFIED: set = set()  # keys: (issue_n, pr)
_MAX_FIX_NOTIFIED: set = set()  # keys: (issue_n, pr)


def _notify_qa_failed(
    *,
    issue_number: Optional[int],
    pr_number: Optional[int],
    reason: str,
    resolved: Dict[str, Any],
    dry_run: bool = False,
) -> None:
    """Notify human channels when QA fails (closes #1002).

    Fires when the qa-daedalus card reports ``qa-failed`` in its summary,
    which blocks the PR from auto-merging. Routes through ``hermes send`` to
    every target subscribed to the ``qa-failed`` event. When no targets are
    configured, returns silently. Failures are logged, never raised.
    Deduplicates per (issue_number, pr_number) within the process lifetime
    to prevent per-tick spam while the QA card stays blocked.
    """
    dedup_key = (issue_number, pr_number)
    if dedup_key in _QA_FAILED_NOTIFIED:
        logger.debug(
            "qa-failed notification for #%s already sent this session — skipping",
            issue_number,
        )
        return
    _QA_FAILED_NOTIFIED.add(dedup_key)

    targets = _notify_targets(resolved, "qa-failed")
    if not targets:
        return

    issue_ref = f"#{issue_number}" if issue_number else "unknown issue"
    pr_ref = f" (PR #{pr_number})" if pr_number else ""
    reason_detail = f"\n\n**Reason**: {reason}" if reason else ""
    body = (
        f"🔴 **QA Failed: {issue_ref}**{pr_ref}\n\n"
        f"The QA agent reported a failure for {issue_ref}{pr_ref}. "
        f"The PR will NOT be auto-merged until the developer fixes the "
        f"failures and QA re-runs successfully.{reason_detail}\n\n"
        f"A developer fix card has been created automatically."
    )

    for target in targets:
        if dry_run:
            logger.info(
                "[dry-run] would send qa-failed notification to %s for %s",
                target,
                issue_ref,
            )
            continue
        ok, _anchor = _hermes_send(target, body)
        if ok:
            logger.info("sent qa-failed notification to %s for %s", target, issue_ref)
        else:
            logger.warning(
                "failed to send qa-failed notification to %s for %s", target, issue_ref
            )


def _notify_max_fix_attempts(
    *,
    issue_number: Optional[int],
    pr_number: Optional[int],
    reason: str,
    resolved: Dict[str, Any],
    dry_run: bool = False,
) -> None:
    """Notify human channels when a QA fix card exhausts MAX_FIX_ATTEMPTS.

    Fires when _execute_qa_fix escalates instead of creating another fix
    card, meaning the developer has failed to fix QA-reported failures after the maximum number
    of attempts and the issue now requires manual intervention. Routes through
    ``hermes send`` to every target subscribed to the ``max-fix-attempts``
    event. Deduplicates per (issue_number, pr_number) within the process
    lifetime.
    """
    dedup_key = (issue_number, pr_number)
    if dedup_key in _MAX_FIX_NOTIFIED:
        logger.debug(
            "max-fix-attempts notification for #%s already sent this session — skipping",
            issue_number,
        )
        return
    _MAX_FIX_NOTIFIED.add(dedup_key)

    targets = _notify_targets(resolved, "max-fix-attempts")
    if not targets:
        return

    issue_ref = f"#{issue_number}" if issue_number else "unknown issue"
    pr_ref = f" (PR #{pr_number})" if pr_number else ""
    reason_detail = f"\n\n**Last failure**: {reason}" if reason else ""
    body = (
        f"🚨 **Max Fix Attempts Reached: {issue_ref}**{pr_ref}\n\n"
        f"The developer has exhausted the maximum number of CI fix attempts "
        f"for {issue_ref}{pr_ref}. The issue has been escalated and requires "
        f"manual intervention.{reason_detail}"
    )

    for target in targets:
        if dry_run:
            logger.info(
                "[dry-run] would send max-fix-attempts notification to %s for %s",
                target,
                issue_ref,
            )
            continue
        ok, _anchor = _hermes_send(target, body)
        if ok:
            logger.info(
                "sent max-fix-attempts notification to %s for %s", target, issue_ref
            )
        else:
            logger.warning(
                "failed to send max-fix-attempts notification to %s for %s",
                target,
                issue_ref,
            )


# _check_confirmed_validators — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


# _retry_or_escalate_planner_stall — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


# _check_completed_planner — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


# _NOT_SUITABLE_RE — moved to core/dispatch/checks.py (issue #1262 PR 2/2)

# _check_planner_not_suitable — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


# _planner_not_suitable_validator_body — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


# _GET_ISSUE_RETRY_DELAYS, _fetch_issue_with_retry, _is_issue_closed_cached
# → moved to core/dispatch/housekeeping.py (issue #1153 PR 2/4)


# _check_completed_pm — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


# _check_completed_developer — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


# ── F5: per-role done-card prefix guard (#1125 F5) ──────────────────────────
# For these roles the pipeline advances via classify_blocked (blocked cards).
# A "done" card with no recognised role prefix means the outer Hermes agent
# completed the card directly (LLM non-compliance or premature completion).
# The guard archives the bad card and creates a new blocked card so human
# intervention is surfaced rather than silently lost.
#
# NOTE: validator / pm / developer / planner have existing _check_completed_*
# handlers with retry logic.  The guard targets the remaining five roles.
# _DONE_GUARD_PREFIXES — moved to core/dispatch/checks.py (issue #1262 PR 2/2)
# Idempotency-key prefix for guard-created blocked cards.
# _GUARD_PREFIX_IK_PREFIX — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


# _guard_prefix_on_done — moved to core/dispatch/checks.py (issue #1262 PR 2/2)


# _pm_consultation_body → moved to core/dispatch/bodies.py (issue #1153 PR 2/4)
# _check_stalled_in_progress → moved to core/dispatch/housekeeping.py (issue #1153 PR 2/4)


def _check_team_blockers(
    slug: str,
    repo: str,
    issues_map: Dict[int, Dict[str, Any]],
    workdir: str,
    base_branch: str,
    provider_name: str,
    profiles: Optional[Dict[str, str]] = None,
    role_skills: Optional[Dict[str, List[str]]] = None,
    *,
    dry_run: bool = False,
    provider=None,
    prefix_fallback: bool = True,
) -> List[int]:
    """PM re-activation trigger: for every blocked team triage card, create a PM
    consultation task if no active one already exists.

    A 'team blocker' is any blocked card assigned to a non-validator, non-PM profile
    whose summary does NOT start with 'ESCALATE:' (those are security escalations,
    handled separately by _enforce_validator_blocks).

    Returns issue numbers for which a PM consultation was created this tick.
    """
    p = profiles or _DEFAULT_PROFILES
    rs = role_skills or {}
    pipeline_profiles = {p["validator"], p["pm"]}
    blocked = kanban.list_blocked(slug)
    if not blocked:
        return []

    triggered: List[int] = []
    for card in blocked:
        assignee = (card.get("assignee") or "").strip()
        if assignee in pipeline_profiles:
            continue  # validator/PM blocks handled elsewhere
        # list --json never populates summary/last_summary; fetch it via show --json
        summary = kanban.get_latest_summary(slug, card["id"]).lower()
        if summary.startswith("escalate:"):
            continue  # security escalation — not a PM blocker
        if summary.startswith("review-required:"):
            continue  # PR in review or awaiting-pr — iterate handles this, PM can't unblock it
        if summary.startswith("a11y-skipped:"):
            continue  # accessibility skipped (no UI changes) — not a real blocker
        # #1205: crash-class blocks (worker died / breaker gave_up) are owned
        # by the crash-retry reconciler, which performs a REAL unblock +
        # re-dispatch with time-bounded retries. A PM consultation for them
        # was advisory-only (it never unblocked anything) and PM routing
        # cannot fix an infrastructure crash anyway.
        if crash_retry.is_crash_class(slug, card, summary):
            continue
        # #1182: reuse the authoritative classifier as the single source of
        # truth. Passing gate verdicts (review-approved / qa-passed /
        # security-approved / security: cleared / accessibility approved /
        # docs posted) are handoffs that iterate.classify_blocked will
        # ADVANCE or APPROVE_ADVANCE on this tick — not PM blockers. Spawning
        # a PM consultation for them races that completion: the PM UNBLOCKs
        # the card, moving it out of the blocked column before APPROVE_ADVANCE
        # can fire, so the card stalls until the next cron tick (and wastes a
        # PM agent). Skip these — iterate owns the handoff.
        action = iterate.classify_blocked(assignee, summary, True)
        if action in (iterate.ADVANCE, iterate.APPROVE_ADVANCE):
            continue
        n = extract_issue_number(card.get("title") or "")
        if n is None:
            continue
        if _has_active_pm_consultation(slug, n, p["pm"]):
            continue  # PM consultation already open for this issue
        # (#1125 F4) Skip if a completed consultation has already resolved this
        # blocker.  The consult-resolved marker is stamped on the blocked card by
        # _stamp_resolved_consultations (runs each tick before this function).
        if _is_consult_resolved(slug, card["id"], n, workdir=workdir, prefix_fallback=prefix_fallback):
            logger.debug(
                "dispatch: consult-resolved marker on %s for #%s — skipping re-creation",
                card["id"],
                n,
            )
            continue
        issue = issues_map.get(n)
        if not issue and provider is not None:
            fetched = _fetch_issue_with_retry(provider, n)
            if fetched:
                issue = fetched.as_dict()
                logger.info(
                    "dispatch: #%s not in issues_map — fell back to get_issue()", n
                )
            else:
                logger.warning(
                    "dispatch: #%s not found in issues_map or via get_issue() fallback",
                    n,
                )
        if not issue:
            logger.debug("dispatch: team blocked #%s but issue not in current scope", n)
            continue
        blocker_raw = summary or "no details provided"
        # Idempotency key: one PM consultation per (issue, blocked-card) pair.
        # hermes kanban create returns the existing task id when the key already
        # exists in any non-archived state, so this prevents the runaway loop
        # where each tick re-creates a consultation once the previous one is done.
        consult_key = f"consult-{n}-{card['id']}"
        if dry_run:
            logger.info(
                "[dry-run] team blocked #%s — would create PM consultation task", n
            )
            triggered.append(n)
            continue
        cid = kanban.create_task(
            slug,
            f"consult: #{n} {issue.get('title', '')}",
            body=_pm_consultation_body(
                repo, issue, blocker_raw, workdir, provider_name
            ),
            assignee=p["pm"],
            workspace=f"dir:{workdir}" if workdir else "",
            skills=rs.get("pm") or None,
            idempotency_key=consult_key,
        )
        if cid:
            # Store the blocked card ID so _stamp_resolved_consultations can
            # find it once the PM completes and stamp the blocked card with a
            # consult-resolved marker (#1125 F4).
            kanban.comment(slug, cid, f"{_BLOCKER_CARD_COMMENT_PREFIX}{card['id']}")
            logger.info(
                "dispatch: team blocked #%s — PM consultation task %s created", n, cid
            )
            triggered.append(n)
    return triggered


# _enforce_validator_blocks — moved to core/dispatch/stages.py (PR 3/4)


def _reconcile_vcs_board(resolved: Dict[str, Any], provider, *, dry_run: bool = False):
    """Auto-configure GitLab board settings in memory so a project works out of
    the box and self-heals on every tick (issue #133).

    GitLab Issue Boards are label-driven, so a project that was scaffolded
    kanban-only (``tracking: null``) silently never polls issues. This reconcile
    repairs that, idempotently and without writing the config file (so the
    heavily-commented YAML keeps its comments):

      1. Enable board mode (``tracking.label_board: true``) when the user has
         not set it explicitly — then rebuild the provider so THIS tick polls.
      2. Ensure the ``status_map`` board labels exist in the project.
      3. Fix ``vcs.target_branch`` when the configured branch does not exist in
         the repo, setting it to the real default branch (e.g. ``main`` → the
         repo's actual ``master``). A valid configured branch is left untouched.

    Returns ``(provider, notes)`` — ``notes`` is a list of one-line change
    descriptions (empty when nothing was reconciled). Non-GitLab providers are
    a no-op. Every API call degrades gracefully on failure.
    """
    notes: List[str] = []
    if provider is None or providers.provider_name(resolved) != "gitlab":
        return provider, notes

    # ── 1. Enable label-board mode unless explicitly configured ───────────────
    tracking = resolved.get("tracking")
    label_board_set = isinstance(tracking, dict) and "label_board" in tracking
    if not label_board_set:
        if dry_run:
            notes.append("would enable board mode (tracking.label_board=true)")
        else:
            if not isinstance(tracking, dict):
                tracking = {}
                resolved["tracking"] = tracking
            tracking["label_board"] = True
            provider = providers.get_provider(resolved) or provider
            notes.append("board mode enabled")

    # ── 2. Ensure required labels exist (epic, subtask + board lanes for GitLab) ──
    if provider is not None:
        if dry_run:
            notes.append("would ensure labels exist")
        else:
            created = provider.ensure_labels()
            if created:
                notes.append("created labels: " + ", ".join(created))
    # Legacy path: GitLab board status labels when not using ensure_labels
    if (
        provider is not None
        and provider.board_configured()
        and hasattr(provider, "ensure_status_labels")
        and not hasattr(provider, "ensure_labels")
    ):
        status_names = [
            provider.status_name(k)
            for k in ("ready", "in_progress", "in_review", "done")
        ]
        if not dry_run:
            created = provider.ensure_status_labels(status_names)
            if created:
                notes.append("created labels " + ", ".join(created))

    # ── 3. Fix target_branch when the configured branch does not exist ────────
    if hasattr(provider, "get_default_branch"):
        vcs = resolved.setdefault("vcs", {})
        configured = (vcs.get("target_branch") or "").strip()
        branches = set(provider.list_branches())
        if branches and configured not in branches:
            default_branch = provider.get_default_branch()
            if default_branch and default_branch != configured:
                if dry_run:
                    notes.append(f"would set target_branch={default_branch}")
                else:
                    vcs["target_branch"] = default_branch
                    notes.append(f"target_branch={default_branch}")

    return provider, notes


# Files that scripts/postinstall.py provisions into ~/.hermes/agent-hooks/.
# `hermes plugins update daedalus` runs `git pull` + copies *.example files but
# NEVER re-runs postinstall.py, so these go stale/missing after an update
# (issue #1354); the self-heal below re-syncs them on drift. Keep in sync with
# postinstall._install_advance_hook + _install_webhook_handler.
_AGENT_HOOK_FILES = (
    "daedalus-advance.sh",
    "daedalus_resolve_project.py",
    "daedalus-ready.sh",
)


def _self_heal_agent_hooks() -> bool:
    """Re-sync postinstall-provisioned agent-hooks when they drift from source.

    `hermes plugins update daedalus` git-pulls + copies *.example files but never
    invokes scripts/postinstall.py, so new or changed hook scripts under
    scripts/ are never re-copied to ~/.hermes/agent-hooks/ (issue #1354). A stale
    or missing daedalus-advance.sh then makes agents' on_session_end advance hook
    silently fail, stalling the pipeline up to ~60 min until the cron fallback.

    Called once per dispatch tick. On the first tick after an update it detects
    the drift and re-runs the idempotent installer helpers (which between them
    cover every agent-hooks file); once current it is a cheap no-op (a few file
    reads and compares).

    Contract: never raises (log + return on any failure), no network, and skips
    entirely under test isolation. The conftest points HERMES_HOME at a tmp dir
    while the installers write to HOME/.hermes; a divergence between the two means
    we are NOT in a live install, so touching the real home would be wrong.

    Returns True when a re-sync was performed, else False (incl. on skip/failure).
    """
    try:
        real_home = Path(os.environ.get("HOME", os.path.expanduser("~")))
        hermes_home = os.environ.get("HERMES_HOME")
        if hermes_home and Path(hermes_home).resolve() != (real_home / ".hermes").resolve():
            # Isolated/test environment — the installers write to HOME/.hermes,
            # not this HERMES_HOME. Do nothing so tests never touch the real home.
            return False
        source_dir = Path(__file__).resolve().parent
        hooks_dir = real_home / ".hermes" / "agent-hooks"
        drifted = False
        for name in _AGENT_HOOK_FILES:
            src = source_dir / name
            if not src.is_file():
                continue  # source not shipped in this build — nothing to sync
            dst = hooks_dir / name
            try:
                if not dst.exists() or dst.read_text() != src.read_text():
                    drifted = True
                    break
            except OSError:
                drifted = True  # unreadable installed copy → treat as drift
                break
        if not drifted:
            return False
        from scripts.postinstall import _install_advance_hook, _install_webhook_handler

        _install_advance_hook()
        _install_webhook_handler()
        logger.info(
            "self-heal: re-synced agent-hooks from plugin source after update (#1354)"
        )
        return True
    except Exception as exc:  # never let self-heal break a tick
        logger.warning("self-heal: agent-hooks re-sync failed: %s", exc)
        return False


def run(
    resolved: Dict[str, Any],
    *,
    assignee: Optional[str] = None,
    max_dispatch: int = 5,
    dry_run: bool = False,
    provider=None,
) -> Dict[str, Any]:
    """Run one dispatch tick with the per-tick kanban ``list_tasks`` cache active.

    Thin wrapper over ``_run_tick`` (issue #1142): the dispatcher calls
    ``kanban.list_tasks`` at ~30 sites per tick, each otherwise spawning a hermes
    subprocess to read identical board state. Enabling the cache here (and
    disabling it in ``finally``, so the cache never leaks across ticks or on an
    exception) collapses those repeated reads to one subprocess per distinct
    ``(slug, status)`` key, while mutations invalidate it so reads never go stale.
    """
    # Self-heal agent-hooks that a `hermes plugins update` left stale (#1354).
    # Drift-guarded + test-isolation-safe; never raises.
    _self_heal_agent_hooks()
    kanban.enable_tick_cache()
    try:
        return _run_tick(
            resolved,
            assignee=assignee,
            max_dispatch=max_dispatch,
            dry_run=dry_run,
            provider=provider,
        )
    finally:
        kanban.disable_tick_cache()


def _direct_then_dispatch(slug, resolved, max_dispatch, *, dry_run=False):
    """#1329 structural delegation: when ``execution.direct_delegate`` is on, first
    spawn ``delegate.sh`` DIRECTLY for dispatchable non-developer delegated cards
    (no local-model deciding hop), claiming each so it goes ``running``; then run the
    normal ``kanban.dispatch`` for the developer + non-delegated cards — it skips the
    cards direct-dispatch already claimed. Flag off → direct-dispatch is a no-op and
    this is byte-identical to a bare ``kanban.dispatch``."""
    try:
        _direct_dispatch(slug, resolved, max_spawns=max_dispatch, dry_run=dry_run)
    except Exception as exc:  # never let direct-dispatch break a tick
        logger.warning("dispatch: direct-dispatch failed: %s", exc)
    return kanban.dispatch(slug, max_spawns=max_dispatch)


def _run_tick(
    resolved: Dict[str, Any],
    *,
    assignee: Optional[str] = None,
    max_dispatch: int = 5,
    dry_run: bool = False,
    provider=None,
) -> Dict[str, Any]:
    """Reconcile statuses, create tasks for new issues, and dispatch. Returns a summary.

    When dry_run is True, no board status moves, kanban cards, or dispatches happen —
    every mutating action is logged as "[dry-run] would ..." and reflected in the
    returned summary, so a tick can be safely previewed before scheduling the cron.

    ``provider`` (a core.providers.VCSProvider) is built from the resolved config
    when not injected (tests inject a fake).
    """
    repo = resolved.get("repo", "")
    filters = (resolved.get("issues") or {}).get("filters", {})
    execution = resolved.get("execution") or {}
    # Native per-task retry/runtime bounds (#1289) — resolved once per tick and
    # threaded into every role create_task call site. When
    # execution.native_bounds is off (default) bounds["enabled"] is False and
    # bounds_kwargs() emits nothing, so CLI args stay byte-identical.
    bounds = native_bounds.resolve_bounds(execution)
    # Goal-mode (#1296) — resolved once per tick alongside bounds.  When
    # execution.goal_mode is off (default) goal_cfg["enabled"] is False and
    # create_with_goal_fallback emits byte-identical args (no --goal).
    _goal_cfg = goal_mode.resolve_goal_mode(execution)
    # Phase-3 (#1170): prefix_fallback flag — default true (current behaviour).
    # Treat None (e.g. `protocol: {prefix_fallback: null}` in YAML) as True so
    # a null value is never silently coerced to False via bool(None).
    _protocol = resolved.get("protocol") or {}
    _pf_raw = _protocol.get("prefix_fallback", True)
    _prefix_fallback = True if _pf_raw is None else bool(_pf_raw)
    # Phase-1 (#1288): metadata_transport flag — default false (behaviour
    # unchanged). Threaded into _guard_prefix_on_done the same way as
    # prefix_fallback so the done-card guard can accept native run metadata.
    _metadata_transport = bool(_protocol.get("metadata_transport", False))
    iterations = int(
        execution.get("max_lifecycle_iterations", 3)
    )  # self-improving loop cap (configurable)
    profiles = _resolve_profiles(execution)
    role_skills: Dict[str, List[str]] = _resolve_role_skills(execution)
    coding_agent = _resolve_coding_agent(execution)
    # Warn-only capability preflight (#1351): when the hermes coding_agent path
    # runs pipeline roles on a local model known to crash-loop at the developer
    # stage (e.g. qwen3.6), log a single heads-up so operators aren't surprised
    # by a silently-stalled pipeline. Never blocks dispatch.
    _preflight_local_model_capability(execution, coding_agent)
    coding_agent_cmd = _resolve_coding_agent_cmd(execution)
    # Ensure a sane turn budget so substantial tasks don't silently hit claude's
    # 25-turn default; respects an explicit --max-turns and non-claude agents (#143).
    coding_agent_cmd = _apply_coding_agent_max_turns(
        coding_agent, coding_agent_cmd, execution
    )
    # Resolve the per-project coding-agent wait ceiling once per tick. run() is
    # single-threaded per process, so the body builders (called below) read this
    # module global rather than threading it through every signature (issue #141).
    global _CODING_AGENT_MAX_WAIT
    _CODING_AGENT_MAX_WAIT = _resolve_coding_agent_max_wait(execution)
    role_agents: Dict[str, str] = {}

    # Resolve epic detection config (issue #455) with soft validation
    try:
        epic_config = _resolve_epic_config(execution)
    except Exception as exc:
        logger.warning(
            "Failed to resolve epic_detection config: %s, using defaults", exc
        )
        epic_config = {
            "enabled": True,
            "min_deliverables": 6,
            "size_threshold": 1000,
            "epic_label": "epic",
            "child_label": "subtask",
        }

    # Inject the correct autonomous-ai-agents skill for every role that delegates to a cloud agent.
    # Inject the correct autonomous-ai-agents skill for every role that delegates to a cloud agent.
    _AGENT_SKILL: Dict[str, str] = {
        "claude-code": "autonomous-ai-agents/claude-code",
        "codex": "autonomous-ai-agents/codex",
        "opencode": "autonomous-ai-agents/opencode",
    }
    for _role, _agent in role_agents.items():
        _skill = _AGENT_SKILL.get(_agent)
        if _skill:
            _role_skills = list(role_skills.get(_role) or [])
            if _skill not in _role_skills:
                _role_skills.append(_skill)
            role_skills = {**role_skills, _role: _role_skills}
    _comment_header_tpl: str = (
        execution.get("comment_header_template")
        or notify_templates.DEFAULT_COMMENT_HEADER_TEMPLATE
    )
    # Validate that every configured profile exists in Hermes (once per tick).
    # Missing profiles either fall back to built-in defaults or are dropped,
    # depending on execution.profile_fallback_behavior.  Logs a warning per
    # missing role so the user knows exactly what to fix.
    fallback_behavior = (
        execution.get("profile_fallback_behavior") or "fallback"
    ).strip()
    profiles = _validate_profiles(profiles, fallback_behavior=fallback_behavior)
    workdir = resolved.get("workdir", "")
    # Compute and persist a config fingerprint (SHA-256 of coding_agent +
    # model.default) so downstream logic can detect when either value changes
    # across ticks (issue #1052).
    if workdir and not dry_run:
        active_model = _resolve_active_model_provider()
        _config_fp = dispatch_state.compute_config_fingerprint(
            coding_agent,
            active_model.get("model"),
        )
        dispatch_state.set_config_fingerprint(workdir, _config_fp)
        logger.debug("dispatch: config fingerprint = %s", _config_fp)
        # Resync *-daedalus profiles when coding_agent or global model changes.
        _stored_resync_fp = dispatch_state.get_resync_fingerprint(workdir)
        if _config_fp != _stored_resync_fp:
            _old_vals = dispatch_state.get_config_values(workdir)
            if _old_vals is None:
                # First tick: establish baseline without resyncing profiles.
                dispatch_state.set_resync_fingerprint(workdir, _config_fp)
                dispatch_state.set_config_values(
                    workdir, coding_agent, active_model.get("model")
                )
            else:
                _project_name = resolved.get("name", workdir)
                logger.info(
                    "dispatch: config fingerprint changed for %s — triggering profile resync",
                    _project_name,
                )
                _n_resynced = _resync_profiles_to_model(
                    workdir,
                    new_model=active_model.get("model"),
                    new_provider=active_model.get("provider"),
                    old_values=_old_vals,
                )
                dispatch_state.set_resync_fingerprint(workdir, _config_fp)
                dispatch_state.set_config_values(
                    workdir, coding_agent, active_model.get("model")
                )
                _log_resync(
                    count=_n_resynced,
                    new_model=active_model.get("model") or "",
                    old_coding_agent=_old_vals.get("coding_agent", ""),
                    new_coding_agent=coding_agent or "",
                    old_model=_old_vals.get("model_default", ""),
                    new_model_for_log=active_model.get("model") or "",
                )
    # Messaging target the documentation agent's completion report is sent to.
    notify_target = (resolved.get("cron") or {}).get("deliver", "")
    slug = _board_slug(repo, resolved.get("name", ""))
    if provider is None:
        provider = providers.get_provider(resolved)
    # Auto-configure GitLab board settings (board mode, status labels, default
    # branch) in memory so freshly-set-up and pre-existing projects work on the
    # first tick without manual daedalus.yaml edits (issue #133). Idempotent.
    provider, vcs_autoconfig = _reconcile_vcs_board(resolved, provider, dry_run=dry_run)
    if vcs_autoconfig:
        logger.info("dispatch: GitLab auto-config — %s", "; ".join(vcs_autoconfig))
    base_branch = (resolved.get("vcs") or {}).get("target_branch", "dev")
    board_mode = bool(provider is not None and provider.board_configured())
    if not board_mode:
        logger.warning(
            "dispatch: no VCS board configured — skipping board status moves"
        )

    # Ready-gating: when a board is configured, ONLY issues whose board status is
    # in the configured ready_statuses become new work. PR-state reconciliation
    # (open/merged -> In review) below still runs for every open issue, regardless of status.
    ready: Optional[set] = None
    if board_mode:
        ready_statuses = (resolved.get("tracking") or {}).get("ready_statuses") or [
            provider.status_name("ready")
        ]
        ready = provider.board_numbers_with_statuses(ready_statuses)
        logger.info(
            "dispatch: %d issue(s) in %s: %s", len(ready), ready_statuses, sorted(ready)
        )

    kanban.ensure_board(slug)

    # Enforcement sweep for orphaned git worktrees (issue #1114): agents are
    # told to `git worktree remove --force` on cleanup, but crashed/reclaimed
    # agents leave theirs behind and they accumulate unboundedly. Runs before
    # any dispatch so a stale worktree is gone within one tick of its pipeline
    # finishing; worktrees with an active kanban task are preserved.
    if workdir:
        swept = _sweep_orphan_worktrees(workdir, slug, dry_run=dry_run)
        if swept:
            logger.info("dispatch: swept %d orphan worktree(s)", swept)

    # ── spec-file trigger source ─────────────────────────────────────────────
    # When sources.local_specs.enabled, scan <repo>/.hermes/pending/ for *.md
    # files and create a triage card for each (idempotent via file-path key).
    # This runs BEFORE auto-advance + GitHub-issue polling so spec-driven work
    # enters the board regardless of whether GitHub issues are configured.
    spec_sources = resolved.get("sources", {}).get("local_specs", {})
    spec_created = []
    if spec_sources.get("enabled"):
        spec_dir = spec_sources.get("directory", ".hermes/pending/")
        spec_files = source_specs.list_spec_files(workdir, directory=spec_dir)
        for sf in spec_files:
            tid = source_specs.spec_to_triage(
                slug,
                workdir,
                sf,
                base_branch=base_branch,
                workspace=f"dir:{workdir}" if workdir else None,
            )
            if tid:
                kanban.decompose(slug, tid)
                spec_created.append(sf.name)
        if spec_created:
            logger.info(
                "dispatch: spec-file source created %d triage card(s): %s",
                len(spec_created),
                spec_created,
            )

    # ── auto-advance (CI-aware routing + self-healing) ────────────────────────
    # For every blocked card: classify its state (dev+green CI → advance,
    # dev+QA-reported failures → fix card, reviewer with findings → PM routing card, etc.) and
    # execute the appropriate action. The self-healing loop creates fix-up tasks
    # for failing CI/review and escalates after MAX_FIX_ATTEMPTS.
    #
    # Also run native diagnostics to surface stuck-in-blocked cards with severity
    # alongside the classify → execute path. Diagnostics degrades gracefully.
    diag = kanban.diagnostics(slug)
    if diag:
        logger.info("dispatch: diagnostics for %s: %d finding(s)", slug, len(diag))
        for d in diag:
            logger.info(
                "dispatch:   [%s] %s — %s",
                d.get("severity", "?"),
                d.get("task_id", "?"),
                d.get("message", ""),
            )

    # Stale-blocked sweeper (#186): warn for cards stuck in blocked > N hours and
    # optionally archive them off the active board. Configurable via
    # tracking.stale_blocked.{hours,archive}; degrades gracefully.
    stale_cfg = (resolved.get("tracking") or {}).get("stale_blocked") or {}
    try:
        sweeper.sweep_stale_blocked(
            slug,
            threshold_hours=float(stale_cfg.get("hours", sweeper.DEFAULT_STALE_HOURS)),
            archive=bool(stale_cfg.get("archive", False)),
            dry_run=dry_run,
        )
    except Exception as exc:  # never let the sweeper break a dispatch tick
        logger.warning("dispatch: stale-blocked sweep failed: %s", exc)

    # Stale-running sweeper (#232, self-heal #1323): warn for running cards whose
    # summary hasn't advanced in > N hours (default 0.5 = 30 min) — a dead, wedged
    # or headless-suspended worker the board still shows as in-progress, holding
    # the max_dispatch slot. With reset on (default), each stale card is re-blocked
    # crash-class so the crash-retry reconciler (which runs next, this same tick)
    # re-dispatches it and frees the slot. Configurable via
    # tracking.stale_running.{hours,reset}; reset skipped in dry-run.
    stale_running_cfg = (resolved.get("tracking") or {}).get("stale_running") or {}
    stale_running = 0
    try:
        stale_running = len(
            sweeper.sweep_stale_running(
                slug,
                threshold_hours=float(
                    stale_running_cfg.get("hours", sweeper.DEFAULT_RUNNING_STALE_HOURS)
                ),
                reset=bool(stale_running_cfg.get("reset", True)) and not dry_run,
            )
        )
    except Exception as exc:  # never let the sweeper break a dispatch tick
        logger.warning("dispatch: stale-running sweep failed: %s", exc)

    # ── generalized triage-recovery ──────────────────────────────────────────
    # Any role card that crash-loops to the terminal `triage` state (a flaky local model)
    # is re-created a bounded number of times so the pipeline recovers hands-off instead of
    # stranding until a human intervenes. The developer is skipped (its PR-aware F10/F12
    # path owns it). Configurable via tracking.triage_recovery.{max,enabled}; skipped in
    # dry-run. Never breaks a tick.
    triage_cfg = (resolved.get("tracking") or {}).get("triage_recovery") or {}
    if triage_cfg.get("enabled", True):
        try:
            sweeper.recover_triaged_cards(
                slug,
                max_recoveries=int(
                    triage_cfg.get("max", sweeper.DEFAULT_MAX_TRIAGE_RECOVERIES)
                ),
                reset=not dry_run,
            )
        except Exception as exc:  # never let recovery break a dispatch tick
            logger.warning("dispatch: triage-recovery failed: %s", exc)

    # ── merged-issue orphan sweep (issue #1373) ──────────────────────────────
    # The one-shot merged-PR reap runs once (when the developer card lands), but
    # triage-recovery / guard-prefix machinery can mint `[recover N]` / `guard:`
    # cards AFTER that reap, orphaning them in blocked forever — board clutter
    # that also fools the serial-queue gate. This every-tick sweep archives any
    # such non-terminal card whose issue's PR is merged (per the provider, not
    # the board's Done column). Configurable via tracking.merged_orphan_sweep.
    # {enabled}; degrades gracefully.
    orphan_cfg = (resolved.get("tracking") or {}).get("merged_orphan_sweep") or {}
    if orphan_cfg.get("enabled", True):
        try:
            sweeper.sweep_merged_orphans(slug, provider, dry_run=dry_run)
        except Exception as exc:  # never let the sweep break a dispatch tick
            logger.warning("dispatch: merged-orphan sweep failed: %s", exc)

    # ── crash-retry reconciler (issue #1205) ─────────────────────────────────
    # Crash-class blocked / gave-up cards (worker died, session limit, provider
    # connection error) are auto-unblocked with time-bounded, backed-off
    # retries instead of stranding until a manual `hermes kanban unblock`.
    # Runs on EVERY dispatch entry point (cron tick and on_session_end advance
    # both funnel through run(), serialized by the main() FileLock), before
    # iterate/team-blockers so a retried card re-runs this same tick and never
    # spawns an advisory-only PM consultation.
    crash_actions: List[Dict[str, Any]] = []
    if workdir:
        # Provider failover (#1207): ordered coding-agent / brain chains let
        # the reconciler re-dispatch a crashed card on the NEXT provider when
        # the active one is limited/down, instead of retrying it forever.
        failover_ctx: Optional[Dict[str, Any]] = None
        try:
            failover_ctx = _build_failover_context(slug, resolved, execution, workdir)
            _maybe_reset_brain_to_primary(workdir, failover_ctx, dry_run)
        except Exception as exc:  # degrade to plain #1205 retries
            logger.warning("dispatch: provider-failover context failed: %s", exc)
        try:
            crash_actions = crash_retry.reconcile(
                slug,
                workdir,
                execution,
                dry_run=dry_run,
                failover=failover_ctx,
                native_bounds=bounds["enabled"],
            )
        except Exception as exc:  # never let the reconciler break a dispatch tick
            logger.warning("dispatch: crash-retry reconcile failed: %s", exc)
    else:
        logger.debug(
            "dispatch: crash-retry reconcile skipped — no workdir configured "
            "(episode state needs the project state file)"
        )
    crash_retried = sum(1 for a in crash_actions if a.get("action") == "retried")
    crash_escalated = [a for a in crash_actions if a.get("action") == "escalated"]
    for _esc in crash_escalated:
        _send_crash_retries_exhausted_notification(
            action=_esc, resolved=resolved, dry_run=dry_run
        )

    (
        iterate_counts,
        advance_prs,
        pending_signal_cards,
        qa_failed_cards,
        escalated_cards,
    ) = iterate.run_iterate(
        slug,
        repo,
        resolved=resolved,
        provider=provider,
        dry_run=dry_run,
        max_fix_attempts=_resolve_max_fix_attempts(execution),
    )
    for _qf in qa_failed_cards:
        _notify_qa_failed(
            issue_number=_qf.get("issue_n"),
            pr_number=_qf.get("pr"),
            reason=_qf.get("reason", ""),
            resolved=resolved,
            dry_run=dry_run,
        )
    for _esc in escalated_cards:
        _notify_max_fix_attempts(
            issue_number=_esc.get("issue_n"),
            pr_number=_esc.get("pr"),
            reason=_esc.get("reason", ""),
            resolved=resolved,
            dry_run=dry_run,
        )
    # Separate advance PR numbers from routed actions (dev_fix / escalate) for
    # the human summary so PR numbers are reported correctly.
    routed_actions = {
        k: v
        for k, v in iterate_counts.items()
        if v > 0
        and k not in (iterate.ADVANCE, iterate.APPROVE_ADVANCE, iterate.PENDING_SIGNAL)
    }
    # crash_retried > 0 forces a dispatch even when iterate saw nothing to do:
    # the crash-retry reconciler just returned card(s) to ready and they must
    # re-run within this same trigger (#1205).
    if (any(c > 0 for c in iterate_counts.values()) or crash_retried) and not dry_run:
        _direct_then_dispatch(slug, resolved, max_dispatch, dry_run=dry_run)

    # ── doc-report delivery ──────────────────────────────────────────────────
    # The dispatcher delivers documentation reports (PR comments prefixed
    # `**Agent: documentation**`) to every configured doc-report target,
    # because agents run in isolated profile HOMEs without messaging config.
    # Idempotent via a hidden PR comment sentinel.
    slack_delivered = _deliver_doc_reports(
        slug,
        provider,
        _notify_targets(resolved, "doc-report"),
        dry_run=dry_run,
    )

    created, reconciled, completed = [], [], []
    blocked_deps: Dict[int, List[int]] = {}  # issue -> open blocker numbers (#139)
    threads_mirrored = 0
    issues: List[Dict[str, Any]] = []

    if not board_mode:
        # Kanban-only mode: no VCS board, so the kanban board IS the tracker.
        # A human creates a triage card (dashboard / `hermes kanban create
        # --triage`); we fan every triage card out across the roster and
        # dispatch. (auto-advance above already flows review-required handoffs.)
        if dry_run:
            logger.info(
                "[dry-run] kanban-only: would decompose triage cards + dispatch"
            )
        else:
            kanban.decompose_all_triage(slug)
            _direct_then_dispatch(slug, resolved, max_dispatch, dry_run=dry_run)
        summary = {
            "board": slug,
            "mode": "kanban",
            "created": created,
            "reconciled": reconciled,
            "completed": completed,
            "advance_prs": advance_prs,
            "routed_actions": routed_actions,
            "issues_seen": 0,
            "spec_created": spec_created,
            "slack_delivered": slack_delivered,
            "vcs_autoconfig": vcs_autoconfig,
            "stale_running": stale_running,
            "crash_retried": crash_retried,
            "crash_escalated": [a.get("task_id") for a in crash_escalated],
            "enrollment_failures": sorted(
                set(getattr(provider, "enrollment_failures", []))
            )[:500],
        }
        logger.info("dispatch summary: %s", summary)
        return summary

    # Board mode: poll Ready issues, reconcile PR state, triage+decompose.
    in_review_name = provider.status_name("in_review")
    existing = kanban.list_issue_numbers(slug)
    issues = _fetch_issues(provider, filters)

    # Priority queue: dispatch P0 → P1 → P2 → unlabeled in that order.
    issues.sort(
        key=lambda i: min(
            (
                _PRIORITY.get((lbl["name"] if isinstance(lbl, dict) else lbl), 99)
                for lbl in (i.get("labels") or [])
            ),
            default=99,
        )
    )

    # Phase-2 (#1290): upfront_dag flag.  Default FALSE — behaviour byte-identical.
    _pipeline_cfg = resolved.get("pipeline") or {}
    _upfront_dag = bool(_pipeline_cfg.get("upfront_dag", False))

    # Enforce validator blocks: set 'Blocked' column on VCS board and cancel
    # downstream tasks for any issue whose validator card is currently blocked.
    # Runs each tick so issues blocked mid-cycle are caught immediately.
    # When pipeline.upfront_dag is ON, the 6-outcome arbiter prunes the pre-built
    # DAG by the validator's structured verdict instead; when OFF the legacy
    # enforcement runs exactly as before (flag-off byte-identical).
    if _upfront_dag:
        blocked_issues = _arbitrate_validator_outcome(
            slug,
            provider,
            existing,
            validator_profile=profiles["validator"],
            dry_run=dry_run,
            workdir=workdir,
            prefix_fallback=_prefix_fallback,
        )
    else:
        blocked_issues = _enforce_validator_blocks(
            slug,
            provider,
            existing,
            validator_profile=profiles["validator"],
            dry_run=dry_run,
            workdir=workdir,
            prefix_fallback=_prefix_fallback,
        )

    _follow_up_cfg: Dict[str, Any] = resolved.get("follow_up_extraction") or {}

    # Stall detection: move in-progress cards older than threshold to blocked.
    # Uses dispatch_stale_timeout_seconds from config (default 30min) as the threshold.
    stall_seconds = int(
        (resolved.get("kanban") or {}).get("dispatch_stale_timeout_seconds", 1800)
    )
    stall_minutes = stall_seconds // 60
    stalled_cards = _check_stalled_in_progress(
        slug, stall_minutes=stall_minutes, dry_run=dry_run
    )
    if stalled_cards:
        logger.info("dispatch: %d stalled card(s) moved to blocked", len(stalled_cards))

    # Defense-in-depth: auto-repair orphan tasks created by the PM agent.
    # Fixes generic assignees (developer → developer-daedalus) AND missing issue-
    # number prefixes in titles (#N). Runs before dispatch so workers are never
    # spawned with unresolvable assignees or untraceable titles.
    _repair_orphan_tasks(slug, profiles, dry_run=dry_run)

    # Phase-2 trigger: validator CONFIRMED → PM spec task.
    # Phase-3 trigger: PM SPEC done → team triage (developer/reviewer/security/docs).
    # Phase-3b: team BLOCKED → PM consultation task (re-activation).
    # All three run every tick for immediate response to phase transitions.
    issues_map: Dict[int, Dict[str, Any]] = {i["number"]: i for i in issues}
    _sec_targets = _notify_targets(resolved, "security-escalation")
    _label_ovr = (execution or {}).get("label_overrides", {})

    # ── epic QA dispatch gate (issue #1098) ────────────────────────────────
    # Before any dispatch call, unblock QA cards whose sub-issue PRs appeared
    # since the last tick, then defer (block) QA cards for epics with no
    # sub-issue PR yet.  This prevents the dispatcher from spawning a QA agent
    # for an epic before any developer has opened a PR.
    if not dry_run:
        _maybe_undefer_epic_qa_tasks(slug, issues_map, kanban, epic_config=epic_config)
        _gate_epic_qa_tasks(
            slug, issues_map, kanban, epic_config=epic_config, dry_run=dry_run
        )

    # Single per-tick closed-issue cache shared across all advancement functions
    # so each unique issue number requires at most one get_issue_state() call (#1115).
    _tick_closed_cache: Dict[int, Optional[bool]] = {}

    confirmed_triggered = _check_confirmed_validators(
        slug,
        repo,
        issues_map,
        iterations,
        workdir,
        notify_target,
        base_branch,
        provider.name,
        _sec_targets,
        label_overrides=_label_ovr,
        profiles=profiles,
        role_skills=role_skills,
        coding_agent=coding_agent,
        coding_agent_cmd=coding_agent_cmd,
        role_agents=role_agents,
        dry_run=dry_run,
        provider=provider,
        resolved=resolved,
        closed_issue_cache=_tick_closed_cache,
        bounds=bounds,
    )
    if confirmed_triggered and not dry_run:
        _direct_then_dispatch(slug, resolved, max_dispatch, dry_run=dry_run)

    planner_triggered = _check_completed_planner(
        slug,
        workdir,
        profiles=profiles,
        dry_run=dry_run,
        provider=provider,
        closed_issue_cache=_tick_closed_cache,
        repo=repo,
        base_branch=base_branch,
        issues_map=issues_map,
        role_skills=role_skills,
        epic_config=epic_config,
        resolved=resolved,
        bounds=bounds,
    )
    if planner_triggered and not dry_run:
        _direct_then_dispatch(slug, resolved, max_dispatch, dry_run=dry_run)

    planner_not_suitable_triggered = _check_planner_not_suitable(
        slug,
        repo,
        issues_map,
        workdir,
        base_branch,
        provider.name,
        profiles=profiles,
        role_skills=role_skills,
        coding_agent=coding_agent,
        coding_agent_cmd=coding_agent_cmd,
        notify_targets=_sec_targets,
        dry_run=dry_run,
        provider=provider,
        closed_issue_cache=_tick_closed_cache,
        bounds=bounds,
    )
    if planner_not_suitable_triggered and not dry_run:
        _direct_then_dispatch(slug, resolved, max_dispatch, dry_run=dry_run)

    pm_triggered = _check_completed_pm(
        slug,
        repo,
        issues_map,
        iterations,
        workdir,
        notify_target,
        base_branch,
        provider.name,
        _sec_targets,
        label_overrides=_label_ovr,
        profiles=profiles,
        role_skills=role_skills,
        coding_agent=coding_agent,
        coding_agent_cmd=coding_agent_cmd,
        role_agents=role_agents,
        dry_run=dry_run,
        provider=provider,
        closed_issue_cache=_tick_closed_cache,
        bounds=bounds,
        goal_cfg=_goal_cfg,
    )
    if pm_triggered and not dry_run:
        _direct_then_dispatch(slug, resolved, max_dispatch, dry_run=dry_run)

    # ── developer empty-summary retry (issue #1104) ────────────────────────
    # When a developer task completes with no PR in its summary (agent crash,
    # context overflow), create a retry task before QA can auto-promote and
    # fail with "qa-failed: no PR". Mirrors the validator/PM retry pattern.
    dev_retry_triggered = _check_completed_developer(
        slug,
        repo,
        issues_map,
        iterations,
        workdir,
        base_branch,
        provider.name,
        profiles=profiles,
        role_skills=role_skills,
        coding_agent=coding_agent,
        coding_agent_cmd=coding_agent_cmd,
        role_agents=role_agents,
        label_overrides=_label_ovr,
        dry_run=dry_run,
        provider=provider,
        resolved=resolved,
        closed_issue_cache=_tick_closed_cache,
        bounds=bounds,
        goal_cfg=_goal_cfg,
    )
    if dev_retry_triggered and not dry_run:
        _direct_then_dispatch(slug, resolved, max_dispatch, dry_run=dry_run)

    # ── F5: mechanical prefix guard for done cards with unexpected summaries ──
    # Catches outer-agent LLM non-compliance: when a QA/reviewer/security/
    # accessibility/docs card transitions to done without the canonical role
    # prefix, the card is archived and a new blocked card is created so human
    # operators can intervene.  Validator/pm/developer/planner have dedicated
    # _check_completed_* handlers with retry logic and are excluded (#1125 F5).
    guard_triggered = _guard_prefix_on_done(
        slug,
        profiles=profiles,
        dry_run=dry_run,
        closed_issue_cache=_tick_closed_cache,
        provider=provider,
        prefix_fallback=_prefix_fallback,
        metadata_transport=_metadata_transport,
    )
    if guard_triggered and not dry_run:
        _direct_then_dispatch(slug, resolved, max_dispatch, dry_run=dry_run)

    # Mirror each completed role's kanban summary to its GitHub issue (#894).
    # Agents no longer post their own comments (GITHUB_TOKEN is absent in the
    # cron worker env); the dispatcher posts via its authenticated provider.
    _post_completion_comments(slug, provider, profiles, workdir, dry_run=dry_run)

    follow_up_count = _check_follow_ups_from_reviewer_prs(
        slug,
        repo,
        provider,
        workdir,
        profiles,
        _follow_up_cfg,
        dry_run=dry_run,
    )
    if follow_up_count and not dry_run:
        _direct_then_dispatch(slug, resolved, max_dispatch, dry_run=dry_run)

    # (#1125 F4) Stamp blocked cards whose PM consultations completed with
    # CLARIFIED/ESCALATED so _check_team_blockers skips re-creating the same
    # consultation.  Must run before _check_team_blockers each tick.
    _pm_profile = (profiles or _DEFAULT_PROFILES).get("pm", _DEFAULT_PROFILES["pm"])
    if not dry_run:
        _stamp_resolved_consultations(slug, _pm_profile, workdir=workdir)

    blocker_triggered = _check_team_blockers(
        slug,
        repo,
        issues_map,
        workdir,
        base_branch,
        provider.name,
        profiles=profiles,
        role_skills=role_skills,
        dry_run=dry_run,
        prefix_fallback=_prefix_fallback,
    )
    if blocker_triggered and not dry_run:
        _direct_then_dispatch(slug, resolved, max_dispatch, dry_run=dry_run)

    for issue in issues:
        n = issue["number"]
        # Reconciliation acts ONLY on daedalus-managed issues — ones that have a
        # kanban card. Issues the daedalus never dispatched (incl. everything not
        # in "Ready") are left untouched, so a tick never surprises non-Ready issues.
        if n in existing:
            merged_pr = None
            open_pr_obj = None
            _linked_pr = provider._pr_for_issue(n)
            if _linked_pr:
                if _linked_pr.state == "merged":
                    # Only treat as merged when the PR targeted the configured branch.
                    # A merge to main/some-other-branch before the project target
                    # branch must not prematurely close the issue.
                    if (
                        not base_branch
                        or not _linked_pr.base_branch
                        or _linked_pr.base_branch == base_branch
                    ):
                        merged_pr = _linked_pr
                    else:
                        logger.info(
                            "dispatch: #%s PR #%s merged to '%s' (not target '%s') — skipping Done",
                            n,
                            _linked_pr.number,
                            _linked_pr.base_branch,
                            base_branch,
                        )
                elif _linked_pr.state == "open":
                    open_pr_obj = _linked_pr
            pr = "merged" if merged_pr else ("open" if open_pr_obj else None)
            if pr == "merged":
                # Merged into target branch = work complete. GitHub does NOT
                # auto-close issues on a non-default-branch merge, so we do it.
                if dry_run:
                    dry_closed = kanban.close_issue_tasks(
                        slug,
                        n,
                        summary=f"closed: parent issue #{n} merged and closed",
                        dry_run=True,
                    )
                    logger.info(
                        "[dry-run] would set #%s -> Done + close issue (PR merged) (%d task(s))",
                        n,
                        len(dry_closed),
                    )
                    completed.append(n)
                else:
                    provider.board_set_status(n, provider.status_name("done"))
                    provider.close_issue(n)
                    # Archive kanban tasks immediately so the orphan cleanup path
                    # on the next tick doesn't re-report this issue as completed.
                    kanban.close_issue_tasks(
                        slug, n, summary=f"closed: parent issue #{n} merged and closed"
                    )
                    # Post the final "merged" reply into the thread BEFORE clearing
                    # dispatch state (which wipes the thread anchor for this issue).
                    threads_mirrored += _mirror_issue_threads(
                        resolved,
                        provider,
                        issue,
                        n,
                        workdir,
                        pr_obj=merged_pr,
                        pr_state="merged",
                        dry_run=dry_run,
                    )
                    dispatch_state.clear_dispatch(workdir, n)
                    completed.append(n)
                    # CHANGELOG auto-update: prepend a brief entry using the PR title.
                    if merged_pr and merged_pr.number and base_branch:
                        cl_entry = (
                            f"## [{issue.get('title', f'Issue #{n}')}]"
                            f"({provider.issue_url(n)}) — "
                            f"[PR #{merged_pr.number}]({provider.pr_url(merged_pr.number)})\n"
                        )
                        if not provider.append_changelog(base_branch, cl_entry):
                            logger.debug(
                                "dispatch: CHANGELOG update skipped for #%s "
                                "(provider doesn't support it or no write token)",
                                n,
                            )
            elif pr == "open":
                # PR open and awaiting review -> In review.
                # Safety net: if the PR body lacks a closing keyword, inject one
                # now so GitHub auto-closes the issue on merge even if the agent
                # forgot to include it.
                if open_pr_obj and open_pr_obj.number:
                    patched_body = ensure_closing_keyword(open_pr_obj.body or "", n)
                    if patched_body != (open_pr_obj.body or ""):
                        if dry_run:
                            logger.info(
                                "[dry-run] PR #%s body missing 'Closes #%s' — would patch",
                                open_pr_obj.number,
                                n,
                            )
                        else:
                            if provider.update_pr_body(
                                open_pr_obj.number, patched_body
                            ):
                                logger.info(
                                    "dispatch: injected 'Closes #%s' into PR #%s body",
                                    n,
                                    open_pr_obj.number,
                                )
                            else:
                                logger.warning(
                                    "dispatch: could not patch PR #%s body — "
                                    "issue #%s may not auto-close on merge",
                                    open_pr_obj.number,
                                    n,
                                )
                    # ── PR size gate + forbidden file guard ──────────────────
                    pr_files = provider.get_pr_files(open_pr_obj.number)
                    if pr_files and workdir:
                        max_pr_lines = int((execution or {}).get("max_pr_lines", 0))
                        if max_pr_lines:
                            total_lines = sum(f.get("changes", 0) for f in pr_files)
                            if (
                                total_lines > max_pr_lines
                                and not dispatch_state.has_pr_flag(
                                    workdir, open_pr_obj.number, "size_warned"
                                )
                            ):
                                warn = (
                                    notify_templates.render_agent_header(
                                        "daedalus", template=_comment_header_tpl
                                    )
                                    + "\n\n"
                                    f"⚠️ **PR too large**: This PR changes **{total_lines} lines** "
                                    f"(project limit: {max_pr_lines}).\n\n"
                                    "Please split into smaller, focused PRs before this is reviewed. "
                                    "Large PRs are harder to review and more likely to introduce bugs."
                                )
                                if dry_run:
                                    logger.info(
                                        "[dry-run] PR #%s too large (%d lines) — would warn",
                                        open_pr_obj.number,
                                        total_lines,
                                    )
                                else:
                                    provider.post_pr_comment(open_pr_obj.number, warn)
                                    dispatch_state.set_pr_flag(
                                        workdir, open_pr_obj.number, "size_warned"
                                    )
                                    logger.info(
                                        "dispatch: PR #%s size warning posted (%d lines > %d)",
                                        open_pr_obj.number,
                                        total_lines,
                                        max_pr_lines,
                                    )
                        forbidden_patterns = (execution or {}).get(
                            "forbidden_files", _DEFAULT_FORBIDDEN
                        )
                        blocked_files = [
                            f["filename"]
                            for f in pr_files
                            if any(
                                fnmatch(f.get("filename", ""), pat)
                                for pat in forbidden_patterns
                            )
                        ]
                        if blocked_files and not dispatch_state.has_pr_flag(
                            workdir, open_pr_obj.number, "forbidden_warned"
                        ):
                            warn = (
                                notify_templates.render_agent_header(
                                    "daedalus", template=_comment_header_tpl
                                )
                                + "\n\n"
                                "🚨 **Forbidden file(s) detected**: This PR touches files that "
                                "require explicit human review before merge:\n\n"
                                + "".join(f"- `{fn}`\n" for fn in blocked_files)
                                + "\n**Do not merge this PR until a human has reviewed these files.**"
                            )
                            if dry_run:
                                logger.info(
                                    "[dry-run] PR #%s touches forbidden files — would warn: %s",
                                    open_pr_obj.number,
                                    blocked_files,
                                )
                            else:
                                provider.post_pr_comment(open_pr_obj.number, warn)
                                dispatch_state.set_pr_flag(
                                    workdir, open_pr_obj.number, "forbidden_warned"
                                )
                                logger.warning(
                                    "dispatch: PR #%s touches forbidden files: %s",
                                    open_pr_obj.number,
                                    blocked_files,
                                )
                if dry_run:
                    logger.info(
                        "[dry-run] would set #%s -> %s (PR open)", n, in_review_name
                    )
                    reconciled.append((n, in_review_name))
                elif provider.board_set_status(n, in_review_name):
                    reconciled.append((n, in_review_name))
            if pr != "merged":
                # Open-PR / no-PR managed issue: mirror the ongoing conversation
                # (root anchor + agent comments + PR-open event). The merged case
                # already mirrored its final reply above, before clear_dispatch.
                threads_mirrored += _mirror_issue_threads(
                    resolved,
                    provider,
                    issue,
                    n,
                    workdir,
                    pr_obj=open_pr_obj,
                    pr_state=pr,
                    dry_run=dry_run,
                )
            # No/closed PR on a managed issue: leave it (worker still in progress).
            continue
        # Unmanaged issue: only "Ready" items become new work.
        if ready is not None and n not in ready:
            continue  # Ready-gating: not in "Ready" -> don't dispatch yet
        # Dependency-aware ready-gating (#139): even a Ready issue is held back
        # while any of its blockers are still open. Re-checked every tick, so a
        # tier auto-unblocks as its blockers' PRs merge — no human relabeling and
        # no project-specific promote cron. Providers never raise; the getattr
        # guard keeps older provider doubles working.
        open_blockers = getattr(provider, "blockers", lambda _n: [])(n)
        if open_blockers:
            blocked_deps[n] = open_blockers
            logger.info(
                "dispatch: #%s is Ready but blocked by %s — skipping until closed",
                n,
                ", ".join(f"#{b}" for b in open_blockers),
            )
            continue
        if provider.pr_state_for_issue(n):
            # Already has an open/merged PR -> work exists; don't dispatch a
            # duplicate worker. (Checked only for Ready candidates to limit API calls.)
            logger.info(
                "dispatch: #%s is Ready but already has a PR — skipping (no duplicate)",
                n,
            )
            continue
        if len(created) >= max_dispatch:
            break  # cap new tasks per tick
        # New work (deterministic, code): board status -> In progress, then
        # create a TRIAGE card and decompose it so the roster fans out across
        # developer -> reviewer -> security-analyst -> documentation. Hermes tracks
        # each sub-task live on the board.
        if dry_run:
            logger.info(
                "[dry-run] would dispatch #%s (%s): set In progress + create triage card + decompose",
                n,
                issue.get("title", ""),
            )
            created.append(n)
            existing.add(n)
            threads_mirrored += _mirror_issue_threads(
                resolved, provider, issue, n, workdir, dry_run=dry_run
            )
            continue
        provider.board_set_status(n, provider.status_name("in_progress"))
        # Epic routing (Phase 1 of #149): large issues go to planner first.
        # Phase 2+ will add codebase analysis + sub-issue decomposition.
        if _is_epic(issue, epic_config):
            planner_key = f"planner-{n}"
            # Idempotency check (#181): query for existing planner card BEFORE
            # creation. The CLI's --idempotency-key returns the existing task ID
            # when the key matches, but the Python code below would still record
            # dispatch state and add to 'created' list. An explicit check here
            # prevents duplicates on re-tick when the key already exists.
            existing_planner = next(
                (
                    t
                    for t in kanban.list_tasks(slug)
                    if (t.get("idempotency_key") or "") == planner_key
                ),
                None,
            )
            if existing_planner is not None:
                logger.info(
                    "dispatch: #%s planner card already exists (%s) — skipping duplicate",
                    n,
                    planner_key,
                )
                # Do NOT 'continue' — fall through to the `if vid:` check with
                # vid=None so dispatch state is not re-recorded.
                vid = None
            else:
                logger.info("dispatch: #%s detected as epic — routing to planner", n)
                vid = kanban.create_task(
                    slug,
                    f"#{n} {issue.get('title', '')}",
                    body=_planner_body(
                        repo, issue, workdir, base_branch, provider.name, epic_config
                    ),
                    assignee=profiles["planner"],
                    idempotency_key=planner_key,
                    workspace=f"dir:{workdir}" if workdir else "",
                    skills=role_skills.get("planner") or None,
                    **native_bounds.bounds_kwargs(bounds, "planner"),
                )
        elif _upfront_dag:
            # Phase-2 (#1290): build the ENTIRE stage DAG at Ready-time. The
            # validator is created unblocked (dispatches now); every downstream
            # stage is created dependency-blocked and auto-promotes as its
            # parents complete. The 6-outcome arbiter (above) prunes by verdict.
            # Idempotency is handled inside build_pipeline_dag via <role>-{n} keys.
            _dag_specs = _pipeline_dag_role_specs(
                repo, issue, workdir, base_branch, provider.name,
                profiles, execution, resolved, role_skills, bounds,
                coding_agent_cmd,
                _notify_targets(resolved, "security-escalation"),
            )
            # Detect a pre-existing DAG so re-ticks don't re-run the one-time
            # bookkeeping below (thread mirror, dispatch-state record) — mirrors
            # the validator-only path's existing-card guard.
            _dag_preexisting = any(
                (t.get("idempotency_key") or "") == f"validator-{n}"
                for t in kanban.list_tasks(slug)
            )
            _dag_ids = iterate.build_pipeline_dag(
                slug, n, _dag_specs, dry_run=dry_run,
            )
            # vid drives the post-creation bookkeeping below. The validator is the
            # only immediately-dispatchable stage; use its id as the sentinel, but
            # only when the DAG was freshly created this tick.
            vid = None if _dag_preexisting else _dag_ids.get("validator")
        else:
            # Phase 1: dispatch ONLY the validator. The dispatcher creates developer/
            # reviewer/security/documentation tasks ONLY after the validator completes
            # with a 'CONFIRMED:' summary. No other agent can start until then.
            # Idempotency check (t_a2f4bc9c): query for existing validator card BEFORE
            # creation. If a validator task for this issue already exists and is
            # pending/active, skip creation to prevent duplicate tasks on re-tick.
            validator_key = f"validator-{n}"
            # "Pending or active" covers every non-terminal state (todo/ready/running/
            # blocked). Terminal states (done/cancelled) fall through so a fresh
            # validator task can be created — the retry path uses distinct keys
            # (validator-retry-{n}-rN) so this check does not interfere with it.
            _ACTIVE_VALIDATOR_STATUSES = {
                "todo",
                "ready",
                "running",
                "in_progress",
                "blocked",
            }
            existing_validator = next(
                (
                    t
                    for t in kanban.list_tasks(slug)
                    if (t.get("idempotency_key") or "") == validator_key
                    and (t.get("status") or "").lower() in _ACTIVE_VALIDATOR_STATUSES
                ),
                None,
            )
            if existing_validator is not None:
                logger.info(
                    "dispatch: #%s validator card already exists (%s, status=%s) — skipping duplicate",
                    n,
                    validator_key,
                    existing_validator.get("status"),
                )
                vid = None
            else:
                vid = kanban.create_task(
                    slug,
                    f"#{n} {issue.get('title', '')}",
                    body=_validator_body(
                        repo,
                        issue,
                        workdir,
                        base_branch,
                        provider.name,
                        _notify_targets(resolved, "security-escalation"),
                        coding_agent=_resolve_agent_for_role(execution, "validator"),
                        coding_agent_cmd=coding_agent_cmd,
                    ),
                    assignee=profiles["validator"],
                    idempotency_key=validator_key,
                    workspace=f"dir:{workdir}" if workdir else "",
                    skills=role_skills.get("validator") or None,
                    **native_bounds.bounds_kwargs(bounds, "validator"),
                )
        if vid:
            created.append(n)
            existing.add(n)
            dispatch_state.record_dispatch(workdir, n)
            # Open the platform thread now so every later agent comment has a
            # root to reply to (posts the anchor; no comments exist yet).
            threads_mirrored += _mirror_issue_threads(
                resolved, provider, issue, n, workdir, dry_run=dry_run
            )

    if created and not dry_run:
        # #1339: route the post-creation "nudge" through direct-dispatch FIRST so a
        # just-created delegated card (e.g. a fresh validator) is claimed + delegate.sh
        # spawned before the kanban.dispatch subprocess spawns a qwen agent for it.
        # (This is the multi-line call the earlier _direct_then_dispatch sweep missed.)
        _direct_then_dispatch(
            slug, resolved, max_dispatch, dry_run=dry_run
        )  # nudge

    # ── bidirectional sync: VCS board Done → archive Hermes kanban tasks ────────
    # If a human manually moved a managed issue to "Done" on the VCS board
    # (without a PR merge), the Hermes kanban still shows tasks as In progress.
    # Detect this and archive the kanban tasks so both boards stay in sync.
    if board_mode:
        board_done_nums = provider.board_numbers_with_statuses(
            [provider.status_name("done")]
        )
        already_completed = set(completed)
        for n in sorted((board_done_nums & existing) - already_completed):
            if dry_run:
                dry_closed = kanban.close_issue_tasks(
                    slug,
                    n,
                    summary=f"closed: parent issue #{n} merged and closed",
                    dry_run=True,
                )
                logger.info(
                    "[dry-run] #%s is Done on VCS board → would archive kanban tasks (%d task(s))",
                    n,
                    len(dry_closed),
                )
                completed.append(n)
            else:
                closed_tasks = kanban.close_issue_tasks(
                    slug, n, summary=f"closed: parent issue #{n} merged and closed"
                )
                if closed_tasks:
                    logger.info(
                        "dispatch: #%s moved to Done on VCS board → archived %d kanban task(s)",
                        n,
                        len(closed_tasks),
                    )
                    completed.append(n)

    # ── staleness check: managed issues stuck in-progress without a PR ──────────
    # If an issue has been dispatched for more than staleness_hours and still has
    # no PR, the assigned agent may be stuck. Post a one-shot warning comment on
    # the issue and log it — humans can decide whether to re-queue.
    staleness_hours = float((execution or {}).get("staleness_hours", 48))
    if board_mode and staleness_hours > 0 and workdir:
        reconciled_nums = {num for num, _ in reconciled}
        already_done = set(completed)
        for n in sorted(existing):
            if n in reconciled_nums or n in already_done:
                continue  # has a PR or just completed
            age = dispatch_state.get_dispatch_age_hours(workdir, n)
            if age is None or age <= staleness_hours:
                continue
            logger.warning(
                "dispatch: #%s in-progress for %.1fh without a PR — possible stale agent",
                n,
                age,
            )
            if not dry_run and not dispatch_state.has_pr_flag(
                workdir, n, "stale_warned"
            ):
                provider.post_issue_comment(
                    n,
                    notify_templates.render_agent_header(
                        "daedalus", template=_comment_header_tpl
                    )
                    + "\n\n"
                    f"⚠️ **Daedalus staleness alert** — Issue #{n} has been in progress "
                    f"for **{age:.0f} hours** without a linked PR.\n\n"
                    "The assigned agent may be stuck. If work is ongoing, add a progress comment. "
                    "If the agent is not making progress, close this issue to re-queue it on the next tick.",
                )
                dispatch_state.set_pr_flag(workdir, n, "stale_warned")

    # ── cleanup: archive kanban tasks for issues closed directly on VCS ───────
    # Issues closed without a merged PR (won't-fix, duplicate, manual close)
    # never appear in the open-issue fetch, so the reconciliation loop above
    # never sees them. Find managed issue numbers absent from this tick's open
    # fetch, check their VCS state, and complete their kanban tasks.
    seen_open = {i["number"] for i in issues}
    orphaned = existing - seen_open
    for n in sorted(orphaned):
        state = provider.get_issue_state(n)
        if state != "closed":
            continue  # still open (filtered by label/limit) or unknown — leave it

        # Guard: if there are still active (non-done) kanban tasks for this issue,
        # the close is likely accidental (bot close, manual mis-click). Skip cleanup
        # so a human reopen can resume the pipeline without data loss.
        active_count = _count_active_issue_tasks(slug, n)
        if active_count:
            logger.warning(
                "dispatch: #%s is closed on VCS but has %d active kanban task(s) — "
                "skipping bulk-complete (likely accidental close; reopen the issue to resume)",
                n,
                active_count,
            )
            continue

        if dry_run:
            dry_closed = kanban.close_issue_tasks(
                slug,
                n,
                summary=f"closed: parent issue #{n} merged and closed",
                dry_run=True,
            )
            logger.info(
                "[dry-run] #%s closed externally → would archive kanban tasks + Done (%d task(s))",
                n,
                len(dry_closed),
            )
            completed.append(n)
            continue
        provider.board_set_status(n, provider.status_name("done"))
        closed_tasks = kanban.close_issue_tasks(
            slug, n, summary=f"closed: parent issue #{n} merged and closed"
        )
        logger.info(
            "dispatch: #%s closed externally → Done (%d task(s) completed: %s)",
            n,
            len(closed_tasks),
            closed_tasks,
        )
        # Only report as completed if we actually archived tasks — guards against
        # re-reporting on every tick when hermes kanban ls still returns done tasks.
        if closed_tasks:
            completed.append(n)

    # ── Global reconcile: catch any orphaned cards not handled above ────────
    # Safety net: if a card references an issue that's Done on the board but the
    # card itself is still non-terminal (bug in earlier cleanup paths, card added
    # after the issue moved to Done, etc.), complete it here.
    if board_mode:
        _global_reconcile_orphan_cards(slug, provider, dry_run=dry_run)

    # ── Label-projection: project canonical pipeline state to VCS labels ──────
    # Gated on pipeline.label_projection (default False).  Reuses the already-
    # cached kanban.list_tasks result — no new kanban API calls.
    if board_mode and provider is not None:
        _reconcile_label_projections(resolved, provider, slug, dry_run=dry_run)

    # ── Tier promotion: re-evaluate sub-issue Ready labels after merges ──────
    # When sub-issues declare ``Depends on:`` dependencies (via the body convention),
    # closure of a blocker should promote its dependents to Ready idempotently.
    # Called after the completed list is fully populated so all just-closed issues
    # participate in one pass. Never raises — logs and records errors in the result.
    if completed and not dry_run and provider is not None:
        try:
            promo_result = tier_promotion.promote_waiting_tiers(
                provider, list(completed)
            )
            if promo_result.promoted:
                logger.info(
                    "tier promotion: promoted %d issue(s) to Ready: %s",
                    len(promo_result.promoted),
                    promo_result.promoted,
                )
            if promo_result.errors:
                logger.warning(
                    "tier promotion: encountered %d error(s): %s",
                    len(promo_result.errors),
                    promo_result.errors,
                )
            if promo_result.cycles:
                logger.warning(
                    "tier promotion: detected %d cycle(s) in dependency graph: %s",
                    len(promo_result.cycles),
                    promo_result.cycles,
                )
        except Exception as e:
            logger.error("tier promotion crashed unexpectedly: %s", e)

    # #1339: under direct_delegate daedalus is the SOLE dispatcher for delegated cards.
    # Run the unconditional direct-dispatch pass HERE, at the END of the tick — fresh
    # validator/review cards are created earlier in THIS same tick, so an earlier pass
    # sees nothing (empirically verified) and a standalone kanban daemon would grab the
    # card for the qwen hop. At tick end all cards created this tick are visible in
    # ``ready``, so direct_dispatch claims + spawns delegate.sh for them. No-op when the
    # flag is off. Requires NO standalone ``hermes kanban daemon`` (see daedalus.yaml).
    if (resolved.get("execution") or {}).get("direct_delegate") and not dry_run:
        try:
            _direct_dispatch(slug, resolved, max_spawns=max_dispatch)
        except Exception as exc:  # never let it break a tick
            logger.warning("dispatch: end-of-tick direct-dispatch failed: %s", exc)

    # F11: unconditional end-of-tick dispatch of any `ready` card. The per-branch nudges
    # above only fire when this tick *created* cards, so a card promoted to `ready` by a
    # self-heal (crash-retry unblock, triage-recovery re-create) or by a gate opening would
    # otherwise sit until the next created-card tick. `hermes kanban dispatch` is idempotent
    # (only spawns for ready cards, capped at max_dispatch) and is what makes local-model
    # self-heal fully hands-off. Skipped in dry-run; never breaks a tick.
    if not dry_run:
        try:
            kanban.dispatch(slug, max_spawns=max_dispatch)
        except Exception as exc:  # never let it break a tick
            logger.warning("dispatch: end-of-tick ready-card dispatch failed: %s", exc)

    summary = {
        "board": slug,
        "mode": provider.name,
        "created": created,
        "reconciled": reconciled,
        "completed": completed,
        "advance_prs": advance_prs,
        "routed_actions": routed_actions,
        "issues_seen": len(issues),
        "spec_created": spec_created,
        "slack_delivered": slack_delivered,
        "blocked": blocked_issues,
        "blocked_deps": blocked_deps,
        "threads_mirrored": threads_mirrored,
        "pm_triggered": pm_triggered,
        "blocker_triggered": blocker_triggered,
        "vcs_autoconfig": vcs_autoconfig,
        "stale_running": stale_running,
        "crash_retried": crash_retried,
        "crash_escalated": [a.get("task_id") for a in crash_escalated],
        "enrollment_failures": sorted(
            set(getattr(provider, "enrollment_failures", []))
        ),
    }
    logger.info("dispatch summary: %s", summary)
    return summary








def _reconcile_label_projections(
    resolved: Dict[str, Any],
    provider,
    slug: str,
    *,
    dry_run: bool = False,
) -> None:
    """Project canonical kanban state to daedalus:* VCS labels for all active issues.

    Gated on pipeline.label_projection (default False).  Uses the already-
    cached kanban.list_tasks result — no new kanban API calls per tick.
    Never raises — errors are logged and swallowed.
    """
    from core.label_projection import reconcile_label_projection

    pipeline_cfg = (resolved.get("pipeline") or {})
    if not pipeline_cfg.get("label_projection"):
        return

    # list_tasks is cached per tick (PR #1142) — this is a free call.
    all_cards = kanban.list_tasks(slug)
    if not all_cards:
        return

    # Collect unique issue numbers from card titles.
    issue_numbers: set[int] = set()
    for c in all_cards:
        n = extract_issue_number(c.get("title") or "")
        if n is not None:
            issue_numbers.add(n)

    for issue_n in sorted(issue_numbers):
        cards_for_issue = [
            c for c in all_cards
            if extract_issue_number(c.get("title") or "") == issue_n
        ]
        if not cards_for_issue:
            continue
        try:
            adds, removes = reconcile_label_projection(
                slug, issue_n, provider, cards=cards_for_issue, dry_run=dry_run,
            )
            if adds or removes:
                logger.info(
                    "label_projection #%s: +%d -%d labels",
                    issue_n, adds, removes,
                )
        except Exception as e:
            logger.warning("label_projection #%s: error (non-fatal): %s", issue_n, e)


def _mirror_issue_threads(
    resolved: Dict[str, Any],
    provider,
    issue: Dict[str, Any],
    n: int,
    workdir: str,
    *,
    pr_obj=None,
    pr_state: Optional[str] = None,
    dry_run: bool = False,
) -> int:
    """Mirror an issue's agent conversation into a thread on every target.

    Posts (idempotently, deduped across ticks) for issue #*n*:
      * a root thread-anchor message (first event on each target);
      * one reply per agent comment on the issue and its linked PR;
      * a reply when the PR is opened / merged.

    Targets come from ``cron.notifications`` (event ``comment-mirror``); catch-all
    entries — those with no ``events`` filter — receive it automatically, so no
    new config keys are required. Returns the number of events actually sent.
    """
    if not workdir:
        return 0
    targets = _notify_targets(resolved, "comment-mirror")
    if not targets:
        return 0

    name = resolved.get("name", "")
    issue_url = provider.issue_url(n) if provider else ""
    pr_number = getattr(pr_obj, "number", None)
    pr_url = provider.pr_url(pr_number) if (provider and pr_number) else ""
    pr_title = getattr(pr_obj, "title", "") or ""

    # Build the ordered event list once (shared across all targets).
    events: List[tuple] = [
        (
            "root",
            notify_templates.render_thread_root(
                name, n, issue.get("title", ""), issue_url
            ),
        ),
    ]
    if pr_number and pr_state in ("open", "merged"):
        verb = "opened" if pr_state == "open" else "merged"
        events.append(
            (
                f"pr-{verb}:{pr_number}",
                notify_templates.render_thread_pr_event(
                    verb, pr_number, pr_title, pr_url
                ),
            )
        )
    for event_key, body in thread_delivery.select_comments(provider, n, pr_number):
        events.append(
            (
                event_key,
                notify_templates.render_thread_comment(
                    n, pr_number, body, issue_url=issue_url, pr_url=pr_url
                ),
            )
        )

    sent = 0

    for target in targets:
        # Get broadcast setting for this target
        broadcast_reply = _get_target_broadcast(target, resolved)

        def sender(target: str, body: str, thread_id: Optional[str], broadcast=False):
            return _hermes_send(target, body, thread_id=thread_id, broadcast=broadcast)

        for event_key, body in events:
            result = thread_delivery.deliver_event(
                workdir,
                n,
                target,
                body,
                event_key,
                send=sender,
                dry_run=dry_run,
                broadcast_thread_reply=broadcast_reply,
            )
            if result == "sent":
                sent += 1
    if sent:
        verb = "[dry-run] would mirror" if dry_run else "mirrored"
        logger.info(
            "dispatch: %s %d thread event(s) for #%s to %s",
            verb,
            sent,
            n,
            ", ".join(targets),
        )
    return sent


def _deliver_doc_reports(
    slug: str,
    provider,
    notify_targets,
    *,
    dry_run: bool = False,
) -> List[int]:
    """Deliver completed documentation reports to the messaging target(s).

    Scans the board's DONE cards for documentation cards whose linked PR has a
    ``**Agent: documentation**`` comment. For each such PR, fetches the report
    body from the comment and sends it to every target in ``notify_targets``
    (a list of ``hermes send`` target strings; a bare string is accepted for
    backward compatibility).

    Idempotent: posts a hidden sentinel PR comment
    (``<!-- daedalus:slack-delivered -->``) after delivery; skips PRs that already
    have it. The sentinel is posted once ANY target received the report (so a
    flaky secondary channel can't re-spam the channels that already got it);
    failed targets are logged.

    Returns the list of PR numbers that were successfully delivered (for the
    human summary).
    """
    if isinstance(notify_targets, str):
        notify_targets = [notify_targets] if notify_targets else []
    notify_targets = [t for t in (notify_targets or []) if t]
    if not notify_targets or provider is None:
        return []

    delivered: List[int] = []
    doc_cards = kanban.list_tasks(slug, status="done")

    for card in doc_cards:
        assignee = (card.get("assignee") or "").strip()
        if assignee != "documentation-daedalus":
            continue

        # Resolve the PR number: try the card's body/events for a PR reference,
        # then fall back to the issue number on the parent triage card.
        pr_number = _parse_pr_from_card(card)
        if pr_number is None:
            # Try parent → issue number → PR
            pr_number = _resolve_pr_from_parents(slug, provider, card)

        if pr_number is None:
            logger.debug(
                "dispatch: doc card %s has no resolvable PR — skipping Slack delivery",
                card.get("id"),
            )
            continue

        # Idempotence: skip if already delivered
        if provider.pr_has_delivery_marker(pr_number):
            logger.debug(
                "dispatch: PR #%s already has slack-delivered marker — skipping",
                pr_number,
            )
            continue

        # Find the **Agent: documentation** comment on the PR
        report_body = _find_doc_comment(provider, pr_number)
        if not report_body:
            logger.debug(
                "dispatch: PR #%s has no **Agent: documentation** comment yet — skipping",
                pr_number,
            )
            continue

        if dry_run:
            logger.info(
                "[dry-run] would deliver doc report for PR #%s to %s",
                pr_number,
                ", ".join(notify_targets),
            )
            delivered.append(pr_number)
            continue

        # Wrap the raw PR comment in a rich notification envelope.
        issue_number = notify_templates.extract_issue_number(report_body)
        notification = notify_templates.render_doc_report_notification(
            repo=provider.display_repo,
            pr_number=pr_number,
            pr_url=provider.pr_url(pr_number),
            report_body=report_body,
            issue_number=issue_number,
            issue_url=provider.issue_url(issue_number) if issue_number else "",
        )

        # Deliver via the dispatcher's root context — fan out to every target.
        sent_to = [t for t in notify_targets if _send_via_hermes(t, notification)]
        if not sent_to:
            # Total send failure → do NOT post the sentinel (retry next tick)
            continue
        if len(sent_to) < len(notify_targets):
            logger.warning(
                "dispatch: doc report for PR #%s reached %d/%d targets (failed: %s)",
                pr_number,
                len(sent_to),
                len(notify_targets),
                ", ".join(t for t in notify_targets if t not in sent_to),
            )

        # Post sentinel so we never re-deliver
        if not provider.post_delivery_marker(pr_number, report_body):
            # Sentinel post failure is noisy but not fatal — we delivered.
            # Next tick might re-deliver but the dedup sentinel is best-effort.
            logger.warning(
                "dispatch: delivered PR #%s but sentinel post failed — may re-deliver",
                pr_number,
            )

        delivered.append(pr_number)
        logger.info(
            "dispatch: delivered doc report for PR #%s to %s",
            pr_number,
            ", ".join(sent_to),
        )

    return delivered








def _notify_project_summary(
    name: str,
    summary: Dict[str, Any],
    resolved: Dict[str, Any],
    *,
    dry_run: bool = False,
) -> bool:
    """Self-deliver a project's tick summary to its ``cron.notifications`` targets.

    Threaded + deduped (issue #137): the summary is mirrored through
    :func:`thread_delivery.deliver_event` under a per-project anchor
    (:data:`_PROJECT_SUMMARY_ANCHOR`) with an ``event_key`` derived from the
    summary's content hash, so:

      * a *silent* tick (``render_dispatch_summary`` returns "") sends nothing;
      * an *unchanged* summary on a later tick / self-healing iteration is
        recognised by its content hash and skipped — no duplicate top-level spam;
      * a *changed* summary posts as a reply under the project's anchor message,
        so the whole dispatch history threads under one message.

    Falls back to a plain (un-threaded) send when the project config carries no
    ``workdir`` to persist dedup state against.

    Returns True when the project uses ``notifications[]`` — the caller must
    then NOT include it in stdout (which the legacy cron ``--deliver`` path
    would deliver a second time). Legacy single-``deliver`` projects return
    False and keep flowing through cron stdout delivery.
    """
    if not ((resolved.get("cron") or {}).get("notifications")):
        return False
    try:
        provider = providers.get_provider(resolved)
    except Exception as exc:
        provider = None
        # Don't silently drop the tick summary: a provider failure here means
        # render_dispatch_summary falls back to degraded rendering, so surface
        # the cause (bad config / missing token) instead of hiding it. The
        # workdir identifies the resolved project config on disk.
        logger.warning(
            "dispatch: provider instantiation failed for project %s "
            "(workdir=%s): %s — tick summary will use degraded rendering",
            name,
            resolved.get("workdir") or "<none>",
            exc,
        )
    msg = notify_templates.render_dispatch_summary(
        name, summary, provider, dry_run=dry_run
    )
    if not msg:
        return True  # silent tick — handled, nothing to send
    targets: List[str] = []
    for event in sorted(_summary_events(summary)):
        for t in _notify_targets(resolved, event):
            if t not in targets:
                targets.append(t)
    if not targets:
        return True

    workdir = (resolved.get("workdir") or "").strip()
    if not workdir:
        # No state dir to dedup/thread against — fall back to a plain send.
        for t in targets:
            if dry_run:
                logger.info(
                    "[dry-run] would send dispatch summary for %s to %s", name, t
                )
            else:
                _send_via_hermes(t, msg)
        return True

    # Stable per-content key: identical summaries dedupe across ticks/processes;
    # different summaries thread as replies under the same per-project anchor.
    event_key = "summary:" + hashlib.sha1(msg.encode("utf-8")).hexdigest()

    def sender(target: str, body: str, thread_id: Optional[str]):
        return _hermes_send(target, body, thread_id=thread_id)

    sent = 0
    for t in targets:
        if (
            thread_delivery.deliver_event(
                workdir,
                _PROJECT_SUMMARY_ANCHOR,
                t,
                msg,
                event_key,
                send=sender,
                dry_run=dry_run,
            )
            == "sent"
        ):
            sent += 1
    if sent:
        verb = "[dry-run] would deliver" if dry_run else "delivered"
        logger.info(
            "dispatch: %s threaded summary for %s to %d target(s)", verb, name, sent
        )
    return True










def _maybe_redirect_dev_mode(resolved: Dict[str, Any]) -> None:
    """Re-exec the dispatcher from a local dev checkout when ``dev_mode`` is on.

    Reads the ``dev_mode`` block from *resolved* config.  When
    ``dev_mode.enabled`` is truthy and ``dev_mode.path`` points at a valid
    local checkout containing ``scripts/daedalus_dispatch.py``, the current
    process is replaced via :func:`os.execve` so the FileLock is not
    double-held.

    Guard chain (all must pass before re-exec):
      1. Skip if ``DAEDALUS_DEV`` env var is already set (infinite-loop guard).
      2. Skip if ``resolved["dev_mode"]`` is not a dict (bad config / missing key).
      3. Skip if ``resolved["dev_mode"]["enabled"]`` is falsy.
      4. Skip if ``dev_mode.path`` is absent or empty.
      5. Warn + skip if ``<path>/scripts/daedalus_dispatch.py`` does not exist.
      6. Skip if ``abspath(dev_script) == abspath(__file__)`` (already in dev).
      7. Set ``DAEDALUS_DEV=1``, prepend *path* to ``PYTHONPATH``, call
         :func:`os.execve` with ``sys.executable`` as the interpreter.

    This function never returns when it redirects — :func:`os.execve` replaces
    the process image.  On any skip condition (or unexpected error during the
    filesystem / execve path) it returns ``None`` so the caller continues with
    the installed-plugin code path (fail safe — never crash the dispatcher).
    """
    import os

    # 1. Infinite-loop guard — we are already running the dev copy.
    if os.environ.get(_DEV_MODE_ENV):
        return

    # 2. dev_mode must be a dict (guards against missing key, string, bool, etc.)
    dev_cfg = resolved.get("dev_mode")
    if not isinstance(dev_cfg, dict):
        return

    # 3. dev_mode must be explicitly enabled.
    if not dev_cfg.get("enabled"):
        return

    # 4. A path must be configured and must be a string.
    raw_path = dev_cfg.get("path")
    if not isinstance(raw_path, str):
        return
    dev_path_raw = raw_path.strip()
    if not dev_path_raw:
        return

    dev_path = os.path.expanduser(dev_path_raw)
    dev_script = os.path.join(dev_path, "scripts", "daedalus_dispatch.py")

    # 5. The dev checkout must contain the dispatcher script.
    #    Wrapped in try/except so permission errors fail safe (no redirect).
    try:
        script_exists = os.path.isfile(dev_script)
    except (OSError, PermissionError):
        logger.warning(
            "dispatch: dev_mode enabled but cannot stat %s — skipping redirect",
            dev_script,
        )
        return
    if not script_exists:
        logger.warning(
            "dispatch: dev_mode enabled but %s does not exist — skipping redirect",
            dev_script,
        )
        return

    # 6. Don't re-exec if we are already running from the dev checkout.
    if os.path.abspath(dev_script) == os.path.abspath(__file__):
        return

    # 7. Replace the current process with the dev copy.
    logger.info("dispatch: dev_mode redirect — re-execing from %s", dev_script)
    env = dict(os.environ)
    env[_DEV_MODE_ENV] = "1"
    python_path_parts = [dev_path]
    existing_pp = env.get("PYTHONPATH", "")
    if existing_pp:
        python_path_parts.append(existing_pp)
    env["PYTHONPATH"] = os.pathsep.join(python_path_parts)
    try:
        os.execve(sys.executable, [sys.executable, dev_script, *sys.argv[1:]], env)
    except (OSError, PermissionError) as e:
        logger.warning(
            "dispatch: dev_mode re-exec failed (%s) — continuing with installed plugin",
            e,
        )














def _rerun_marker_path() -> Path:
    """Path of the rerun-request marker, always a sibling of the current lock.

    Derived at call time (not import time) so tests that patch
    ``_MUTEX_LOCK_PATH`` automatically redirect the marker too.
    """
    return Path(_MUTEX_LOCK_PATH).with_suffix(".rerun")


def _rerun_scope_from_argv(argv: List[str]) -> Optional[str]:
    """Intended dispatch scope of an invocation that lost the lock race.

    Returns the resolved repo path the dropped invocation would have processed,
    ``_RERUN_GLOBAL_SCOPE`` for a registry sweep, or None for invocations that
    must NOT be rerun by the lock holder (--dry-run / --history / --self-test:
    read-only or explicitly no-mutate, and --history/--self-test output would
    go to the wrong process anyway).
    """
    for a in argv:
        if a in ("--dry-run", "--history", "--self-test") or a.startswith(
            ("--history=", "--dry-run=", "--self-test=")
        ):
            return None
    raw: Optional[str] = None
    for i, a in enumerate(argv):
        if a == "--repo" and i + 1 < len(argv):
            raw = argv[i + 1]
            break
        if a.startswith("--repo="):
            raw = a.split("=", 1)[1]
            break
    try:
        if raw:
            # Mirror _main_inner()'s resolution NOW, in the dropping process:
            # the holder has a different cwd, so a relative path or slug must
            # be pinned to an absolute path before it is recorded.
            return _resolve_repo_arg(raw) or str(Path(raw).expanduser().resolve())
        return _resolve_repo_from_cwd() or _RERUN_GLOBAL_SCOPE
    except Exception as e:  # never let scope resolution break the exit path
        logger.warning(
            "dispatch: rerun scope resolution failed (%s) — recording global", e
        )
        return _RERUN_GLOBAL_SCOPE


def _record_rerun_request(scope: str) -> None:
    """Append ``scope`` to the rerun marker (best-effort, O_APPEND atomic line)."""
    try:
        with open(_rerun_marker_path(), "a", encoding="utf-8") as f:
            f.write(scope + "\n")
    except OSError as e:
        logger.warning("dispatch: could not record rerun request: %s", e)


def _consume_rerun_requests() -> List[str]:
    """Read + delete the rerun marker; return recorded scopes deduped in order."""
    marker = _rerun_marker_path()
    try:
        lines = marker.read_text(encoding="utf-8").splitlines()
        marker.unlink()
    except FileNotFoundError:
        return []
    except OSError as e:
        logger.warning("dispatch: could not consume rerun marker: %s", e)
        return []
    return list(dict.fromkeys(s.strip() for s in lines if s.strip()))


def _drain_rerun_requests() -> None:
    """Run extra dispatch passes for scopes dropped while we held the lock.

    Called by the lock holder after its own pass, before releasing. Drains the
    rerun marker until it is EMPTY so a burst of near-simultaneous completions is
    fully handed off in this lock cycle instead of stalling until the next cron
    tick (issue #1235). Bounded by a wall-clock budget
    (``_RERUN_DRAIN_BUDGET_SECS``, a fraction of the #1115 watchdog window) and a
    hard round cap (``_RERUN_MAX_PASSES``, a non-Unix safety net); whichever
    trips first leaves any residual marker for the next tick and logs it so the
    stall stays visible.
    """
    start = time.monotonic()
    passes = 0
    while passes < _RERUN_MAX_PASSES:
        if time.monotonic() - start >= _RERUN_DRAIN_BUDGET_SECS:
            break
        scopes = _consume_rerun_requests()
        if not scopes:
            return
        passes += 1
        for scope in scopes:
            logger.info(
                "dispatch: rerun requested while lock was held — extra pass (scope=%s)",
                scope,
            )
            try:
                _main_inner([] if scope == _RERUN_GLOBAL_SCOPE else ["--repo", scope])
            except Exception as e:
                logger.error("dispatch: rerun pass failed for scope %s: %s", scope, e)
    if _rerun_marker_path().exists():
        logger.warning(
            "dispatch: rerun marker still present after draining (%d passes) — "
            "leaving it for the next tick",
            passes,
        )


def _resolve_plugin_version() -> str:
    """Return the plugin version from ``plugin.yaml``, or ``"unknown"``.

    Read-only and never raises (issue #1328): any failure — missing/unreadable
    manifest, no ``version:`` field, malformed line — resolves to ``"unknown"``
    so the ``--version`` path always exits 0. Parses the single ``version:`` line
    directly (no YAML dependency) to keep this path dependency-free.
    """
    try:
        for line in (
            (_PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines()
        ):
            stripped = line.strip()
            if stripped.startswith("version:"):
                value = stripped.split(":", 1)[1].strip().strip("\"'")
                return value or "unknown"
    except Exception:
        pass
    return "unknown"


def main() -> int:
    """Process-level mutex wrapper.

    Acquires a FileLock with timeout=0 (non-blocking). If another instance
    holds the lock, records a rerun request in a marker file (issue #1160) so
    the holder runs the dropped scope before releasing, then exits cleanly
    (rc=0) to prevent concurrent dispatchers on the same host. Otherwise calls
    _main_inner() for the actual dispatch logic and drains any rerun requests
    recorded while the lock was held.

    A SIGALRM watchdog (Unix only) force-exits after _LOCK_WATCHDOG_SECS so a
    stuck tick cannot starve queued advance-hook invocations for hours (#1115).
    """
    # --version is a read-only report (issue #1328): print the plugin version and
    # exit WITHOUT acquiring the process mutex or running a dispatch tick. Handled
    # in _main_inner so --help lists the flag alongside the argparse definition.
    if "--version" in sys.argv[1:]:
        return _main_inner()

    lock = FileLock(_MUTEX_LOCK_PATH)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        scope = _rerun_scope_from_argv(sys.argv[1:])
        if scope is not None:
            _record_rerun_request(scope)
            logger.warning(
                "FileLock already held by another dispatcher process at %s — "
                "recorded rerun request (scope=%s); the lock holder will run "
                "another pass before releasing (issue #1160).",
                _MUTEX_LOCK_PATH,
                scope,
            )
        else:
            logger.warning(
                "FileLock already held by another dispatcher process at %s — exiting cleanly. "
                "(This is expected when two cron ticks land on top of each other.)",
                _MUTEX_LOCK_PATH,
            )
        return 0

    _t0 = time.monotonic()

    # SIGALRM watchdog — only supported on Unix and only settable from the main
    # thread. Tests that call main() from worker threads must not set signal handlers.
    _watchdog_armed = (
        hasattr(signal, "SIGALRM")
        and threading.current_thread() is threading.main_thread()
    )
    if _watchdog_armed:

        def _watchdog(signum, frame):
            elapsed_min = (time.monotonic() - _t0) / 60
            logger.warning(
                "dispatch: WATCHDOG — lock held %.0f of %d minutes — "
                "force-exiting so queued advance hooks can proceed (issue #1115)",
                elapsed_min,
                _LOCK_WATCHDOG_SECS // 60,
            )
            try:
                lock.release()  # idempotent: finally block will attempt again safely
            except Exception:
                pass
            sys.exit(
                1
            )  # non-zero: watchdog trip is an operational event, not clean exit

        signal.signal(signal.SIGALRM, _watchdog)
        signal.alarm(_LOCK_WATCHDOG_SECS)

    try:
        rc = _main_inner()
        # Serve dispatches dropped while we held the lock (issue #1160) BEFORE
        # releasing, so a session-end advance that collided with this tick
        # lands now instead of waiting for the next cron tick. Still inside
        # the watchdog window, so total hold time stays bounded (#1115).
        _drain_rerun_requests()
        return rc
    finally:
        if _watchdog_armed:
            signal.alarm(0)  # cancel watchdog before releasing lock
        try:
            lock.release()  # idempotent: watchdog may have already released above
        except Exception:
            pass  # best-effort cleanup on shutdown


# _sweep_exit_code → moved to core/dispatch/cli_helpers.py (issue #1153 PR 4/4)


def _main_inner(argv: Optional[List[str]] = None) -> int:
    """Cron / single-repo entrypoint.

    With --repo <path-or-slug>: resolves that single repo (a filesystem path or a
    registered ``owner/repo`` VCS identifier and calls run() for it.

    Without --repo: auto-scopes to the registered project containing cwd (set by a
    cron's ``--workdir`` or a kanban worker's working dir) so a cron/hook/webhook
    tick processes only its own project (issue #137). Only when cwd is outside
    every registered project does it fall back to the legacy registry sweep —
    resolving each via ConfigLoader, calling run(), and aggregating per-repo
    summaries into a human Slack message.

    Returns 0 on success (including partial failure and no-op ticks) and 1 when
    at least one project ran and every project errored, so cron mail-on-error /
    CI gates can detect a total dispatch failure (issue #1112). --self-test and
    --history keep their own exit codes.
    """
    import argparse

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    parser = argparse.ArgumentParser(
        description="Daedalus dispatch — sweep registered repos or run a single one."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log intended actions without mutating anything.",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Run dispatch for a single repo path (skips the registry sweep).",
    )
    parser.add_argument(
        "--history",
        nargs="?",
        const=10,
        type=int,
        default=None,
        metavar="N",
        help="Print the last N dispatch-history entries (default 10) and exit.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the plugin version (from plugin.yaml) and exit 0, without "
        "running a dispatch tick.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run an offline pipeline self-test (seeds fake "
        "issues/tasks, drives a real tick, asserts state "
        "transitions) without touching real GitHub, then exit.",
    )
    parser.add_argument(
        "--sync-profiles-model",
        action="store_true",
        help="Force sync all *-daedalus profiles to the current global model, "
        "bypassing manual-override skip, then exit.",
    )
    # argv is None for a normal invocation (parse sys.argv); the lock holder
    # passes an explicit argv when rerunning a scope dropped on contention
    # (issue #1160).
    args = parser.parse_args(argv)

    # --version is a read-only report (issue #1328): print the plugin version and
    # exit 0 before any dispatch work. Never raises — _resolve_plugin_version()
    # falls back to "unknown" if the manifest can't be read.
    if args.version:
        print(_resolve_plugin_version())
        return 0

    # --self-test is a hermetic, GitHub-free smoke of the pipeline wiring: seed
    # fake data, drive a real tick, print PASS/FAIL, and exit non-zero on failure
    # so CI can gate on it (issue #900). Runs before any real dispatch work.
    if args.self_test:
        from core import dispatch_selftest

        report = dispatch_selftest.run_selftest(sys.modules[__name__])
        print(report.format())
        return 0 if report.ok else 1

    # --history is a read-only report: print and exit before any dispatch work.
    if args.history is not None:
        n = args.history if args.history and args.history > 0 else 10
        print(_format_history(_read_history(n)))
        return 0

    # --sync-profiles-model is a one-shot operation: sync all *-daedalus profiles
    # to the current global model (bypassing manual-override skip) and exit.
    # This is a standalone admin task, not part of the dispatch pipeline, so it
    # runs before any project dispatch work.
    if args.sync_profiles_model:
        from core.sync_profiles import sync_profiles_to_model
        count, updated = sync_profiles_to_model(force=True)
        if updated:
            print(f"Synced {count} profile(s) to global model:")
            for name in updated:
                print(f"  - {name}")
        else:
            print("No profiles updated (already in sync or no *-daedalus profiles found)")
        return 0

    dry_run = args.dry_run
    if dry_run:
        logger.info(
            "dispatch: DRY RUN — no GitHub status moves, kanban cards, or dispatches"
        )

    loader = ConfigLoader()
    summaries: Dict[str, Dict[str, Any]] = {}
    # Per-project run tally: exit non-zero only when >=1 project ran and all
    # errored (issue #1112). Incremented in both dispatch paths below.
    n_ok = 0
    n_err = 0

    # Scope resolution (issue #137): an explicit --repo (path or VCS slug) wins;
    # otherwise auto-scope to the registered project containing cwd (cron
    # --workdir / worker cwd). Only when neither resolves do we fall back to the
    # legacy all-projects registry sweep, so a cron/hook/webhook tick processes
    # only its own project instead of double-processing every registered repo.
    repo_path: Optional[str] = None
    if args.repo:
        repo_path = _resolve_repo_arg(args.repo)
        if not repo_path:
            logger.warning(
                "dispatch: --repo %s matched no registered project; "
                "treating it as a literal path",
                args.repo,
            )
            repo_path = str(Path(args.repo).expanduser().resolve())
    else:
        repo_path = _resolve_repo_from_cwd()
        if repo_path:
            logger.info("dispatch: scoped to %s (cwd)", repo_path)

    # -- single-repo path ----------------------------------------------------
    if repo_path:
        try:
            resolved = loader.resolve_repo_config(repo_path)
        except Exception as e:
            logger.warning("dispatch: could not resolve %s: %s", repo_path, e)
            return 0
        _maybe_redirect_dev_mode(resolved)
        name = resolved.get("name", repo_path)
        try:
            summaries[name] = run(
                resolved,
                dry_run=dry_run,
                max_dispatch=_resolve_max_dispatch(resolved.get("execution") or {}),
            )
            n_ok += 1
        except Exception as e:
            logger.error("dispatch: run failed for %s: %s", name, e)
            summaries[name] = {"error": str(e)}
            n_err += 1
        if not dry_run:
            _append_history(summaries[name], project=name, resolved=resolved)
        if _notify_project_summary(name, summaries[name], resolved, dry_run=dry_run):
            return _sweep_exit_code(n_ok, n_err)
        try:
            _single_provider = providers.get_provider(resolved)
        except Exception:
            _single_provider = None
        msg = _human_summary(
            summaries, dry_run=dry_run, provider_map={name: _single_provider}
        )
        if msg:
            print(msg)
        return _sweep_exit_code(n_ok, n_err)

    # -- registry sweep ------------------------------------------------------
    repo_paths = registry.list_projects()
    if not repo_paths:
        logger.info("dispatch: registry is empty — nothing to do")
        return 0

    resolved_map: Dict[str, Dict[str, Any]] = {}
    for rp in repo_paths:
        try:
            resolved = loader.resolve_repo_config(rp)
        except FileNotFoundError:
            logger.warning("dispatch: no .hermes/daedalus.yaml in %s — skipping", rp)
            continue
        except Exception as e:
            logger.warning("dispatch: could not resolve %s: %s", rp, e)
            continue
        _maybe_redirect_dev_mode(resolved)
        name = resolved.get("name", rp)
        resolved_map[name] = resolved
        try:
            summaries[name] = run(
                resolved,
                dry_run=dry_run,
                max_dispatch=_resolve_max_dispatch(resolved.get("execution") or {}),
            )
            n_ok += 1
        except Exception as e:
            logger.error("dispatch: run failed for %s: %s", name, e)
            summaries[name] = {"error": str(e)}
            n_err += 1
        if not dry_run:
            _append_history(summaries[name], project=name, resolved=resolved)

    # Projects with cron.notifications self-deliver their summary (multi-target,
    # any platform); the rest flow through stdout, which the no-agent cron
    # delivers to its legacy --deliver target. stdout stays EMPTY on a no-op
    # tick so the cron is silent (no JSON spam). Full detail still goes to
    # stderr via the per-project logger.info above.
    legacy: Dict[str, Dict[str, Any]] = {}
    legacy_providers: Dict[str, Any] = {}
    for name, s in summaries.items():
        r = resolved_map.get(name)
        if r is None or not _notify_project_summary(name, s, r, dry_run=dry_run):
            legacy[name] = s
            try:
                legacy_providers[name] = providers.get_provider(r) if r else None
            except Exception:
                legacy_providers[name] = None
    msg = _human_summary(legacy, dry_run=dry_run, provider_map=legacy_providers)
    if msg:
        print(msg)
    return _sweep_exit_code(n_ok, n_err)


if __name__ == "__main__":
    raise SystemExit(main())

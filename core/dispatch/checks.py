"""core.dispatch.checks — stage-check family extracted from daedalus_dispatch.py.

Moved from scripts/daedalus_dispatch.py (issue #1262 PR 2/2).
The dispatcher re-exports every symbol via ``from core.dispatch.checks import X  # noqa: F401``
so the public surface and all test monkeypatching against the ``disp`` module are unchanged.

Dispatcher-resident callees (notification senders, body builders, dedup helpers that may be
patched on a non-"disp" dispatcher instance) are reached via ``_fn()`` at call time so that
test patches on the spec-loaded dispatcher instance are intercepted correctly regardless of
how the dispatcher was loaded (``sys.modules["disp"]`` fast path or stack-walk slow path).
"""
from __future__ import annotations

import logging
import re
import sys
import types as _types
from typing import Any, Dict, List, Optional

from core import kanban
from core.dispatch.bodies import _pm_consultation_body, _resolve_howtos
from core.iterate.outcomes import parse as _parse_outcome
from core.iterate.outcomes import parse_dict as _parse_outcome_dict
from core.iterate.classify import _ASSIGNEE_TO_ROLE as _ASSIGNEE_TO_ROLE_MAP
from core.dispatch.dedup import (
    _RETRY_CAP_MARKER,
    _has_notified_block as _has_notified_block_impl,
    _mark_notified_block as _mark_notified_block_impl,
)
from core.dispatch.delivery import _build_security_notify_cmds, _validator_summary_burns_cap
from core.dispatch.housekeeping import (
    _fetch_issue_with_retry as _fetch_issue_with_retry_impl,
    _is_issue_closed_cached,
)
from core.dispatch.resolvers import (
    _DEFAULT_PROFILES,
    _delimit_issue_content,
    _resolve_max_developer_retries,
    _resolve_max_planner_retries,
    _resolve_max_pm_retries,
    _resolve_max_validator_retries,
    _unpack_issue,
)
from core.dispatch.stages import (
    _compute_planner_fallback_idempotency_key,
    _downstream_tasks_running_or_done,
)
from core.dispatch.validator_comment import (
    _pm_spec_comment,
    _validator_github_comment_outcome as _validator_github_comment_outcome_impl,
)
from core.util import extract_issue_number, extract_pr_number_from_summary

logger = logging.getLogger("daedalus.dispatch")


# ── dispatcher-routing helpers ────────────────────────────────────────────────


def _kanban():
    """Return the active kanban module, routing through the dispatcher for test isolation.

    When a test or self-test replaces ``disp.kanban`` with an in-memory double
    via ``disp.kanban = board``, functions in checks.py must see that replacement.
    ``_kanban()`` asks ``_disp()`` for the ``kanban`` attribute, falling back to
    the module-level ``kanban`` import if no dispatcher is active.
    """
    d = _disp()
    if d is not None:
        k = getattr(d, "kanban", None)
        if k is not None:
            return k
    return kanban


def _disp():
    """Return the active dispatcher module for call-time patch interception.

    Priority 1 — stack walk: finds the *closest* dispatcher in the call stack.
    This ensures test patches applied to a specific dispatcher instance
    (e.g. ``mock.patch.object(self.disp, ...)``) are found even when a second
    dispatcher instance is registered in ``sys.modules["disp"]`` (e.g. the
    conftest-loaded copy that exists alongside a test-specific ``disp_1161``).

    The stack walk recognises three patterns per frame:
      - ``f_locals``: a ``types.ModuleType`` with ``_run_tick`` as a *local* variable
        (pattern: ``disp = _load_dispatch()`` inside the test function)
      - ``f_locals``: the ``.disp`` attribute on a TestCase instance
        (unittest-style: ``self.disp = _load_dispatch()``)
      - ``f_globals["disp"]``: module-level dispatcher variable in the same
        test file (pattern: ``disp = _load_dispatch()`` at module scope — very
        common in test files like ``test_planner_retry_1125.py``). This is a
        targeted O(1) lookup, not a full globals scan.

    ``_run_tick`` is unique to ``daedalus_dispatch.py`` — no other module in the
    codebase defines it — so the heuristic is safe.

    Priority 2 — ``sys.modules`` fallback: covers production (``"__main__"``)
    and conftest-only tests that never store the module in a local variable.

    The stack walk is unbounded — it ascends every frame until the stack root
    (``frame.f_back is None``) before falling through to the ``sys.modules``
    fallback; the ``except Exception: pass`` guard ensures any
    ``AttributeError`` or ``SystemError`` from frame introspection does not
    propagate.
    """
    try:
        frame = sys._getframe(1)
        while frame is not None:
            # Check locals first (inline disp variable, or self.disp in unittest).
            for val in frame.f_locals.values():
                if isinstance(val, _types.ModuleType) and hasattr(val, "_run_tick"):
                    return val
                if hasattr(val, "disp"):
                    cand = getattr(val, "disp", None)
                    if isinstance(cand, _types.ModuleType) and hasattr(cand, "_run_tick"):
                        return cand
            # Check for a module-level ``disp`` in the frame's globals (test files
            # that do ``disp = _load_dispatch()`` at module scope — local to THAT
            # file so not overwritten when another test file loads a new dispatcher).
            g_disp = frame.f_globals.get("disp")
            if isinstance(g_disp, _types.ModuleType) and hasattr(g_disp, "_run_tick"):
                return g_disp
            frame = frame.f_back
    except Exception:
        pass
    # sys.modules fallback: test conftest registers as "disp"; production runs
    # as "__main__".
    for key in ("disp", "__main__"):
        m = sys.modules.get(key)
        if m is not None and hasattr(m, "_run_tick"):
            return m
    return None


def _fn(name: str, fallback=None):
    """Return a function by name from the active dispatcher (for test-patch routing).

    When test code calls ``mock.patch.object(self.disp, "func_name", mock_fn)``,
    the patch replaces ``self.disp.func_name``.  Since checks.py functions call
    each other directly (not via a dispatcher attribute), the patch would be
    silently bypassed.  ``_fn(name, fallback)`` resolves the function through
    ``_disp()`` at call time so any patch applied to the active dispatcher
    instance is intercepted correctly.
    """
    d = _disp()
    if d is not None:
        fn = getattr(d, name, None)
        if fn is not None:
            return fn
    return fallback


# ── pure query helpers ────────────────────────────────────────────────────────


def _get_task_summary(task: Dict[str, Any], slug: str) -> str:
    """Return a task's latest summary, falling back to ``show_card``.

    ``hermes kanban list --json`` omits per-card summaries, so when the listed
    task carries no inline ``summary``/``last_summary`` we fetch the card and
    read ``latest_summary``. Mirrors the inline fallback previously copy-pasted
    into ``_pm_task_state``, ``_check_confirmed_validators`` and
    ``_check_completed_pm``.
    """
    summary_raw = (task.get("summary") or task.get("last_summary") or "").strip()
    if not summary_raw:
        tid = (task.get("id") or task.get("task_id") or "").strip()
        if tid:
            card = _kanban().show_card(slug, tid) or {}
            summary_raw = (card.get("latest_summary") or "").strip()
    return summary_raw


def _pm_task_state(
    slug: str, issue_number: int, pm_profile: str = "project-manager-daedalus"
) -> tuple:
    """Return (state, stale_count) for PM spec tasks for issue_number.

    state values:
      'none'     — no PM spec task found
      'running'  — at least one PM spec task is not yet done
      'complete' — a done PM spec task has a valid SPEC: summary
      'stale'    — all done PM spec tasks lack SPEC: (hermes premature-completion bug)

    stale_count is the number of done PM tasks without SPEC:, used to generate
    unique retry idempotency keys (pm-{n}-r{stale_count}).
    """
    pattern = f"#{issue_number}"
    has_running = False
    has_complete = False
    stale_count = 0
    for t in _kanban().list_tasks(slug):
        if pattern not in (t.get("title") or ""):
            continue
        if (t.get("assignee") or "").strip() != pm_profile:
            continue
        if (t.get("title") or "").lower().startswith("consult:"):
            continue
        status = (t.get("status") or "").lower()
        if status not in ("done", "complete", "completed"):
            has_running = True
            continue
        summary_raw = _get_task_summary(t, slug)
        s = summary_raw.lower()
        if s.startswith("spec:") or s.startswith("assigned:"):
            has_complete = True
        else:
            stale_count += 1
    if has_running:
        return ("running", stale_count)
    if has_complete:
        return ("complete", stale_count)
    if stale_count:
        return ("stale", stale_count)
    return ("none", 0)


def _has_pm_tasks(
    slug: str, issue_number: int, pm_profile: str = "project-manager-daedalus"
) -> bool:
    """Shim for backward compatibility — returns True if a non-stale PM spec task exists."""
    state, _ = _fn("_pm_task_state", _pm_task_state)(slug, issue_number, pm_profile)
    return state in ("running", "complete")


def _developer_task_state(
    slug: str, issue_number: int, developer_profile: str = "developer-daedalus"
) -> tuple:
    """Return (state, stale_count) for developer tasks for issue_number.

    state values:
      'none'     — no developer task found
      'running'  — at least one developer task is not yet done
      'complete' — a done developer task has a PR number in its summary
      'stale'    — all done developer tasks lack a PR number (empty summary,
                   agent crash, context-limit dropout)

    stale_count is the number of done developer tasks without a PR number,
    used to generate unique retry idempotency keys (developer-{n}-r{stale_count}).
    """
    pattern = f"#{issue_number}"
    has_running = False
    has_complete = False
    stale_count = 0
    for t in _kanban().list_tasks(slug):
        if pattern not in (t.get("title") or ""):
            continue
        if (t.get("assignee") or "").strip() != developer_profile:
            continue
        status = (t.get("status") or "").lower()
        if status not in ("done", "complete", "completed"):
            has_running = True
            continue
        summary_raw = _get_task_summary(t, slug)
        if extract_pr_number_from_summary(summary_raw) is not None:
            has_complete = True
        else:
            stale_count += 1
    if has_running:
        return ("running", stale_count)
    if has_complete:
        return ("complete", stale_count)
    if stale_count:
        return ("stale", stale_count)
    return ("none", 0)


def _has_downstream_tasks(
    slug: str,
    issue_number: int,
    *,
    validator_profile: str = "validator-daedalus",
    pm_profile: str = "project-manager-daedalus",
    planner_profile: str = "planner-daedalus",
) -> bool:
    """Return True if any non-validator, non-PM, non-planner kanban task exists for issue_number.

    Used by _check_completed_pm to avoid creating duplicate team triage cards.
    Planner tasks are upstream dispatch artifacts, not downstream team tasks.
    """
    pattern = f"#{issue_number}"
    pipeline_profiles = {validator_profile, pm_profile, planner_profile}
    # Status-blind guard (epic #1008): ignore terminal states so stale
    # completed/cancelled downstream cards never block a fresh triage dispatch.
    terminal_statuses = {
        "done",
        "complete",
        "completed",
        "cancelled",
        "canceled",
        "archived",
    }
    for t in _kanban().list_tasks(slug):
        if pattern not in (t.get("title") or ""):
            continue
        assignee = (t.get("assignee") or "").strip()
        if assignee in pipeline_profiles:
            # Validator/PM/planner cards are upstream dispatch artifacts, not
            # downstream work. Even a stale planner card must not count.
            continue
        status = (t.get("status") or "").strip().lower()
        if status in terminal_statuses:
            continue  # epic #1008: terminal downstream tasks are invisible
        return True  # active downstream / triage card exists
    return False


def _has_active_pm_consultation(
    slug: str, issue_number: int, pm_profile: str = "project-manager-daedalus"
) -> bool:
    """Return True if there is already an ACTIVE PM consultation for issue_number.

    Status-blind guard (epic #1008): only consultations in non-terminal states
    count. The idempotency key on create_task is the primary runaway-prevention
    guard; this function only needs to detect in-flight consultations so that we
    don't spawn a duplicate subprocess call on the same tick. Archived
    consultations are not returned by list_tasks, so they are excluded
    automatically.
    """
    pattern = f"#{issue_number}"
    terminal_statuses = {
        "done",
        "complete",
        "completed",
        "cancelled",
        "canceled",
        "archived",
    }
    for t in _kanban().list_tasks(slug):
        title = t.get("title") or ""
        if pattern not in title:
            continue
        if (t.get("assignee") or "").strip() != pm_profile:
            continue
        if not title.lower().startswith("consult:"):
            continue
        status = (t.get("status") or "").strip().lower()
        if status in terminal_statuses:
            continue  # epic #1008: terminal consultations do not block new ones
        return True
    return False


# ── self-heal helpers ─────────────────────────────────────────────────────────


def _try_adopt_pm_spec_comment(
    slug: str,
    issue_number: int,
    pm_profile: str,
    provider,
    *,
    dry_run: bool = False,
) -> bool:
    """Self-heal a stale PM card by adopting the issue's spec comment (#1161).

    Rewrites the newest done PM card lacking a SPEC:/assigned: summary so that
    _check_completed_pm sees a SPEC: outcome and fans out the team — instead of
    retrying PM to the cap and stalling for manual `hermes kanban edit` recovery.
    Returns True when adopted (caller must skip retry and cap notification).
    """
    head = _pm_spec_comment(provider, issue_number, pm_profile)
    if not head:
        return False
    if dry_run:
        logger.info(
            "[dry-run] PM card for #%s lacks SPEC: but issue has an Implementation "
            "Spec comment — would adopt it as the card summary",
            issue_number,
        )
        return True
    # Newest done PM spec card without SPEC:/assigned: (same filter as _pm_task_state).
    pattern = f"#{issue_number}"
    target_tid = ""
    for t in _kanban().list_tasks(slug):
        if pattern not in (t.get("title") or ""):
            continue
        if (t.get("assignee") or "").strip() != pm_profile:
            continue
        if (t.get("title") or "").lower().startswith("consult:"):
            continue
        if (t.get("status") or "").lower() not in ("done", "complete", "completed"):
            continue
        s = _get_task_summary(t, slug).lower()
        if s.startswith("spec:") or s.startswith("assigned:"):
            continue
        tid = str(t.get("id") or t.get("task_id") or "")
        if tid:
            target_tid = tid  # list order is creation order — keep the newest
    if not target_tid:
        return False
    if not _kanban().edit_summary(
        slug, target_tid, f"SPEC: (adopted from issue comment) {head}"
    ):
        return False
    logger.warning(
        "dispatch: PM card %s for #%s lacked SPEC: but issue has an Implementation "
        "Spec comment — auto-adopted it as the card summary (#1161)",
        target_tid,
        issue_number,
    )
    return True


def _try_adopt_developer_pr(
    slug: str,
    issue_number: int,
    developer_profile: str,
    provider,
    *,
    base_branch: str = "",
    dry_run: bool = False,
) -> bool:
    """Self-heal a stale developer card by adopting its in-flight PR (#1164).

    A developer session can open a PR and then die before writing its card
    summary (hermes premature completion). ``_check_completed_developer`` saw
    only the empty summary and minted a retry developer, which opened a
    duplicate PR (#1160 → #1163, #1161 → #1165). Instead, when the provider
    already reports an open/merged PR for the issue, rewrite the newest stale
    developer card's summary to ``review-required: PR #N`` so the normal
    reviewer/QA flow proceeds against the existing PR.

    A pushed branch without a PR is deliberately NOT adopted — it is not a
    completion, and the retry developer can reuse the branch.

    Security (#1168): validates the PR is NOT from a fork and (when
    ``base_branch`` is supplied) targets the correct base branch before
    adopting. An outsider could otherwise open a PR with ``Closes #42`` or a
    branch named ``fix/issue-42-evil`` and have the dispatcher adopt it into
    the daedalus pipeline. Fork PRs and base-branch mismatches are rejected
    so the retry path runs instead.

    Returns True when adopted (caller must skip retry and cap notification).
    Provider errors are treated as "no PR" so the tick never crashes and the
    existing retry behavior remains the fallback.
    """
    if provider is None:
        return False
    try:
        pr = provider._pr_for_issue(issue_number)
    except Exception as exc:
        logger.warning(
            "dispatch: _pr_for_issue(#%s) raised %s — falling back to developer retry",
            issue_number,
            exc,
        )
        return False
    if not pr or not pr.number:
        return False
    # Security: reject fork PRs — only adopt PRs from the canonical repo.
    if getattr(pr, "is_fork", False):
        logger.warning(
            "dispatch: PR #%s for issue #%s is from a fork — rejecting "
            "adoption, falling back to developer retry (#1168)",
            pr.number,
            issue_number,
        )
        return False
    # Security: validate the PR targets the expected base branch.
    if base_branch and pr.base_branch and pr.base_branch != base_branch:
        logger.warning(
            "dispatch: PR #%s for issue #%s targets base %r, expected %r — "
            "rejecting adoption, falling back to developer retry (#1168)",
            pr.number,
            issue_number,
            pr.base_branch,
            base_branch,
        )
        return False
    if dry_run:
        logger.info(
            "[dry-run] developer stale #%s but PR #%s exists — would adopt it "
            "as the card summary instead of retrying",
            issue_number,
            pr.number,
        )
        return True
    # Newest done developer card without a PR reference (same filter as
    # _developer_task_state). Use extract_issue_number instead of substring
    # matching so a task for issue #420 is NOT adopted as a target for #42
    # (substring bug, #1168).
    target_tid = ""
    for t in _kanban().list_tasks(slug):
        title = t.get("title") or ""
        # Exact issue-number match — not a substring check.
        if extract_issue_number(title) != issue_number:
            continue
        if (t.get("assignee") or "").strip() != developer_profile:
            continue
        if (t.get("status") or "").lower() not in ("done", "complete", "completed"):
            continue
        if extract_pr_number_from_summary(_get_task_summary(t, slug)) is not None:
            continue
        tid = str(t.get("id") or t.get("task_id") or "")
        if tid:
            target_tid = tid  # list order is creation order — keep the newest
    if not target_tid:
        return False
    if not _kanban().edit_summary(
        slug,
        target_tid,
        f"review-required: PR #{pr.number} (adopted from provider state — "
        f"developer completed with empty summary, #1164)",
    ):
        return False
    logger.warning(
        "dispatch: developer card %s for #%s completed with no PR in summary "
        "but provider shows PR #%s — auto-adopted it as the card summary (#1164)",
        target_tid,
        issue_number,
        pr.number,
    )
    return True


# ── stage-recovery helper ─────────────────────────────────────────────────────


def _retry_cap_stage_recovered(
    slug: str,
    issue_number: int,
    role: str,
    *,
    profiles: Dict[str, str] | None = None,
    provider=None,
) -> bool:
    """Return True when the stage has recovered and should NOT be notified (#1167).

    A stage is considered recovered when:
    - A newer card for the same issue+role is running or complete-with-PR/summary.
    - For role="developer": an open PR referencing the issue exists (even a fork PR
      or base-mismatch proves the stage isn't stalled — the developer did produce output).
    - For role="developer": a downstream role card (QA/reviewer) is running or done.

    Provider errors during the PR check fail open to "not recovered" (better one
    duplicate alert than a silently swallowed real one).
    """
    p = profiles or {}
    pattern = f"#{issue_number}"

    if role == "developer":
        dev_profile = p.get("developer", "developer-daedalus")
        dev_state, _ = _fn("_developer_task_state", _developer_task_state)(
            slug, issue_number, dev_profile
        )
        if dev_state in ("running", "complete"):
            return True
        # Check for downstream role cards (QA, reviewer) that are running or done.
        if _downstream_tasks_running_or_done(
            slug,
            issue_number,
            (p.get("qa", "qa-daedalus"), p.get("reviewer", "reviewer-daedalus")),
        ):
            return True
        # Check for an open PR for the issue (provider lookup).
        if provider is not None:
            try:
                pr = provider._pr_for_issue(issue_number)
                if pr and pr.number:
                    logger.info(
                        "dispatch: _retry_cap_stage_recovered: developer PR #%s "
                        "exists for #%s — suppressing retry-cap notification",
                        pr.number,
                        issue_number,
                    )
                    return True
            except Exception as exc:
                logger.warning(
                    "dispatch: _retry_cap_stage_recovered: _pr_for_issue(#%s) "
                    "raised %s — failing open (not recovered)",
                    issue_number,
                    exc,
                )

    elif role == "pm":
        pm_profile = p.get("pm", "project-manager-daedalus")
        pm_state, _ = _fn("_pm_task_state", _pm_task_state)(
            slug, issue_number, pm_profile
        )
        if pm_state in ("running", "complete"):
            return True
        # Check for downstream role cards (developer) that are running or done.
        if _downstream_tasks_running_or_done(
            slug,
            issue_number,
            (p.get("developer", "developer-daedalus"),),
        ):
            return True

    elif role == "validator":
        val_profile = p.get("validator", "validator-daedalus")
        # Check for validator cards that are running (not stale/done-empty).
        for t in _kanban().list_tasks(slug):
            if pattern not in (t.get("title") or ""):
                continue
            if (t.get("assignee") or "").strip() != val_profile:
                continue
            status = (t.get("status") or "").lower()
            if status == "running":
                return True
            if status in ("done", "complete", "completed"):
                summary = _get_task_summary(t, slug).lower()
                if summary.startswith("confirmed"):
                    return True
        # Check for downstream role cards (PM, developer) that are running or done.
        if _downstream_tasks_running_or_done(
            slug,
            issue_number,
            (
                p.get("pm", "project-manager-daedalus"),
                p.get("developer", "developer-daedalus"),
            ),
        ):
            return True

    return False


# ── validator phase ───────────────────────────────────────────────────────────


def _check_confirmed_validators(
    slug: str,
    repo: str,
    issues_map: Dict[int, Dict[str, Any]],
    iterations: int,
    workdir: str,
    notify_target: str,
    base_branch: str,
    provider_name: str,
    security_notify_targets: Optional[List[str]] = None,
    label_overrides: Optional[Dict[str, Any]] = None,
    profiles: Optional[Dict[str, str]] = None,
    role_skills: Optional[Dict[str, List[str]]] = None,
    coding_agent: str = "none",
    coding_agent_cmd: str = "",
    role_agents: Optional[Dict[str, str]] = None,
    *,
    dry_run: bool = False,
    provider=None,
    resolved: Optional[Dict[str, Any]] = None,
    closed_issue_cache: Optional[Dict[int, Optional[bool]]] = None,
) -> List[int]:
    """Phase-2 trigger: for every validator task completed with 'CONFIRMED:' summary,
    create a PM task to write the spec + acceptance criteria.

    Runs each tick so the PM phase starts as soon as the validator completes.
    Idempotency via 'pm-{n}' key prevents duplicate PM cards.
    """
    p = profiles or _DEFAULT_PROFILES
    rs = role_skills or {}
    triggered: List[int] = []
    # Per-tick memo caches keyed by issue number. A single issue with many done
    # validator tasks (e.g. 13 retry rounds from a runaway loop) otherwise re-fetched
    # the same issue + comments once per *task*, burning O(tasks) API calls and
    # exhausting rate limits before the dispatcher reached Ready issues (#961).
    # Both calls depend only on the issue number, so memoizing collapses them to
    # O(unique issues) with no behavior change. The cache lives for one function
    # call, so cross-tick freshness is unchanged.
    _issue_fetch_cache: Dict[int, Any] = {}
    _gh_outcome_cache: Dict[int, str] = {}
    # Shared across all advancement functions in one tick when passed from run() (#1115).
    _closed_issue_cache: Dict[int, Optional[bool]] = (
        closed_issue_cache if closed_issue_cache is not None else {}
    )

    # Resolve patched/dispatcher-resident callees once at function entry.
    d = _disp()
    _fetch_issue = (
        getattr(d, "_fetch_issue_with_retry", _fetch_issue_with_retry_impl)
        if d else _fetch_issue_with_retry_impl
    )
    _gh_outcome_fn = (
        getattr(d, "_validator_github_comment_outcome", _validator_github_comment_outcome_impl)
        if d else _validator_github_comment_outcome_impl
    )
    _has_notified = (
        getattr(d, "_has_notified_block", _has_notified_block_impl)
        if d else _has_notified_block_impl
    )
    _mark_notified = (
        getattr(d, "_mark_notified_block", _mark_notified_block_impl)
        if d else _mark_notified_block_impl
    )
    _send_retry_cap = getattr(d, "_send_retry_cap_notification", None) if d else None
    _send_retry_attempt = getattr(d, "_send_retry_attempt_notification", None) if d else None
    _notify_blocked = getattr(d, "_notify_validator_blocked", None) if d else None
    _rsr = (
        getattr(d, "_retry_cap_stage_recovered", _retry_cap_stage_recovered)
        if d else _retry_cap_stage_recovered
    )
    _pm_state_fn = (
        getattr(d, "_pm_task_state", _pm_task_state) if d else _pm_task_state
    )
    _pm_body_fn = getattr(d, "_pm_body", None) if d else None
    _validator_body_fn = getattr(d, "_validator_body", None) if d else None

    def _fetch_issue_cached(num: int):
        if num not in _issue_fetch_cache:
            _issue_fetch_cache[num] = (
                _fetch_issue(provider, num) if provider is not None else None
            )
        return _issue_fetch_cache[num]

    def _gh_outcome_cached(num: int) -> str:
        if num not in _gh_outcome_cache:
            _gh_outcome_cache[num] = _gh_outcome_fn(
                provider, num, p["validator"]
            )
        return _gh_outcome_cache[num]

    for task in _kanban().list_tasks(slug, status="done"):
        if (task.get("assignee") or "").strip() != p["validator"]:
            continue
        # `hermes kanban list --json` omits the summary; fetch it from show.
        # Fall back to inline fields so unit-test mocks that stub list_tasks
        # with summary pre-populated still work without calling show_card.
        summary_raw = _get_task_summary(task, slug)
        summary = summary_raw.lower()
        if not summary.startswith("confirmed"):
            # Non-CONFIRMED validator done cards: re-triage instead of silent drop.
            n_nr = extract_issue_number(task.get("title") or "")
            if n_nr is None:
                continue
            # Skip stale tasks for closed/unknown issues (issues #1115, #1120).
            if (
                _is_issue_closed_cached(provider, n_nr, _closed_issue_cache)
                is not False
            ):
                logger.debug(
                    "dispatch: skipping done validator task for closed/unknown issue #%s",
                    n_nr,
                )
                continue
            if summary.startswith("escalate:"):
                # Security/harm escalation — existing human escalation path, skip silently.
                continue
            if summary.startswith("blocked:"):
                # Validator couldn't proceed with a blocking issue — PM consultation.
                issue_nt = issues_map.get(n_nr)
                if not issue_nt and provider is not None:
                    fetched = _fetch_issue_cached(n_nr)
                    if fetched:
                        issue_nt = fetched.as_dict()
                        logger.info(
                            "dispatch: validator BLOCKED #%s — not in issues_map window, "
                            "fetched directly from provider",
                            n_nr,
                        )
                if issue_nt:
                    # An in-flight consultation already covers this block — don't
                    # spawn a duplicate while the PM is still working it. Without
                    # this guard the incrementing key below would mint a fresh
                    # -rN consultation on every tick until the PM finishes (#994).
                    if _has_active_pm_consultation(slug, n_nr, p["pm"]):
                        continue
                    if dry_run:
                        logger.info(
                            "[dry-run] validator BLOCKED #%s — would create PM consultation",
                            n_nr,
                        )
                        triggered.append(n_nr)
                        continue
                    blocker_text = summary_raw
                    # Incrementing idempotency key per block cycle (#994). A static
                    # key silenced repeat blocks: once the first consultation was
                    # done, create_task matched it and returned None, so a second
                    # validator block created no consultation and the issue stalled
                    # with no human notification. Count prior consultations for this
                    # issue (done or in any state) and suffix -rN so each block cycle
                    # gets a distinct key: validator-blocked-42, validator-blocked-42-r1, …
                    base_key = f"validator-blocked-{n_nr}"
                    block_count = sum(
                        1
                        for t in _kanban().list_tasks(slug)
                        if (t.get("idempotency_key") or "") == base_key
                        or (t.get("idempotency_key") or "").startswith(f"{base_key}-r")
                    )
                    ikey = (
                        base_key if block_count == 0 else f"{base_key}-r{block_count}"
                    )
                    cid = _kanban().create_task(
                        slug,
                        f"consult: #{n_nr} {issue_nt.get('title', '')}",
                        body=_pm_consultation_body(
                            repo,
                            issue_nt,
                            f"Validator blocked: {blocker_text}",
                            workdir,
                            provider_name,
                        ),
                        assignee=p["pm"],
                        idempotency_key=ikey,
                        workspace=f"dir:{workdir}" if workdir else "",
                        skills=rs.get("pm") or None,
                    )
                    if cid:
                        logger.info(
                            "dispatch: validator BLOCKED #%s — PM consultation %s (key=%s)",
                            n_nr,
                            cid,
                            ikey,
                        )
                        triggered.append(n_nr)
                        if _notify_blocked is not None:
                            _notify_blocked(
                                n_nr,
                                issue_nt.get("title", ""),
                                blocker_text,
                                block_count + 1,
                                resolved or {},
                                dry_run=dry_run,
                            )
                continue
            if summary.startswith("stop:"):
                # Validator marked duplicate/already-fixed/cannot-reproduce.
                # Idempotency key: only close if we haven't already processed this issue.
                ikey = f"validator-stop-closed-{n_nr}"
                already_handled = any(
                    (t.get("idempotency_key") or "") == ikey
                    for t in _kanban().list_tasks(slug)
                )
                if already_handled:
                    triggered.append(n_nr)
                    continue
                if provider is None:
                    logger.warning(
                        "dispatch: validator STOP #%s but no provider — cannot auto-close",
                        n_nr,
                    )
                    continue
                if dry_run:
                    logger.info(
                        "dispatch: [dry-run] would auto-close issue #%s (validator STOP)",
                        n_nr,
                    )
                    triggered.append(n_nr)
                    continue
                # The closed-issue filter at the top of the loop (#1115) ensures
                # we only reach here when the issue is currently open, so
                # close_issue() will not race against an already-closed state.
                if provider.close_issue(n_nr):
                    stop_reason = summary_raw[5:].strip()
                    logger.info(
                        "dispatch: validator done with STOP:%s for #%s — auto-closed issue",
                        stop_reason,
                        n_nr,
                    )
                    # Post an explanatory comment on the closed issue so readers
                    # understand why it was closed (issue #115).
                    comment_body = (
                        f"Auto-closed by STOP: validator — {stop_reason}\n\n"
                        f"The validator determined this issue should not proceed "
                        f"(duplicate / already fixed / cannot reproduce). "
                        f"Reopen if this was a mistake."
                    )
                    try:
                        if not provider.post_issue_comment(n_nr, comment_body):
                            logger.warning(
                                "dispatch: failed to post auto-close comment on #%s — "
                                "issue closed but comment missing",
                                n_nr,
                            )
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.warning(
                            "dispatch: post_issue_comment #%s raised %s — "
                            "issue closed but comment failed",
                            n_nr,
                            exc,
                        )
                    # Mark as handled so we don't re-close on future dispatches.
                    _kanban().create_task(
                        slug,
                        f"validator-stop #{n_nr}",
                        body=f"Issue #{n_nr} auto-closed by validator STOP directive",
                        assignee=p["validator"],
                        idempotency_key=ikey,
                        workspace=f"dir:{workdir}" if workdir else "",
                    )
                else:
                    logger.warning(
                        "dispatch: failed to close issue #%s (validator STOP) - will retry next tick",
                        n_nr,
                    )
                triggered.append(n_nr)
                continue
            # Empty or unrecognized summary — check GitHub comments before retrying.
            # When a validator's context window fills before kanban_complete runs,
            # its GitHub comment is the only record of its decision.
            if not summary:
                logger.warning(
                    "dispatch: validator for #%s completed with no summary — scheduling retry",
                    n_nr,
                )
            issue_nr = issues_map.get(n_nr)
            if not issue_nr and provider is not None:
                fetched = _fetch_issue_cached(n_nr)
                if fetched:
                    issue_nr = fetched.as_dict()
                    logger.info(
                        "dispatch: #%s not in issues_map (gh-comment) — fell back to get_issue()",
                        n_nr,
                    )
            gh_outcome = _gh_outcome_cached(n_nr)
            if gh_outcome == "confirmed" and issue_nr:
                # GitHub comment confirms — advance to PM without another validator run.
                logger.warning(
                    "dispatch: validator #%s kanban summary is None but GitHub comment "
                    "contains CONFIRMED — advancing to PM (github-comment fallback)",
                    n_nr,
                )
                if not dry_run:
                    pm_state, stale_count = _pm_state_fn(slug, n_nr, p["pm"])
                    if pm_state not in ("running", "complete"):
                        # Enforce the same retry cap as the primary PM path (#1104).
                        # Without this guard the github-fallback branch created
                        # unbounded PM tasks — observed: 6 consecutive empty-summary
                        # PM runs for one issue with no cap enforcement.
                        if pm_state == "stale":
                            # Self-heal (#1161): adopt the issue's spec comment
                            # instead of retrying (mirrors the primary path).
                            if _try_adopt_pm_spec_comment(
                                slug, n_nr, p["pm"], provider, dry_run=dry_run
                            ):
                                continue
                            max_pm_retries = _resolve_max_pm_retries(
                                (resolved or {}).get("execution") or {}
                            )
                            absolute_max = max(max_pm_retries * 3, max_pm_retries + 3)
                            if (
                                stale_count >= max_pm_retries
                                or stale_count >= absolute_max
                            ):
                                logger.error(
                                    "dispatch: PM for #%s has %d stale completions "
                                    "(github-fallback) — manual intervention required",
                                    n_nr,
                                    stale_count,
                                )
                                if resolved is not None and not _has_notified(
                                    slug,
                                    n_nr,
                                    validator_profile=p["validator"],
                                    marker=_RETRY_CAP_MARKER,
                                    role="pm",
                                ):
                                    if _rsr(
                                        slug,
                                        n_nr,
                                        "pm",
                                        profiles=p,
                                        provider=provider,
                                    ):
                                        logger.info(
                                            "dispatch: PM retry-cap for #%s suppressed "
                                            "— stage recovered (#1167)",
                                            n_nr,
                                        )
                                    else:
                                        if _send_retry_cap is not None:
                                            _send_retry_cap(
                                                role="pm",
                                                issue_number=n_nr,
                                                retry_count=stale_count,
                                                max_retries=max_pm_retries,
                                                resolved=resolved,
                                                dry_run=dry_run,
                                            )
                                        if not dry_run:
                                            _mark_notified(
                                                slug,
                                                n_nr,
                                                validator_profile=p["validator"],
                                                marker=_RETRY_CAP_MARKER,
                                                role="pm",
                                            )
                                        # Post a GitHub comment only when not suppressed
                                        # by stage recovery (#1167).
                                        if provider is not None and not dry_run:
                                            try:
                                                cap_comment = (
                                                    f"⚠️ **Project Manager retry cap exhausted** "
                                                    f"for issue #{n_nr}\n\n"
                                                    f"The PM has completed {stale_count} times "
                                                    f"(max: {max_pm_retries}) without a SPEC: outcome.\n\n"
                                                    f"**Manual intervention required.**\n\n"
                                                    f"Likely cause: PM agent completed without SPEC: summary "
                                                    f"(context window overflow, agent crash, or silent failure).\n\n"
                                                    f"Recovery: `hermes kanban edit <task-id>` and add `SPEC:` "
                                                    f"summary, or manually requeue with fresh context."
                                                )
                                                if not provider.post_issue_comment(
                                                    n_nr, cap_comment
                                                ):
                                                    logger.warning(
                                                        "dispatch: failed to post retry-cap "
                                                        "comment on #%s (github-fallback)",
                                                        n_nr,
                                                    )
                                            except Exception as exc:
                                                logger.warning(
                                                    "dispatch: post_issue_comment #%s raised %s "
                                                    "— retry-cap comment failed (github-fallback)",
                                                    n_nr,
                                                    exc,
                                                )
                                continue
                            # Under cap — send retry-attempt notification (#287).
                            if resolved is not None and _send_retry_attempt is not None:
                                _send_retry_attempt(
                                    role="pm",
                                    issue_number=n_nr,
                                    retry_count=stale_count,
                                    max_retries=max_pm_retries,
                                    resolved=resolved,
                                    dry_run=dry_run,
                                )
                            logger.warning(
                                "dispatch: PM for #%s completed with no summary "
                                "(github-fallback) — scheduling retry (run %d/%d)",
                                n_nr,
                                stale_count,
                                max_pm_retries,
                            )
                        ikey = (
                            f"pm-{n_nr}"
                            if pm_state == "none"
                            else f"pm-{n_nr}-r{stale_count}"
                        )
                        issue_for_pm = issue_nr
                        vid = _kanban().create_task(
                            slug,
                            f"#{n_nr} {issue_for_pm.get('title', '')}",
                            body=_pm_body_fn(
                                repo,
                                issue_for_pm,
                                "CONFIRMED: (from github comment fallback)",
                                workdir,
                                base_branch,
                                provider_name,
                                profiles=p,
                                coding_agent=coding_agent,
                                coding_agent_cmd=coding_agent_cmd,
                            ) if _pm_body_fn is not None else "",
                            assignee=p["pm"],
                            idempotency_key=ikey,
                            workspace=f"dir:{workdir}" if workdir else "",
                            skills=rs.get("pm") or None,
                        )
                        if vid:
                            logger.info(
                                "dispatch: github-fallback PM task %s created for #%s",
                                vid,
                                n_nr,
                            )
                            triggered.append(n_nr)
                    else:
                        triggered.append(n_nr)
                continue
            # Count existing validator tasks (original + retries) for this issue.
            # NOTE: this runs BEFORE the `if not issue_nr: continue` guard, so we can
            # emit the retry-cap-exhausted notification even when the issue is missing
            # from issues_map. Otherwise, a stale validator task with no resolvable
            # issue would never trigger the notification (#378).
            #
            # `retry_count` (all runs) drives the unique retry idempotency key below.
            # `cap_count` (runs that produced a real, non-CONFIRMED verdict) drives the
            # cap gate: a run that completed with an empty/None summary means the
            # delegated agent died/timed out before deciding (a failed delegation, not
            # a wasted decision) and must be retried without burning the cap (#916).
            validator_tasks = [
                t
                for t in _kanban().list_tasks(slug)
                if (t.get("assignee") or "") == p["validator"]
                and f"#{n_nr}" in (t.get("title") or "")
            ]
            retry_count = len(validator_tasks)
            cap_count = sum(
                1
                for t in validator_tasks
                if _validator_summary_burns_cap(_get_task_summary(t, slug))
            )
            max_validator_retries = _resolve_max_validator_retries(
                (resolved or {}).get("execution") or {}
            )
            # Hard ceiling: if total run count exceeds 3× the cap, stop even if every
            # run produced an empty summary (e.g. closed issue, always-crashing agent).
            # Without this, cap_count stays 0 forever and the loop is infinite (#958).
            absolute_max = max(max_validator_retries * 3, max_validator_retries + 3)
            if cap_count >= max_validator_retries + 1 or retry_count >= absolute_max:
                logger.error(
                    "dispatch: validator for #%s has %d runs (cap %d) with no CONFIRMED — "
                    "manual intervention required",
                    n_nr,
                    retry_count,
                    max_validator_retries,
                )
                # Notify once: this branch re-runs on every tick (no new task is
                # created past the cap), so guard against re-sending the identical
                # alert each tick (#183). The marker is stamped on the validator
                # task and outlives the dispatcher process.
                if resolved is not None and not _has_notified(
                    slug,
                    n_nr,
                    validator_profile=p["validator"],
                    marker=_RETRY_CAP_MARKER,
                    role="validator",
                ):
                    if _rsr(
                        slug,
                        n_nr,
                        "validator",
                        profiles=p,
                        provider=provider,
                    ):
                        logger.info(
                            "dispatch: validator retry-cap for #%s suppressed "
                            "— stage recovered (#1167)",
                            n_nr,
                        )
                    else:
                        if _send_retry_cap is not None:
                            _send_retry_cap(
                                role="validator",
                                issue_number=n_nr,
                                retry_count=retry_count,
                                max_retries=max_validator_retries,
                                resolved=resolved,
                                dry_run=dry_run,
                            )
                        if not dry_run:
                            _mark_notified(
                                slug,
                                n_nr,
                                validator_profile=p["validator"],
                                marker=_RETRY_CAP_MARKER,
                                role="validator",
                            )
                        # Post a GitHub comment only when not suppressed by stage
                        # recovery (#1167).  Matches the pattern used in all other
                        # validator completion paths (STOP/BLOCKED/ESCALATE).
                        if provider is not None and not dry_run:
                            try:
                                cap_comment = (
                                    f"⚠️ **Validator retry cap exhausted** for issue #{n_nr}\n\n"
                                    f"The validator has completed {retry_count} times "
                                    f"(max: {max_validator_retries}) without a CONFIRMED outcome.\n\n"
                                    f"**Manual intervention required.**\n\n"
                                    f"Likely cause: Validator agent completed without CONFIRMED summary "
                                    f"(context window overflow, agent crash, or silent failure).\n\n"
                                    f"Recovery: Check agent logs, verify issue context, then manually "
                                    f"requeue validator or escalate to human review."
                                )
                                if not provider.post_issue_comment(n_nr, cap_comment):
                                    logger.warning(
                                        "dispatch: failed to post retry-cap comment on #%s",
                                        n_nr,
                                    )
                            except Exception as exc:
                                logger.warning(
                                    "dispatch: post_issue_comment #%s raised %s — "
                                    "retry-cap comment failed",
                                    n_nr,
                                    exc,
                                )
                continue
            if not issue_nr:
                # Unresolvable issue: warn + notify instead of silent drop (#1099).
                if not summary:
                    logger.warning(
                        "dispatch: validator for #%s completed with no summary "
                        "but issue is unresolvable — cannot retry without issue "
                        "context; manual intervention required",
                        n_nr,
                    )
                    if resolved is not None and not _has_notified(
                        slug,
                        n_nr,
                        validator_profile=p["validator"],
                        marker=_RETRY_CAP_MARKER,
                        role="validator",
                    ):
                        if _rsr(
                            slug,
                            n_nr,
                            "validator",
                            profiles=p,
                            provider=provider,
                        ):
                            logger.info(
                                "dispatch: validator retry-cap for #%s suppressed "
                                "— stage recovered (#1167)",
                                n_nr,
                            )
                        else:
                            if _send_retry_cap is not None:
                                _send_retry_cap(
                                    role="validator",
                                    issue_number=n_nr,
                                    retry_count=retry_count,
                                    max_retries=max_validator_retries,
                                    resolved=resolved,
                                    dry_run=dry_run,
                                )
                            if not dry_run:
                                _mark_notified(
                                    slug,
                                    n_nr,
                                    validator_profile=p["validator"],
                                    marker=_RETRY_CAP_MARKER,
                                    role="validator",
                                )
                continue
            # Intermediate retry — send a distinct "retry-attempt" notification before retrying (#287).
            # Fires only when we are actually about to create a new retry task (not at cap exhaustion).
            # SUPPRESSED at the boundary (retry_count >= max_retries): cap-exhausted fires on the
            # next tick, avoiding a duplicate "manual intervention required" notification — issue t_928bfae8.
            if resolved is not None and retry_count < max_validator_retries:
                if _send_retry_attempt is not None:
                    _send_retry_attempt(
                        role="validator",
                        issue_number=n_nr,
                        retry_count=retry_count,
                        max_retries=max_validator_retries,
                        resolved=resolved,
                        dry_run=dry_run,
                    )
            retry_key = f"validator-retry-{n_nr}-r{retry_count}"
            if dry_run:
                logger.info(
                    "[dry-run] validator empty summary #%s — would retry (run %d/%d)",
                    n_nr,
                    retry_count,
                    max_validator_retries,
                )
                triggered.append(n_nr)
                continue
            vbody = _validator_body_fn(
                repo,
                issue_nr,
                workdir,
                base_branch,
                provider_name,
                coding_agent=coding_agent,
                coding_agent_cmd=coding_agent_cmd,
            ) if _validator_body_fn is not None else ""
            vid = _kanban().create_task(
                slug,
                f"#validate: #{n_nr} {issue_nr.get('title', '')}",
                body=vbody,
                assignee=p["validator"],
                idempotency_key=retry_key,
                workspace=f"dir:{workdir}" if workdir else "",
                skills=rs.get("validator") or None,
            )
            if vid:
                logger.warning(
                    "dispatch: validator for #%s completed with no summary — "
                    "scheduling retry (run %d/%d, key=%s)",
                    n_nr,
                    retry_count,
                    max_validator_retries,
                    retry_key,
                )
                triggered.append(n_nr)
            continue
        n = extract_issue_number(task.get("title") or "")
        if n is None:
            continue
        # Skip CONFIRMED cards for closed/unknown issues (issues #1115, #1120).
        if _is_issue_closed_cached(provider, n, _closed_issue_cache) is not False:
            logger.debug(
                "dispatch: skipping CONFIRMED validator task for closed/unknown issue #%s",
                n,
            )
            continue
        pm_state, stale_count = _pm_state_fn(slug, n, p["pm"])
        if pm_state in ("running", "complete"):
            continue  # PM task active or properly done
        if pm_state == "stale":
            # Self-heal (#1161): the PM may have posted a full spec comment on
            # the issue even though its card completed with an empty summary.
            # Adopt it instead of retrying — _check_completed_pm then fans out.
            if _try_adopt_pm_spec_comment(slug, n, p["pm"], provider, dry_run=dry_run):
                continue
            max_pm_retries = _resolve_max_pm_retries(
                (resolved or {}).get("execution") or {}
            )
            if stale_count >= max_pm_retries:
                logger.error(
                    "dispatch: PM for #%s has %d stale premature completions — "
                    "manual intervention required (hermes kanban edit + SPEC: summary)",
                    n,
                    stale_count,
                )
                if resolved is not None and not _has_notified(
                    slug,
                    n,
                    validator_profile=p["validator"],
                    marker=_RETRY_CAP_MARKER,
                    role="pm",
                ):
                    if _rsr(
                        slug,
                        n,
                        "pm",
                        profiles=p,
                        provider=provider,
                    ):
                        logger.info(
                            "dispatch: PM retry-cap for #%s suppressed "
                            "— stage recovered (#1167)",
                            n,
                        )
                    else:
                        if _send_retry_cap is not None:
                            _send_retry_cap(
                                role="pm",
                                issue_number=n,
                                retry_count=stale_count,
                                max_retries=max_pm_retries,
                                resolved=resolved,
                                dry_run=dry_run,
                            )
                        if not dry_run:
                            _mark_notified(
                                slug,
                                n,
                                validator_profile=p["validator"],
                                marker=_RETRY_CAP_MARKER,
                                role="pm",
                            )
                        # Post a GitHub comment only when not suppressed by stage
                        # recovery (#1167).  Matches the pattern used in all other
                        # validator/PM completion paths.
                        if provider is not None and not dry_run:
                            try:
                                cap_comment = (
                                    f"⚠️ **Project Manager retry cap exhausted** for issue #{n}\n\n"
                                    f"The PM has completed {stale_count} times "
                                    f"(max: {max_pm_retries}) without a SPEC: outcome.\n\n"
                                    f"**Manual intervention required.**\n\n"
                                    f"Likely cause: PM agent completed without SPEC: summary "
                                    f"(context window overflow, agent crash, or silent failure).\n\n"
                                    f"Recovery: `hermes kanban edit <task-id>` and add `SPEC:` "
                                    f"summary, or manually requeue with fresh context."
                                )
                                if not provider.post_issue_comment(n, cap_comment):
                                    logger.warning(
                                        "dispatch: failed to post retry-cap comment on #%s",
                                        n,
                                    )
                            except Exception as exc:
                                logger.warning(
                                    "dispatch: post_issue_comment #%s raised %s — "
                                    "retry-cap comment failed",
                                    n,
                                    exc,
                                )
                continue
            # Intermediate PM retry — send a distinct "retry-attempt" notification before retrying (#287).
            if resolved is not None and _send_retry_attempt is not None:
                _send_retry_attempt(
                    role="pm",
                    issue_number=n,
                    retry_count=stale_count,
                    max_retries=max_pm_retries,
                    resolved=resolved,
                    dry_run=dry_run,
                )
            logger.warning(
                "dispatch: PM task for #%s prematurely completed without SPEC: "
                "(attempt %d/%d) — re-creating with retry key",
                n,
                stale_count + 1,
                max_pm_retries,
            )
        ikey = f"pm-{n}" if pm_state == "none" else f"pm-{n}-r{stale_count}"
        issue = issues_map.get(n)
        if not issue and provider is not None:
            fetched = _fetch_issue(provider, n)
            if fetched:
                issue = fetched.as_dict()
                logger.info(
                    "dispatch: #%s not in issues_map (confirmed) — fell back to get_issue()",
                    n,
                )
        if not issue:
            logger.debug(
                "dispatch: validator confirmed #%s but issue not in current scope", n
            )
            continue
        if dry_run:
            logger.info("[dry-run] validator CONFIRMED #%s — would create PM task", n)
            triggered.append(n)
            continue
        _pm_agent = (role_agents or {}).get("pm", coding_agent)
        vid = _kanban().create_task(
            slug,
            f"#{n} {issue.get('title', '')}",
            body=_pm_body_fn(
                repo,
                issue,
                summary_raw,
                workdir,
                base_branch,
                provider_name,
                profiles=p,
                coding_agent=_pm_agent,
                coding_agent_cmd=coding_agent_cmd,
            ) if _pm_body_fn is not None else "",
            assignee=p["pm"],
            idempotency_key=ikey,
            workspace=f"dir:{workdir}" if workdir else "",
            skills=rs.get("pm") or None,
        )
        if vid:
            logger.info(
                "dispatch: validator CONFIRMED #%s — PM task %s created", n, vid
            )
            triggered.append(n)
    return triggered


# ── planner phase ─────────────────────────────────────────────────────────────


def _retry_or_escalate_planner_stall(
    slug: str,
    task: Dict[str, Any],
    *,
    workdir: str,
    repo: str,
    base_branch: str,
    provider,
    profiles: Dict[str, str],
    role_skills: Dict[str, str],
    issues_map: Dict[int, Dict[str, Any]],
    epic_config: Optional[Dict[str, Any]],
    resolved: Optional[Dict[str, Any]],
    closed_issue_cache: Dict[int, Optional[bool]],
    dry_run: bool,
) -> Optional[int]:
    """Retry a silently-stalled planner card or escalate at the cap (#1125 F2).

    A planner card that completes without ``PLANNING COMPLETE`` / ``PLAN:`` /
    ``NOT SUITABLE`` is a delegation failure (context overflow, agent crash).
    This mirrors the validator done-without-CONFIRMED path:

      * count planner cards for the issue (all statuses) → attempt number;
      * if a planner run is still in flight (non-terminal), wait — this gives
        per-tick idempotency (never double-count) and prevents spawning a
        duplicate retry while one is running;
      * under the cap, create a fresh planner retry task with a unique
        ``planner-retry-{n}-r{count}`` idempotency key;
      * at the cap, fire a one-shot retry-cap notification + GitHub comment so
        a human is pinged instead of the issue stalling silently.

    Returns the issue number when a retry task was (or would be, in dry_run)
    created; otherwise ``None`` (in-flight, capped, closed, or unresolvable).
    """
    p = profiles
    n = extract_issue_number(task.get("title") or "")
    if n is None:
        logger.warning(
            "dispatch: planner stall for task %s — no issue number in title, skipping",
            task.get("id"),
        )
        return None
    # Skip closed/unknown issues (issues #1115, #1120 — treat None as skip).
    if _is_issue_closed_cached(provider, n, closed_issue_cache) is not False:
        logger.debug(
            "dispatch: skipping planner stall for closed/unknown issue #%s", n
        )
        return None

    planner_tasks = [
        t
        for t in _kanban().list_tasks(slug)
        if (t.get("assignee") or "").strip() == p["planner"]
        and f"#{n}" in (t.get("title") or "")
    ]
    # In-flight guard: if any planner card for this issue is non-terminal, a run
    # (original or a prior retry) is still active — wait for it rather than
    # spawning a duplicate. Guarantees per-tick idempotency (no double-count).
    _ACTIVE_STATUSES = {"todo", "ready", "running", "in_progress", "blocked"}
    if any((t.get("status") or "").lower() in _ACTIVE_STATUSES for t in planner_tasks):
        logger.debug(
            "dispatch: planner stall #%s — a planner run is still in flight, waiting",
            n,
        )
        return None

    retry_count = len(planner_tasks)
    max_planner_retries = _resolve_max_planner_retries(
        (resolved or {}).get("execution") or {}
    )

    d = _disp()
    _has_notified = (
        getattr(d, "_has_notified_block", _has_notified_block_impl)
        if d else _has_notified_block_impl
    )
    _mark_notified = (
        getattr(d, "_mark_notified_block", _mark_notified_block_impl)
        if d else _mark_notified_block_impl
    )
    _send_retry_cap = getattr(d, "_send_retry_cap_notification", None) if d else None
    _send_retry_attempt = getattr(d, "_send_retry_attempt_notification", None) if d else None
    _fetch_issue = (
        getattr(d, "_fetch_issue_with_retry", _fetch_issue_with_retry_impl)
        if d else _fetch_issue_with_retry_impl
    )
    _planner_body_fn = getattr(d, "_planner_body", None) if d else None

    if retry_count > max_planner_retries:
        # Cap exhausted — notify once + post GitHub comment so a human is pinged.
        logger.error(
            "dispatch: planner for #%s has %d runs (cap %d) with no PLANNING COMPLETE "
            "— manual intervention required",
            n,
            retry_count,
            max_planner_retries,
        )
        if not _has_notified(slug, n, marker=_RETRY_CAP_MARKER, role="planner"):
            if _send_retry_cap is not None:
                _send_retry_cap(
                    role="planner",
                    issue_number=n,
                    retry_count=retry_count,
                    max_retries=max_planner_retries,
                    resolved=resolved or {},
                    dry_run=dry_run,
                )
            if not dry_run:
                _mark_notified(
                    slug,
                    n,
                    marker=_RETRY_CAP_MARKER,
                    role="planner",
                    fallback_task_id=str(task.get("id") or ""),
                )
            if provider is not None and not dry_run:
                try:
                    cap_comment = (
                        f"⚠️ **Planner retry cap exhausted** for issue #{n}\n\n"
                        f"The planner has completed {retry_count} times "
                        f"(max: {max_planner_retries}) without a PLANNING COMPLETE "
                        f"outcome.\n\n"
                        f"**Manual intervention required.**\n\n"
                        f"Likely cause: Planner agent completed without a PLANNING "
                        f"COMPLETE summary (context window overflow, agent crash, or "
                        f"silent failure).\n\n"
                        f"Recovery: Check agent logs, verify issue context, then "
                        f"manually requeue the planner or escalate to human review."
                    )
                    if not provider.post_issue_comment(n, cap_comment):
                        logger.warning(
                            "dispatch: failed to post planner retry-cap comment on #%s",
                            n,
                        )
                except Exception as exc:
                    logger.warning(
                        "dispatch: post_issue_comment #%s raised %s — planner "
                        "retry-cap comment failed",
                        n,
                        exc,
                    )
        return None

    # Under cap — send an intermediate retry-attempt notification (suppressed at
    # the boundary so the cap-exhausted ping next tick is not duplicated), then
    # create the retry task with a unique per-attempt idempotency key.
    if resolved is not None and retry_count < max_planner_retries:
        if _send_retry_attempt is not None:
            _send_retry_attempt(
                role="planner",
                issue_number=n,
                retry_count=retry_count,
                max_retries=max_planner_retries,
                resolved=resolved,
                dry_run=dry_run,
            )

    retry_key = f"planner-retry-{n}-r{retry_count}"
    if dry_run:
        logger.info(
            "[dry-run] planner stall #%s — would retry (run %d/%d)",
            n,
            retry_count,
            max_planner_retries,
        )
        return n

    issue = issues_map.get(n)
    if not issue and provider is not None:
        fetched = _fetch_issue(provider, n)
        if fetched:
            issue = fetched.as_dict() if hasattr(fetched, "as_dict") else fetched
    if not issue:
        logger.warning(
            "dispatch: planner stall #%s but issue not in current scope — "
            "cannot retry without issue context",
            n,
        )
        return None

    provider_name = provider.name if provider is not None else "github"
    pid = _kanban().create_task(
        slug,
        f"#{n} {issue.get('title', '')}",
        body=_planner_body_fn(
            repo, issue, workdir, base_branch, provider_name, epic_config
        ) if _planner_body_fn is not None else "",
        assignee=p["planner"],
        idempotency_key=retry_key,
        workspace=f"dir:{workdir}" if workdir else "",
        skills=role_skills.get("planner") or None,
    )
    if pid:
        logger.warning(
            "dispatch: planner for #%s completed with no PLANNING COMPLETE — "
            "scheduling retry (run %d/%d, key=%s)",
            n,
            retry_count,
            max_planner_retries,
            retry_key,
        )
        return n
    return None


def _check_completed_planner(
    slug: str,
    workdir: str,
    profiles: Optional[Dict[str, str]] = None,
    *,
    dry_run: bool = False,
    provider=None,
    closed_issue_cache: Optional[Dict[int, Optional[bool]]] = None,
    repo: str = "",
    base_branch: str = "dev",
    issues_map: Optional[Dict[int, Dict[str, Any]]] = None,
    role_skills: Optional[Dict[str, str]] = None,
    epic_config: Optional[Dict[str, Any]] = None,
    resolved: Optional[Dict[str, Any]] = None,
) -> List[int]:
    """Phase-3 epic trigger: planner PLANNING COMPLETE → create sub-issues + triage cards.

    Runs each tick. Idempotency is handled inside _execute_planner_decompose
    via the <!-- daedalus:sub-issues:[...] --> marker comment on the parent issue.

    A planner card that completes WITHOUT ``PLANNING COMPLETE`` / ``PLAN:`` and
    WITHOUT a ``NOT SUITABLE`` signal is a silent stall (context overflow, agent
    crash). When ``resolved`` config context is supplied, such a stall is
    retried up to ``execution.max_planner_retries`` (mirroring the validator
    done-without-CONFIRMED path); on cap exhaustion a retry-cap notification is
    fired and a GitHub comment is posted so a human is pinged (#1125 F2).
    """
    from core.iterate import _execute_planner_decompose

    p = profiles or _DEFAULT_PROFILES
    rs = role_skills or {}
    _issues_map = issues_map or {}
    triggered: List[int] = []
    _closed_issue_cache: Dict[int, Optional[bool]] = (
        closed_issue_cache if closed_issue_cache is not None else {}
    )
    for task in _kanban().list_tasks(slug, status="done"):
        if (task.get("assignee") or "").strip() != p["planner"]:
            continue
        summary_raw = _get_task_summary(task, slug)
        summary_upper = summary_raw.upper().lstrip()
        # startswith enforces prefix position; a mid-body "PLANNING COMPLETE" mention
        # in unrelated text no longer trips the decompose gate (#1125 F1).
        if not summary_upper.startswith("PLANNING COMPLETE"):
            if summary_upper.startswith("PLAN:"):
                # "PLAN:" is a valid synonym — planner finished analysis, issue warrants decomposition
                logger.info(
                    "dispatch: planner done task %s has 'PLAN:' summary — treating as PLANNING COMPLETE synonym",
                    task.get("id"),
                )
            elif _NOT_SUITABLE_RE.search(summary_raw or ""):
                # NOT SUITABLE FOR DECOMPOSITION is a legitimate terminal signal
                # routed to the validator-fallback path by _check_planner_not_suitable.
                # Skip it here so we do not treat it as a silent stall.
                logger.debug(
                    "dispatch: planner done task %s is NOT SUITABLE — handled by "
                    "not_suitable path, skipping stall check",
                    task.get("id"),
                )
                continue
            elif resolved is None:
                # No config context (legacy/unit-test caller). Preserve the
                # original warn-and-skip behaviour — the dispatcher always
                # threads ``resolved`` so the retry path only activates in prod.
                logger.warning(
                    "dispatch: planner done task %s has unrecognised summary signal — skipping "
                    "(no retry context)",
                    task.get("id"),
                )
                continue
            else:
                # F2 (#1125): silent stall — planner completed without a
                # recognised signal. Retry up to max_planner_retries, then
                # escalate via notification + GitHub comment. Mirrors the
                # validator done-without-CONFIRMED retry mechanism.
                stalled_n = _retry_or_escalate_planner_stall(
                    slug,
                    task,
                    workdir=workdir,
                    repo=repo,
                    base_branch=base_branch,
                    provider=provider,
                    profiles=p,
                    role_skills=rs,
                    issues_map=_issues_map,
                    epic_config=epic_config,
                    resolved=resolved,
                    closed_issue_cache=_closed_issue_cache,
                    dry_run=dry_run,
                )
                if stalled_n is not None:
                    triggered.append(stalled_n)
                continue
        n = extract_issue_number(task.get("title") or "")
        if n is None:
            continue
        # Skip done planner tasks for closed/unknown issues (issues #1115, #1120).
        if _is_issue_closed_cached(provider, n, _closed_issue_cache) is not False:
            logger.debug(
                "dispatch: skipping done planner task for closed/unknown issue #%s", n
            )
            continue
        logger.info("dispatch: planner PLANNING COMPLETE #%s — triggering decompose", n)
        # Use a minimal body with ONLY the bare issue number so that
        # _extract_issue_number_from_card (prefer_qualified=True) cannot be
        # fooled by qualified benmarte/daedalus#<other> references that may
        # appear inside test-code examples in the task body.
        card = dict(task)
        card["body"] = f"Issue #{n}"
        ok = _execute_planner_decompose(
            slug,
            card,
            "",
            summary_raw,
            workdir=workdir,
            dry_run=dry_run,
            provider=provider,
        )
        if ok:
            triggered.append(n)
    return triggered


_NOT_SUITABLE_RE = re.compile(
    r"not\s+suitable(?:\s+for\s+decomposition)?", re.IGNORECASE
)


def _check_planner_not_suitable(
    slug: str,
    repo: str,
    issues_map: Dict[int, Dict[str, Any]],
    workdir: str,
    base_branch: str,
    provider_name: str,
    profiles: Optional[Dict[str, str]] = None,
    role_skills: Optional[Dict[str, List[str]]] = None,
    coding_agent: str = "none",
    coding_agent_cmd: str = "",
    notify_targets: Optional[List[str]] = None,
    *,
    dry_run: bool = False,
    provider=None,
    closed_issue_cache: Optional[Dict[int, Optional[bool]]] = None,
) -> List[int]:
    """Reroute a planner card signaling 'NOT SUITABLE FOR DECOMPOSITION'.

    When the planner completes a parent/epic issue but concludes it should not
    go through the decomposition path (already small, blocking dep, etc.), no
    downstream child task is produced and the parent issue would be left
    In-Progress with no active work. This handler detects that signal and
    creates a validator task for the parent issue so the pipeline keeps moving
    (refs issue #931 / epic #918).

    The handler scans both ``done`` and ``blocked`` planner cards. A blocked
    card with the NOT SUITABLE signal is treated as a valid route to the
    fallback validator path (defense in depth — the planner soul instructs
    completion, but if the planner blocks instead we still route correctly).

    Idempotency is enforced via a monotonic idempotency key
    ``planner-fallback-validator-{N}-g{gen}`` where ``gen`` increments each time
    a prior generation closes (done/cancelled/archived). This allows recurring
    issues to spawn fresh validators without creating duplicates within the
    same generation (epic #1008).

    Returns the issue numbers that were (or would be, in dry_run) routed to
    the validator path.
    """
    p = profiles or _DEFAULT_PROFILES
    rs = role_skills or {}
    triggered: List[int] = []
    processed_ids: set = set()
    _closed_issue_cache: Dict[int, Optional[bool]] = (
        closed_issue_cache if closed_issue_cache is not None else {}
    )

    d = _disp()
    _fetch_issue = (
        getattr(d, "_fetch_issue_with_retry", _fetch_issue_with_retry_impl)
        if d else _fetch_issue_with_retry_impl
    )

    # Scan both done and blocked cards. The done cards are the normal path;
    # blocked cards are defense in depth (soul says "always complete", but the
    # planner may block instead — handler must still route correctly).
    for status in ("done", "blocked"):
        for task in _kanban().list_tasks(slug, status=status):
            task_id = task.get("id")
            if task_id in processed_ids:
                continue
            if (task.get("assignee") or "").strip() != p["planner"]:
                continue
            summary_raw = _get_task_summary(task, slug)
            summary_upper = summary_raw.upper().lstrip()
            # Happy path is handled by _check_completed_planner — skip to avoid overlap.
            # Use startswith so a body that merely mentions PLANNING COMPLETE doesn't
            # short-circuit this handler (#1125 F1).
            if summary_upper.startswith("PLANNING COMPLETE"):
                logger.debug(
                    "dispatch: planner #%s has PLANNING COMPLETE signal — skipping not_suitable handler",
                    task_id,
                )
                continue
            if not _NOT_SUITABLE_RE.search(summary_raw or ""):
                if summary_raw:
                    logger.debug(
                        "dispatch: planner #%s summary does not match NOT SUITABLE pattern — skipping",
                        task_id,
                    )
                else:
                    logger.info(
                        "dispatch: planner #%s has empty summary — skipping",
                        task_id,
                    )
                continue

            n = extract_issue_number(task.get("title") or "")
            if n is None:
                logger.debug(
                    "dispatch: planner NOT SUITABLE #%s — no issue number in title, skipping",
                    task.get("title", "<untitled>"),
                )
                continue

            # Skip planner-NOT-SUITABLE cards for closed/unknown issues so the
            # dispatcher stops spawning validators for already-closed issues
            # (issue #1120 — the guard PR #1117 added to the other 5 scans but
            # missed here). ``None`` (rate-limited/unknown) is treated as skip.
            if _is_issue_closed_cached(provider, n, _closed_issue_cache) is not False:
                logger.debug(
                    "dispatch: skipping planner NOT SUITABLE task for closed/unknown issue #%s",
                    n,
                )
                continue

            logger.info(
                "dispatch: planner NOT SUITABLE #%s — routing to validator (fallback)",
                n,
            )

            issue = issues_map.get(n)
            if not issue and provider is not None:
                fetched = _fetch_issue(provider, n)
                if fetched:
                    issue = fetched.as_dict()
                    logger.info(
                        "dispatch: planner-fallback #%s not in issues_map window, "
                        "fetched directly from provider",
                        n,
                    )
            if not issue:
                logger.warning(
                    "dispatch: planner NOT SUITABLE #%s but issue not in current scope — skipping",
                    n,
                )
                continue

            if dry_run:
                logger.info(
                    "[dry-run] planner NOT SUITABLE #%s — would create validator task",
                    n,
                )
                triggered.append(n)
                processed_ids.add(task_id)
                continue

            ikey = _compute_planner_fallback_idempotency_key(slug, n)
            vid = _kanban().create_task(
                slug,
                f"#{n} {issue.get('title', '')}",
                body=_planner_not_suitable_validator_body(
                    repo,
                    issue,
                    summary_raw,
                    workdir,
                    base_branch,
                    provider_name,
                    coding_agent=coding_agent,
                    coding_agent_cmd=coding_agent_cmd,
                    security_targets=notify_targets,
                ),
                assignee=p["validator"],
                idempotency_key=ikey,
                workspace=f"dir:{workdir}" if workdir else "",
                skills=rs.get("validator") or None,
            )
            if vid:
                logger.info(
                    "dispatch: planner NOT SUITABLE #%s — validator task %s created",
                    n,
                    vid,
                )
                triggered.append(n)
            # Mark this issue as processed so we don't create duplicate validators
            # if the same issue appears in both done and blocked states.
            processed_ids.add(task_id)
    return triggered


def _planner_not_suitable_validator_body(
    repo: str,
    issue: Dict[str, Any],
    planner_summary: str,
    workdir: str,
    base_branch: str,
    provider_name: str,
    *,
    coding_agent: str = "none",
    coding_agent_cmd: str = "",
    security_targets: Optional[List[str]] = None,
) -> str:
    """Validator body for the planner-fallback path.

    The parent issue was routed to the planner, who decided it is not suitable
    for decomposition. We re-validate the issue as a regular (non-epic) bug
    or feature, then continue through the normal validator → PM → developer
    flow rather than stalling.
    """
    n, title, body, _ = _unpack_issue(issue)
    _h = _resolve_howtos(provider_name, repo, n)
    # NOTE: built but not yet interpolated into the body below — unlike the other
    # validator body builders (see ~1962/~2184). Kept as-is pending a scoped fix.
    _security_notify_cmds = _build_security_notify_cmds(
        repo, n, title, security_targets or []
    )
    _body = (
        f"Validate issue {repo}#{n}: {title}\n"
        f"Repo at {workdir} (read only — cd there for git/grep). Base branch: {base_branch}.\n\n"
        f"⛔ READ-ONLY — You may run existing tests to verify bug reproduction but MUST NOT write, "
        f"modify, or commit any code. DO NOT create or modify files. DO NOT run `git commit`, "
        f"`git add`, or any git write command. DO NOT open pull requests. "
        f"NEVER call hermes kanban create or any kanban write command — "
        f"you are read-only. The only kanban write allowed is completing or blocking YOUR OWN card.\n\n"
        f"📋 PROGRESS COMMENTS ARE AUTOMATIC: Do NOT post GitHub comments yourself. When you "
        f"complete (or block) your kanban card, the dispatcher mirrors your summary to GitHub "
        f"issue #{n} automatically.\n\n"
        f"You are the VALIDATOR for issue #{n}. This issue was originally routed to the planner "
        f"for epic decomposition, but the planner determined it is NOT suitable for that path:\n\n"
        f"    {planner_summary.strip()}\n\n"
        f"Treat it as a standard (non-epic) issue and classify using the normal rules:\n\n"
        f"CONFIRMED — issue is real, unaddressed, and safe to proceed. "
        f"Complete with summary starting 'CONFIRMED: ' + 1–2 sentence reproduction note.\n\n"
        f"CANNOT_REPRODUCE — POST comment on #{n} via {_h['comment']} then STOP.\n\n"
        f"ALREADY_FIXED — POST comment naming the fix then STOP.\n\n"
        f"DUPLICATE — POST comment linking the original then STOP.\n\n"
        f"NEEDS_MORE_INFO — POST comment listing required info, then BLOCK.\n\n"
        + _delimit_issue_content(n, body)
    )
    _prepend_delegation_fn = _fn("_prepend_delegation")
    if _prepend_delegation_fn is not None:
        return _prepend_delegation_fn(
            _body,
            coding_agent,
            coding_agent_cmd,
            role="validator",
            issue_number=n,
            append=True,
        )
    return _body


# ── PM phase ──────────────────────────────────────────────────────────────────


def _check_completed_pm(
    slug: str,
    repo: str,
    issues_map: Dict[int, Dict[str, Any]],
    iterations: int,
    workdir: str,
    notify_target: str,
    base_branch: str,
    provider_name: str,
    security_notify_targets: Optional[List[str]] = None,
    label_overrides: Optional[Dict[str, Any]] = None,
    profiles: Optional[Dict[str, str]] = None,
    role_skills: Optional[Dict[str, List[str]]] = None,
    coding_agent: str = "none",
    coding_agent_cmd: str = "",
    role_agents: Optional[Dict[str, str]] = None,
    *,
    dry_run: bool = False,
    provider=None,
    closed_issue_cache: Optional[Dict[int, Optional[bool]]] = None,
) -> List[int]:
    """Phase-3 trigger: for every PM task completed with 'SPEC:' summary,
    create the downstream triage (Developer + Reviewer + Security + Docs).

    Runs each tick so the team starts as soon as the PM finishes the spec.
    Idempotency via 'issue-{n}' key prevents duplicate triage cards.
    """
    p = profiles or _DEFAULT_PROFILES
    rs = role_skills or {}
    ra = role_agents or {}
    triggered: List[int] = []
    _closed_issue_cache: Dict[int, Optional[bool]] = (
        closed_issue_cache if closed_issue_cache is not None else {}
    )

    d = _disp()
    _has_downstream = (
        getattr(d, "_has_downstream_tasks", _has_downstream_tasks)
        if d else _has_downstream_tasks
    )
    _fetch_issue = (
        getattr(d, "_fetch_issue_with_retry", _fetch_issue_with_retry_impl)
        if d else _fetch_issue_with_retry_impl
    )
    _dev_body = getattr(d, "_dev_task_body", None) if d else None
    _qa_body = getattr(d, "_qa_task_body", None) if d else None
    _rev_body = getattr(d, "_reviewer_task_body", None) if d else None
    _sec_body = getattr(d, "_security_task_body", None) if d else None
    _docs_body = getattr(d, "_docs_task_body", None) if d else None

    for task in _kanban().list_tasks(slug, status="done"):
        if (task.get("assignee") or "").strip() != p["pm"]:
            continue
        # `hermes kanban list --json` omits the summary; fetch it from show.
        summary_raw = _get_task_summary(task, slug)
        summary = summary_raw.lower()
        # Accept both old "SPEC:" and new "assigned:" PM completion signals.
        # "assigned:" means PM already created all team tasks directly — skip triage creation.
        if summary.startswith("assigned:"):
            # PM created team tasks directly via SOUL.md. Log and skip — tasks already exist.
            _n2 = extract_issue_number(task.get("title") or "")
            if _n2 is not None:
                logger.info(
                    "dispatch: PM assigned #%s — team tasks created by PM directly, skipping triage",
                    _n2,
                )
            continue
        if not summary.startswith("spec:"):
            # Empty/None summary — PM agent crashed or context-limit dropout.
            # Log a warning so operators get visibility instead of a silent drop (#1104).
            if not summary:
                n_warn = extract_issue_number(task.get("title") or "")
                if n_warn is not None:
                    logger.warning(
                        "dispatch: PM for #%s completed with no summary — "
                        "retry handled by validator confirmed path",
                        n_warn,
                    )
            continue
        # Skip consultation tasks (title starts with "consult:") — only spec tasks trigger team
        title = (task.get("title") or "").lower()
        if title.startswith("consult:"):
            continue
        n = extract_issue_number(task.get("title") or "")
        if n is None:
            continue
        # Skip done PM tasks for closed/unknown issues (issues #1115, #1120).
        if _is_issue_closed_cached(provider, n, _closed_issue_cache) is not False:
            logger.debug(
                "dispatch: skipping done PM task for closed/unknown issue #%s", n
            )
            continue
        if _has_downstream(
            slug, n, validator_profile=p["validator"], pm_profile=p["pm"]
        ):
            continue  # team triage already exists
        issue = issues_map.get(n)
        if not issue and provider is not None:
            fetched = _fetch_issue(provider, n)
            if fetched:
                issue = fetched.as_dict()
                logger.info(
                    "dispatch: PM completed #%s — not in issues_map window, "
                    "fetched directly from provider",
                    n,
                )
        if not issue:
            logger.warning(
                "dispatch: PM completed #%s but issue not in scope and direct fetch failed "
                "— skipping team triage creation",
                n,
            )
            continue
        if dry_run:
            logger.info("[dry-run] PM SPEC #%s — would create downstream team tasks", n)
            triggered.append(n)
            continue
        workspace_arg = f"dir:{workdir}" if workdir else None
        issue_title = issue.get("title", "")[:60]

        # Resolve label-driven overrides for this issue.
        issue_labels = [
            (lbl["name"] if isinstance(lbl, dict) else lbl).lower()
            for lbl in (issue.get("labels") or [])
        ]
        merged_override: Dict[str, Any] = {}
        for lbl in issue_labels:
            merged_override.update((label_overrides or {}).get(lbl) or {})
        skip_developer = merged_override.get("skip_developer", False)
        security_first = merged_override.get("security_first", False)

        created_ids: Dict[str, Optional[str]] = {}

        if security_first:
            sec_id = _kanban().create_task(
                slug,
                f"#{n} Security: {issue_title}",
                body=_sec_body(
                    repo,
                    issue,
                    workdir,
                    provider_name,
                    profiles=p,
                    coding_agent=ra.get("security", coding_agent),
                    coding_agent_cmd=coding_agent_cmd,
                ) if _sec_body is not None else "",
                assignee=p.get("security", _DEFAULT_PROFILES["security"]),
                idempotency_key=f"security-{n}",
                workspace=workspace_arg,
                skills=rs.get("security") or None,
            )
            created_ids["security"] = sec_id

        dev_id = None
        if not skip_developer:
            dev_id = _kanban().create_task(
                slug,
                f"#{n} Developer: {issue_title}",
                body=_dev_body(
                    repo,
                    issue,
                    iterations,
                    workdir,
                    base_branch,
                    provider_name,
                    ra.get("developer", coding_agent),
                    coding_agent_cmd,
                    profiles=p,
                    label_overrides=label_overrides,
                ) if _dev_body is not None else "",
                assignee=p.get("developer", _DEFAULT_PROFILES["developer"]),
                idempotency_key=f"developer-{n}",
                workspace=workspace_arg,
                skills=rs.get("developer") or None,
            )
            created_ids["developer"] = dev_id

        qa_id = _kanban().create_task(
            slug,
            f"#{n} QA: {issue_title}",
            body=_qa_body(
                repo,
                issue,
                workdir,
                provider_name,
                profiles=p,
                coding_agent=ra.get("qa", coding_agent),
                coding_agent_cmd=coding_agent_cmd,
            ) if _qa_body is not None else "",
            assignee=p.get("qa", _DEFAULT_PROFILES["qa"]),
            idempotency_key=f"qa-{n}",
            workspace=workspace_arg,
            parents=[dev_id] if dev_id else None,
            skills=rs.get("qa") or None,
        )
        created_ids["qa"] = qa_id

        rev_id = _kanban().create_task(
            slug,
            f"#{n} Reviewer: {issue_title}",
            body=_rev_body(
                repo,
                issue,
                workdir,
                provider_name,
                profiles=p,
                coding_agent=ra.get("reviewer", coding_agent),
                coding_agent_cmd=coding_agent_cmd,
            ) if _rev_body is not None else "",
            assignee=p.get("reviewer", _DEFAULT_PROFILES["reviewer"]),
            idempotency_key=f"reviewer-{n}",
            workspace=workspace_arg,
            parents=[qa_id] if qa_id else None,
            skills=rs.get("reviewer") or None,
        )
        created_ids["reviewer"] = rev_id

        if not security_first:
            sec_id = _kanban().create_task(
                slug,
                f"#{n} Security: {issue_title}",
                body=_sec_body(
                    repo,
                    issue,
                    workdir,
                    provider_name,
                    profiles=p,
                    coding_agent=ra.get("security", coding_agent),
                    coding_agent_cmd=coding_agent_cmd,
                ) if _sec_body is not None else "",
                assignee=p.get("security", _DEFAULT_PROFILES["security"]),
                idempotency_key=f"security-{n}",
                workspace=workspace_arg,
                parents=[qa_id] if qa_id else None,
                skills=rs.get("security") or None,
            )
            created_ids["security"] = sec_id

        docs_parents = [
            x
            for x in [
                created_ids.get("developer"),
                created_ids.get("reviewer"),
                created_ids.get("security"),
            ]
            if x
        ]
        _kanban().create_task(
            slug,
            f"#{n} Docs: {issue_title}",
            body=_docs_body(
                repo,
                issue,
                workdir,
                provider_name,
                notify_target,
                profiles=p,
                coding_agent=ra.get("documentation", coding_agent),
                coding_agent_cmd=coding_agent_cmd,
            ) if _docs_body is not None else "",
            assignee=p.get("documentation", _DEFAULT_PROFILES["documentation"]),
            idempotency_key=f"docs-{n}",
            workspace=workspace_arg,
            parents=docs_parents or None,
            skills=rs.get("documentation") or None,
        )

        logger.info(
            "dispatch: PM SPEC #%s — created team tasks directly (no triage/decompose): %s",
            n,
            {k: v for k, v in created_ids.items() if v},
        )
        triggered.append(n)
    return triggered


# ── developer phase ───────────────────────────────────────────────────────────


def _check_completed_developer(
    slug: str,
    repo: str,
    issues_map: Dict[int, Dict[str, Any]],
    iterations: int,
    workdir: str,
    base_branch: str,
    provider_name: str,
    profiles: Optional[Dict[str, str]] = None,
    role_skills: Optional[Dict[str, List[str]]] = None,
    coding_agent: str = "none",
    coding_agent_cmd: str = "",
    role_agents: Optional[Dict[str, str]] = None,
    label_overrides: Optional[Dict[str, Any]] = None,
    *,
    dry_run: bool = False,
    provider=None,
    resolved: Optional[Dict[str, Any]] = None,
    closed_issue_cache: Optional[Dict[int, Optional[bool]]] = None,
) -> List[int]:
    """Phase-4 retry: when a developer task completes with no PR in its summary,
    retry up to ``max_developer_retries`` times before surfacing a cap-exhausted
    notification.

    Developer tasks are normally linked to QA via ``parents=[dev_id]`` — when
    the developer reaches "done", QA auto-promotes regardless of summary
    content.  When the developer completes with an empty/None summary (no PR
    opened), QA runs, finds no PR, and blocks with ``qa-failed: no PR``.
    This function detects the empty-summary case and creates a retry developer
    task before QA can run, mirroring the validator/PM retry pattern (#1104).
    """
    p = profiles or _DEFAULT_PROFILES
    rs = role_skills or {}
    ra = role_agents or {}
    triggered: List[int] = []
    _closed_issue_cache: Dict[int, Optional[bool]] = (
        closed_issue_cache if closed_issue_cache is not None else {}
    )

    d = _disp()
    _dev_state_fn = (
        getattr(d, "_developer_task_state", _developer_task_state)
        if d else _developer_task_state
    )
    _has_notified = (
        getattr(d, "_has_notified_block", _has_notified_block_impl)
        if d else _has_notified_block_impl
    )
    _mark_notified = (
        getattr(d, "_mark_notified_block", _mark_notified_block_impl)
        if d else _mark_notified_block_impl
    )
    _rsr = (
        getattr(d, "_retry_cap_stage_recovered", _retry_cap_stage_recovered)
        if d else _retry_cap_stage_recovered
    )
    _send_retry_cap = getattr(d, "_send_retry_cap_notification", None) if d else None
    _send_retry_attempt = getattr(d, "_send_retry_attempt_notification", None) if d else None
    _fetch_issue = (
        getattr(d, "_fetch_issue_with_retry", _fetch_issue_with_retry_impl)
        if d else _fetch_issue_with_retry_impl
    )
    _dev_body = getattr(d, "_dev_task_body", None) if d else None

    for task in _kanban().list_tasks(slug, status="done"):
        if (task.get("assignee") or "").strip() != p.get(
            "developer", _DEFAULT_PROFILES["developer"]
        ):
            continue
        summary_raw = _get_task_summary(task, slug)
        # A developer task with a PR number in its summary is well-formed — skip.
        if extract_pr_number_from_summary(summary_raw) is not None:
            continue
        n = extract_issue_number(task.get("title") or "")
        if n is None:
            continue
        # Skip done developer tasks for closed/unknown issues (issues #1115, #1120).
        if _is_issue_closed_cached(provider, n, _closed_issue_cache) is not False:
            logger.debug(
                "dispatch: skipping done developer task for closed/unknown issue #%s", n
            )
            continue
        # Check if there's already a running developer task for this issue
        # (a retry may have been created on a previous tick).
        dev_state, stale_count = _dev_state_fn(
            slug, n, p.get("developer", _DEFAULT_PROFILES["developer"])
        )
        if dev_state in ("running", "complete"):
            continue
        # Only process stale developer tasks (done with no PR).
        if dev_state != "stale":
            continue
        # Before minting a retry, adopt an in-flight PR left by the crashed
        # session (#1164) — a fresh developer would open a duplicate PR
        # (#1160 → #1163). Mirrors the intake "already has a PR — skipping"
        # guard for the re-dispatch-after-stale-completion path.
        if _try_adopt_developer_pr(
            slug,
            n,
            p.get("developer", _DEFAULT_PROFILES["developer"]),
            provider,
            base_branch=base_branch,
            dry_run=dry_run,
        ):
            continue
        max_dev_retries = _resolve_max_developer_retries(
            (resolved or {}).get("execution") or {}
        )
        absolute_max = max(max_dev_retries * 3, max_dev_retries + 3)
        if stale_count >= max_dev_retries or stale_count >= absolute_max:
            logger.error(
                "dispatch: developer for #%s has %d stale completions (no PR) — "
                "manual intervention required",
                n,
                stale_count,
            )
            if resolved is not None and not _has_notified(
                slug,
                n,
                validator_profile=p.get("validator", _DEFAULT_PROFILES["validator"]),
                marker=_RETRY_CAP_MARKER,
                role="developer",
            ):
                if _rsr(
                    slug,
                    n,
                    "developer",
                    profiles=p,
                    provider=provider,
                ):
                    logger.info(
                        "dispatch: developer retry-cap for #%s suppressed "
                        "— stage recovered (#1167)",
                        n,
                    )
                else:
                    if _send_retry_cap is not None:
                        _send_retry_cap(
                            role="developer",
                            issue_number=n,
                            retry_count=stale_count,
                            max_retries=max_dev_retries,
                            resolved=resolved,
                            dry_run=dry_run,
                        )
                    if not dry_run:
                        _mark_notified(
                            slug,
                            n,
                            validator_profile=p.get(
                                "validator", _DEFAULT_PROFILES["validator"]
                            ),
                            marker=_RETRY_CAP_MARKER,
                            role="developer",
                            fallback_task_id=str(
                                task.get("id") or task.get("task_id") or ""
                            ),
                        )
                    # Post a GitHub comment only when not suppressed by stage
                    # recovery (#1167).
                    if provider is not None and not dry_run:
                        try:
                            cap_comment = (
                                f"⚠️ **Developer retry cap exhausted** for issue #{n}\n\n"
                                f"The developer has completed {stale_count} times "
                                f"(max: {max_dev_retries}) without opening a PR.\n\n"
                                f"**Manual intervention required.**\n\n"
                                f"Likely cause: Developer agent completed without opening a PR "
                                f"(context window overflow, agent crash, or silent failure).\n\n"
                                f"Recovery: Check agent logs, verify issue context, then manually "
                                f"requeue developer or escalate to human review."
                            )
                            if not provider.post_issue_comment(n, cap_comment):
                                logger.warning(
                                    "dispatch: failed to post retry-cap comment on #%s (developer)",
                                    n,
                                )
                        except Exception as exc:
                            logger.warning(
                                "dispatch: post_issue_comment #%s raised %s "
                                "— retry-cap comment failed (developer)",
                                n,
                                exc,
                            )
            continue
        # Under cap — send retry-attempt notification and create retry task.
        if resolved is not None and _send_retry_attempt is not None:
            _send_retry_attempt(
                role="developer",
                issue_number=n,
                retry_count=stale_count,
                max_retries=max_dev_retries,
                resolved=resolved,
                dry_run=dry_run,
            )
        logger.warning(
            "dispatch: developer for #%s completed with no summary — "
            "scheduling retry (run %d/%d)",
            n,
            stale_count,
            max_dev_retries,
        )
        retry_key = f"developer-{n}-r{stale_count}"
        if dry_run:
            logger.info(
                "[dry-run] developer empty summary #%s — would retry (run %d/%d)",
                n,
                stale_count,
                max_dev_retries,
            )
            triggered.append(n)
            continue
        issue = issues_map.get(n)
        if not issue and provider is not None:
            fetched = _fetch_issue(provider, n)
            if fetched:
                issue = fetched.as_dict()
        if not issue:
            logger.warning(
                "dispatch: developer stale #%s but issue not in scope — cannot retry",
                n,
            )
            continue
        workspace_arg = f"dir:{workdir}" if workdir else None
        issue_title = issue.get("title", "")[:60]
        dev_id = _kanban().create_task(
            slug,
            f"#{n} Developer: {issue_title}",
            body=_dev_body(
                repo,
                issue,
                iterations,
                workdir,
                base_branch,
                provider_name,
                ra.get("developer", coding_agent),
                coding_agent_cmd,
                profiles=p,
                label_overrides=label_overrides,
            ) if _dev_body is not None else "",
            assignee=p.get("developer", _DEFAULT_PROFILES["developer"]),
            idempotency_key=retry_key,
            workspace=workspace_arg,
            skills=rs.get("developer") or None,
        )
        if dev_id:
            logger.warning(
                "dispatch: developer for #%s completed with no summary — "
                "scheduling retry (run %d/%d, key=%s)",
                n,
                stale_count,
                max_dev_retries,
                retry_key,
            )
            triggered.append(n)
    return triggered


# ── F5: per-role done-card prefix guard (#1125 F5) ────────────────────────────
# A "done" card with no recognised role prefix means the outer Hermes agent
# completed the card directly (LLM non-compliance or premature completion).
# The guard archives the bad card and creates a new blocked card so human
# intervention is surfaced rather than silently lost.
#
# NOTE: validator / pm / developer / planner have existing _check_completed_*
# handlers with retry logic.  The guard targets the remaining five roles.
_DONE_GUARD_PREFIXES: Dict[str, tuple] = {
    "qa-daedalus": ("qa-passed:", "qa-failed:", "qa-deferred:"),
    "reviewer-daedalus": ("review-approved:", "review-changes-requested:"),
    "security-analyst-daedalus": (
        "security-approved:", "security-changes-requested:", "security: cleared",
        "security cleared:",
    ),
    "accessibility-daedalus": (
        "approved:", "accessibility-na:", "a11y-skipped:", "changes requested:",
        "a11y-approved:",        # legacy form still emitted by some SOUL versions
        "a11y-changes-requested:",  # legacy form before SOUL position update
    ),
    "documentation-daedalus": ("docs posted:",),
}

# Idempotency-key prefix for guard-created blocked cards.
_GUARD_PREFIX_IK_PREFIX = "guard-prefix-"


def _guard_prefix_on_done(
    slug: str,
    profiles: Optional[Dict[str, str]] = None,
    *,
    dry_run: bool = False,
    closed_issue_cache: Optional[Dict[int, Optional[bool]]] = None,
    provider=None,
    prefix_fallback: bool = True,
    metadata_transport: bool = False,
) -> int:
    """Mechanical backstop: archive done cards that lack the expected role prefix (#1125 F5).

    For each role tracked by :data:`_DONE_GUARD_PREFIXES`, scan done cards.  If a
    card's summary does NOT start with any expected prefix, the outer agent
    completed the card without going through the proper block/classify_blocked
    path (LLM non-compliance, premature completion, or inner-agent failure that
    was not translated to a ``coding-agent-failed:`` block).

    Action taken:
    1. Archive the bad done card so it leaves the active board.
    2. Create a new blocked card with reason
       ``coding-agent-failed: unexpected completion summary: <first 100 chars>``
       so the sweeper and human operators see the problem.

    Idempotency: the archived card disappears from the ``done`` list on the next
    tick so the guard does not re-fire.  The ``idempotency_key`` on the new
    card prevents a duplicate even on concurrent ticks that both see the same
    done card before either archive completes.

    When ``prefix_fallback=True`` (default — Phase-1/2 soak behaviour):
      A well-formed completion is one whose summary starts with a recognised
      role prefix.  A valid JSON outcome record is NOT required.

    When ``prefix_fallback=False`` (Phase-3 flip — JSON primary):
      A well-formed completion MUST carry a valid :class:`~core.iterate.outcomes.OutcomeRecord`
      whose ``role`` matches the card's assignee.  A prefix line alone no
      longer satisfies the guard — agents that only wrote the legacy prefix (no
      JSON) are treated as non-compliant and are archived+recreated.
      Driven by ``protocol.prefix_fallback: false`` (#1170 Phase 3).

    When ``metadata_transport=True`` (#1288, default OFF):
      A done card is ALSO well-formed if its closing run carries a valid native
      outcome record (``kanban.run_outcome``) whose role matches the assignee —
      even when neither the prefix nor a free-text JSON block is present. This
      accepts the native transport emitted by ``complete(metadata=)``. Flag OFF
      → no ``run_outcome`` calls → behaviour is byte-identical.

    Returns: count of guards triggered.
    """
    p = profiles or _DEFAULT_PROFILES
    _closed_issue_cache: Dict[int, Optional[bool]] = (
        closed_issue_cache if closed_issue_cache is not None else {}
    )

    # Build profile→expected_prefixes mapping, respecting profile overrides.
    profile_to_prefixes: Dict[str, tuple] = {}
    for role_key, default_profile in _DEFAULT_PROFILES.items():
        active_profile = p.get(role_key, default_profile)
        if active_profile in _DONE_GUARD_PREFIXES:
            profile_to_prefixes[active_profile] = _DONE_GUARD_PREFIXES[active_profile]

    triggered = 0
    for task in _kanban().list_tasks(slug, status="done"):
        assignee = (task.get("assignee") or "").strip()
        expected_prefixes = profile_to_prefixes.get(assignee)
        if expected_prefixes is None:
            continue  # Not a guarded role

        summary_raw = _get_task_summary(task, slug)
        summary_check = (summary_raw or "").lower().lstrip()

        # #1288: native run metadata satisfies the guard when metadata_transport
        # is ON — the closing run carries the structured outcome even if the
        # human-readable summary prefix is absent.  Checked before the prefix/
        # JSON summary checks so a metadata-only completion is not flagged.
        if metadata_transport:
            _tid = task.get("id") or task.get("task_id")
            _expected_role_md = _ASSIGNEE_TO_ROLE_MAP.get(assignee)
            if _tid and _expected_role_md:
                _meta = _kanban().run_outcome(slug, str(_tid))
                _meta_rec = _parse_outcome_dict(_meta) if _meta else None
                if _meta_rec is not None and _meta_rec.role == _expected_role_md:
                    continue

        # Well-formed completion check depends on protocol.prefix_fallback.
        if prefix_fallback:
            # Phase-1/2 (default): prefix line satisfies the guard.
            if any(summary_check.startswith(pf.lower()) for pf in expected_prefixes):
                continue
        else:
            # Phase-3 (JSON primary): a valid outcome record is required.
            # A prefix line alone is NOT sufficient; the agent must also have
            # written the structured JSON block with a matching role.
            _rec = _parse_outcome(summary_raw or "")
            _expected_role = _ASSIGNEE_TO_ROLE_MAP.get(assignee)
            if _rec is not None and _expected_role and _rec.role == _expected_role:
                continue

        n = extract_issue_number(task.get("title") or "")
        if n is None:
            continue

        # Skip done cards for closed/unknown issues.
        if _is_issue_closed_cached(provider, n, _closed_issue_cache) is not False:
            logger.debug(
                "dispatch: guard_prefix_on_done skipping %s card for closed/unknown issue #%s",
                assignee, n,
            )
            continue

        task_id = str(task.get("id") or task.get("task_id") or "")
        truncated = (summary_raw or "")[:100]

        logger.warning(
            "dispatch: guard_prefix_on_done: done %s card %s for issue #%s "
            "lacks expected prefix — archiving and recreating as blocked. "
            "summary[:100]=%r",
            assignee, task_id, n, truncated,
        )

        if dry_run:
            logger.info(
                "[dry-run] guard_prefix_on_done: would archive %s and create "
                "coding-agent-failed blocked card for issue #%s (%s)",
                task_id, n, assignee,
            )
            triggered += 1
            continue

        # Step 1: Archive the bad done card.
        if task_id and not _kanban().archive_task(slug, task_id):
            logger.warning(
                "dispatch: guard_prefix_on_done: archive_task %s failed — "
                "skipping recreate for issue #%s",
                task_id, n,
            )
            continue

        # Step 2: Create a new blocked card to surface the failure.
        ik = f"{_GUARD_PREFIX_IK_PREFIX}{assignee}-{n}"
        fail_reason = f"coding-agent-failed: unexpected completion summary: {truncated!r}"
        new_id = _kanban().create_task(
            slug,
            title=f"#{n} {assignee} guard: unexpected completion",
            body=(
                f"**Guard triggered** (#1125 F5): `{assignee}` card `{task_id}` for issue #{n} "
                f"transitioned to done with an unrecognised summary (no canonical role prefix).\n\n"
                f"**Original summary (first 100 chars):** `{truncated}`\n\n"
                f"The original card has been archived.  Human intervention is required:\n"
                f"1. Inspect the agent run log for `{task_id}`.\n"
                f"2. Determine if the work was actually completed.\n"
                f"3. If yes, manually complete/re-queue the appropriate downstream stage.\n"
                f"4. If no, re-queue `{assignee}` for issue #{n}."
            ),
            assignee=assignee,
            idempotency_key=ik,
        )
        if not new_id:
            logger.warning(
                "dispatch: guard_prefix_on_done: create_task failed for issue #%s role %s — "
                "card %s archived but no replacement created",
                n, assignee, task_id,
            )
            continue

        _kanban().block_task(slug, new_id, fail_reason)
        logger.info(
            "dispatch: guard_prefix_on_done: archived %s, created replacement blocked card %s "
            "for issue #%s role %s",
            task_id, new_id, n, assignee,
        )
        triggered += 1

    return triggered

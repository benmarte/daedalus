"""core.iterate.gates — Block-loop rescue, merge gates, CI-rerun, deferred sweep.

This is the third extracted layer of core/iterate (PR 3/3, issue #1154).

Package layout after this PR:

  classify.py   — action constants, _parse_handoff, classify_blocked  (PR 1/3)
  executors.py  — decompose lock, fix-attempt tracking, role-gate helpers,
                  all _execute_* functions, planner helpers, _ACTION_EXECUTORS (PR 2/3)
  sources.py    — Phase 4 epic-context extraction + source-file reading (PR 3/3)
  gates.py      — block-loop rescue scan, merge-gate helpers, CI-rerun,
                  sweep_deferred_merges (PR 3/3, this file)
  __init__.py   — package root: re-exports all layers + kanban binding,
                  _source_reading_fallback_count, run_iterate

kanban BINDING MECHANICS (same pattern as executors.py)
--------------------------------------------------------
This module must NOT bind ``kanban`` at import time via ``from core import kanban``.
Tests may replace the whole ``kanban`` reference in ``__init__``'s namespace:

    mock.patch("core.iterate.kanban", fake_kanban)

Any module that imports ``kanban`` directly at load time holds the original module
object and misses the replacement.  The fix: all kanban access goes through
``_pkg().kanban`` at call time, where ``_pkg()`` resolves to
``sys.modules["core.iterate"]``.

The same applies to other package-level names that tests rebind (gate checkers,
executor references) — they are called via ``_pkg().name`` so that
``mock.patch.multiple(iterate, _qa_passed_for_issue=..., ...)``-style patches
intercept the call.

  Patch target                              Resolved via
  ─────────────────────────────────────     ──────────────────────────────────────
  core.iterate.kanban.list_tasks/show_card  _pkg().kanban.X
  core.iterate._qa_passed_for_issue         _pkg()._qa_passed_for_issue
  core.iterate._reviewer_passed_for_issue   _pkg()._reviewer_passed_for_issue
  core.iterate._security_passed_for_issue   _pkg()._security_passed_for_issue
  core.iterate._execute_advance             _pkg()._execute_advance
  core.iterate._execute_approve_advance     _pkg()._execute_approve_advance
  core.iterate._parse_pr_number             _pkg()._parse_pr_number
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

from core.iterate.classify import ADVANCE, APPROVE_ADVANCE
from core.providers.base import CIStatus
from core.util import extract_issue_number

logger = logging.getLogger("daedalus.iterate")


def _pkg() -> Any:
    """Return the core.iterate package, resolved at call time.

    Used throughout gates.py to access ``kanban`` and other package-level
    names so that ``mock.patch("core.iterate.kanban")`` and similar test
    patches take effect even for code defined in this submodule.
    """
    return sys.modules["core.iterate"]


# ── block-loop rescue (issue #1119) ──────────────────────────────────────────

# Gate profiles whose block reason can carry a terminal passing verdict, and
# the verdict prefixes that mean "the gate passed — complete, don't re-run".
# Matched with startswith on the lowercased reason so prose that merely
# contains the word ("tests pass") can't false-positive (same rationale as
# the removed "pass" signal in _parse_handoff).
_BLOCK_LOOP_PASS_PREFIXES: dict[str, tuple] = {
    "qa-daedalus": ("qa-passed",),
    "reviewer-daedalus": ("review-approved", "approved", "lgtm"),
    "security-analyst-daedalus": ("security-approved", "security-passed"),
}

# Statuses the rescue scan never touches: terminal states, plus 'blocked'
# (a card still in the blocked column is owned by the main blocked scan).
_RESCUE_SKIP_STATUSES = ("done", "complete", "completed", "archived",
                         "cancelled", "blocked")


def _latest_block_loop_reason(detail: dict) -> str | None:
    """Reason of the most recent ``block_loop_detected`` event, or None.

    ``detail`` is a ``kanban.show_card`` dict; its ``events`` list carries the
    framework's loop-detection events with ``payload.reason`` set to the block
    reason that kept recurring. Returns None when the task never hit a block
    loop (the empty string when the event has no reason).
    """
    for ev in reversed(detail.get("events") or []):
        if (ev.get("kind") or "") != "block_loop_detected":
            continue
        payload = ev.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return str(payload.get("reason") or "")
    return None


def _rescue_block_loop_gate_cards(
    slug: str,
    repo: str,
    *,
    exclude_ids: set[str] | None = None,
    dry_run: bool = False,
    coding_agent: str = "none",
    coding_agent_cmd: str = "",
) -> list[dict[str, Any]]:
    """Complete gate cards the framework re-promoted despite a passing verdict.

    When ``kanban.complete()`` fails transiently (rate limit) on a gate card
    blocked with a passing verdict (``qa-passed:`` / ``review-approved:`` /
    ``security-approved:``), the Hermes framework's loop detection fires
    ``block_loop_detected`` and auto-resolves by posting ``specified`` +
    ``promoted`` — putting the task back into running and re-running the whole
    gate (#1119). Those cards leave the blocked column, so the main blocked
    scan in ``run_iterate`` never sees them.

    This scan finds active gate-profile tasks whose most recent
    ``block_loop_detected`` event carries a passing verdict and routes them to
    the same executors the blocked-card path uses: QA cards to
    ``_execute_advance`` (complete + downstream review tasks) and
    reviewer/security cards to ``_execute_approve_advance`` (complete). If
    ``complete()`` fails again the card stays active and the next tick
    retries — degrade gracefully, never crash the tick.

    Returns a list of ``{tid, action, pr, ok}`` dicts for attempted rescues.
    """
    _p = _pkg()
    exclude = exclude_ids or set()
    try:
        tasks = _p.kanban.list_tasks(slug)
    except Exception as e:
        logger.error("iterate: block-loop rescue — list_tasks failed for %s: %s", slug, e)
        return []

    entries: list[dict[str, Any]] = []
    for t in tasks or []:
        tid = str(t.get("id") or "")
        assignee = (t.get("assignee") or "").lower().strip()
        status = (t.get("status") or "").lower().strip()
        if not tid or tid in exclude or status in _RESCUE_SKIP_STATUSES:
            continue
        prefixes = _BLOCK_LOOP_PASS_PREFIXES.get(assignee)
        if not prefixes:
            continue
        detail = _p.kanban.show_card(slug, tid)
        if not detail:
            continue
        reason = _latest_block_loop_reason(detail)
        if reason is None:
            continue  # never hit a block loop — the blocked-card path owns it
        verdict = (reason or detail.get("latest_summary") or "").strip()
        if not verdict.lower().startswith(prefixes):
            continue
        card = dict(detail.get("task") or {})
        card.setdefault("id", tid)
        pr = _p._parse_pr_number(verdict)
        try:
            if assignee == "qa-daedalus":
                action = ADVANCE
                ok = _p._execute_advance(slug, card, repo, verdict,
                                         dry_run=dry_run, pr_number=pr,
                                         coding_agent=coding_agent,
                                         coding_agent_cmd=coding_agent_cmd)
            else:
                action = APPROVE_ADVANCE
                ok = _p._execute_approve_advance(slug, card, repo, verdict,
                                                 dry_run=dry_run)
        except Exception as e:
            logger.error("iterate: block-loop rescue executor failed for %s: %s", tid, e)
            continue
        logger.info(
            "iterate: block-loop rescue — %s %s (%s), verdict %r → %s",
            action, tid, assignee, verdict[:80],
            "ok" if ok else "failed (retry next tick)")
        entries.append({"tid": tid, "action": action, "pr": pr, "ok": bool(ok)})
    return entries


# ── merge gate ───────────────────────────────────────────────────────────────


def _try_merge_if_gates_pass(
    slug: str,
    issue_n: int | None,
    pr: int | None,
    provider: Any,
    *,
    merge_method: str,
    skip_qa: bool,
    ci_status: str,
    dry_run: bool = False,
    active_tasks: list[dict[str, Any]] | None = None,
    archived_tasks: list[dict[str, Any]] | None = None,
) -> bool:
    """Merge ``pr`` iff every pipeline gate passes. Returns True only on an actual
    merge; idempotent and safe to call repeatedly.

    Called from two places: the docs-card-completion path (below) AND the per-tick
    deferred-merge sweep (``sweep_deferred_merges``). A failed ``merge_pr`` — e.g.
    the PR is momentarily un-mergeable due to a conflict, or CI hadn't gone green
    at docs-completion — returns False instead of consuming the only merge attempt,
    so a later tick retries. This is what makes auto-merge no longer one-shot (#1178).

    Gates (all bypassed by the ``skip-qa`` label, per #1074): QA passed, reviewer
    approved, security cleared, CI green, and the PR not already merged.

    ``active_tasks``/``archived_tasks`` let the deferred-merge sweep pass a
    once-per-tick board snapshot so the three gate checks don't each re-run
    ``list_tasks`` per PR (#1135). Both default to ``None`` (self-fetch).
    """
    _p = _pkg()
    if pr is None or provider is None:
        return False
    if not skip_qa and not _p._qa_passed_for_issue(
            slug, issue_n, active_tasks=active_tasks, archived_tasks=archived_tasks):
        logger.warning(
            "iterate: Skipping merge: QA has not passed for PR #%s (issue #%s).", pr, issue_n)
        return False
    if skip_qa:
        logger.info(
            "iterate: skip-qa label present on PR #%s — bypassing QA/reviewer/security gates", pr)
    if not skip_qa and not _p._reviewer_passed_for_issue(
            slug, issue_n, active_tasks=active_tasks, archived_tasks=archived_tasks):
        logger.warning(
            "iterate: Skipping merge: reviewer has not approved PR #%s (issue #%s).", pr, issue_n)
        return False
    if not skip_qa and not _p._security_passed_for_issue(
            slug, issue_n, active_tasks=active_tasks, archived_tasks=archived_tasks):
        logger.warning(
            "iterate: Skipping merge: security has not cleared PR #%s (issue #%s).", pr, issue_n)
        return False
    # CI gate: green required when the provider supports CI checks. NONE (the PR has zero
    # checks — repo has no CI) is treated as green so CI-less repos aren't blocked (F8).
    provider_supports_ci = getattr(provider, "supports_ci_status", False)
    if provider_supports_ci and ci_status not in (CIStatus.GREEN, CIStatus.NONE):
        logger.warning(
            "iterate: Skipping merge: CI not green for PR #%s (status: %s).", pr, ci_status)
        return False
    # Idempotency: never double-merge.
    if hasattr(provider, "is_pr_merged"):
        try:
            if provider.is_pr_merged(pr):
                logger.info(
                    "iterate: Skipping merge: PR #%s already merged (idempotent skip)", pr)
                return False
        except Exception as e:
            logger.warning(
                "iterate: is_pr_merged check failed for PR #%s: %s — proceeding", pr, e)
    logger.info(
        "iterate: all gates passed for PR #%s (QA/reviewer/security: %s, CI: %s) — merging",
        pr, "skip-qa" if skip_qa else "passed",
        ci_status if provider_supports_ci else "n/a",
    )
    if dry_run:
        logger.info("[dry-run] auto_merge=true: would merge PR #%s (%s)", pr, merge_method)
        return False
    merged = provider.merge_pr(pr, merge_method=merge_method)
    if merged:
        logger.info("iterate: auto-merged PR #%s (%s)", pr, merge_method)
        return True
    logger.warning(
        "iterate: auto_merge failed for PR #%s — leaving open; a later tick will retry", pr)
    return False


# ── bounded CI re-run for transiently-red pipeline-complete PRs (#1199) ───────
# Max automatic CI re-runs per PR head SHA before we escalate. A persistent red
# after this many re-runs is a real failure, not a flake, so we stop and notify.
CI_RERUN_MAX = 2
# Per-SHA marker comments posted on the PR make the retry idempotent across ticks
# and same-tick re-invocations (the PR itself is the source of truth). A new head
# SHA (branch pushed) yields a fresh budget — new code deserves fresh attempts.
_CI_RERUN_MARKER_PREFIX = "<!-- daedalus:ci-rerun:"
_CI_ESCALATED_MARKER_PREFIX = "<!-- daedalus:ci-escalated:"


def _ci_rerun_attempts(comments: list[Any], sha: str) -> int:
    """Count re-run marker comments already posted for ``sha``."""
    marker = f"{_CI_RERUN_MARKER_PREFIX}{sha}:"
    return sum(1 for c in comments if marker in (getattr(c, "body", "") or ""))


def _ci_already_escalated(comments: list[Any], sha: str) -> bool:
    """True if this SHA has already been escalated — the loop stop."""
    marker = f"{_CI_ESCALATED_MARKER_PREFIX}{sha} -->"
    return any(marker in (getattr(c, "body", "") or "") for c in comments)


def _rerun_or_escalate_red_ci(
    slug: str,
    issue_n: int | None,
    pr: int,
    provider: Any,
    *,
    dry_run: bool = False,
) -> str:
    """Handle a pipeline-complete PR whose required CI is genuinely RED (#1199).

    Bounded-retry the failed CI run (``CI_RERUN_MAX`` per head SHA); once the
    budget is spent and CI is still red, escalate with the failing-run URL
    instead of looping. Idempotent via per-SHA marker comments on the PR.

    Natural inter-tick backoff: issuing a re-run flips CI to PENDING, so the
    sweep won't act again until it settles back to RED — no timer needed.

    Returns one of ``"rerun"``, ``"escalated"``, or ``""`` (no-op this tick).
    """
    if provider is None or not getattr(provider, "supports_ci_rerun", False):
        return ""
    try:
        sha = provider.get_pr_head_sha(pr)
    except Exception as e:
        logger.warning("iterate: CI-rerun: get_pr_head_sha failed for PR #%s: %s", pr, e)
        return ""
    if not sha:
        logger.warning("iterate: CI-rerun: no head SHA for PR #%s — skipping", pr)
        return ""
    try:
        comments = provider.list_pr_comments(pr)
    except Exception:
        comments = []
    if _ci_already_escalated(comments, sha):
        return ""  # already escalated for this SHA — never loop
    attempts = _ci_rerun_attempts(comments, sha)

    if attempts < CI_RERUN_MAX:
        n = attempts + 1
        if dry_run:
            logger.info(
                "[dry-run] would re-run failed CI for PR #%s (attempt %d/%d, sha %s)",
                pr, n, CI_RERUN_MAX, sha[:8])
            return "rerun"
        ok = False
        try:
            ok = bool(provider.rerun_failed_ci(pr))
        except Exception as e:
            logger.warning("iterate: CI-rerun failed for PR #%s: %s", pr, e)
        if not ok:
            logger.warning(
                "iterate: CI-rerun no-op for PR #%s (attempt %d/%d) — no failed run or API error",
                pr, n, CI_RERUN_MAX)
            return ""
        # Persist the marker only after a successful re-run so a failed request
        # doesn't silently burn an attempt.
        provider.post_pr_comment(
            pr,
            f"{_CI_RERUN_MARKER_PREFIX}{sha}:{n} -->\n\n"
            f"♻️ Auto re-ran failed CI (attempt {n}/{CI_RERUN_MAX}) — "
            f"transient failure suspected; will merge automatically once green.")
        logger.info(
            "iterate: re-ran failed CI for PR #%s (issue #%s, attempt %d/%d, sha %s)",
            pr, issue_n, n, CI_RERUN_MAX, sha[:8])
        return "rerun"

    # Budget spent and still red → escalate once (no loop).
    try:
        run_url = provider.failed_ci_run_url(pr) or ""
    except Exception:
        run_url = ""
    msg = (
        f"⚠️ ESCALATE: PR #{pr} (issue #{issue_n}) — required CI is still RED after "
        f"{CI_RERUN_MAX} automatic re-runs. A persistent red is a real failure, not a "
        f"flake — manual intervention required.")
    if run_url:
        msg += f"\n\nFailing run: {run_url}"
    if dry_run:
        logger.info("[dry-run] %s", msg)
        return "escalated"
    provider.post_pr_comment(pr, f"{_CI_ESCALATED_MARKER_PREFIX}{sha} -->\n\n{msg}")
    logger.warning("iterate: %s", msg)
    return "escalated"


def sweep_deferred_merges(
    slug: str,
    repo: str,
    provider: Any,
    resolved: dict[str, Any] | None,
    *,
    dry_run: bool = False,
) -> list[int]:
    """Retry auto-merge for PRs whose pipeline finished but that weren't merged at
    docs-card completion (#1178).

    The docs-completion merge is one-shot: if the PR was momentarily un-mergeable
    (e.g. a CHANGELOG conflict) or CI hadn't gone green yet, the docs card still
    completes and drops out of ``list_blocked``, so the merge never retries. This
    sweep runs every tick: for each DONE ``docs-<n>`` card whose issue is still open
    with an open PR, it re-checks the gates and merges. Idempotent. Returns the PR
    numbers merged this tick.
    """
    _p = _pkg()
    execution = (resolved or {}).get("execution") or {}
    if not bool(execution.get("auto_merge", False)) or provider is None:
        return []
    merge_method = str(execution.get("merge_method", "squash")).lower()
    try:
        tasks = _p.kanban.list_tasks(slug) or []
    except Exception as e:
        logger.error("iterate: deferred-merge sweep failed to list tasks: %s", e)
        return []
    # Pre-fetch the archived list once too, so the per-PR gate checks
    # (_qa/_reviewer/_security_passed_for_issue) reuse both board snapshots
    # instead of each re-running list_tasks (active + archived) per PR (#1135).
    # Done gate cards archive quickly, so the archived list is the common
    # match path. On fetch failure fall back to None → gate helpers self-fetch
    # (prior behaviour) rather than silently seeing zero archived cards.
    try:
        archived_tasks: list[dict[str, Any]] | None = _p.kanban.list_tasks(slug, status="archived") or []
    except Exception as e:
        logger.error("iterate: deferred-merge sweep failed to list archived tasks: %s", e)
        archived_tasks = None
    merged: list[int] = []
    ci_cache: dict[int, str] = {}
    seen_issues: set[int] = set()
    # Match documentation cards by ASSIGNEE (title formats vary; idempotency_key
    # is no longer returned by the kanban API). Extract the issue number from the
    # title via the canonical helper.
    #
    # Scan BOTH the active board AND the archived list, and accept a docs card in
    # either the DONE or ARCHIVED state. Completed gate cards archive quickly
    # (#1141) — the documentation card (terminal stage) is frequently already
    # archived by the time this every-tick sweep runs. Scanning only active DONE
    # cards therefore never even *considered* a pipeline-complete PR once its docs
    # card archived, stranding it open until a manual merge (#1226). Archiving only
    # happens post-completion, so an archived docs card is a valid completion
    # signal; the gate/CI/mergeability checks below still re-verify before merging.
    candidate_tasks = list(tasks)
    if archived_tasks:
        candidate_tasks += archived_tasks
    for task in candidate_tasks:
        if (task.get("status") or "").lower() not in ("done", "archived"):
            continue
        if not (task.get("assignee") or "").strip().lower().startswith("documentation-"):
            continue
        issue_n = extract_issue_number(task.get("title") or "")
        if issue_n is None or issue_n in seen_issues:
            continue
        seen_issues.add(issue_n)
        # A closed issue already landed; only still-open issues need merging.
        if hasattr(provider, "is_issue_open"):
            try:
                if not provider.is_issue_open(issue_n):
                    continue
            except Exception:
                pass
        # Worktree isolation (#1176) forks fix/issue-<n>, but an agent may push a
        # descriptive suffix (fix/issue-<n>-<slug>). find_pr_for_issue tolerates
        # both so a suffixed branch does not strand the merge sweep. Fall back to
        # the exact-branch lookup for providers/doubles without the newer helper.
        try:
            finder = getattr(provider, "find_pr_for_issue", None)
            if callable(finder):
                pr = finder(issue_n)
            else:
                pr = provider.find_pr_for_branch(f"fix/issue-{issue_n}")
        except Exception:
            pr = None
        if pr is None:
            logger.debug(
                "iterate: deferred-merge sweep: no open PR for issue #%s "
                "(branch fix/issue-%s) — skipping", issue_n, issue_n)
            continue
        if pr not in ci_cache:
            try:
                ci_cache[pr] = provider.get_pr_ci_status(pr)
            except Exception:
                ci_cache[pr] = CIStatus.UNKNOWN
        skip_qa = False
        if hasattr(provider, "has_label"):
            try:
                skip_qa = bool(provider.has_label(pr, "skip-qa"))
            except Exception:
                skip_qa = False
        if _try_merge_if_gates_pass(
            slug, issue_n, pr, provider,
            merge_method=merge_method, skip_qa=skip_qa,
            ci_status=ci_cache[pr], dry_run=dry_run,
            active_tasks=tasks, archived_tasks=archived_tasks,
        ):
            merged.append(pr)
        elif ci_cache[pr] == CIStatus.RED:
            # Pipeline-complete PR with a genuinely RED (not pending/unknown)
            # required CI: bounded-retry the failed run, then escalate (#1199).
            # A re-run flips CI to PENDING, so the merge path picks it up on a
            # later tick once it goes green.
            _rerun_or_escalate_red_ci(
                slug, issue_n, pr, provider, dry_run=dry_run)
    if merged:
        logger.info("iterate: deferred-merge sweep merged PR(s): %s", merged)
    return merged

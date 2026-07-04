"""core.iterate — CI-aware auto-advance routing and self-healing loop.

Package layout (issue #1154, fully extracted after PR 3/3):

  classify.py   — action constants, _parse_handoff, classify_blocked (PR 1/3)
                  Pure routing layer; no kanban, no I/O.

  executors.py  — decompose lock, fix-attempt tracking, role-gate helpers,
                  all _execute_* functions, planner helpers, _ACTION_EXECUTORS
                  (PR 2/3). Accesses kanban and package-level names via _pkg()
                  at call time so that mock.patch("core.iterate.kanban") works.

  sources.py    — Phase 4 epic-context extraction + source-file reading
                  infrastructure (PR 3/3). Pure functions with subprocess;
                  no kanban, no provider. Re-exported here so patch targets
                  core.iterate.identify_relevant_files / load_known_components /
                  read_source_files resolve in __init__ as expected by tests.

  gates.py      — Block-loop rescue scan, merge-gate helper
                  (_try_merge_if_gates_pass), bounded CI-rerun, and
                  sweep_deferred_merges (PR 3/3). Uses _pkg() for all kanban
                  and gate-checker calls to respect whole-object patch style.

  __init__.py   — Package root (this file). Responsibilities:
                    • ``kanban`` binding (from core import kanban) — must live
                      here so mock.patch("core.iterate.kanban") replaces the
                      name in this namespace, making it visible to executors /
                      gates via their _pkg().kanban call-time lookups.
                    • ``_source_reading_fallback_count`` — mutable int counter
                      incremented by executors via _pkg()._source_reading_fallback_count
                      and read/reset by tests directly via iterate_mod.X.
                      Must be an attribute of this module object; cannot live
                      in sources.py (tests restore it with iterate_mod.X = N).
                    • Re-exports of every symbol from all submodules so that
                      ``from core.iterate import X`` and
                      ``mock.patch("core.iterate.X")`` continue to resolve for
                      the entire test suite without modification.
                    • ``run_iterate`` — the main auto-advance loop. Kept here
                      so all its by-name calls to kanban, classify_blocked,
                      _ACTION_EXECUTORS, gate helpers, etc. resolve through this
                      namespace where tests may have patched them.

For every blocked card on the board, classify its blocked state into an action,
then execute that action (complete, create fix-up tasks, unblock, escalate).
Runs as part of the daedalus dispatcher auto-advance block.

Pure helpers are unit-testable; the executors call ``core.kanban`` and the
configured VCS provider (``core.providers``) and are guarded so failures log
and continue.

``kanban`` is bound at package level (``from core import kanban``) so that
``mock.patch("core.iterate.kanban")`` and
``mock.patch("core.iterate.kanban.X")`` continue to work unchanged for
existing test suites.
"""

from __future__ import annotations

import logging
import subprocess as subprocess  # re-exported: tests patch core.iterate.subprocess.run
from typing import Any

from core import kanban
from core.providers.base import CIStatus, issue_linked_to_pr as issue_linked_to_pr

logger = logging.getLogger("daedalus.iterate")

# ── classify layer (extracted to core/iterate/classify.py, PR 1/3) ───────────
# Re-exported here so ``from core.iterate import X`` and
# ``mock.patch("core.iterate.X")`` / ``mock.patch("core.iterate.classify_blocked")``
# continue to resolve unchanged.
from core.iterate.classify import (  # noqa: E402
    ADVANCE,
    APPROVE_ADVANCE,
    ESCALATE,
    MAX_FIX_ATTEMPTS,
    PENDING_PR,
    PENDING_SIGNAL,
    PLANNER_DECOMPOSE,
    PM_ROUTE,
    QA_FIX,
    RECONCILE_MERGED,
    _ASSIGNEE_TO_ROLE as _ASSIGNEE_TO_ROLE,
    _classify_by_outcome as _classify_by_outcome,
    _parse_handoff as _parse_handoff,
    classify_blocked,
)

# ── outcomes layer (Phase 1 of #1170) ────────────────────────────────────────
# Re-exported so ``from core.iterate import X`` and
# ``mock.patch("core.iterate.X")`` continue to resolve for tests.
from core.iterate.outcomes import (  # noqa: E402
    SCHEMA_VERSION as SCHEMA_VERSION,
    VERDICT_TABLE as VERDICT_TABLE,
    OutcomeRecord as OutcomeRecord,
    parse as parse,
)

# ── verify layer (Phase 2 of #1170) ──────────────────────────────────────────
# Ground-truth outcome verification (config-gated; default OFF).
# Re-exported for ``mock.patch("core.iterate.verify_outcome")``.
from core.dispatch.verify import (  # noqa: E402
    VerifyResult as VerifyResult,
    verify_outcome as verify_outcome,
)

# Source-reading fallback counter for observability.
# MUST live here (not in sources.py) because:
#   1. Tests restore it via ``iterate_mod._source_reading_fallback_count = N``
#      which sets an attribute on this module object.
#   2. Executors increment it via ``_pkg()._source_reading_fallback_count += 1``
#      which also targets this module object's attribute.
#   3. get/reset helpers below read the same module-level variable.
_source_reading_fallback_count: int = 0


def get_source_reading_fallback_count() -> int:
    """Return the count of Phase 4 fallback events (for testing/monitoring)."""
    return _source_reading_fallback_count


def reset_source_reading_fallback_count() -> None:
    """Reset the source-reading fallback counter to zero (for tests)."""
    global _source_reading_fallback_count
    _source_reading_fallback_count = 0


# ── executors layer (extracted to core/iterate/executors.py, PR 2/3) ──────────
# Every symbol re-exported here so ``from core.iterate import X`` and
# ``mock.patch("core.iterate.X")`` continue to resolve unchanged for callers
# in __init__.py and for the test suite.
from core.iterate.executors import (  # noqa: E402
    _ACTION_EXECUTORS as _ACTION_EXECUTORS,
    _CHECKLIST_RE as _CHECKLIST_RE,
    _CODE_BLOCK_RE as _CODE_BLOCK_RE,
    _DECOMPOSE_LOCK_STALE_SECONDS as _DECOMPOSE_LOCK_STALE_SECONDS,
    _DECOMPOSE_MARKER_PREFIX as _DECOMPOSE_MARKER_PREFIX,
    _DECOMPOSED_MARKER_RE as _DECOMPOSED_MARKER_RE,
    _DOWNSTREAM_REVIEW_ROLES as _DOWNSTREAM_REVIEW_ROLES,
    _ESCALATION_STAMP_PREFIX as _ESCALATION_STAMP_PREFIX,
    _FILE_SYMBOL_CAP as _FILE_SYMBOL_CAP,
    _LEGACY_DECOMPOSED_MARKER_RE as _LEGACY_DECOMPOSED_MARKER_RE,
    _MAX_SUB_ISSUES as _MAX_SUB_ISSUES,
    _ROLE_ASSIGNEE_PREFIX as _ROLE_ASSIGNEE_PREFIX,
    _acquire_decompose_lock as _acquire_decompose_lock,
    _build_decomposed_marker as _build_decomposed_marker,
    _check_and_maybe_escalate as _check_and_maybe_escalate,
    _count_fix_attempts as _count_fix_attempts,
    _create_downstream_review_tasks as _create_downstream_review_tasks,
    _default_sub_issue_titles as _default_sub_issue_titles,
    _downstream_parents as _downstream_parents,
    _execute_advance as _execute_advance,
    _execute_approve_advance as _execute_approve_advance,
    _execute_escalate as _execute_escalate,
    _execute_legacy_dev_fix_review as _execute_legacy_dev_fix_review,
    _execute_pending_pr as _execute_pending_pr,
    _execute_planner_decompose as _execute_planner_decompose,
    _execute_planner_decompose_inner as _execute_planner_decompose_inner,
    _execute_pm_route as _execute_pm_route,
    _execute_qa_fix as _execute_qa_fix,
    _execute_reconcile_merged as _execute_reconcile_merged,
    _extract_issue_number_from_card as _extract_issue_number_from_card,
    _extract_sub_issues_from_body as _extract_sub_issues_from_body,
    _fix_attempts_path as _fix_attempts_path,
    _handoff_from_card as _handoff_from_card,
    _increment_fix_attempts as _increment_fix_attempts,
    _is_card_already_escalated as _is_card_already_escalated,
    _lock_file_path as _lock_file_path,
    _parse_pr_number as _parse_pr_number,
    _qa_passed_for_issue as _qa_passed_for_issue,
    _read_fix_attempts as _read_fix_attempts,
    _release_decompose_lock as _release_decompose_lock,
    _render_affected_files_section as _render_affected_files_section,
    _reviewer_passed_for_issue as _reviewer_passed_for_issue,
    _role_cards_for_issue as _role_cards_for_issue,
    _role_gate_passed as _role_gate_passed,
    _security_passed_for_issue as _security_passed_for_issue,
    _strip_code_blocks as _strip_code_blocks,
    _sub_issue_body as _sub_issue_body,
    _write_fix_attempts as _write_fix_attempts,
    has_decomposed_marker as has_decomposed_marker,
)

# ── sources layer (extracted to core/iterate/sources.py, PR 3/3) ─────────────
# Re-exported here so ``from core.iterate import X`` and
# ``mock.patch("core.iterate.X")`` continue to resolve unchanged.
# Callers in executors.py access these through _pkg().X at call time, which
# resolves to this namespace and picks up any test patch. ✓
from core.iterate.sources import (  # noqa: E402
    AggregateEpicContext as AggregateEpicContext,
    EpicContext as EpicContext,
    _KNOWN_DIRS as _KNOWN_DIRS,
    _MergedTask as _MergedTask,
    _build_aggregate_context as _build_aggregate_context,
    _compute_sub_issue_dependencies as _compute_sub_issue_dependencies,
    _extract_keywords as _extract_keywords,
    _file_matches_sub_context as _file_matches_sub_context,
    _file_paths_overlap as _file_paths_overlap,
    _grep_py_definitions as _grep_py_definitions,
    _merge_same_file_tasks as _merge_same_file_tasks,
    build_blocking_edges as build_blocking_edges,
    build_enhanced_scope as build_enhanced_scope,
    build_sub_issue_context as build_sub_issue_context,
    detect_file_overlap as detect_file_overlap,
    extract_epic_context as extract_epic_context,
    filter_context_for_sub as filter_context_for_sub,
    identify_relevant_files as identify_relevant_files,
    load_known_components as load_known_components,
    read_source_files as read_source_files,
)

# ── gates layer (extracted to core/iterate/gates.py, PR 3/3) ─────────────────
# Re-exported here so ``from core.iterate import X`` and
# ``mock.patch("core.iterate.X")`` continue to resolve unchanged.
# gates.py uses _pkg() internally so whole-object kanban patches and
# mock.patch.multiple(iterate, _qa_passed_for_issue=...) both work. ✓
from core.iterate.gates import (  # noqa: E402
    CI_RERUN_MAX as CI_RERUN_MAX,
    _BLOCK_LOOP_PASS_PREFIXES as _BLOCK_LOOP_PASS_PREFIXES,
    _CI_ESCALATED_MARKER_PREFIX as _CI_ESCALATED_MARKER_PREFIX,
    _CI_RERUN_MARKER_PREFIX as _CI_RERUN_MARKER_PREFIX,
    _RESCUE_SKIP_STATUSES as _RESCUE_SKIP_STATUSES,
    _ci_already_escalated as _ci_already_escalated,
    _ci_rerun_attempts as _ci_rerun_attempts,
    _latest_block_loop_reason as _latest_block_loop_reason,
    _rerun_or_escalate_red_ci as _rerun_or_escalate_red_ci,
    _rescue_block_loop_gate_cards as _rescue_block_loop_gate_cards,
    _try_merge_if_gates_pass as _try_merge_if_gates_pass,
    sweep_deferred_merges as sweep_deferred_merges,
)


# ── main loop ───────────────────────────────────────────────────────────────


def run_iterate(
    slug: str,
    repo: str,
    *,
    resolved: dict[str, Any] | None = None,
    provider: Any | None = None,
    dry_run: bool = False,
    max_fix_attempts: int = MAX_FIX_ATTEMPTS,
) -> tuple[dict[str, int], list[int], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the auto-advance routing and self-healing loop.

    For every blocked card on the board, classify its state and execute the
    appropriate action. Returns (counts, advance_prs, pending_signal_cards,
    qa_failed_cards, escalated_cards) where advance_prs lists PR numbers for
    cards that were successfully advanced, pending_signal_cards lists cards skipped
    because QA/a11y signal was unrecognized, qa_failed_cards lists dicts with
    {issue_n, pr, reason} for QA cards that created a developer fix card, and
    escalated_cards lists dicts with {issue_n, pr, reason} for QA cards that
    exhausted MAX_FIX_ATTEMPTS and triggered escalation.

    Args:
        slug: Kanban board slug.
        repo: Repo identifier (org/name) — used in card bodies only.
        resolved: Optional resolved project config (for workdir, notify_target).
        provider: Optional VCS provider (core.providers.VCSProvider) for PR/CI
            lookups. Without one, branch→PR resolution is skipped and CI is
            treated as not-green.
        dry_run: If True, log intentions without mutating anything.
        max_fix_attempts: Escalation cap for developer/reviewer/security fix
            cycles. Defaults to the module constant ``MAX_FIX_ATTEMPTS`` (3);
            the dispatcher resolves ``execution.max_fix_attempts`` and threads
            the per-project override in here.

    Returns:
        (counts, advance_prs, pending_signal_cards, qa_failed_cards, escalated_cards) tuple.
    """
    counts: dict[str, int] = {
        ADVANCE: 0,
        QA_FIX: 0,
        PENDING_SIGNAL: 0,
        PENDING_PR: 0,
        PM_ROUTE: 0,
        APPROVE_ADVANCE: 0,
        ESCALATE: 0,
        PLANNER_DECOMPOSE: 0,
        RECONCILE_MERGED: 0,
        # Phase-1 telemetry (#1170): tracks how many cards were routed via the
        # structured JSON outcome vs the legacy prefix path.  These counters
        # land in the dispatch history JSONL via routed_actions so Phase-3 can
        # observe when agents reliably emit valid records.
        "_outcome_json": 0,
        "_outcome_prefix": 0,
        # Phase-2 telemetry (#1170): verify_outcome results (when enabled).
        "_verify_verified": 0,
        "_verify_mismatch": 0,
        "_verify_skipped": 0,
    }
    # Per-tick outcome-source collector.  classify_blocked appends "json" or
    # "prefix" for each card it processes; we tally at end-of-loop.
    _outcome_sources: list[str] = []
    advance_prs: list[int] = []  # PR numbers for cards that were advanced
    pending_signal_cards: list[dict[str, Any]] = []  # Cards with unrecognized QA/a11y signal
    qa_failed_cards: list[dict[str, Any]] = []  # QA cards that created a fix card
    escalated_cards: list[dict[str, Any]] = []  # QA cards that hit MAX_FIX_ATTEMPTS

    workdir = (resolved or {}).get("workdir", "")
    notify_target = (resolved or {}).get("cron", {}).get("deliver", "")
    router_profile = (resolved or {}).get("router_profile", "project-manager-daedalus")
    execution = (resolved or {}).get("execution") or {}
    auto_merge = bool(execution.get("auto_merge", False))
    merge_method = str(execution.get("merge_method", "squash")).lower()
    # Phase-2 (#1170): ground-truth verification gate.  Default OFF.
    _verify_outcomes = bool(execution.get("verify_outcomes", False))
    # Phase-3 (#1170): prefix_fallback flag.  Default TRUE (current behaviour).
    _protocol = (resolved or {}).get("protocol") or {}
    _prefix_fallback = bool(_protocol.get("prefix_fallback", True))

    blocked_cards = kanban.list_blocked(slug)

    # ── block-loop rescue (issue #1119) ──────────────────────────────────
    # Gate cards whose complete() failed transiently get auto-promoted out
    # of the blocked column by the framework's loop detection, so the
    # blocked scan below never sees them. Rescue them here — this must run
    # even when the blocked column is empty (the re-promoted card is the
    # only sign anything is wrong).
    blocked_ids = {str(c.get("id")) for c in blocked_cards if c.get("id")}
    for entry in _rescue_block_loop_gate_cards(
            slug, repo, exclude_ids=blocked_ids, dry_run=dry_run):
        if not entry.get("ok"):
            continue
        counts[entry["action"]] = counts.get(entry["action"], 0) + 1
        if entry["action"] == ADVANCE and entry.get("pr") is not None:
            advance_prs.append(entry["pr"])

    # Deferred auto-merge sweep (#1178): retry merges the one-shot docs-completion
    # path missed (PR became mergeable / CI-green only after the docs card completed).
    # Runs every tick — including when nothing is blocked — since a merge-ready PR
    # leaves no blocked card behind to re-trigger the merge.
    try:
        advance_prs.extend(
            sweep_deferred_merges(slug, repo, provider, resolved, dry_run=dry_run)
        )
    except Exception as e:  # never let the merge sweep break a dispatch tick
        logger.error("iterate: deferred-merge sweep error: %s", e)

    if not blocked_cards:
        return counts, advance_prs, pending_signal_cards, qa_failed_cards, escalated_cards

    # Collect PR→CI cache so we don't call the provider for the same PR twice.
    # Stores the raw CIStatus string (not bool) so UNKNOWN/PENDING are distinguishable.
    ci_cache: dict[int, str] = {}

    # Per-tick escalation dedup: tracks which issue numbers have already been
    # escalated this tick. Maps issue number → first card's tid that escalated.
    escalated_issues: dict[int, str] = {}

    for card in blocked_cards:
        tid = card.get("id")
        if not tid:
            continue

        assignee = (card.get("assignee") or "").strip()
        handoff = _handoff_from_card(card)

        # Fallback: list_blocked returns minimal dicts without runs/reasons.
        # Fetch the full card detail via show_card and use latest_summary.
        if not handoff and tid:
            detail = kanban.show_card(slug, tid)
            if detail:
                handoff = (detail.get("latest_summary") or "").strip()

        fix_attempts = _count_fix_attempts(card)

        pr = _parse_pr_number(handoff)

        # Fallback: if handoff has no PR #, try the card's branch_name.
        if pr is None:
            branch_name = (card.get("branch_name") or "").strip()
            if branch_name and provider is not None:
                pr = provider.find_pr_for_branch(branch_name)
                if pr is not None:
                    logger.info("iterate: %s resolved PR #%s via branch %s",
                                tid, pr, branch_name)

        ci_green = False
        raw_ci = CIStatus.UNKNOWN
        if pr is not None and provider is not None:
            if pr not in ci_cache:
                ci_cache[pr] = provider.get_pr_ci_status(pr)
            raw_ci = ci_cache[pr]

            # No CI configured → no gate: treat UNKNOWN as green when the
            # provider doesn't support CI status checks (e.g. no check runs).
            if not getattr(provider, "supports_ci_status", False) and raw_ci == CIStatus.UNKNOWN:
                logger.info("iterate: %s provider has no CI support — treating as green", tid)
                ci_green = True
            else:
                ci_green = (raw_ci == CIStatus.GREEN)

        # #953: verify the resolved PR is a real, open PR before a developer
        # card can advance and release its QA child. Only checked for developer
        # cards (the only branch that gates on it) to avoid extra provider
        # calls. Unverifiable (provider lacks the capability or errors) stays
        # None → prior behaviour; only an affirmative "not open" blocks advance.
        pr_is_open: bool | None = None
        pr_is_merged: bool | None = None
        if (pr is not None and provider is not None
                and assignee.lower().strip() == "developer-daedalus"
                and hasattr(provider, "is_pr_open")):
            try:
                pr_is_open = bool(provider.is_pr_open(pr))
            except Exception:
                pr_is_open = None
            # #957: only when the PR is not open is "merged" interesting — a
            # merged PR means the work landed; reconcile the issue's cards
            # instead of holding in PENDING_PR. Skip the extra provider call
            # when the PR is open or its state is unverifiable.
            if pr_is_open is False and hasattr(provider, "is_pr_merged"):
                try:
                    pr_is_merged = bool(provider.is_pr_merged(pr))
                except Exception:
                    pr_is_merged = None

        # Detect skip-qa label on PR (bypass QA gate)
        skip_qa = False
        if pr is not None and provider is not None:
            skip_qa = bool(provider.has_label(pr, "skip-qa"))

        action = classify_blocked(assignee, handoff, ci_green,
                                  fix_attempts=fix_attempts, pr_number=pr,
                                  raw_ci=raw_ci, pr_is_open=pr_is_open,
                                  pr_is_merged=pr_is_merged,
                                  skip_qa=skip_qa,
                                  max_fix_attempts=max_fix_attempts,
                                  _source_collector=_outcome_sources,
                                  prefix_fallback=_prefix_fallback)

        # ── Phase-2 ground-truth verification (#1170) ─────────────────────
        # Only runs when:
        #   • verify_outcomes=true in config (default OFF — zero-cost no-op)
        #   • The card was routed via a JSON OutcomeRecord (prefix-only cards
        #     carry no structured VCS claims; nothing to verify)
        #   • The action is ADVANCE or APPROVE_ADVANCE (completion actions)
        # On mismatch: increment the role's fix-attempt counter (same counter
        # family as qa_fix/max_fix_attempts).  Under cap → post a mismatch
        # comment and leave the card blocked for the next tick's re-evaluation.
        # At cap → escalate via the existing escalation executor.
        if (
            _verify_outcomes
            and _outcome_sources
            and _outcome_sources[-1] == "json"
            and action in (ADVANCE, APPROVE_ADVANCE)
        ):
            _record = parse(handoff)
            if _record is not None:
                _issue_n_for_verify = _extract_issue_number_from_card(card)
                _vresult = verify_outcome(
                    _record,
                    provider,
                    issue_number=_issue_n_for_verify,
                    pr_number=pr,
                )
                counts[f"_verify_{_vresult.verdict}"] = (
                    counts.get(f"_verify_{_vresult.verdict}", 0) + 1
                )
                if _vresult.verdict == "mismatch":
                    logger.warning(
                        "iterate: verify_outcome MISMATCH for card %s "
                        "(action=%s, role=%s/%s): %s",
                        tid, action, _record.role, _record.verdict, _vresult.note,
                    )
                    # Increment fix-attempt counter (bounded by max_fix_attempts).
                    _vm_attempts = _increment_fix_attempts(card, workdir)
                    if _vm_attempts >= max_fix_attempts:
                        # At cap: escalate via existing machinery.
                        logger.warning(
                            "iterate: verify-mismatch for card %s hit cap "
                            "(%d/%d) — escalating",
                            tid, _vm_attempts, max_fix_attempts,
                        )
                        _esc = _ACTION_EXECUTORS.get(ESCALATE)
                        if _esc:
                            try:
                                _esc(
                                    slug, card, repo, handoff,
                                    workdir=workdir,
                                    notify_target=notify_target,
                                    router_profile=router_profile,
                                    dry_run=dry_run,
                                    pr_number=pr,
                                    provider=provider,
                                    max_fix_attempts=max_fix_attempts,
                                )
                            except Exception as _esc_exc:
                                logger.error(
                                    "iterate: escalation executor failed for "
                                    "verify-mismatch card %s: %s",
                                    tid, _esc_exc,
                                )
                        counts[ESCALATE] = counts.get(ESCALATE, 0) + 1
                        if _issue_n_for_verify is not None:
                            escalated_issues[_issue_n_for_verify] = tid
                    else:
                        # Under cap: post mismatch comment; card stays blocked
                        # and the next tick will re-run verify.
                        if not dry_run:
                            kanban.comment(
                                slug, tid,
                                f"verify-mismatch (attempt {_vm_attempts}/"
                                f"{max_fix_attempts}): {_vresult.note}",
                            )
                        else:
                            logger.info(
                                "[dry-run] would post verify-mismatch comment "
                                "on card %s (attempt %d/%d)",
                                tid, _vm_attempts, max_fix_attempts,
                            )
                    continue  # always skip the ADVANCE/APPROVE_ADVANCE executor

        # ── Escalation dedup (issue #35) ─────────────────────────────────
        # Before executing ESCALATE, check two layers of dedup:
        #   1. Cross-tick stamp: card already has "escalated: issue #N" comment.
        #   2. Per-tick sentinel: another card already escalated for this issue.
        # Both layers skip the card silently (or complete duplicates).
        if action == ESCALATE:
            issue_n = _extract_issue_number_from_card(card)

            # Layer 2: per-tick dedup (different card, same issue, same tick)
            if issue_n is not None and issue_n in escalated_issues:
                first_tid = escalated_issues[issue_n]
                if dry_run:
                    logger.info(
                        "[dry-run] would skip duplicate ESCALATE for %s "
                        "(already escalated by %s)", tid, first_tid)
                else:
                    logger.info(
                        "iterate: %s skipping duplicate ESCALATE for "
                        "issue #%s (already escalated by %s)",
                        tid, issue_n, first_tid)
                    kanban.complete(
                        slug, tid,
                        summary=f"skipped: escalated by {first_tid}")
                continue

            # Layer 1: cross-tick stamp (same card, previous tick already escalated)
            if issue_n is not None and _is_card_already_escalated(slug, tid, issue_n):
                logger.info(
                    "iterate: %s already stamped escalated: issue #%s — skipping",
                    tid, issue_n)
                continue

            # Record this card as the escalation owner for this issue/tick
            if issue_n is not None:
                escalated_issues[issue_n] = tid

        # PENDING_SIGNAL is a skip-action: card goes to pending_signal_cards
        # because the QA/a11y agent posted an unrecognized signal (still running,
        # crash, typo). No executor needed — the next cron tick re-evaluates.
        if action == PENDING_SIGNAL:
            pending_signal_cards.append({"tid": tid, "pr": pr, "card": card})
            counts[PENDING_SIGNAL] += 1
            logger.info("iterate: %s unrecognized QA/a11y signal — deferred to next tick", tid)
            continue

        # PENDING_PR: run the executor inline (it updates the block reason when
        # a PR is found; if no PR yet it's a no-op). Count and continue.
        if action == PENDING_PR:
            _execute_pending_pr(slug, card, repo, handoff, provider=provider, dry_run=dry_run)
            counts[PENDING_PR] += 1
            logger.info("iterate: %s awaiting PR for issue #%s", tid,
                        _extract_issue_number_from_card(card))
            continue

        if not action:
            continue  # nothing to do for this card

        executor = _ACTION_EXECUTORS.get(action)
        if not executor:
            logger.warning("iterate: unknown action '%s' for card %s", action, tid)
            continue

        # ── Pre-executor CI gate for docs auto-merge (issue #1085) ──────────
        # When the docs card is about to be completed (APPROVE_ADVANCE) and
        # auto_merge is enabled, CI must be green BEFORE we complete the card.
        # If CI is not green, we skip the executor entirely so the card stays
        # blocked — the next cron tick will re-evaluate and merge when CI
        # turns green. Without this gate, the card would be completed and
        # disappear from list_blocked, making the deferred merge impossible.
        if (
            action == APPROVE_ADVANCE
            and assignee == "documentation-daedalus"
            and auto_merge
            and pr is not None
            and provider is not None
        ):
            ci_status_for_merge = ci_cache.get(pr, CIStatus.UNKNOWN)
            provider_supports_ci = getattr(provider, "supports_ci_status", False)
            if provider_supports_ci and ci_status_for_merge != CIStatus.GREEN:
                logger.info(
                    "iterate: deferring docs card %s — CI not green for PR #%s (status: %s). "
                    "Card stays blocked; next tick will retry when CI passes.",
                    tid, pr, ci_status_for_merge,
                )
                counts[action] = counts.get(action, 0)  # no increment — nothing executed
                continue

        try:
            ok = executor(
                slug, card, repo, handoff,
                workdir=workdir,
                notify_target=notify_target,
                router_profile=router_profile,
                dry_run=dry_run,
                pr_number=pr,
                provider=provider,
                max_fix_attempts=max_fix_attempts,
            )

            # Gate on ok=True: prevents notification when the executor fails
            # (no PR number found, or kanban.create_task returned None/False).
            # Distinguish fix-card creation from escalation so callers can send
            # the right notification for each case.
            if action == QA_FIX and assignee == "qa-daedalus" and ok:
                issue_n = _extract_issue_number_from_card(card)
                entry = {"issue_n": issue_n, "pr": pr, "reason": handoff}
                # Escalation: fix_attempts file counter already at MAX before this
                # tick's increment (executor called _execute_escalate, not create_task).
                # Use file-only counter to avoid a second kanban.list_tasks round-trip.
                _tid = card.get("id", "")
                _file_count = _read_fix_attempts(workdir).get(_tid, 0) if workdir and _tid else 0
                _escalated = (_file_count >= max_fix_attempts)
                if _escalated:
                    escalated_cards.append(entry)
                else:
                    qa_failed_cards.append(entry)

            if ok:
                counts[action] += 1
                # Track PR number for advance actions so the human summary can
                # report which PRs were advanced (not just a count tuple).
                if action == ADVANCE and pr is not None:
                    advance_prs.append(pr)

                # Auto-merge: when the docs card completes and auto_merge is enabled,
                # the dispatcher merges the PR via the VCS API. This is the ONLY path
                # that can trigger a merge — agents never merge directly.
                if (
                    action == APPROVE_ADVANCE
                    and assignee == "documentation-daedalus"
                    and auto_merge
                    and pr is not None
                    and provider is not None
                ):
                    # Merge now if every gate passes. If not (CI still pending, PR
                    # momentarily un-mergeable), the docs card is already done — but
                    # sweep_deferred_merges() retries on later ticks, so the merge is
                    # no longer one-shot (#1178).
                    issue_n = _extract_issue_number_from_card(card)
                    _try_merge_if_gates_pass(
                        slug, issue_n, pr, provider,
                        merge_method=merge_method, skip_qa=skip_qa,
                        ci_status=ci_cache.get(pr, CIStatus.UNKNOWN), dry_run=dry_run,
                    )
        except Exception as e:
            logger.error("iterate: executor %s failed for card %s: %s", action, tid, e)

    # Tally outcome-source telemetry into counts for the history JSONL.
    # These land in routed_actions → summary → _append_history so Phase-3
    # can observe when agents reliably emit valid JSON outcome records.
    counts["_outcome_json"] = _outcome_sources.count("json")
    counts["_outcome_prefix"] = _outcome_sources.count("prefix")

    return counts, advance_prs, pending_signal_cards, qa_failed_cards, escalated_cards

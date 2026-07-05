"""Upfront-DAG stage body enrichment at dependency-lift time (#1301).

When ``pipeline.upfront_dag`` is ON, all stage cards are created at Ready-time
with a generic placeholder body (``_dag_stage_body`` — see
``scripts/daedalus_dispatch.py``). When each stage's dependency block lifts
(its parents complete), the card needs the same rich instruction body the
sequential path produces so agents receive full context.

This module provides:

  ``enrich_promoted_dag_stages(slug, issue_number, completing_assignee,
                               pr_number, *, provider, resolved, workdir,
                               dry_run=False)``

    Called from ``core.iterate.run_iterate`` after each successful executor call
    when ``pipeline.upfront_dag`` is ON. Determines which child stage cards to
    enrich based on the completing role, fetches the issue from the VCS provider
    when needed, renders the stage-appropriate body via the dispatcher's
    body-builder functions, and writes the body back via ``kanban.edit_body``.

Design decisions
----------------
Body editing
    Uses ``kanban.edit_body`` (direct SQLite write), NOT ``hermes kanban edit``
    which only accepts ``--result``/``--summary``/``--metadata`` and cannot touch
    the card body.  ``kanban.edit_body`` is the same mechanism the provider-
    failover path (PR #1207) uses to rewrite delegation blocks before re-dispatch
    — it is already present in production and tested by the existing test suite.

Body builders
    Accessed via ``_disp()`` from ``core.dispatch.checks``, the same call-time
    lazy-loader used by ``_check_completed_pm`` and ``_check_completed_developer``
    throughout checks.py.  This ensures tests that patch the dispatcher module
    (``disp = _load_dispatch(); with kanban_as(disp.kanban, fk): ...``) see the
    correct patched bodies, and production always gets the live dispatcher in its
    call stack.

Enrichment mapping (mirrors the sequential path)
    * PM completes   → enrich ``developer`` (needs full issue body, branch
                        naming, ⛔ NEVER merge invariant, iterations cap).
    * Developer completes → enrich ``qa``, ``reviewer``, ``security``, ``docs``
                        (need PR number reference in their instruction text).
    * ``accessibility`` has no dedicated body builder in the sequential path
                        either; it keeps its generic body in both modes.

Idempotency
    ``_ENRICHMENT_SENTINEL`` is appended to the body after enrichment.  A
    subsequent call checks for the sentinel and skips without re-writing, so
    re-promotion or re-ticks never stack duplicate enrichments.

Never raises
    Any per-stage failure logs a warning and preserves the generic body rather
    than crashing the dispatch tick.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import core.kanban as kanban
from core.dispatch.checks import _disp
from core.dispatch.resolvers import (
    _resolve_coding_agent,
    _resolve_coding_agent_cmd,
    _resolve_profiles,
)

logger = logging.getLogger("daedalus.dag_enrichment")

# ── Idempotency sentinel ──────────────────────────────────────────────────────
# Appended to the card body after enrichment.  Presence means "already done".
_ENRICHMENT_SENTINEL = "<!-- daedalus:dag-enriched -->"

# ── Assignee → stage key ──────────────────────────────────────────────────────
_ASSIGNEE_TO_STAGE: dict[str, str] = {
    "validator-daedalus": "validator",
    "project-manager-daedalus": "pm",
    "developer-daedalus": "developer",
    "qa-daedalus": "qa",
    "reviewer-daedalus": "reviewer",
    "security-analyst-daedalus": "security",
    "accessibility-daedalus": "accessibility",
    "documentation-daedalus": "docs",
}

# ── Enrichment trigger map ────────────────────────────────────────────────────
# When stage X completes, enrich these child stages.
# Mirrors the sequential path:
#   - PM completion → rich developer body (_dev_task_body)
#   - Developer completion → rich qa/reviewer/security/docs bodies
# ``accessibility`` is omitted intentionally: the sequential path uses the same
# generic downstream body for it, so we match that behaviour.
_ENRICH_ON_COMPLETE: dict[str, list[str]] = {
    "pm": ["developer"],
    "developer": ["qa", "reviewer", "security", "docs"],
}


# ── Internal helpers ──────────────────────────────────────────────────────────


def _stage_card(slug: str, stage_key: str, issue_number: int) -> Optional[dict]:
    """Return the DAG stage card by its stable idempotency key, or None."""
    ikey = f"{stage_key}-{issue_number}"
    for task in kanban.list_tasks(slug):
        if (task.get("idempotency_key") or "") == ikey:
            return task
    return None


def _already_enriched(body: str) -> bool:
    """True when the enrichment sentinel is already present in *body*."""
    return _ENRICHMENT_SENTINEL in (body or "")


def _resolve_ctx(resolved: Optional[dict], provider: Any) -> dict:
    """Extract every field the body builders need from *resolved* + *provider*."""
    r = resolved or {}
    execution = r.get("execution") or {}
    return {
        "repo": r.get("repo", ""),
        "base_branch": (r.get("vcs") or {}).get("target_branch") or "dev",
        "provider_name": getattr(provider, "name", None) or "github",
        "profiles": _resolve_profiles(execution),
        "coding_agent": _resolve_coding_agent(execution),
        "coding_agent_cmd": _resolve_coding_agent_cmd(execution),
        "iterations": int(execution.get("max_lifecycle_iterations", 3)),
        "notify_target": (r.get("cron") or {}).get("deliver", ""),
        "label_overrides": execution.get("label_overrides"),
    }


def _fetch_issue_dict(issue_number: int, provider: Any) -> Optional[dict]:
    """Fetch the issue as a plain dict from the VCS provider.

    Returns None when the provider is unavailable or the issue cannot be found.
    Never raises.
    """
    if provider is None:
        return None
    try:
        summary = provider.get_issue(issue_number)
        if summary is None:
            return None
        # IssueSummary.as_dict() gives the same shape _unpack_issue expects.
        if hasattr(summary, "as_dict"):
            return summary.as_dict()
        return dict(summary)
    except Exception as exc:
        logger.warning(
            "dag_enrichment: failed to fetch issue #%s from provider: %s",
            issue_number, exc,
        )
        return None


def _build_body(
    stage_key: str,
    issue_number: int,
    card: dict,
    issue: Optional[dict],
    ctx: dict,
) -> Optional[str]:
    """Render the enriched body for *stage_key* via the dispatcher body builders.

    Returns None when the dispatcher is not reachable, the builder is missing,
    or the issue dict is needed but unavailable (caller keeps generic body).
    """
    d = _disp()
    if d is None:
        logger.debug(
            "dag_enrichment: _disp() returned None — dispatcher not in call stack; skip"
        )
        return None

    repo = ctx["repo"]
    # Workspace is stored as "dir:/path" in card; strip the prefix for builders.
    ws = (card.get("workspace") or "").removeprefix("dir:")
    base_branch = ctx["base_branch"]
    provider_name = ctx["provider_name"]
    coding_agent = ctx["coding_agent"]
    coding_agent_cmd = ctx["coding_agent_cmd"]
    iterations = ctx["iterations"]
    notify_target = ctx["notify_target"]
    profiles = ctx["profiles"]
    label_overrides = ctx["label_overrides"]

    try:
        if stage_key == "developer":
            if issue is None:
                logger.warning(
                    "dag_enrichment: issue #%s not available — "
                    "keeping generic developer body",
                    issue_number,
                )
                return None
            fn = getattr(d, "_dev_task_body", None)
            if fn is None:
                return None
            return fn(
                repo, issue, iterations, ws, base_branch, provider_name,
                coding_agent, coding_agent_cmd,
                profiles=profiles,
                label_overrides=label_overrides,
            )

        if stage_key == "qa":
            if issue is None:
                return None
            fn = getattr(d, "_qa_task_body", None)
            if fn is None:
                return None
            return fn(
                repo, issue, ws, provider_name,
                coding_agent=coding_agent,
                coding_agent_cmd=coding_agent_cmd,
            )

        if stage_key == "reviewer":
            if issue is None:
                return None
            fn = getattr(d, "_reviewer_task_body", None)
            if fn is None:
                return None
            return fn(
                repo, issue, ws, provider_name,
                coding_agent=coding_agent,
                coding_agent_cmd=coding_agent_cmd,
            )

        if stage_key == "security":
            if issue is None:
                return None
            fn = getattr(d, "_security_task_body", None)
            if fn is None:
                return None
            return fn(
                repo, issue, ws, provider_name,
                coding_agent=coding_agent,
                coding_agent_cmd=coding_agent_cmd,
            )

        if stage_key == "docs":
            if issue is None:
                return None
            fn = getattr(d, "_docs_task_body", None)
            if fn is None:
                return None
            return fn(
                repo, issue, ws, provider_name, notify_target,
                coding_agent=coding_agent,
                coding_agent_cmd=coding_agent_cmd,
            )

    except Exception as exc:
        logger.warning(
            "dag_enrichment: body build raised for stage %s issue #%s: %s",
            stage_key, issue_number, exc,
        )
    return None


# ── Public API ────────────────────────────────────────────────────────────────


def enrich_promoted_dag_stages(
    slug: str,
    issue_number: int,
    completing_assignee: str,
    pr_number: Optional[int],
    *,
    provider: Any = None,
    resolved: Optional[dict] = None,
    workdir: str = "",
    dry_run: bool = False,
) -> list[str]:
    """Enrich child DAG stage card bodies after a parent stage completes.

    Called from ``core.iterate.run_iterate`` when ``pipeline.upfront_dag`` is ON
    and an executor returns True (card successfully advanced).  Maps the
    just-completing role to its downstream stage cards, renders the appropriate
    body via the dispatcher's body builders, and writes it back via
    ``kanban.edit_body``.

    Returns the list of stage keys that were actually enriched.  Never raises.

    Args:
        slug:               Kanban board slug.
        issue_number:       Issue number the completing card belongs to.
        completing_assignee: Assignee string of the card that just completed
                            (e.g. ``"project-manager-daedalus"``).
        pr_number:          PR number when the completing stage is developer;
                            unused for other stages (body builders look it up
                            via the card chain).
        provider:           VCS provider for issue fetches.  ``None`` → issue
                            dict unavailable; stages that need it keep generic
                            bodies instead of crashing.
        resolved:           Resolved project config dict (passed from
                            ``run_iterate`` unchanged).
        workdir:            Project workdir (unused; cards carry their own
                            workspace; kept for call-site symmetry with other
                            ``run_iterate`` kwargs).
        dry_run:            If True, log intent without mutating cards.
    """
    completing_stage = _ASSIGNEE_TO_STAGE.get(completing_assignee)
    if completing_stage is None:
        return []

    children = _ENRICH_ON_COMPLETE.get(completing_stage)
    if not children:
        return []

    ctx = _resolve_ctx(resolved, provider)
    enriched: list[str] = []

    # Lazy-fetch the issue dict on first need (avoids a provider round-trip for
    # stages that don't require the full issue body).
    issue: Optional[dict] = None
    _issue_fetched = False

    for stage_key in children:
        try:
            card = _stage_card(slug, stage_key, issue_number)
            if card is None:
                logger.debug(
                    "dag_enrichment: no card for stage=%s issue=#%s — skip",
                    stage_key, issue_number,
                )
                continue

            body = card.get("body") or ""
            if _already_enriched(body):
                logger.debug(
                    "dag_enrichment: stage=%s issue=#%s already enriched — skip (idempotent)",
                    stage_key, issue_number,
                )
                continue

            if not _issue_fetched:
                issue = _fetch_issue_dict(issue_number, provider)
                _issue_fetched = True

            if dry_run:
                logger.info(
                    "[dry-run] dag_enrichment: would enrich stage=%s issue=#%s (pr=%s)",
                    stage_key, issue_number, pr_number,
                )
                enriched.append(stage_key)
                continue

            new_body = _build_body(stage_key, issue_number, card, issue, ctx)
            if new_body is None:
                logger.debug(
                    "dag_enrichment: no body rendered for stage=%s issue=#%s — keeping generic",
                    stage_key, issue_number,
                )
                continue

            # Append the idempotency sentinel so re-ticks skip this card.
            final_body = new_body.rstrip() + "\n\n" + _ENRICHMENT_SENTINEL
            tid = card.get("id") or ""
            if tid and kanban.edit_body(slug, tid, final_body):
                enriched.append(stage_key)
                logger.info(
                    "dag_enrichment: enriched stage=%s card=%s issue=#%s",
                    stage_key, tid, issue_number,
                )
            else:
                logger.warning(
                    "dag_enrichment: edit_body failed for stage=%s card=%s issue=#%s",
                    stage_key, tid, issue_number,
                )

        except Exception as exc:
            logger.warning(
                "dag_enrichment: unexpected error for stage=%s issue=#%s: %s",
                stage_key, issue_number, exc,
            )

    return enriched

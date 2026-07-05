"""core.label_projection — canonical pipeline state → VCS label projection.

Projects the Hermes kanban DAG state for a single issue to a consistent set of
namespaced labels on the VCS side.  One pure function (project_labels) derives
the wanted label set from the card snapshot; a reconciler (reconcile_label_projection)
applies the diff via provider.add_label/remove_label.

Pipeline state truth lives in the kanban graph; labels are a write-through
projection for human visibility.  Advancement NEVER reads labels.

Label protocol
--------------
  daedalus:stage/<role>        active pipeline stage(s) (running or non-dep-blocked)
  daedalus:state/running       at least one card is running
  daedalus:state/blocked       no card running; at least one card blocked (non-dep)
  daedalus:state/done          all cards are terminal (done/cancelled/complete)
  daedalus:gate/needs-human    validator returned block_for_review
  daedalus:gate/needs-info     validator returned needs_more_info

Flag: pipeline.label_projection (default False).  When off this module is byte-inert.
"""
from __future__ import annotations

import logging
from typing import Any, Collection, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("daedalus.label_projection")

# ── Label namespace ─────────────────────────────────────────────────────────
DAEDALUS_NS  = "daedalus:"
STAGE_PREFIX = "daedalus:stage/"
STATE_PREFIX = "daedalus:state/"
GATE_PREFIX  = "daedalus:gate/"

# Role assignee → stage label value (after "daedalus:stage/")
STAGE_ROLES: Dict[str, str] = {
    "validator-daedalus":        "validator",
    "project-manager-daedalus":  "pm",
    "developer-daedalus":        "developer",
    "qa-daedalus":               "qa",
    "reviewer-daedalus":         "reviewer",
    "security-analyst-daedalus": "security",
    "accessibility-daedalus":    "accessibility",
    "documentation-daedalus":    "docs",
}

# Statuses treated as terminal for state projection.
_TERMINAL = frozenset({"done", "complete", "completed", "cancelled"})

# Validator verdicts that activate gate labels.
_GATE_HUMAN    = frozenset({"block_for_review", "security_threat"})
_GATE_NEEDS_INFO = frozenset({"needs_more_info"})


def project_labels(
    cards: List[Dict[str, Any]],
    current_labels: Collection[str],
) -> Tuple[Set[str], Set[str]]:
    """Compute label diff for ONE issue's canonical pipeline state.

    Pure function — no side effects.  ``cards`` must be pre-filtered to a
    single issue.  ``current_labels`` is the label set currently applied on
    the VCS issue (provider.list_issue_labels or cached).

    Returns ``(to_add, to_remove)`` — both are sets of label name strings.
    The caller is responsible for applying the diff only when either set is
    non-empty (skip-write optimisation).
    """
    if not cards:
        # No pipeline — remove every daedalus: label we may have set.
        to_remove = {lbl for lbl in current_labels if lbl.startswith(DAEDALUS_NS)}
        return set(), to_remove

    # ── partition cards by status ────────────────────────────────────────
    running: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []  # non-dependency blocks only
    for c in cards:
        s  = (c.get("status")     or "").strip().lower()
        bk = (c.get("block_kind") or "").strip().lower()
        if s == "running":
            running.append(c)
        elif s == "blocked" and bk != "dependency":
            blocked.append(c)

    all_terminal = all(
        (c.get("status") or "").strip().lower() in _TERMINAL for c in cards
    )

    wanted: Set[str] = set()

    # ── state label (exactly one) ────────────────────────────────────────
    if running:
        wanted.add(STATE_PREFIX + "running")
    elif blocked:
        wanted.add(STATE_PREFIX + "blocked")
    elif all_terminal:
        wanted.add(STATE_PREFIX + "done")
    # ready/pending/dep-blocked: no state label yet (pipeline not started)

    # ── stage labels (all currently active roles) ────────────────────────
    active = running if running else blocked
    for card in active:
        assignee  = (card.get("assignee") or "").strip()
        role_slug = STAGE_ROLES.get(assignee)
        if role_slug:
            wanted.add(STAGE_PREFIX + role_slug)

    # ── gate labels ──────────────────────────────────────────────────────
    # Derive from the done validator card's run_metadata verdict, or from a
    # blocked validator card with block_kind="needs_input".
    for card in cards:
        assignee = (card.get("assignee") or "").strip()
        if assignee != "validator-daedalus":
            continue
        status = (card.get("status")     or "").strip().lower()
        bk     = (card.get("block_kind") or "").strip().lower()

        if status in _TERMINAL:
            meta    = card.get("run_metadata") or {}
            verdict = (meta.get("verdict") or "").strip().lower()
            if verdict in _GATE_HUMAN:
                wanted.add(GATE_PREFIX + "needs-human")
            elif verdict in _GATE_NEEDS_INFO:
                wanted.add(GATE_PREFIX + "needs-info")
        elif status == "blocked" and bk == "needs_input":
            # Inspect the block reason to distinguish gate type.
            reason = (
                card.get("latest_summary") or card.get("reason") or ""
            ).lower()
            if "block_for_review" in reason or "security_threat" in reason:
                wanted.add(GATE_PREFIX + "needs-human")
            elif "needs_more_info" in reason:
                wanted.add(GATE_PREFIX + "needs-info")
            else:
                wanted.add(GATE_PREFIX + "needs-human")  # safe default

    # ── diff ─────────────────────────────────────────────────────────────
    existing = {lbl for lbl in current_labels if lbl.startswith(DAEDALUS_NS)}
    return wanted - existing, existing - wanted


def reconcile_label_projection(
    slug: str,
    issue_number: int,
    provider: Any,
    *,
    cards: Optional[List[Dict[str, Any]]] = None,
    dry_run: bool = False,
) -> Tuple[int, int]:
    """Apply the label diff for one issue.  Returns (adds, removes) counts.

    ``cards`` must be pre-filtered to this issue (avoids a new kanban call).
    When ``dry_run=True`` the diff is computed and logged but no API calls
    are made.  Never raises — logs errors and returns (0, 0) on failure.
    """
    try:
        # Get current VCS labels; use list_issue_labels if the provider
        # supports it, else fall back to an empty set (adds only, never
        # removes stale data — safe conservative default).
        current: Set[str] = set()
        if hasattr(provider, "list_issue_labels"):
            try:
                current = set(provider.list_issue_labels(issue_number))
            except Exception as e:
                logger.debug("list_issue_labels #%s failed: %s", issue_number, e)

        to_add, to_remove = project_labels(cards or [], current)

        if not to_add and not to_remove:
            return 0, 0  # nothing to do — skip write

        if dry_run:
            logger.info(
                "[dry-run] label_projection #%s: would add=%s remove=%s",
                issue_number, sorted(to_add), sorted(to_remove),
            )
            return len(to_add), len(to_remove)

        adds = removes = 0
        for lbl in sorted(to_add):
            if provider.add_label(issue_number, lbl):
                adds += 1
                logger.debug("label_projection #%s: + %s", issue_number, lbl)
        for lbl in sorted(to_remove):
            if provider.remove_label(issue_number, lbl):
                removes += 1
                logger.debug("label_projection #%s: - %s", issue_number, lbl)
        return adds, removes
    except Exception as e:
        logger.warning(
            "label_projection #%s: reconcile failed (non-fatal): %s",
            issue_number, e,
        )
        return 0, 0

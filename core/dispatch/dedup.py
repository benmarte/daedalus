"""core.dispatch.dedup — kanban comment-marker deduplication helpers.

These functions stamp and check idempotency markers stored as kanban card
comments, preventing duplicate notifications from firing on subsequent
dispatcher ticks.

Moved from scripts/daedalus_dispatch.py (issue #1153 PR 1/4).
The dispatcher re-exports every symbol so the public surface is unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from core import kanban

logger = logging.getLogger("daedalus.dispatch")

# ── Marker constants ──────────────────────────────────────────────────────────

_ESCALATION_MARKER = "<!-- daedalus:escalation-notified -->"

# Stamped on the validator task once a retry-cap-exhausted notification has been
# sent, so subsequent dispatcher ticks don't re-send the identical alert (#183).
# Role-scoped variant (#1167): <!-- daedalus:retry-cap-notified:<role> -->
_RETRY_CAP_MARKER = "<!-- daedalus:retry-cap-notified -->"

_RETRY_CAP_NOTIFICATION_MARKER = "RETRY_CAP_NOTIFICATION_SENT"


def _retry_cap_marker_for_role(role: str) -> str:
    """Return the role-scoped retry-cap marker for the given role (#1167)."""
    return f"<!-- daedalus:retry-cap-notified:{role} -->"


# ── Dedup check / stamp ───────────────────────────────────────────────────────


def _has_notified_block(
    slug: str,
    issue_number: int,
    validator_profile: str = "validator-daedalus",
    marker: str = _ESCALATION_MARKER,
    *,
    role: str = "",
) -> bool:
    """Return True if we already sent ``marker``'s notification for this issue.

    Uses the kanban task comments as a persistent, zero-overhead
    idempotency store — no local JSON files needed. ``marker`` selects which
    one-shot notification to check (block-escalation by default, or
    ``_RETRY_CAP_MARKER`` for retry-cap exhaustion — #183).

    When ``role`` is supplied (#1167), the check is role-scoped: it looks for
    the role-specific marker ``<!-- daedalus:retry-cap-notified:<role> -->``
    OR the legacy bare ``<!-- daedalus:retry-cap-notified -->`` for backward
    compatibility. It also scans cards from ALL pipeline assignees (not just
    the validator profile) so a marker stamped on a developer or PM card is
    also found.
    """
    # Determine which marker strings to look for.
    markers_to_check = {marker}
    if role:
        markers_to_check.add(_retry_cap_marker_for_role(role))

    pattern = f"#{issue_number}"
    for task in kanban.list_tasks(slug):
        if pattern not in (task.get("title") or ""):
            continue
        # When role-scoped (#1167), scan all assignees — the marker may have
        # been stamped on the triggering developer/PM card, not just the
        # validator card. When not role-scoped (escalation marker), keep the
        # original validator-only behaviour.
        if not role and (task.get("assignee") or "") != validator_profile:
            continue
        tid = str(task.get("id") or task.get("task_id") or "")
        if not tid:
            continue
        card = kanban.show_card(slug, tid)
        if not card:
            continue
        for c in card.get("comments") or []:
            body = c.get("body") or ""
            if any(m in body for m in markers_to_check):
                return True
    return False


def _mark_notified_block(
    slug: str,
    issue_number: int,
    validator_profile: str = "validator-daedalus",
    marker: str = _ESCALATION_MARKER,
    *,
    role: str = "",
    fallback_task_id: str = "",
) -> bool:
    """Stamp a kanban task with ``marker`` so future ticks skip re-sending.

    Returns True on success, False when no suitable card was found or the
    comment failed to post (#1167 — never fail silently).

    When ``role`` is supplied, stamps the role-scoped marker
    ``<!-- daedalus:retry-cap-notified:<role> -->``.

    Stamp target priority (#1167):
    1. The validator card for the issue (original behaviour).
    2. If no validator card is found and ``fallback_task_id`` is provided,
       stamp the triggering card directly (it always exists in the cap path).
    3. If neither is found, log a warning and return False.
    """
    actual_marker = _retry_cap_marker_for_role(role) if role else marker
    pattern = f"#{issue_number}"
    for task in kanban.list_tasks(slug):
        if pattern not in (task.get("title") or ""):
            continue
        if (task.get("assignee") or "") != validator_profile:
            continue
        tid = str(task.get("id") or task.get("task_id") or "")
        if tid:
            if kanban.comment(slug, tid, actual_marker):
                return True
            logger.warning(
                "dispatch: _mark_notified_block kanban.comment failed for "
                "issue #%s (role=%s, marker=%s) — marker may not persist",
                issue_number,
                role or "n/a",
                actual_marker,
            )
            return False
    # Fallback: stamp the triggering card directly (#1167).
    if fallback_task_id:
        if kanban.comment(slug, fallback_task_id, actual_marker):
            logger.info(
                "dispatch: _mark_notified_block used fallback card %s for "
                "issue #%s (role=%s) — validator card not found",
                fallback_task_id,
                issue_number,
                role or "n/a",
            )
            return True
        logger.warning(
            "dispatch: _mark_notified_block fallback kanban.comment failed "
            "for issue #%s (role=%s, card=%s) — marker may not persist",
            issue_number,
            role or "n/a",
            fallback_task_id,
        )
        return False
    logger.warning(
        "dispatch: _mark_notified_block found no target card for issue #%s "
        "(role=%s, marker=%s) — notification may re-fire on next tick",
        issue_number,
        role or "n/a",
        actual_marker,
    )
    return False

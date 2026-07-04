"""core.dispatch.dedup — kanban comment-marker deduplication helpers.

These functions stamp and check idempotency markers stored as kanban card
comments, preventing duplicate notifications from firing on subsequent
dispatcher ticks.

Phase 2 of #1170 adds dual-read / dual-write support via an optional
``workdir`` parameter.  When *workdir* is supplied:

  * **Readers** check ``dispatch_state`` first, fall back to the comment scan,
    and backfill the state on a fallback hit (lazy migration).
  * **Writers** write to ``dispatch_state`` in addition to posting the comment.

When *workdir* is empty (all existing callers), behaviour is unchanged.

Moved from scripts/daedalus_dispatch.py (issue #1153 PR 1/4).
The dispatcher re-exports every symbol so the public surface is unchanged.
"""

from __future__ import annotations

import logging

from core import kanban
import core.dispatch_state as _ds

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
    workdir: str = "",
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

    Phase 2 (#1170): when *workdir* is supplied, check ``dispatch_state``
    FIRST before scanning kanban comments.  On a comment-scan hit (fallback),
    backfill the state record for future ticks (lazy migration).
    """
    # ── Phase 2 dual-read: state-first, comment-scan fallback ─────────────────
    if workdir:
        try:
            if role:
                if _ds.is_retry_cap_notified(workdir, issue_number, role):
                    return True
            else:
                if _ds.is_escalation_notified(workdir, issue_number):
                    return True
        except Exception as exc:
            logger.warning(
                "dispatch: _has_notified_block state-read failed for #%s (role=%s): %s",
                issue_number,
                role or "n/a",
                exc,
            )

    # ── Comment-scan fallback (original behaviour) ─────────────────────────────
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
                # ── lazy backfill into state on comment-scan hit ───────────────
                if workdir:
                    try:
                        if role:
                            _ds.mark_retry_cap_notified(workdir, issue_number, role)
                        else:
                            _ds.mark_escalation_notified(workdir, issue_number)
                    except Exception as exc:
                        logger.warning(
                            "dispatch: _has_notified_block backfill failed for #%s: %s",
                            issue_number,
                            exc,
                        )
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
    workdir: str = "",
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

    Phase 2 (#1170): when *workdir* is supplied, also write the state record
    in ``dispatch_state`` (dual-write) so future reads can skip the comment
    scan (dual-read path in ``_has_notified_block``).
    """
    actual_marker = _retry_cap_marker_for_role(role) if role else marker
    pattern = f"#{issue_number}"

    comment_posted = False

    for task in kanban.list_tasks(slug):
        if pattern not in (task.get("title") or ""):
            continue
        if (task.get("assignee") or "") != validator_profile:
            continue
        tid = str(task.get("id") or task.get("task_id") or "")
        if tid:
            if kanban.comment(slug, tid, actual_marker):
                comment_posted = True
            else:
                logger.warning(
                    "dispatch: _mark_notified_block kanban.comment failed for "
                    "issue #%s (role=%s, marker=%s) — marker may not persist",
                    issue_number,
                    role or "n/a",
                    actual_marker,
                )
            break  # only stamp the first matching validator card

    if not comment_posted:
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
                comment_posted = True
            else:
                logger.warning(
                    "dispatch: _mark_notified_block fallback kanban.comment failed "
                    "for issue #%s (role=%s, card=%s) — marker may not persist",
                    issue_number,
                    role or "n/a",
                    fallback_task_id,
                )
                # Fall through: even if comment failed, try to persist in state.
        else:
            logger.warning(
                "dispatch: _mark_notified_block found no target card for issue #%s "
                "(role=%s, marker=%s) — notification may re-fire on next tick",
                issue_number,
                role or "n/a",
                actual_marker,
            )

    # ── Phase 2 dual-write: persist to dispatch_state ────────────────────────
    if workdir:
        try:
            if role:
                _ds.mark_retry_cap_notified(workdir, issue_number, role)
            else:
                _ds.mark_escalation_notified(workdir, issue_number)
        except Exception as exc:
            logger.warning(
                "dispatch: _mark_notified_block state-write failed for #%s: %s",
                issue_number,
                exc,
            )

    return comment_posted

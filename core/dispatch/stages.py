"""core.dispatch.stages — stage-check auxiliary helpers.

Collects stage-check utility functions that have no dependency on mutable
dispatcher globals or on functions tested with whole-kanban replacement
(``disp.kanban = fk`` rebind).  Every function here is safe to move because
its tests exclusively use method-level patches:
``mock.patch.object(disp.kanban, "<method>", ...)`` — which modify the
attribute on the shared kanban module object, so the patch is visible to
callers in any module that holds a reference to the same object.

  consultation helpers  — _CONSULT_RESOLVED_MARKER_TMPL,
                          _CONSULT_RESOLVED_MARKER_PREFIX,
                          _BLOCKER_CARD_COMMENT_PREFIX,
                          _is_consult_resolved,
                          _stamp_resolved_consultations
  downstream probe      — _downstream_tasks_running_or_done
  planner fallback key  — _PLANNER_FALLBACK_KEY_RE,
                          _PLANNER_FALLBACK_TERMINAL_STATUSES,
                          _compute_planner_fallback_idempotency_key
  validator enforcement — _enforce_validator_blocks

Note: _is_consult_resolved and _stamp_resolved_consultations were
originally listed as "STAY" in housekeeping.py's docstring because they
were grouped with _has_active_pm_consultation (which does use
``disp.kanban = fk`` rebinding).  Static analysis of the actual test
suite (test_dispatch.py:2818-2985, test_issue_1125_*.py) confirms they
only use method-level patches and are safe to extract here.

The large stage-check functions (_check_completed_validator/planner/pm/
developer/qa, _guard_prefix_on_done, _check_team_blockers, etc.) STAY in
scripts/daedalus_dispatch.py because they call _get_task_summary, which is
tested with ``disp.kanban = fk`` whole-module replacement in
test_dispatch.py and test_dispatch_closed_issue_skip_1115.py.  Moving them
would require importing _get_task_summary from the dispatcher, creating a
circular import.

Moved from scripts/daedalus_dispatch.py (issue #1153 PR 3/4).
The dispatcher re-exports every symbol so the public surface is unchanged.
"""

from __future__ import annotations

import logging
import re
from typing import List

from core import crash_retry  # noqa: E402
from core import kanban  # noqa: E402
import core.dispatch_state as _ds  # noqa: E402
from core.dispatch.dedup import _has_notified_block, _mark_notified_block  # noqa: E402
from core.util import extract_issue_number  # noqa: E402

logger = logging.getLogger("daedalus.dispatch")

# ── Consultation-resolved markers ────────────────────────────────────────────

# Stamped on a blocked team-member card once its PM consultation has completed,
# preventing re-creation of the same consultation on subsequent ticks (#1125 F4).
# Format: <!-- daedalus:consult-resolved:{issue_number} -->
_CONSULT_RESOLVED_MARKER_TMPL = "<!-- daedalus:consult-resolved:{n} -->"
# Prefix used to identify any consult-resolved comment during scanning.
_CONSULT_RESOLVED_MARKER_PREFIX = "<!-- daedalus:consult-resolved:"
# Comment posted on the PM consultation task body to link back to the blocked card.
_BLOCKER_CARD_COMMENT_PREFIX = "blocker-card: "


def _is_consult_resolved(
    slug: str,
    card_id: str,
    issue_n: int,
    *,
    workdir: str = "",
    prefix_fallback: bool = True,
) -> bool:
    """Return True if the blocked card has a consult-resolved stamp for this issue.

    Fetches the card via ``kanban.show_card`` and checks its comments for the
    ``<!-- daedalus:consult-resolved:{n} -->`` marker.  Returns False when the
    card cannot be fetched or the marker is absent.

    Phase 2 (#1170): when *workdir* is supplied, check ``dispatch_state``
    FIRST before scanning card comments.  On a comment-scan hit, backfill the
    state record for future ticks (lazy migration).

    Phase 3 (#1170): when ``prefix_fallback=False`` AND *workdir* is supplied,
    the comment-scan fallback is skipped entirely — ``dispatch_state`` is
    authoritative.  Consultation stamps are still WRITTEN (see
    :func:`_stamp_resolved_consultations`) for human visibility; only the
    READ path changes.  When *workdir* is empty the flag has no effect.
    """
    # ── Phase 2 dual-read: state-first ────────────────────────────────────────
    if workdir:
        try:
            if _ds.is_consult_resolved_for_card(workdir, card_id, issue_n):
                return True
        except Exception as exc:
            logger.warning(
                "dispatch: _is_consult_resolved state-read failed for "
                "card %s issue #%s: %s",
                card_id,
                issue_n,
                exc,
            )

    # ── Comment-scan fallback (original behaviour) ─────────────────────────────
    # Phase 3: when prefix_fallback=False and workdir is set, dispatch_state is
    # authoritative — skip the comment scan entirely (read-path only).
    if workdir and not prefix_fallback:
        return False

    marker = _CONSULT_RESOLVED_MARKER_TMPL.format(n=issue_n)
    card = kanban.show_card(slug, card_id)
    if not card:
        return False
    for c in card.get("comments") or []:
        if (c.get("body") or "").strip() == marker:
            # ── lazy backfill into state on comment-scan hit ───────────────────
            if workdir:
                try:
                    _ds.mark_consult_resolved_for_card(workdir, card_id, issue_n)
                except Exception as exc:
                    logger.warning(
                        "dispatch: _is_consult_resolved backfill failed for "
                        "card %s issue #%s: %s",
                        card_id,
                        issue_n,
                        exc,
                    )
            return True
    return False


def _stamp_resolved_consultations(
    slug: str,
    pm_profile: str = "project-manager-daedalus",
    *,
    workdir: str = "",
) -> int:
    """Stamp blocked cards whose PM consultations have completed with CLARIFIED/ESCALATED.

    Scans done PM consultation tasks (title starts with ``consult:``) for a
    ``blocker-card: <id>`` comment (posted at consult-creation time) and, for
    those whose summary begins with ``clarified:`` or ``escalated:``, stamps the
    original blocked card with a ``consult-resolved:`` marker so that
    ``_check_team_blockers`` skips re-creation on subsequent ticks (#1125 F4).

    Phase 2 (#1170): when *workdir* is supplied, also writes the resolved state
    to ``dispatch_state`` (dual-write) alongside the existing comment stamp.

    Returns the count of newly-stamped blocked cards.
    """
    resolved_prefixes = ("clarified:", "escalated:")
    stamped = 0
    for t in kanban.list_tasks(slug, status="done"):
        title = (t.get("title") or "").lower()
        if not title.startswith("consult:"):
            continue
        if (t.get("assignee") or "").strip() != pm_profile:
            continue
        cid = str(t.get("id") or "")
        if not cid:
            continue
        card_data = kanban.show_card(slug, cid)
        if not card_data:
            continue
        summary = (
            card_data.get("summary") or card_data.get("latest_summary") or ""
        ).lower()
        if not any(summary.startswith(p) for p in resolved_prefixes):
            continue
        issue_n = extract_issue_number(t.get("title") or "")
        if issue_n is None:
            continue
        marker = _CONSULT_RESOLVED_MARKER_TMPL.format(n=issue_n)
        # Locate the blocked-card reference stored as a comment at creation time.
        for c in card_data.get("comments") or []:
            body = (c.get("body") or "").strip()
            if not body.startswith(_BLOCKER_CARD_COMMENT_PREFIX):
                continue
            blocked_card_id = body[len(_BLOCKER_CARD_COMMENT_PREFIX):]
            if not blocked_card_id:
                continue
            # Idempotent: only stamp if the marker is not already there.
            blocked_card = kanban.show_card(slug, blocked_card_id)
            if not blocked_card:
                continue
            already = any(
                (cc.get("body") or "").strip() == marker
                for cc in blocked_card.get("comments") or []
            )
            if not already:
                kanban.comment(slug, blocked_card_id, marker)
                # Phase 2 (#1170): dual-write to dispatch_state.
                if workdir:
                    try:
                        _ds.mark_consult_resolved_for_card(
                            workdir, blocked_card_id, issue_n
                        )
                    except Exception as exc:
                        logger.warning(
                            "dispatch: _stamp_resolved_consultations state-write "
                            "failed for card %s / issue #%s: %s",
                            blocked_card_id,
                            issue_n,
                            exc,
                        )
                logger.info(
                    "dispatch: consult-resolved #%s stamped on card %s",
                    issue_n,
                    blocked_card_id,
                )
                stamped += 1
            break  # one blocker-card comment per consult task
    return stamped


# ── Downstream task probe ─────────────────────────────────────────────────────


def _downstream_tasks_running_or_done(
    slug: str,
    issue_number: int,
    downstream_profiles: tuple[str, ...],
) -> bool:
    """Return True if any downstream role card for *issue_number* is running or done.

    Shared helper used by _retry_cap_stage_recovered to avoid 3× duplicated loops.
    """
    pattern = f"#{issue_number}"
    for t in kanban.list_tasks(slug):
        if pattern not in (t.get("title") or ""):
            continue
        assignee = (t.get("assignee") or "").strip()
        if assignee not in downstream_profiles:
            continue
        status = (t.get("status") or "").lower()
        if status in ("running", "done", "complete", "completed"):
            return True
    return False


# ── Planner fallback idempotency key ─────────────────────────────────────────

# Pattern for monotonic planner-fallback idempotency keys: planner-fallback-validator-{N}-g{gen}
_PLANNER_FALLBACK_KEY_RE = re.compile(r"^planner-fallback-validator-(\d+)-g(\d+)$")
# Terminal statuses that close a generation (task is done/cancelled/archived)
_PLANNER_FALLBACK_TERMINAL_STATUSES = frozenset(
    {"done", "complete", "completed", "cancelled", "canceled", "archived"}
)


def _compute_planner_fallback_idempotency_key(slug: str, issue_number: int) -> str:
    """Compute a monotonic idempotency key for the planner-fallback validator path.

    Returns ``planner-fallback-validator-{N}-g{gen}`` where ``gen`` is the
    lowest non-negative integer such that no task with that generation has a
    terminal status (done/cancelled/archived). This allows a recurring issue
    to spawn a fresh validator after the previous one closes, while still
    preventing duplicates within the same generation.

    Legacy static keys (``planner-fallback-validator-{N}`` without a -g{gen}
    suffix) are ignored so existing production boards can migrate cleanly.

    Epic #1008 (dispatcher race condition fixes).
    """
    # Gather all tasks on the board. We need to scan regardless of status
    # because archived/cancelled tasks still carry their generation number.
    all_tasks = kanban.list_tasks(slug)
    # Collect {gen: status} pairs for this issue's planner-fallback keys
    generations: dict[int, str] = {}
    prefix = f"planner-fallback-validator-{issue_number}-g"
    for task in all_tasks:
        ikey = (task.get("idempotency_key") or "").strip()
        if not ikey.startswith(prefix):
            continue
        m = _PLANNER_FALLBACK_KEY_RE.match(ikey)
        if not m:
            continue
        gen = int(m.group(2))
        status = (task.get("status") or "").strip().lower()
        generations[gen] = status

    # Find the lowest generation that is NOT terminal
    gen = 0
    while (
        gen in generations and generations[gen] in _PLANNER_FALLBACK_TERMINAL_STATUSES
    ):
        gen += 1
    return f"planner-fallback-validator-{issue_number}-g{gen}"


# ── Validator block enforcement ───────────────────────────────────────────────


def _enforce_validator_blocks(
    slug: str,
    provider,
    existing: set,
    *,
    validator_profile: str = "validator-daedalus",
    dry_run: bool = False,
) -> List[int]:
    """For every blocked kanban card that is a validator card for a managed issue:
    set the VCS board status to 'Blocked' (auto-creating the column if needed),
    and complete all non-blocked downstream tasks so they cannot be dispatched.

    Called each tick AFTER existing issue numbers are known so we only touch
    issues the dispatcher is actually managing.  Returns enforced issue numbers.
    """
    if provider is None or not provider.board_configured():
        return []
    blocked = kanban.list_blocked(slug)
    if not blocked:
        return []

    enforced: List[int] = []
    for card in blocked:
        assignee_card = (card.get("assignee") or "").strip()
        summary = (card.get("summary") or card.get("last_summary") or "").lower()
        # #1205: an infrastructure crash is not a validator verdict — the
        # crash-retry reconciler owns these (board enforcement + downstream
        # cancellation would fight the auto re-dispatch during backoff).
        if crash_retry.is_crash_class(slug, card, summary):
            continue
        # Identify validator cards by profile name OR by the block-summary prefix
        is_validator = (
            assignee_card == validator_profile
            or summary.startswith("blocked:")
            or summary.startswith("escalate:")
        )
        if not is_validator:
            continue
        n = extract_issue_number(card.get("title") or "")
        if n is None:
            continue
        if n not in existing:
            continue
        if dry_run:
            logger.info(
                "[dry-run] validator blocked #%s — would set 'Blocked' on board + cancel downstream tasks",
                n,
            )
            enforced.append(n)
            continue
        provider.board_set_status(n, "Blocked")
        logger.info("dispatch: validator blocked #%s — set board status to Blocked", n)
        cancelled = kanban.close_non_blocked_issue_tasks(slug, n)
        if cancelled:
            logger.info(
                "dispatch: cancelled %d downstream task(s) for blocked #%s: %s",
                len(cancelled),
                n,
                cancelled,
            )
        # Only include in the returned list (which triggers notifications) once —
        # subsequent ticks still enforce board/kanban state but stay silent.
        if not _has_notified_block(slug, n, validator_profile=validator_profile):
            enforced.append(n)
            _mark_notified_block(slug, n, validator_profile=validator_profile)
    return enforced

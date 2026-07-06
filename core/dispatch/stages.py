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
    workdir: str = "",
    prefix_fallback: bool = True,
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
        if not _has_notified_block(
            slug, n,
            validator_profile=validator_profile,
            workdir=workdir,
            prefix_fallback=prefix_fallback,
        ):
            enforced.append(n)
            _mark_notified_block(slug, n, validator_profile=validator_profile, workdir=workdir)
    return enforced


# ── 6-outcome validator arbiter (upfront pipeline DAG, #1290) ─────────────────
#
# When ``pipeline.upfront_dag`` is ON the entire stage graph is created at
# Ready-time (see :func:`core.iterate.build_pipeline_dag`).  This arbiter is the
# generalisation of :func:`_enforce_validator_blocks` for that world: instead of
# only reacting to a *blocked* validator card, it reads the validator's
# structured 6-outcome verdict (native run metadata first — #1288 transport —
# then the free-text JSON block, then the legacy prefix) and prunes the DAG:
#
#     confirmed                       → KEEP     (Hermes auto-promotes PM)
#     already_fixed | duplicate       → CANCEL   (cancel every downstream branch)
#     needs_more_info | block_for_review → HUMAN  (block --kind needs_input; human)
#     security_threat                 → ESCALATE (notify + cancel every branch)
#     unknown / unparseable           → PARK     (== needs_more_info; NEVER auto-proceed)
#
# Active ONLY when the flag is on — the dispatcher calls _enforce_validator_blocks
# instead when it is off, so flag-off behaviour is byte-identical.

# Arbiter action constants (return values of :func:`_map_validator_outcome`).
ARBITER_KEEP = "keep"
ARBITER_CANCEL = "cancel"
ARBITER_HUMAN = "human"
ARBITER_ESCALATE = "escalate"
ARBITER_PARK = "park"

# Summary prefixes written by close_issue_tasks() on arbiter-cancelled/deferred cards.
# Used by _guard_prefix_on_done to exempt these cards from the "unexpected completion"
# detection — they are intentional orchestrator cancellations, not agent failures.
# Shared with core/dispatch/checks.py to avoid a magic-string ×2.
ARBITER_CLOSED_SUMMARY_PREFIXES = ("cancelled: validator", "deferred: validator")

# Validator verdict → arbiter action.  Any verdict not in this table (including
# None / unparseable) safe-parks — the pipeline never auto-proceeds on ambiguity.
_VALIDATOR_ARBITER_MAP: dict[str, str] = {
    "confirmed": ARBITER_KEEP,
    "already_fixed": ARBITER_CANCEL,
    "duplicate": ARBITER_CANCEL,
    "needs_more_info": ARBITER_HUMAN,
    "block_for_review": ARBITER_HUMAN,
    "security_threat": ARBITER_ESCALATE,
}

# Legacy prefix → verdict, for the prefix-fallback read path.  Validators emit
# the outcome as a LEADING token, so the summary is matched with ``startswith``
# (anchored), not a substring scan — a "CONFIRMED: … not a duplicate of #5"
# body must NOT resolve to ``duplicate`` (#1290).  Conservative outcomes
# (``security_threat`` / ``block_for_review`` / ``needs_more_info``) are ordered
# BEFORE the silent-cancel outcomes (``already_fixed`` / ``duplicate``) so that
# on any residual ambiguity the arbiter never favours a silent cancel.
_VALIDATOR_PREFIX_VERDICTS: list[tuple[str, str]] = [
    ("SECURITY_THREAT", "security_threat"),
    ("BLOCK_FOR_REVIEW", "block_for_review"),
    ("NEEDS_MORE_INFO", "needs_more_info"),
    ("CONFIRMED", "confirmed"),
    ("ALREADY_FIXED", "already_fixed"),
    ("DUPLICATE", "duplicate"),
]

# Validator card statuses the arbiter acts on.  A still-running / pending
# validator has no verdict yet and must NOT be parked — only terminal-or-blocked
# cards are arbitrated.
_ARBITER_READABLE_STATUSES = {"done", "complete", "completed", "blocked"}


def _map_validator_outcome(verdict: str | None) -> str:
    """Map a validator verdict to an arbiter action (never raises).

    ``None`` or any unrecognised verdict maps to :data:`ARBITER_PARK` — the
    pipeline is held for a human rather than silently advanced on ambiguity.
    """
    if not verdict:
        return ARBITER_PARK
    return _VALIDATOR_ARBITER_MAP.get(verdict.strip().lower(), ARBITER_PARK)


def _read_validator_verdict(slug: str, card: dict) -> str | None:
    """Resolve a validator card's structured verdict, or None (never raises).

    Read order mirrors ``classify_blocked`` (#1288): native closing-run metadata
    first, then the free-text JSON outcome block in the summary, then the legacy
    uppercase prefix.  Returns the verdict string (e.g. ``"confirmed"``) or None
    when nothing parseable is present — the caller safe-parks on None.
    """
    from core.iterate import outcomes as _outcomes  # lazy: avoid import cycle

    tid = card.get("id") or card.get("task_id") or ""

    # 1) native run metadata (metadata_transport transport).
    if tid:
        try:
            meta = kanban.run_outcome(slug, str(tid))
        except Exception:
            meta = None
        if meta:
            rec = _outcomes.parse_dict(meta)
            if rec is not None and rec.role == "validator":
                return rec.verdict

    # 2) free-text JSON outcome block in the recorded summary.
    summary = (card.get("summary") or card.get("last_summary")
               or card.get("latest_summary") or "")
    if summary:
        rec = _outcomes.parse(summary)
        if rec is not None and rec.role == "validator":
            return rec.verdict

    # 3) legacy prefix fallback — anchored to the leading token (mirrors
    # classify_blocked's startswith convention) so mid-body mentions of another
    # outcome cannot flip the verdict (#1290).
    upper = summary.strip().upper()
    for prefix, verdict in _VALIDATOR_PREFIX_VERDICTS:
        if upper.startswith(prefix):
            return verdict
    return None


def _arbitrate_validator_outcome(
    slug: str,
    provider,
    existing: set,
    *,
    validator_profile: str = "validator-daedalus",
    dry_run: bool = False,
    workdir: str = "",
    prefix_fallback: bool = True,
) -> List[int]:
    """6-outcome DAG pruner for the upfront-DAG world (#1290).

    For every terminal-or-blocked validator card of a managed issue, read the
    structured verdict and prune the pre-built stage graph accordingly (see the
    module comment above).  Returns the issue numbers that warrant an operator
    notification (human-gate / escalation) — cancels are silent so pruned
    branches never fire notifications (AC7).  Idempotent: cancels skip
    already-done cards and notifications are de-duped via ``_has_notified_block``.
    Never raises — provider / kanban helpers already degrade gracefully.
    """
    if provider is None or not provider.board_configured():
        return []
    enforced: List[int] = []
    for card in kanban.list_tasks(slug):
        if (card.get("assignee") or "").strip() != validator_profile:
            continue
        status = (card.get("status") or "").strip().lower()
        if status not in _ARBITER_READABLE_STATUSES:
            continue  # still running/pending — no verdict yet
        summary = (card.get("summary") or card.get("last_summary") or "").lower()
        # An infrastructure crash is not a validator verdict — leave it to the
        # crash-retry reconciler (mirrors _enforce_validator_blocks).
        if crash_retry.is_crash_class(slug, card, summary):
            continue
        n = extract_issue_number(card.get("title") or "")
        if n is None or n not in existing:
            continue

        verdict = _read_validator_verdict(slug, card)
        action = _map_validator_outcome(verdict)

        if action == ARBITER_KEEP:
            logger.info("dispatch: arbiter #%s — validator confirmed; DAG proceeds", n)
            continue

        if dry_run:
            logger.info(
                "[dry-run] arbiter #%s — verdict=%s action=%s", n, verdict, action)
            if action in (ARBITER_HUMAN, ARBITER_ESCALATE, ARBITER_PARK):
                enforced.append(n)
            continue

        if action == ARBITER_CANCEL:
            cancelled = kanban.close_issue_tasks(
                slug, n, summary=f"cancelled: validator {verdict}")
            logger.info(
                "dispatch: arbiter #%s — validator %s; cancelled %d downstream card(s)",
                n, verdict, len(cancelled))
            # silent: pruned branch fires no notification (AC7).
            continue

        if action == ARBITER_ESCALATE:
            provider.board_set_status(n, "Blocked")
            cancelled = kanban.close_issue_tasks(
                slug, n, summary="cancelled: validator SECURITY_THREAT — escalated")
            logger.info(
                "dispatch: arbiter #%s — SECURITY_THREAT; escalated + cancelled %d card(s)",
                n, len(cancelled))
        else:  # ARBITER_HUMAN or ARBITER_PARK
            provider.board_set_status(n, "Blocked")
            # Cancel any DAG stages that Hermes auto-promoted once the validator
            # completed (#1300 audit fix): mirrors the CANCEL/ESCALATE branches.
            # Without this, Hermes' dependency auto-promotion unblocks the PM
            # stage between the validator completing and the arbiter running, and
            # that PM card dispatches next tick even though the issue explicitly
            # requires human/reporter input.
            cancelled = kanban.close_issue_tasks(
                slug, n,
                summary=f"deferred: validator {verdict or 'unparseable'} — awaiting human",
            )
            logger.info(
                "dispatch: arbiter #%s — verdict=%s → human gate; "
                "cancelled %d downstream card(s)",
                n, verdict, len(cancelled))
            tid = card.get("id") or card.get("task_id")
            if tid:
                # Tag the validator card as awaiting human input (degrades if the
                # card is already terminal — block_task never raises).
                kanban.block_task(
                    slug, str(tid),
                    f"needs_input: validator {verdict or 'unparseable'} — human required",
                    kind="needs_input",
                )
            logger.info(
                "dispatch: arbiter #%s — verdict=%s → human gate (needs_input)",
                n, verdict)

        # Notify once per issue (human-gate + escalation), deduped.
        if not _has_notified_block(
            slug, n,
            validator_profile=validator_profile,
            workdir=workdir,
            prefix_fallback=prefix_fallback,
        ):
            enforced.append(n)
            _mark_notified_block(slug, n, validator_profile=validator_profile, workdir=workdir)
    return enforced

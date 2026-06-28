"""Stale-card sweeper (issues #186, #232; epic #181).

Kanban cards can get stuck with no visibility. This module detects two cases
and logs a warning for each:

* **blocked** cards that have sat with no activity for longer than a threshold
  (default 48h) — optionally archived off the active board via the native
  ``hermes kanban archive`` command (issue #186).
* **running** cards whose summary hasn't advanced for longer than a threshold
  (default 24h) — a worker that has died or wedged, otherwise invisible since
  the board still shows it as in-progress (issue #232).

It runs each dispatch tick alongside ``kanban.diagnostics`` and degrades
gracefully: any failure logs and returns, never breaking a run.

**Last-progress signal.** The board has no ``blocked_at``/``updated_at``
column, and its ``task_runs`` table is not reliably populated across
deployments. The most dependable "last made progress" timestamp is therefore
``last_heartbeat_at`` (which freezes once a worker stops reporting its summary),
falling back to ``started_at`` then ``created_at``. ``hermes kanban list
--json`` omits ``last_heartbeat_at``, so the sweeps enrich cards with a single
direct SQLite read (mirroring ``kanban.rename_task``).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

from core import kanban

logger = logging.getLogger("daedalus.sweeper")

DEFAULT_STALE_HOURS = 48
DEFAULT_RUNNING_STALE_HOURS = 24

# Timestamp columns to consult, in order of preference, for "last progress".
_SINCE_KEYS = ("last_heartbeat_at", "started_at", "created_at")


def blocked_since(card: Dict) -> Optional[int]:
    """Best epoch-seconds estimate of when ``card`` last made progress.

    Returns the first present, truthy value among ``last_heartbeat_at``,
    ``started_at``, ``created_at`` (coerced to ``int``), or ``None`` if none are
    available — in which case the card cannot be aged and is skipped.

    Status-agnostic: reused for both blocked and running cards.
    """
    for key in _SINCE_KEYS:
        val = card.get(key)
        if val:
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
    return None


def _find_stale(
    cards: List[Dict],
    *,
    status: str,
    now: int,
    threshold_hours: float,
) -> List[Tuple[Dict, float]]:
    """Pure detection: cards in ``status`` with no progress for > ``threshold_hours``.

    Returns ``[(card, age_hours)]`` sorted oldest-first. Cards not in ``status``,
    or whose age can't be determined, are skipped.
    """
    cutoff = now - threshold_hours * 3600
    stale: List[Tuple[Dict, float]] = []
    for card in cards:
        if (card.get("status") or "").lower() != status:
            continue
        # Skip cards that are already archived or have a non-matching status
        if (card.get("archived") or False):
            continue
        since = blocked_since(card)
        # Strict > threshold (i.e., > NOT >=): a card sitting for exactly
        # threshold_hours is NOT stale — it must have been stuck MORE than that.
        if since is None or since >= cutoff:
            continue
        stale.append((card, (now - since) / 3600.0))
    stale.sort(key=lambda pair: pair[1], reverse=True)
    return stale


def find_stale_blocked(
    cards: List[Dict],
    *,
    now: int,
    threshold_hours: float = DEFAULT_STALE_HOURS,
) -> List[Tuple[Dict, float]]:
    """Pure detection: blocked cards with no activity for > ``threshold_hours``.

    Returns ``[(card, age_hours)]`` sorted oldest-first. Cards that are not
    blocked, or whose age can't be determined, are skipped.
    """
    return _find_stale(cards, status="blocked", now=now, threshold_hours=threshold_hours)


def find_stale_running(
    cards: List[Dict],
    *,
    now: int,
    threshold_hours: float = DEFAULT_RUNNING_STALE_HOURS,
) -> List[Tuple[Dict, float]]:
    """Pure detection: running cards with no progress for > ``threshold_hours``.

    A running card whose ``last_heartbeat_at`` (the freshest summary-update
    signal) has frozen for longer than the threshold is almost certainly a dead
    or wedged worker. Returns ``[(card, age_hours)]`` sorted oldest-first; cards
    not running, or whose age can't be determined, are skipped.
    """
    return _find_stale(cards, status="running", now=now, threshold_hours=threshold_hours)


def _db_path(slug: str) -> str:
    return os.path.expanduser(f"~/.hermes/kanban/boards/{slug}/kanban.db")


def _heartbeats(slug: str, task_ids: List[str]) -> Dict[str, int]:
    """``{task_id: last_heartbeat_at}`` from the board DB. Degrades to ``{}``.

    Only ids found in the DB with a non-null heartbeat are returned, so callers
    can safely leave already-present timestamps untouched for the rest.
    """
    if not task_ids:
        return {}
    path = _db_path(slug)
    if not os.path.exists(path):
        return {}
    try:
        conn = sqlite3.connect(path)
        placeholders = ",".join("?" for _ in task_ids)
        rows = conn.execute(
            f"SELECT id, last_heartbeat_at FROM tasks WHERE id IN ({placeholders})",
            task_ids,
        ).fetchall()
        conn.close()
        return {r[0]: int(r[1]) for r in rows if r[1] is not None}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("sweeper: heartbeat lookup failed for %s: %s", slug, exc)
        return {}


def _archive_with_retry(
    slug: str,
    tid: str,
    *,
    max_attempts: int = 3,
) -> bool:
    """Archive a task with retry-safe error handling and idempotent logging.

    Retries up to ``max_attempts`` times on failure. Each attempt is logged at
    WARNING level with attempt number so retries are audit-safe. Failures are
    contained: exception or False both result in a WARNING and continue. After
    all attempts exhausted, logs ERROR and returns False. Never raises.

    Returns True on first successful archive, False if all attempts fail.
    Idempotent: re-archiving an already-archived card is treated as success by
    the underlying ``archive_task`` CLI — the retry logic mirrors that.
    """
    if not tid:
        return False
    for attempt in range(1, max_attempts + 1):
        try:
            ok = kanban.archive_task(slug, tid)
            if ok:
                logger.info("sweeper: archived card %s (attempt %d/%d)", tid, attempt, max_attempts)
                return True
            # archive_task already logged the failure internally
            logger.warning(
                "sweeper: archive returned False for card %s (attempt %d/%d)", tid, attempt, max_attempts
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "sweeper: archive raised for card %s (attempt %d/%d): %s", tid, attempt, max_attempts, exc
            )
        if attempt < max_attempts:
            time.sleep(0.1)
    logger.error("sweeper: failed to archive card %s after %d attempts", tid, max_attempts)
    return False


def sweep_stale_blocked(
    slug: str,
    *,
    threshold_hours: float = DEFAULT_STALE_HOURS,
    archive: bool = False,
    now: Optional[int] = None,
    dry_run: bool = False,
) -> List[str]:
    """Detect, warn, and optionally archive stale blocked cards.

    Returns the list of stale card ids found. ``archive`` (off by default) moves
    each stale card off the active board via ``_archive_with_retry`` with retry
    and error handling; failures do not break the sweep.
    ``dry_run`` logs intended actions without mutating the board.
    """
    now = int(time.time()) if now is None else int(now)
    cards = kanban.list_blocked(slug) or []
    if not cards:
        return []

    # Enrich blocked cards that lack a heartbeat (list --json omits it) with a
    # single DB read, so aging uses the freshest available activity timestamp.
    needs = [str(c.get("id")) for c in cards
             if c.get("id") and not c.get("last_heartbeat_at")]
    hb = _heartbeats(slug, needs)
    if hb:
        for c in cards:
            beat = hb.get(str(c.get("id")))
            if beat is not None:
                c["last_heartbeat_at"] = beat

    stale = find_stale_blocked(cards, now=now, threshold_hours=threshold_hours)
    stale_ids: List[str] = []
    for card, age in stale:
        tid = str(card.get("id") or "")
        label = card.get("title") or card.get("assignee") or "?"
        action = (
            "archiving" if (archive and not dry_run)
            else "[dry-run] would archive" if (archive and dry_run)
            else "manual intervention may be required"
        )
        logger.warning(
            "sweeper: card %s (%s) stuck in blocked for %.0fh (>%gh) — %s",
            tid, label, age, threshold_hours, action,
        )
        stale_ids.append(tid)
        if archive and not dry_run and tid:
            _archive_with_retry(slug, tid)
    return stale_ids


def sweep_stale_running(
    slug: str,
    *,
    threshold_hours: float = DEFAULT_RUNNING_STALE_HOURS,
    now: Optional[int] = None,
) -> List[str]:
    """Detect and warn about running cards stuck with no update for > N hours.

    A card whose worker has died or wedged stays in ``running`` indefinitely and
    is otherwise invisible. This warns (card id, assignee, hours elapsed) so a
    human can intervene; unlike blocked cards, running cards are never archived
    automatically. Returns the list of stale running card ids found.
    """
    now = int(time.time()) if now is None else int(now)
    cards = kanban.list_tasks(slug, status="running") or []
    if not cards:
        return []

    # ``list --json`` omits last_heartbeat_at; enrich from the DB so aging uses
    # the freshest summary-update signal (mirrors sweep_stale_blocked).
    needs = [str(c.get("id")) for c in cards
             if c.get("id") and not c.get("last_heartbeat_at")]
    hb = _heartbeats(slug, needs)
    if hb:
        for c in cards:
            beat = hb.get(str(c.get("id")))
            if beat is not None:
                c["last_heartbeat_at"] = beat

    stale = find_stale_running(cards, now=now, threshold_hours=threshold_hours)
    stale_ids: List[str] = []
    for card, age in stale:
        tid = str(card.get("id") or "")
        assignee = card.get("assignee") or card.get("title") or "?"
        logger.warning(
            "sweeper: card %s (%s) stuck in running for %.0fh (>%gh) with no "
            "summary update — worker may have died; manual intervention may be required",
            tid, assignee, age, threshold_hours,
        )
        stale_ids.append(tid)
    return stale_ids

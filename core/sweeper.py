"""Stale-card sweeper (issues #186, #232; epic #181).

Kanban cards can get stuck with no visibility. This module detects two cases
and logs a warning for each:

* **blocked** cards that have sat with no activity for longer than a threshold
  (default 48h) — optionally archived off the active board via the native
  ``hermes kanban archive`` command (issue #186).
* **running** cards whose summary hasn't advanced for longer than a threshold
  (default 30 min) — a worker that has died, wedged, or suspended on a headless
  permission prompt, otherwise invisible since the board still shows it as
  in-progress (issue #232). When ``reset`` is enabled (issue #1323) the sweep
  also self-heals: it re-blocks each stale running card with a crash-class
  reason so the crash-retry reconciler re-dispatches it on the SAME tick,
  freeing the ``execution.max_dispatch`` slot in minutes instead of stranding
  the whole pipeline until a 24h warn or a human unblock.

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
import re
import time

from core import kanban
from core.db import connect_wal

logger = logging.getLogger("daedalus.sweeper")

DEFAULT_STALE_HOURS = 48
# Shortened from 24h → 30 min (issue #1323): a suspended/dead worker holds the
# ``execution.max_dispatch`` slot the whole time it sits ``running``, so a fast
# threshold is the minimum needed for the pipeline to self-recover. The delegate
# wrapper heartbeats every 300s, so 30 min without any heartbeat (6 missed) is a
# confident dead-worker signal — a live worker never ages out.
DEFAULT_RUNNING_STALE_HOURS = 0.5

# Crash-class block reason stamped on a reclaimed stale-running card. Must begin
# with a marker that ``core.crash_retry.classify`` maps to the ``crash`` class
# (``coding-agent-failed:``) — and NOT a non-crash prefix — so the crash-retry
# reconciler owns the card and re-dispatches it (issue #1323).
STALE_RUNNING_RESET_REASON = "coding-agent-failed: STALE_RUNNING"

# Timestamp columns to consult, in order of preference, for "last progress".
_SINCE_KEYS = ("last_heartbeat_at", "started_at", "created_at")


def blocked_since(card: dict) -> int | None:
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
    cards: list[dict],
    *,
    status: str,
    now: int,
    threshold_hours: float,
) -> list[tuple[dict, float]]:
    """Pure detection: cards in ``status`` with no progress for > ``threshold_hours``.

    Returns ``[(card, age_hours)]`` sorted oldest-first. Cards not in ``status``,
    or whose age can't be determined, are skipped.
    """
    cutoff = now - threshold_hours * 3600
    stale: list[tuple[dict, float]] = []
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
    cards: list[dict],
    *,
    now: int,
    threshold_hours: float = DEFAULT_STALE_HOURS,
) -> list[tuple[dict, float]]:
    """Pure detection: blocked cards with no activity for > ``threshold_hours``.

    Returns ``[(card, age_hours)]`` sorted oldest-first. Cards that are not
    blocked, or whose age can't be determined, are skipped.
    """
    return _find_stale(cards, status="blocked", now=now, threshold_hours=threshold_hours)


def find_stale_running(
    cards: list[dict],
    *,
    now: int,
    threshold_hours: float = DEFAULT_RUNNING_STALE_HOURS,
) -> list[tuple[dict, float]]:
    """Pure detection: running cards with no progress for > ``threshold_hours``.

    A running card whose ``last_heartbeat_at`` (the freshest summary-update
    signal) has frozen for longer than the threshold is almost certainly a dead
    or wedged worker. Returns ``[(card, age_hours)]`` sorted oldest-first; cards
    not running, or whose age can't be determined, are skipped.
    """
    return _find_stale(cards, status="running", now=now, threshold_hours=threshold_hours)


def _db_path(slug: str) -> str:
    # Honor HERMES_HOME so tests (and non-default installs) never touch the real
    # ~/.hermes board. A hardcoded ~/.hermes here let test runs write cards onto
    # the live board, which the running gateway then executed — a runaway loop
    # (2026-07-02 incident).
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return os.path.join(home, "kanban", "boards", slug, "kanban.db")


def _heartbeats(slug: str, task_ids: list[str]) -> dict[str, int]:
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
        conn = connect_wal(path)
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


def _worker_pids(slug: str, task_ids: list[str]) -> dict[str, int]:
    """``{task_id: worker_pid}`` from the board DB for a fast dead-worker signal.

    The board records the OS pid of the worker it spawned. When that process is
    gone the card is a crash zombie *right now* — no need to wait out the
    heartbeat threshold. Degrades to ``{}`` (callers fall back to heartbeat aging).
    """
    if not task_ids:
        return {}
    path = _db_path(slug)
    if not os.path.exists(path):
        return {}
    try:
        conn = connect_wal(path)
        placeholders = ",".join("?" for _ in task_ids)
        rows = conn.execute(
            f"SELECT id, worker_pid FROM tasks WHERE id IN ({placeholders})",
            task_ids,
        ).fetchall()
        conn.close()
        return {r[0]: int(r[1]) for r in rows if r[1]}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("sweeper: worker_pid lookup failed for %s: %s", slug, exc)
        return {}


def _pid_alive(pid: int) -> bool:
    """True if a local process with ``pid`` exists. ``os.kill(pid, 0)`` sends no
    signal — it only probes existence/permission. Assumes the worker ran on THIS
    host (true for local coding-agent / local-LLM workers); a cross-host pid can
    only yield a false *alive* (→ safe heartbeat fallback), never a false reclaim."""
    if pid <= 0:
        return True  # unknown → don't treat as dead
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False  # no such process → worker is gone
    except PermissionError:
        return True  # exists but not ours → alive
    except OSError:
        return True  # inconclusive → don't reclaim on this signal


def find_dead_worker_running(
    cards: list[dict], pid_by_id: dict[str, int], *, alive_fn=_pid_alive,
) -> list[dict]:
    """Running cards whose recorded worker pid is DEAD — reclaim immediately,
    independent of heartbeat age. Pure/testable: liveness is injected."""
    dead: list[dict] = []
    for card in cards:
        if (card.get("status") or "").lower() != "running":
            continue
        pid = pid_by_id.get(str(card.get("id")))
        if pid and not alive_fn(pid):
            dead.append(card)
    return dead


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
    now: int | None = None,
    dry_run: bool = False,
) -> list[str]:
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
    stale_ids: list[str] = []
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
    reset: bool = False,
    now: int | None = None,
) -> list[str]:
    """Detect stale running cards; warn, and optionally self-heal via reset.

    A card whose worker has died, wedged, or suspended (e.g. on a headless
    permission prompt, issue #1323) stays in ``running`` indefinitely and is
    otherwise invisible — worse, it holds the ``execution.max_dispatch`` slot so
    the whole pipeline stalls. This always warns (card id, assignee, hours
    elapsed).

    When ``reset`` is True (issue #1323) it also re-blocks each stale card with
    a crash-class reason (``STALE_RUNNING_RESET_REASON``). Because the crash-retry
    reconciler runs immediately after the sweep on the same dispatch tick, the
    card is re-dispatched with a fresh worker and the ``max_dispatch`` slot is
    freed in minutes — no human unblock needed. Unlike blocked cards, running
    cards are never archived. Returns the list of stale running card ids found.
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

    # Fast dead-worker path: a running card whose recorded worker pid is gone is a
    # crash zombie NOW — reclaim without waiting out the heartbeat threshold. This is
    # what makes local-model self-heal fast instead of ~30 min (the qwen developer
    # crashes mid-run; heartbeat aging alone is slow). Heartbeat aging below still
    # catches wedged-but-alive workers and cross-host cases (dead-pid → safe fallback).
    all_ids = [str(c.get("id")) for c in cards if c.get("id")]
    dead_cards = find_dead_worker_running(cards, _worker_pids(slug, all_ids))
    dead_ids = {str(c.get("id")) for c in dead_cards}

    stale = find_stale_running(cards, now=now, threshold_hours=threshold_hours)
    # Reclaim the union: dead-pid cards (immediate) + heartbeat-stale cards. Dedup so a
    # card that is both isn't reblocked twice.
    seen: set = set()
    stale_ids: list[str] = []
    work: list[tuple[dict, float | None]] = (
        [(c, None) for c in dead_cards] + [(c, age) for c, age in stale]
    )
    for card, age in work:
        tid = str(card.get("id") or "")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        assignee = card.get("assignee") or card.get("title") or "?"
        is_dead = tid in dead_ids
        action = (
            "re-blocking crash-class so it re-dispatches and frees the slot"
            if reset else "manual intervention may be required"
        )
        if is_dead:
            logger.warning(
                "sweeper: card %s (%s) worker process is gone — crash zombie; %s",
                tid, assignee, action,
            )
        else:
            logger.warning(
                "sweeper: card %s (%s) stuck in running for %.0fh (>%gh) with no "
                "summary update — worker may have died; %s",
                tid, assignee, age, threshold_hours, action,
            )
        stale_ids.append(tid)
        if reset and tid:
            detail = (
                "worker process gone (fast dead-worker reclaim)"
                if is_dead
                else f"no summary update for {age:.0f}h (issue #1323 auto-reclaim)"
            )
            reason = f"{STALE_RUNNING_RESET_REASON} — {detail}"
            if kanban.block_task(slug, tid, reason):
                logger.info("sweeper: reset stale-running card %s → %s", tid, reason)
            else:
                logger.warning("sweeper: failed to reset stale-running card %s", tid)
    return stale_ids


# Bounded re-creation of role cards stuck in the terminal `triage` state. A flaky local
# model can crash-loop a role until the block-recurrence breaker escalates it to `triage`
# (terminal — complete/unblock/promote all reject it), which strands the whole pipeline
# and needs a human. This gives each such card a bounded number of fresh attempts.
DEFAULT_MAX_TRIAGE_RECOVERIES = 3
# The developer is recovered by the PR-aware F10/F12 path (adopt an open PR / open one
# from a pushed branch), so it is skipped here to avoid double-handling.
_TRIAGE_SKIP_ROLES = ("developer",)
_TRIAGE_RECOVER_RE = re.compile(r"\s*\[recover (\d+)\]\s*$")


def recover_triaged_cards(
    slug: str,
    *,
    skip_roles: tuple[str, ...] = _TRIAGE_SKIP_ROLES,
    max_recoveries: int = DEFAULT_MAX_TRIAGE_RECOVERIES,
    reset: bool = False,
) -> list[str]:
    """Re-create role cards stuck in terminal ``triage`` (bounded) so a flaky local model
    gets fresh attempts instead of being permanently stuck.

    Triage is terminal and immovable, so recovery = archive the stuck card (which, being
    terminal, keeps any gated child unblocked) and create a fresh card of the same role
    (same assignee/body/parents) with a ``[recover N]`` marker in the title. The marker is
    the attempt counter — once it reaches ``max_recoveries`` the card is left in triage for
    a human. Role is derived from the ``<role>-daedalus`` assignee convention; ``skip_roles``
    (developer) are left to the PR-aware path. Returns the ids of triaged cards acted on.
    When ``reset`` is False this only *reports* (warns) — nothing is mutated (dry-run).
    """
    recovered: list[str] = []
    for card in kanban.list_tasks(slug, status="triage") or []:
        if (card.get("status") or "").lower() != "triage":
            continue
        assignee = (card.get("assignee") or "").strip()
        role = assignee.split("-daedalus")[0] if assignee else ""
        if not role or role in skip_roles:
            continue
        tid = str(card.get("id") or "")
        if not tid:
            continue
        title = card.get("title") or ""
        m = _TRIAGE_RECOVER_RE.search(title)
        count = int(m.group(1)) if m else 0
        if count >= max_recoveries:
            logger.warning(
                "sweeper: triage card %s (%s) exhausted %d/%d recoveries — leaving for a human",
                tid, role, count, max_recoveries,
            )
            continue
        if not reset:
            logger.warning(
                "sweeper: triage card %s (%s) is recoverable (attempt %d/%d)",
                tid, role, count + 1, max_recoveries,
            )
            recovered.append(tid)
            continue
        base_title = _TRIAGE_RECOVER_RE.sub("", title).rstrip()
        detail = kanban.show_card(slug, tid) or {}
        dt = detail.get("task") or {}
        body = dt.get("body") or card.get("body") or ""
        parents = [
            p if isinstance(p, str) else (p.get("id") if isinstance(p, dict) else None)
            for p in (detail.get("parents") or [])
        ]
        parents = [p for p in parents if p]
        new_tid = kanban.create_task(
            slug,
            f"{base_title} [recover {count + 1}]",
            assignee=assignee,
            body=body,
            parents=parents or None,
        )
        if not new_tid:
            logger.warning("sweeper: triage-recovery could not re-create %s (%s)", tid, role)
            continue
        kanban.archive_task(slug, tid)
        logger.info(
            "sweeper: triage-recovery re-created %s (%s) as %s (attempt %d/%d)",
            tid, role, new_tid, count + 1, max_recoveries,
        )
        recovered.append(tid)
    return recovered

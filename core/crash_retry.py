"""Time-bounded crash retry for blocked / gave-up kanban cards (issue #1205).

When a card's worker crashes (agent process death, session/usage limit,
provider connection error, OS hiccup) the Hermes-core circuit breaker trips
after ``failure_limit`` *consecutive* failures and parks the card — with no
cooldown or time-based reset, two crashes seconds apart (a single transient
episode) used to strand the issue until a human ran ``hermes kanban unblock``
(incident: card ``t_34adae1f`` / #1198, stranded 46 minutes).

This module is the per-tick crash-recovery reconciler that replaces the
permanent give-up with bounded retries over wall-clock time:

* Candidates: ``gave_up`` cards, plus ``blocked`` cards whose evidence
  (block summary or ``last_failure_error``) matches a crash signature.
  Non-crash blocks (validator verdicts, ``qa-failed:``, ``review-required:``,
  human blocks) are never touched.
* Each candidate gets an *episode* record in the dispatch state file
  (``core.dispatch_state``): first crash time, attempts, last attempt time.
* Retries follow the stepped backoff schedule ``crash_retry_backoff_minutes``
  (default ``[0, 15, 30, 60, 120]``) and the episode counter RESETS after a
  quiet ``crash_retry_cooldown_minutes`` window, so a fresh transient episode
  never inherits a stale count.
* Once ``max_crash_retries`` attempts OR the ``crash_retry_window_hours``
  wall clock is exhausted, the card is escalated: its summary is rewritten to
  ``crash-retries-exhausted: <last error>``, a marker-deduped diagnostic
  comment is stamped on the card, and the caller is handed an ``escalated``
  action so it can notify humans. Terminal until a human unblocks.
* Re-dispatch goes through the native ``hermes kanban unblock``, which also
  resets the core breaker's ``consecutive_failures`` (a deliberate fresh
  start) and — because the ``gave_up`` breaker blocks via direct SQL rather
  than ``block_task`` — never increments ``block_recurrences``, so
  unblock → crash → unblock cannot trip Hermes' block-recurrence triage.

Transient vs deterministic: this reconciler only handles the *crash* class
(worker died / never produced output). A worker that RAN TO COMPLETION but
produced no artifact (PM without ``SPEC:``, validator without a verdict) is
already governed by the existing role retry caps (``max_pm_retries``,
``max_validator_retries``, ``max_developer_retries``) which escalate sooner —
that split is intentional (#1205).

Idempotency: the incremented attempt is persisted BEFORE unblocking so a
crash mid-tick cannot lose the count and a concurrent tick can never
double-dispatch the same card. The dispatcher's process FileLock (#1206) is
the first line of defense; the state-first write is the second.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from core import dispatch_state, kanban
from core.util import extract_issue_number

logger = logging.getLogger("daedalus.crash_retry")

# Comment marker stamped on a card when its retries are exhausted. Persistent
# escalation dedup (survives a lost state file) and human-visible diagnostics.
ESCALATED_MARKER = "<!-- daedalus:crash-retry-exhausted -->"

# Summary prefix a card gets when its crash retries are exhausted.
EXHAUSTED_PREFIX = "crash-retries-exhausted:"

# Crash signatures: the worker/agent process died or never ran. Mirrors
# iterate.classify_blocked's ``_crash_markers`` plus the transient
# provider/session failures from the #1205 incident and #1200.
_CRASH_MARKERS = (
    EXHAUSTED_PREFIX,
    "coding-agent-failed:",
    "coding_agent_died",
    "coding_agent_timeout",
    "pid not alive",
    "apiconnectionerror",
    "api connection error",
    "permission-error:",
    "exited with code",
    "agent crash",
    "session limit",
    "usage limit",
)

_DEFAULT_BACKOFF_MINUTES = [0, 15, 30, 60, 120]

_DEFAULTS: Dict[str, Any] = {
    "crash_retry_enabled": True,
    "max_crash_retries": 5,
    "crash_retry_backoff_minutes": _DEFAULT_BACKOFF_MINUTES,
    "crash_retry_cooldown_minutes": 120,
    "crash_retry_window_hours": 6,
}


def classify(evidence: str) -> Optional[str]:
    """Return ``"crash"`` when *evidence* matches a crash signature, else None.

    ``None`` means not crash-class — review-required / qa-failed / escalate /
    human blocks are owned by iterate and the PM flow, never retried here.
    """
    s = (evidence or "").lower()
    if not s:
        return None
    if any(m in s for m in _CRASH_MARKERS):
        return "crash"
    return None


def resolve_config(execution: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the crash-retry knobs from ``execution:`` over built-in defaults.

    Flat keys mirroring the existing retry-cap settings (``max_pm_retries``
    etc.). Missing, non-numeric, or non-positive values fall back to the
    default; the backoff schedule must be a non-empty list of non-negative
    numbers. Returns a copy callers can mutate freely.
    """
    raw = execution or {}
    out = _DEFAULTS.copy()
    out["crash_retry_backoff_minutes"] = list(_DEFAULT_BACKOFF_MINUTES)
    if not isinstance(raw, dict):
        return out
    if "crash_retry_enabled" in raw:
        out["crash_retry_enabled"] = bool(raw["crash_retry_enabled"])
    for key in ("max_crash_retries", "crash_retry_cooldown_minutes", "crash_retry_window_hours"):
        if key not in raw:
            continue
        try:
            iv = int(raw[key])
            if iv > 0:
                out[key] = iv
        except (TypeError, ValueError):
            continue
    sched = raw.get("crash_retry_backoff_minutes")
    if isinstance(sched, (list, tuple)) and sched:
        try:
            vals = [float(v) for v in sched]
            if all(v >= 0 for v in vals):
                out["crash_retry_backoff_minutes"] = vals
        except (TypeError, ValueError):
            pass
    return out


def _backoff_seconds(attempts: int, cfg: Dict[str, Any]) -> float:
    """Seconds to wait before attempt ``attempts + 1`` (stepped schedule).

    ``attempts`` completed retries index into the schedule; past the end the
    last step repeats. ``schedule[0]`` (default 0) makes the first retry of an
    episode immediate — crash → re-run on the very next tick.
    """
    sched = cfg["crash_retry_backoff_minutes"]
    minutes = sched[min(max(attempts, 0), len(sched) - 1)]
    return float(minutes) * 60.0


def _card_evidence(slug: str, card: Dict[str, Any]) -> str:
    """Best-effort crash evidence: block summary + last_failure_error.

    ``list --json`` never populates summary/last_summary, so fall back to
    ``show --json`` via get_latest_summary (same pattern as the team-blockers
    handler).
    """
    parts = [
        str(card.get("summary") or card.get("last_summary") or card.get("result") or ""),
        str(card.get("last_failure_error") or ""),
    ]
    if not any(parts):
        tid = str(card.get("id") or card.get("task_id") or "")
        if tid:
            parts.append(kanban.get_latest_summary(slug, tid))
    return " ".join(p for p in parts if p).strip()


def _gave_up_evidence_from_events(slug: str, task_id: str) -> Optional[str]:
    """Crash evidence from the card's event log, or None if not crash-class.

    The Hermes-core breaker does NOT leave a crash summary: it flips the card
    to ``blocked`` via direct SQL and records a ``gave_up`` task event (the
    error text lives in the event payload / ``last_failure_error``). So a
    blocked card whose summary doesn't classify is crash-class iff the most
    recent block-lifecycle event among {``gave_up``, ``blocked``,
    ``unblocked``} is ``gave_up`` — a worker/human ``blocked`` event or a
    later ``unblocked`` means the breaker is not what parked the card.

    Event schema is parsed defensively (``type``/``event``/``kind`` keys,
    error under ``error``/``data.error``/``message``); anything unparseable
    yields None — the safe default is to never auto-unblock an
    unclassifiable block.
    """
    card = kanban.show_card(slug, task_id)
    if not card:
        return None
    events = card.get("events")
    if not isinstance(events, list):
        return None
    for ev in reversed(events):  # most recent last — scan backwards
        if not isinstance(ev, dict):
            continue
        kind = str(ev.get("type") or ev.get("event") or ev.get("kind") or "").lower()
        if kind not in ("gave_up", "blocked", "unblocked"):
            continue
        if kind != "gave_up":
            return None
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        error = (
            ev.get("error")
            or data.get("error")
            or ev.get("message")
            or card.get("last_failure_error")
            or "worker gave up (crash breaker)"
        )
        return str(error)
    return None


def _already_escalated_on_card(slug: str, task_id: str) -> bool:
    """True if the card already carries the exhausted marker comment."""
    card = kanban.show_card(slug, task_id)
    if not card:
        return False
    return any(
        ESCALATED_MARKER in (c.get("body") or "") for c in card.get("comments") or []
    )


def reconcile(
    slug: str,
    workdir: str,
    execution: Dict[str, Any],
    *,
    now: Optional[float] = None,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Retry / escalate crashed cards; return the actions taken this tick.

    Each returned action is a dict with ``task_id``, ``issue``, ``action``
    (``retried`` | ``escalated``), ``attempt``, ``max_attempts``,
    ``elapsed_minutes``, ``summary``, ``title`` and ``assignee``. Cards still
    inside their backoff window produce no action (logged at debug). The
    caller sends notifications for ``escalated`` actions and forces a
    same-tick ``kanban.dispatch`` when any ``retried`` action exists, so the
    unblocked card re-runs within the same trigger (cron tick or
    ``on_session_end`` advance — both funnel through ``run()``).

    At most one re-dispatch per card per tick by construction (each card is
    visited once and the attempt is persisted state-first).

    Never raises: kanban/state failures are logged and the card is skipped
    (it will be reconsidered next tick).
    """
    cfg = resolve_config(execution)
    if not cfg["crash_retry_enabled"]:
        return []
    ts = time.time() if now is None else float(now)
    actions: List[Dict[str, Any]] = []

    try:
        tasks = kanban.list_tasks(slug)
    except Exception as exc:  # pragma: no cover - kanban helpers already degrade
        logger.warning("crash-retry: list_tasks failed for %s: %s", slug, exc)
        return []

    candidate_ids: set = set()
    for card in tasks:
        try:
            action = _reconcile_card(slug, workdir, cfg, card, ts, dry_run, candidate_ids)
        except Exception as exc:
            logger.warning(
                "crash-retry: card %s failed: %s — skipping until next tick",
                card.get("id"), exc,
            )
            continue
        if action:
            actions.append(action)

    _cleanup_recovered(workdir, candidate_ids, dry_run)
    return actions


def _reconcile_card(
    slug: str,
    workdir: str,
    cfg: Dict[str, Any],
    card: Dict[str, Any],
    ts: float,
    dry_run: bool,
    candidate_ids: set,
) -> Optional[Dict[str, Any]]:
    """Apply the retry policy to one card. Returns an action dict or None."""
    status = (card.get("status") or "").lower()
    if status not in ("blocked", "gave_up"):
        return None
    tid = str(card.get("id") or card.get("task_id") or "")
    if not tid:
        return None
    evidence = _card_evidence(slug, card)
    # A gave_up card is by definition a repeated worker crash even when its
    # evidence carries no marker (the breaker trips on spawn/session deaths);
    # a blocked card must positively match a crash signature — in its
    # summary/last_failure_error, or via a breaker ``gave_up`` event (the
    # primary incident case: the breaker blocks with an EMPTY summary and
    # only the event log carries the crash).
    if status == "blocked" and classify(evidence) is None:
        event_evidence = _gave_up_evidence_from_events(slug, tid)
        if event_evidence is None:
            return None  # non-crash block — owned by iterate / PM consultation
        evidence = event_evidence
    candidate_ids.add(tid)

    entry = dispatch_state.get_crash_retry(workdir, tid) or {}
    if entry.get("escalated"):
        # Exhausted — terminal until a human unblocks (which moves the card
        # out of blocked/gave_up, so _cleanup_recovered resets the episode).
        return None
    attempts = int(entry.get("attempts") or 0)
    first_ts = float(entry.get("first_crash_ts") or ts)
    last_ts = float(entry.get("last_attempt_ts") or first_ts)

    # Cooldown: a quiet window since the last activity means this is a FRESH
    # transient episode — it must not inherit the old count (#1205).
    if entry and ts - last_ts > cfg["crash_retry_cooldown_minutes"] * 60:
        logger.info(
            "crash-retry: %s cooldown elapsed (%.0f min quiet) — resetting episode",
            tid, (ts - last_ts) / 60,
        )
        entry, attempts, first_ts, last_ts = {}, 0, ts, ts

    max_attempts = int(cfg["max_crash_retries"])
    elapsed_min = (ts - first_ts) / 60
    issue_n = extract_issue_number(card.get("title") or "")

    # Exhaustion: attempt cap OR wall-clock window (only meaningful once at
    # least one retry happened — a brand-new episode starts at elapsed 0).
    exhausted = attempts >= max_attempts or (
        attempts > 0 and ts - first_ts > cfg["crash_retry_window_hours"] * 3600
    )
    if exhausted:
        return _escalate(
            slug, workdir, tid, entry, card, evidence,
            attempts, max_attempts, elapsed_min, issue_n, ts, dry_run,
        )

    # Backoff: attempt n+1 is allowed only after schedule[n] minutes.
    wait = _backoff_seconds(attempts, cfg)
    if attempts > 0 and ts - last_ts < wait:
        logger.debug(
            "crash-retry: %s in backoff — %.0fs of %.0fs elapsed (attempt %d/%d)",
            tid, ts - last_ts, wait, attempts, max_attempts,
        )
        return None

    attempt_n = attempts + 1
    if dry_run:
        logger.info(
            "[dry-run] crash-retry: would unblock %s (#%s) — attempt %d/%d, "
            "%.0f min since first crash",
            tid, issue_n, attempt_n, max_attempts, elapsed_min,
        )
        return None
    # Persist the attempt BEFORE unblocking: a crash mid-tick cannot lose the
    # count, and a concurrent tick reading the state mid-flight sees the
    # attempt as spent and stays in backoff.
    dispatch_state.set_crash_retry(
        workdir, tid,
        {
            "first_crash_ts": first_ts,
            "attempts": attempt_n,
            "last_attempt_ts": ts,
            "escalated": False,
            "class": "crash",
        },
    )
    reason = (
        f"crash-retry: auto re-dispatch attempt {attempt_n}/{max_attempts} "
        f"({elapsed_min:.0f} min since first crash) "
        f"[{(evidence or 'no failure details')[:120]}] (#1205)"
    )
    if not kanban.unblock_task(slug, tid, reason=reason):
        logger.warning(
            "crash-retry: unblock failed for %s (#%s) — attempt %d recorded, "
            "will back off and retry next tick",
            tid, issue_n, attempt_n,
        )
        return None
    logger.info(
        "crash-retry: unblocked %s (#%s) — attempt %d/%d, %.0f min since first crash",
        tid, issue_n, attempt_n, max_attempts, elapsed_min,
    )
    return {
        "action": "retried",
        "task_id": tid,
        "issue": issue_n,
        "attempt": attempt_n,
        "max_attempts": max_attempts,
        "elapsed_minutes": round(elapsed_min, 1),
        "summary": evidence,
        "title": card.get("title") or "",
        "assignee": card.get("assignee") or "",
    }


def _escalate(
    slug: str,
    workdir: str,
    tid: str,
    entry: Dict[str, Any],
    card: Dict[str, Any],
    evidence: str,
    attempts: int,
    max_attempts: int,
    elapsed_min: float,
    issue_n: Optional[int],
    ts: float,
    dry_run: bool,
) -> Optional[Dict[str, Any]]:
    """Exhausted retries → real hard block + diagnostics + notify action."""
    if dry_run:
        logger.info(
            "[dry-run] crash-retry: would escalate %s (#%s) — %d/%d attempts "
            "over %.0f min exhausted",
            tid, issue_n, attempts, max_attempts, elapsed_min,
        )
        return None
    entry.update(
        {
            "attempts": attempts,
            "last_attempt_ts": ts,
            "first_crash_ts": entry.get("first_crash_ts") or ts,
            "escalated": True,
            "class": "crash",
        }
    )
    # Belt-and-braces dedup: a lost/reset state file must not re-notify a card
    # that already carries the exhausted marker.
    if _already_escalated_on_card(slug, tid):
        dispatch_state.set_crash_retry(workdir, tid, entry)
        return None
    dispatch_state.set_crash_retry(workdir, tid, entry)
    last_error = (evidence or "no failure details").strip()
    kanban.edit_summary(slug, tid, f"{EXHAUSTED_PREFIX} {last_error[:200]}")
    diag = (
        f"{ESCALATED_MARKER}\n"
        f"⚠️ **Crash retries exhausted** — {attempts}/{max_attempts} automatic "
        f"re-dispatches over {elapsed_min:.0f} min failed.\n\n"
        f"Last failure: {last_error[:300]}\n\n"
        f"The card stays hard-blocked. Recovery: fix the underlying cause, then "
        f"`hermes kanban unblock {tid}` (this resets the crash-retry counter)."
    )
    if not kanban.comment(slug, tid, diag):
        logger.warning(
            "crash-retry: failed to stamp escalation comment on %s — "
            "state flag still set, no retry loop",
            tid,
        )
    logger.warning(
        "crash-retry: ESCALATED %s (#%s) — %d/%d attempts over %.0f min "
        "exhausted — human unblock required (last error: %s)",
        tid, issue_n, attempts, max_attempts, elapsed_min, last_error[:200],
    )
    return {
        "action": "escalated",
        "task_id": tid,
        "issue": issue_n,
        "attempt": attempts,
        "max_attempts": max_attempts,
        "elapsed_minutes": round(elapsed_min, 1),
        "summary": last_error,
        "title": card.get("title") or "",
        "assignee": card.get("assignee") or "",
    }


def _cleanup_recovered(workdir: str, candidate_ids: set, dry_run: bool) -> None:
    """Drop episodes whose card is no longer blocked/gave_up (recovered).

    Covers both success-mid-retry (the card ran and completed) and a manual
    human unblock of an escalated card — either way the next crash starts a
    fresh episode with a zeroed counter.
    """
    if dry_run:
        return
    for tid, entry in dispatch_state.all_crash_retry(workdir).items():
        if tid in candidate_ids:
            continue
        dispatch_state.clear_crash_retry(workdir, tid)
        logger.info(
            "crash-retry: %s recovered after %s attempt(s)%s — episode cleared",
            tid,
            entry.get("attempts", 0),
            " (was escalated)" if entry.get("escalated") else "",
        )

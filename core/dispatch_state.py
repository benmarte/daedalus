"""File-based dispatch state for Daedalus.

State is persisted to ``{workdir}/.hermes/daedalus_dispatch_state.json`` so it
survives across cron ticks.  All public functions are safe to call concurrently
from a single process — the file is read and written atomically (read / mutate /
write-to-tmpfile / rename).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional


def _state_path(workdir: str) -> Path:
    return Path(workdir) / ".hermes" / "daedalus_dispatch_state.json"


def _load(workdir: str) -> Dict[str, Any]:
    p = _state_path(workdir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(workdir: str, state: Dict[str, Any]) -> None:
    p = _state_path(workdir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Issue dispatch timestamps ─────────────────────────────────────────────────

def record_dispatch(workdir: str, issue_number: int) -> None:
    """Record the current time as the dispatch timestamp for *issue_number*."""
    state = _load(workdir)
    issues = state.setdefault("issues", {})
    issues[str(issue_number)] = {"dispatched_at": time.time()}
    _save(workdir, state)


def get_dispatch_age_hours(workdir: str, issue_number: int) -> Optional[float]:
    """Return hours since *issue_number* was dispatched, or *None* if unknown.

    Returns *None* (not an error) when the issue has never been dispatched or
    when the stored record is malformed.
    """
    state = _load(workdir)
    entry = state.get("issues", {}).get(str(issue_number))
    if not isinstance(entry, dict):
        return None
    ts = entry.get("dispatched_at")
    if not isinstance(ts, (int, float)):
        return None
    return (time.time() - float(ts)) / 3600.0


def clear_dispatch(workdir: str, issue_number: int) -> None:
    """Remove the dispatch record for *issue_number* (e.g. after it's closed)."""
    state = _load(workdir)
    state.get("issues", {}).pop(str(issue_number), None)
    _save(workdir, state)


# ── PR / issue flags (idempotency guards) ─────────────────────────────────────

def has_pr_flag(workdir: str, number: int, flag: str) -> bool:
    """Return *True* if *flag* is set for *number* (PR or issue)."""
    state = _load(workdir)
    return bool(state.get("pr_flags", {}).get(str(number), {}).get(flag))


def set_pr_flag(workdir: str, number: int, flag: str) -> None:
    """Set *flag* for *number* (PR or issue)."""
    state = _load(workdir)
    flags = state.setdefault("pr_flags", {})
    flags.setdefault(str(number), {})[flag] = True
    _save(workdir, state)


# ── Reviewer SHA tracking ─────────────────────────────────────────────────────

def record_review(workdir: str, pr_number: int, reviewer: str, sha: str) -> None:
    """Record that *reviewer* reviewed PR *pr_number* at commit *sha*."""
    state = _load(workdir)
    reviews = state.setdefault("reviews", {})
    reviews.setdefault(str(pr_number), {})[reviewer] = sha
    _save(workdir, state)


def get_review_sha(workdir: str, pr_number: int, reviewer: str) -> Optional[str]:
    """Return the commit SHA at which *reviewer* last reviewed *pr_number*, or *None*."""
    state = _load(workdir)
    return state.get("reviews", {}).get(str(pr_number), {}).get(reviewer)


# ── Notification thread anchors (issue #121) ──────────────────────────────────
# Each managed issue gets one conversation thread per notification target so
# agent comments (spec posts, PR opens, reviews, merges) mirror into the same
# thread instead of scattering as standalone messages. The anchor is whatever
# id the platform threads on — Slack ``thread_ts``, Discord/Telegram message id
# — captured from ``hermes send --json`` and reused as ``target:<anchor>`` on
# subsequent replies. ``events`` records which lifecycle events already reached
# a given target so the same event is not re-posted on a later tick.


def _thread_entry(state: Dict[str, Any], issue_number: int) -> Dict[str, Any]:
    """Return the (mutable) per-issue thread entry, creating it if absent."""
    threads = state.setdefault("threads", {})
    entry = threads.setdefault(str(issue_number), {})
    entry.setdefault("anchors", {})
    entry.setdefault("events", {})
    return entry


def get_thread_anchor(workdir: str, issue_number: int, target: str) -> Optional[str]:
    """Return the stored thread anchor for *issue_number* on *target*, or *None*.

    *None* means no root message has been posted yet (or the record is
    malformed) — the caller should post a fresh root and store its id via
    :func:`set_thread_anchor`.
    """
    state = _load(workdir)
    entry = state.get("threads", {}).get(str(issue_number), {})
    anchors = entry.get("anchors") if isinstance(entry, dict) else None
    anchor = anchors.get(target) if isinstance(anchors, dict) else None
    return anchor if isinstance(anchor, str) and anchor else None


def set_thread_anchor(workdir: str, issue_number: int, target: str, anchor: str) -> None:
    """Store *anchor* as the thread root for *issue_number* on *target*.

    A falsy *anchor* is ignored — platforms that deliver without a usable
    thread id simply get standalone (un-threaded) replies.
    """
    if not anchor:
        return
    state = _load(workdir)
    _thread_entry(state, issue_number)["anchors"][target] = anchor
    _save(workdir, state)


def thread_event_seen(workdir: str, issue_number: int, target: str, event_key: str) -> bool:
    """Return *True* if *event_key* was already mirrored to *issue_number* on *target*."""
    state = _load(workdir)
    entry = state.get("threads", {}).get(str(issue_number), {})
    events = entry.get("events") if isinstance(entry, dict) else None
    seen = events.get(target) if isinstance(events, dict) else None
    return event_key in seen if isinstance(seen, list) else False


def mark_thread_event(workdir: str, issue_number: int, target: str, event_key: str) -> None:
    """Record that *event_key* was mirrored to *issue_number* on *target*.

    Idempotent: recording the same key twice is a no-op. Pairs with
    :func:`thread_event_seen` for cross-tick duplicate suppression.
    """
    state = _load(workdir)
    seen = _thread_entry(state, issue_number)["events"].setdefault(target, [])
    if event_key not in seen:
        seen.append(event_key)
        _save(workdir, state)


def clear_threads(workdir: str, issue_number: int) -> None:
    """Drop all thread anchors / event markers for *issue_number*.

    Call once the issue's lifecycle is over (after the final reply is posted)
    so a future re-open starts a fresh thread.
    """
    state = _load(workdir)
    if state.get("threads", {}).pop(str(issue_number), None) is not None:
        _save(workdir, state)

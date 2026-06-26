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
    """Record the current time as the dispatch timestamp for *issue_number*.

    Preserves any other keys already on the entry (e.g. ``threads`` /
    ``thread_events``) so re-dispatching an issue never wipes its thread anchors.
    """
    state = _load(workdir)
    issues = state.setdefault("issues", {})
    entry = issues.setdefault(str(issue_number), {})
    if not isinstance(entry, dict):
        entry = {}
        issues[str(issue_number)] = entry
    entry["dispatched_at"] = time.time()
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


# ── Per-issue platform threads (issue/PR comment mirroring) ───────────────────
#
# Each managed issue mirrors its agent conversation into one thread per
# configured notification target.  We persist two maps under the issue entry:
#
#   "threads":       {target: anchor}        — the thread anchor per target
#                                              (Slack thread_ts / Discord msg id)
#   "thread_events": {target: [event_key]}   — events already mirrored, for
#                                              cross-tick duplicate suppression
#
# Keyed by the *full* ``hermes send`` target string (e.g. ``slack:C123``) rather
# than by bare platform name, because an anchor is channel-specific: a Slack ts
# only resolves a thread within the channel that produced it.


def _issue_entry(state: Dict[str, Any], issue_number: int) -> Dict[str, Any]:
    """Return (creating if needed) the mutable issue entry for *issue_number*."""
    issues = state.setdefault("issues", {})
    entry = issues.setdefault(str(issue_number), {})
    if not isinstance(entry, dict):
        entry = {}
        issues[str(issue_number)] = entry
    return entry


def get_thread_anchor(workdir: str, issue_number: int, target: str) -> Optional[str]:
    """Return the stored thread anchor for *target* on *issue_number*, or *None*."""
    entry = _load(workdir).get("issues", {}).get(str(issue_number))
    if not isinstance(entry, dict):
        return None
    threads = entry.get("threads")
    if not isinstance(threads, dict):
        return None
    anchor = threads.get(target)
    return str(anchor) if anchor else None


def set_thread_anchor(workdir: str, issue_number: int, target: str, anchor: str) -> None:
    """Persist the thread *anchor* for *target* on *issue_number*."""
    state = _load(workdir)
    entry = _issue_entry(state, issue_number)
    threads = entry.setdefault("threads", {})
    if not isinstance(threads, dict):
        threads = {}
        entry["threads"] = threads
    threads[target] = str(anchor)
    _save(workdir, state)


def get_thread_anchors(workdir: str, issue_number: int) -> Dict[str, str]:
    """Return the full ``{target: anchor}`` map for *issue_number* (may be empty)."""
    entry = _load(workdir).get("issues", {}).get(str(issue_number))
    if not isinstance(entry, dict):
        return {}
    threads = entry.get("threads")
    return dict(threads) if isinstance(threads, dict) else {}


def has_thread_event(workdir: str, issue_number: int, target: str, event_key: str) -> bool:
    """Return *True* if *event_key* was already mirrored to *target* for this issue."""
    entry = _load(workdir).get("issues", {}).get(str(issue_number))
    if not isinstance(entry, dict):
        return False
    events = entry.get("thread_events")
    if not isinstance(events, dict):
        return False
    return event_key in (events.get(target) or [])


def mark_thread_event(workdir: str, issue_number: int, target: str, event_key: str) -> None:
    """Record that *event_key* has been mirrored to *target* for this issue."""
    state = _load(workdir)
    entry = _issue_entry(state, issue_number)
    events = entry.setdefault("thread_events", {})
    if not isinstance(events, dict):
        events = {}
        entry["thread_events"] = events
    lst = events.setdefault(target, [])
    if not isinstance(lst, list):
        lst = []
        events[target] = lst
    if event_key not in lst:
        lst.append(event_key)
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

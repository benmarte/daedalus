"""File-based dispatch state for Daedalus.

State is persisted to ``{workdir}/.hermes/daedalus_dispatch_state.json`` so it
survives across cron ticks.  All public functions are safe to call concurrently
from a single process — the file is read and written atomically (read / mutate /
write-to-tmpfile / rename).
"""

from __future__ import annotations

import hashlib
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


# ── Crash-retry bookkeeping (issue #1205) ─────────────────────────────────────
#
# Per-card retry episodes for the crash reconciler (core/crash_retry.py).
# Keyed by kanban task id under a top-level ``crash_retry`` map:
#
#   {"first_crash_ts": float, "attempts": int, "last_attempt_ts": float,
#    "escalated": bool, "class": "crash"}
#
# The reconciler persists the incremented attempt BEFORE unblocking the card so
# a concurrent tick that reads the state mid-flight sees the attempt as already
# spent (at most one re-dispatch per card per tick).


def get_crash_retry(workdir: str, task_id: str) -> Optional[Dict[str, Any]]:
    """Return the crash-retry entry for *task_id*, or *None* if absent/malformed."""
    table = _load(workdir).get("crash_retry")
    if not isinstance(table, dict):
        return None
    entry = table.get(str(task_id))
    return dict(entry) if isinstance(entry, dict) else None


def set_crash_retry(workdir: str, task_id: str, entry: Dict[str, Any]) -> None:
    """Persist *entry* as the crash-retry record for *task_id*."""
    state = _load(workdir)
    table = state.setdefault("crash_retry", {})
    if not isinstance(table, dict):
        table = {}
        state["crash_retry"] = table
    table[str(task_id)] = dict(entry)
    _save(workdir, state)


def clear_crash_retry(workdir: str, task_id: str) -> None:
    """Remove the crash-retry record for *task_id* (card recovered / archived)."""
    state = _load(workdir)
    table = state.get("crash_retry")
    if isinstance(table, dict) and table.pop(str(task_id), None) is not None:
        _save(workdir, state)


def all_crash_retry(workdir: str) -> Dict[str, Dict[str, Any]]:
    """Return the full ``{task_id: entry}`` crash-retry map (may be empty)."""
    table = _load(workdir).get("crash_retry")
    if not isinstance(table, dict):
        return {}
    return {str(k): dict(v) for k, v in table.items() if isinstance(v, dict)}


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


# ── Config fingerprint (coding_agent + model.default) ────────────────────────
#
# On each dispatcher tick, compute a deterministic SHA-256 hash of the current
# values of ``execution.coding_agent`` and the global ``model.default`` setting.
# The fingerprint is persisted in the dispatch state file so a subsequent tick
# can detect when either value has changed (e.g. to trigger re-injection of the
# ``--model`` flag or re-evaluation of delegation blocks).

def compute_config_fingerprint(coding_agent: Optional[str], model_default: Optional[str]) -> str:
    """Return a deterministic SHA-256 hex digest of *coding_agent* and *model_default*.

    Both values are coerced to strings (``None`` → ``""``) and encoded as a
    canonical JSON object with sorted keys so the hash is stable regardless of
    argument order or whitespace.  Identical inputs always produce the same
    digest; changing either value produces a different digest.
    """
    payload = json.dumps(
        {"coding_agent": coding_agent or "", "model_default": model_default or ""},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def set_config_fingerprint(workdir: str, fingerprint: str) -> None:
    """Persist *fingerprint* as the current config fingerprint for *workdir*."""
    state = _load(workdir)
    state["config_fingerprint"] = fingerprint
    _save(workdir, state)


def get_config_fingerprint(workdir: str) -> Optional[str]:
    """Return the stored config fingerprint for *workdir*, or *None* if unset."""
    state = _load(workdir)
    fp = state.get("config_fingerprint")
    return str(fp) if fp else None


# ── Resync fingerprint (profile-sync tracking) ────────────────────────────────
#
# The config fingerprint captures coding_agent + model.default. The *resync*
# fingerprint captures the same tuple but is used specifically for detecting
# when to re-sync global model config into *-daedalus profile config files.
# Keeping these separate allows the config fingerprint to be used for other
# fingerprint-based logic (e.g. caching) without triggering profile resyncs.

def set_resync_fingerprint(workdir: str, fingerprint: str) -> None:
    """Persist *fingerprint* as the current resync fingerprint for *workdir*."""
    state = _load(workdir)
    state["resync_fingerprint"] = fingerprint
    _save(workdir, state)


def get_resync_fingerprint(workdir: str) -> Optional[str]:
    """Return the stored resync fingerprint for *workdir*, or *None* if unset."""
    state = _load(workdir)
    fp = state.get("resync_fingerprint")
    return str(fp) if fp else None


# ── Config values (coding_agent + model.default for resync) ──────────────────
#
# Persist the resolved coding_agent and model.default values so that a subsequent
# tick can compare the current resolved values against what was last used to
# trigger a profile resync. This lets _resync_profiles_to_model() detect changes
# between ticks even if the fingerprint hasn't changed (e.g. if the fingerprint
# is the same but the individual values did).

def set_config_values(workdir: str, coding_agent: Optional[str], model_default: Optional[str]) -> None:
    """Persist the resolved coding_agent and model.default for *workdir*.

    Used by the resync logic to compare last-resynced values against current
    resolved values. None values are stored as empty strings for stability.
    """
    state = _load(workdir)
    state["config_values"] = {
        "coding_agent": coding_agent or "",
        "model_default": model_default or "",
    }
    _save(workdir, state)


def get_config_values(workdir: str) -> Optional[Dict[str, str]]:
    """Return the stored config values for *workdir*, or *None* if unset.

    Returns a dict with keys "coding_agent" and "model_default". Both values
    are strings (empty string if originally None).
    """
    state = _load(workdir)
    vals = state.get("config_values")
    if not isinstance(vals, dict):
        return None
    return {
        "coding_agent": str(vals.get("coding_agent", "")),
        "model_default": str(vals.get("model_default", "")),
    }

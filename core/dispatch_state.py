"""Lightweight dispatch-state persistence.

Tracks per-issue dispatch timestamps and reviewer-approval SHAs in
{workdir}/.hermes/daedalus_dispatch_state.json. All I/O is best-effort:
read/write failures are logged at DEBUG level and ignored so a corrupt
state file can never break a dispatch tick.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("daedalus.dispatch_state")

_STATE_FILE = ".hermes/daedalus_dispatch_state.json"


def _path(workdir: str) -> Path:
    return Path(workdir) / _STATE_FILE


def _load(workdir: str) -> Dict[str, Any]:
    if not workdir:
        return {}
    p = _path(workdir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text()) or {}
    except Exception as e:
        logger.debug("dispatch_state: could not read %s: %s", p, e)
        return {}


def _save(workdir: str, state: Dict[str, Any]) -> None:
    if not workdir:
        return
    p = _path(workdir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.warning("dispatch_state: could not write %s: %s", p, e)


# ── dispatch timestamp tracking ──────────────────────────────────────────────

def record_dispatch(workdir: str, issue_number: int) -> None:
    """Record that issue_number was dispatched now (first-write-wins)."""
    if not workdir:
        return
    state = _load(workdir)
    key = str(issue_number)
    if key not in state:
        state[key] = {"dispatched_at": time.time()}
        _save(workdir, state)


def get_dispatch_age_hours(workdir: str, issue_number: int) -> Optional[float]:
    """Hours since issue_number was first dispatched, or None if not recorded.

    Returns None (never raises) when the state file is missing, corrupted, or
    the dispatched_at value is not a valid numeric timestamp.
    """
    if not workdir:
        return None
    state = _load(workdir)
    entry = state.get(str(issue_number))
    if not isinstance(entry, dict):
        return None
    ts = entry.get("dispatched_at")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    return (time.time() - ts) / 3600.0


def clear_issue(workdir: str, issue_number: int) -> None:
    """Remove all state for an issue (call when the issue is completed)."""
    if not workdir:
        return
    state = _load(workdir)
    removed = state.pop(str(issue_number), None)
    # Also remove any PR-level flags for this issue
    pr_keys = [k for k in state if k.startswith("pr_") and k.endswith("_flags")]
    if removed is not None or pr_keys:
        _save(workdir, state)


# ── reviewer SHA tracking (re-review trigger) ────────────────────────────────

def record_reviewer_sha(workdir: str, issue_number: int, sha: str) -> None:
    """Store the PR head SHA at the moment of reviewer approval."""
    if not workdir or not sha:
        return
    state = _load(workdir)
    entry = state.setdefault(str(issue_number), {})
    entry["reviewer_approved_sha"] = sha
    _save(workdir, state)


def get_reviewer_sha(workdir: str, issue_number: int) -> Optional[str]:
    """The SHA recorded when the reviewer last approved, or None."""
    if not workdir:
        return None
    entry = _load(workdir).get(str(issue_number))
    if not isinstance(entry, dict):
        return None
    return entry.get("reviewer_approved_sha") or None


# ── per-PR one-shot flag tracking (size gate / forbidden file warnings) ──────

def has_pr_flag(workdir: str, pr_number: int, flag: str) -> bool:
    """True if ``flag`` was already posted for ``pr_number``."""
    state = _load(workdir)
    return flag in (state.get(f"pr_{pr_number}_flags") or [])


def set_pr_flag(workdir: str, pr_number: int, flag: str) -> None:
    """Record that ``flag`` has been posted for ``pr_number``."""
    if not workdir:
        return
    state = _load(workdir)
    key = f"pr_{pr_number}_flags"
    flags: List[str] = state.setdefault(key, [])
    if flag not in flags:
        flags.append(flag)
        _save(workdir, state)

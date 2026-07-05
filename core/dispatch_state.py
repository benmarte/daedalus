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
from typing import Any


def _state_path(workdir: str) -> Path:
    return Path(workdir) / ".hermes" / "daedalus_dispatch_state.json"


def _load(workdir: str) -> dict[str, Any]:
    p = _state_path(workdir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(workdir: str, state: dict[str, Any]) -> None:
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


def get_dispatch_age_hours(workdir: str, issue_number: int) -> float | None:
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


def _issue_entry(state: dict[str, Any], issue_number: int) -> dict[str, Any]:
    """Return (creating if needed) the mutable issue entry for *issue_number*."""
    issues = state.setdefault("issues", {})
    entry = issues.setdefault(str(issue_number), {})
    if not isinstance(entry, dict):
        entry = {}
        issues[str(issue_number)] = entry
    return entry


def get_thread_anchor(workdir: str, issue_number: int, target: str) -> str | None:
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


def get_thread_anchors(workdir: str, issue_number: int) -> dict[str, str]:
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


def get_crash_retry(workdir: str, task_id: str) -> dict[str, Any] | None:
    """Return the crash-retry entry for *task_id*, or *None* if absent/malformed."""
    table = _load(workdir).get("crash_retry")
    if not isinstance(table, dict):
        return None
    entry = table.get(str(task_id))
    return dict(entry) if isinstance(entry, dict) else None


def set_crash_retry(workdir: str, task_id: str, entry: dict[str, Any]) -> None:
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


def all_crash_retry(workdir: str) -> dict[str, dict[str, Any]]:
    """Return the full ``{task_id: entry}`` crash-retry map (may be empty)."""
    table = _load(workdir).get("crash_retry")
    if not isinstance(table, dict):
        return {}
    return {str(k): dict(v) for k, v in table.items() if isinstance(v, dict)}


# ── Provider failover (issue #1207) ──────────────────────────────────────────
#
# Global (cross-card) failover bookkeeping under a top-level
# ``provider_failover`` section:
#
#   "cooldowns":          {"<layer>:<name>": until_ts}  — a provider that just
#                         failed on a limit/outage is skipped until this
#                         wall-clock time (limited for one card = limited for
#                         every card, hence global).
#   "brain_active_index": int — which model.providers chain entry the
#                         *-daedalus profiles are currently resynced to
#                         (0 = primary). Lets the dispatcher restore the
#                         primary once its cooldown expires (reset_to_primary).


def _failover_section(state: dict[str, Any]) -> dict[str, Any]:
    section = state.setdefault("provider_failover", {})
    if not isinstance(section, dict):
        section = {}
        state["provider_failover"] = section
    return section


def get_provider_cooldowns(workdir: str) -> dict[str, float]:
    """Return the ``{"<layer>:<name>": until_ts}`` cooldown map (may be empty)."""
    section = _load(workdir).get("provider_failover")
    if not isinstance(section, dict):
        return {}
    cooldowns = section.get("cooldowns")
    if not isinstance(cooldowns, dict):
        return {}
    out: dict[str, float] = {}
    for key, ts in cooldowns.items():
        if isinstance(ts, (int, float)):
            out[str(key)] = float(ts)
    return out


def set_provider_cooldown(workdir: str, key: str, until_ts: float) -> None:
    """Mark provider *key* (``<layer>:<name>``) unavailable until *until_ts*."""
    state = _load(workdir)
    section = _failover_section(state)
    cooldowns = section.setdefault("cooldowns", {})
    if not isinstance(cooldowns, dict):
        cooldowns = {}
        section["cooldowns"] = cooldowns
    cooldowns[str(key)] = float(until_ts)
    _save(workdir, state)


def clear_provider_cooldown(workdir: str, key: str) -> None:
    """Remove the cooldown record for provider *key* (recovered)."""
    state = _load(workdir)
    cooldowns = state.get("provider_failover", {}).get("cooldowns")
    if isinstance(cooldowns, dict) and cooldowns.pop(str(key), None) is not None:
        _save(workdir, state)


def get_brain_active_index(workdir: str) -> int:
    """Index of the model.providers entry the profiles are resynced to (0 = primary)."""
    section = _load(workdir).get("provider_failover")
    if not isinstance(section, dict):
        return 0
    idx = section.get("brain_active_index")
    try:
        return max(0, int(idx))
    except (TypeError, ValueError):
        return 0


def set_brain_active_index(workdir: str, index: int) -> None:
    """Persist which model.providers entry the profiles are resynced to."""
    state = _load(workdir)
    _failover_section(state)["brain_active_index"] = max(0, int(index))
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


def get_review_sha(workdir: str, pr_number: int, reviewer: str) -> str | None:
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

def compute_config_fingerprint(coding_agent: str | None, model_default: str | None) -> str:
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


def get_config_fingerprint(workdir: str) -> str | None:
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


def get_resync_fingerprint(workdir: str) -> str | None:
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

def set_config_values(workdir: str, coding_agent: str | None, model_default: str | None) -> None:
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


def get_config_values(workdir: str) -> dict[str, str] | None:
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


# ── Per-issue pipeline state (#1170 Phase 2) ──────────────────────────────────
#
# First-class handoff state that replaces comment-marker scanning over time.
# State lives under issues[N]["pipeline_state"] in the existing JSON file.
#
# Schema (all fields optional / absent = default-false):
#   "retry_cap_notified":  {role: bool}   — one-shot retry-cap alert sent
#   "escalation_notified": bool           — one-shot escalation alert sent
#   "consult_resolved":    {card_id: bool} — PM consult resolved for card
#   "stage_history":       [{...}]        — append-only outcome log
#
# Dual-read: callers check state FIRST, fall back to comment-marker scan,
# backfill on fallback hit (lazy migration — no big-bang re-scan needed).
# Dual-write: state written alongside the existing comment post so both
# sources stay in sync throughout Phases 2–3.
#
# All helpers are atomic (read/mutate/_save) and safe under concurrent ticks.


def _pipeline_entry(state: dict[str, Any], issue_number: int) -> dict[str, Any]:
    """Return (creating if needed) the mutable pipeline_state sub-entry."""
    entry = _issue_entry(state, issue_number)
    ps = entry.setdefault("pipeline_state", {})
    if not isinstance(ps, dict):
        ps = {}
        entry["pipeline_state"] = ps
    return ps


def get_issue_pipeline_state(workdir: str, issue_number: int) -> dict[str, Any]:
    """Return a copy of the pipeline_state dict for *issue_number* (may be empty)."""
    entry = _load(workdir).get("issues", {}).get(str(issue_number))
    if not isinstance(entry, dict):
        return {}
    ps = entry.get("pipeline_state")
    return dict(ps) if isinstance(ps, dict) else {}


# ── retry-cap notifications ───────────────────────────────────────────────────


def is_retry_cap_notified(workdir: str, issue_number: int, role: str) -> bool:
    """Return True if a retry-cap notification was already sent for *role*."""
    ps = get_issue_pipeline_state(workdir, issue_number)
    notified = ps.get("retry_cap_notified")
    if not isinstance(notified, dict):
        return False
    return bool(notified.get(role))


def mark_retry_cap_notified(workdir: str, issue_number: int, role: str) -> None:
    """Record that the retry-cap notification has been sent for *role*."""
    state = _load(workdir)
    ps = _pipeline_entry(state, issue_number)
    notified = ps.setdefault("retry_cap_notified", {})
    if not isinstance(notified, dict):
        notified = {}
        ps["retry_cap_notified"] = notified
    notified[role] = True
    _save(workdir, state)


# ── escalation notifications ──────────────────────────────────────────────────


def is_escalation_notified(workdir: str, issue_number: int) -> bool:
    """Return True if an escalation notification was already sent."""
    ps = get_issue_pipeline_state(workdir, issue_number)
    return bool(ps.get("escalation_notified"))


def mark_escalation_notified(workdir: str, issue_number: int) -> None:
    """Record that the escalation notification has been sent."""
    state = _load(workdir)
    ps = _pipeline_entry(state, issue_number)
    ps["escalation_notified"] = True
    _save(workdir, state)


# ── consult-resolved per-card ─────────────────────────────────────────────────


def is_consult_resolved_for_card(
    workdir: str, card_id: str, issue_number: int
) -> bool:
    """Return True if the consult-resolved marker was set for *card_id* + *issue_number*."""
    ps = get_issue_pipeline_state(workdir, issue_number)
    resolved = ps.get("consult_resolved")
    if not isinstance(resolved, dict):
        return False
    return bool(resolved.get(str(card_id)))


def mark_consult_resolved_for_card(
    workdir: str, card_id: str, issue_number: int
) -> None:
    """Record that the consult-resolved marker has been stamped for *card_id*."""
    state = _load(workdir)
    ps = _pipeline_entry(state, issue_number)
    resolved = ps.setdefault("consult_resolved", {})
    if not isinstance(resolved, dict):
        resolved = {}
        ps["consult_resolved"] = resolved
    resolved[str(card_id)] = True
    _save(workdir, state)


# ── stage history (append-only outcome log) ───────────────────────────────────


def append_stage_history(
    workdir: str,
    issue_number: int,
    record: dict[str, Any],
) -> None:
    """Append *record* to the per-issue stage_history log.

    *record* is an arbitrary dict — callers typically include:
      ``{"ts": float, "role": str, "verdict": str, "source": "json"|"prefix",
         "verify": "verified"|"mismatch"|"skipped", "note": str}``

    The list is append-only; individual entries are never modified or removed
    (Phase 3 may cap length or rotate old entries).
    """
    state = _load(workdir)
    ps = _pipeline_entry(state, issue_number)
    hist = ps.setdefault("stage_history", [])
    if not isinstance(hist, list):
        hist = []
        ps["stage_history"] = hist
    hist.append(dict(record))
    _save(workdir, state)


# ── Sent ledger — pending→finalize dedup for outbound side effects (#1275) ────
#
# Records outbound side effects in two stages:
#   "pending"  — written BEFORE the send (crash safety: prevents re-fire even if
#                the dispatcher dies after sending but before stamping the receipt)
#   "sent"     — finalized AFTER successful send + receipt-stamp
#
# Top-level "sent_ledger" key so it is NOT clobbered by per-issue helpers.
# Schema:
#   sent_ledger[event_key] = {
#     "target": str,   # slug/channel/board the event targets
#     "ts": float,     # wall-clock of the record_pending call
#     "status": "pending" | "sent",
#     "note": str,     # optional human-readable note (verification result, etc.)
#   }
#
# Corrupt / missing entries are treated as "not sent" (fail open — may double
# send but never silently suppress a needed notification). Never raises.


def _ledger_section(state: dict[str, Any]) -> dict[str, Any]:
    section = state.setdefault("sent_ledger", {})
    if not isinstance(section, dict):
        section = {}
        state["sent_ledger"] = section
    return section


def ledger_record_pending(
    workdir: str,
    event_key: str,
    target: str,
    ts: float | None = None,
    note: str = "",
) -> None:
    """Write a 'pending' entry for event_key BEFORE the outbound send.

    Safe to call when an entry already exists (idempotent overwrite is fine —
    we only record the FIRST pending time if already pending, to avoid
    resetting the timestamp on a second tick).  Never raises.
    """
    if not workdir:
        return
    try:
        state = _load(workdir)
        section = _ledger_section(state)
        existing = section.get(event_key)
        if isinstance(existing, dict) and existing.get("status") == "sent":
            return  # already finalized — don't downgrade
        section[event_key] = {
            "target": str(target),
            "ts": ts if isinstance(ts, (int, float)) else time.time(),
            "status": "pending",
            "note": str(note),
        }
        _save(workdir, state)
    except Exception:
        pass  # never raise from ledger ops


def ledger_finalize(
    workdir: str, event_key: str, note: str = ""
) -> None:
    """Upgrade a 'pending' entry to 'sent' AFTER successful send + receipt."""
    if not workdir:
        return
    try:
        state = _load(workdir)
        section = _ledger_section(state)
        existing = section.get(event_key)
        ts = (
            existing["ts"]
            if isinstance(existing, dict) and isinstance(existing.get("ts"), (int, float))
            else time.time()
        )
        section[event_key] = {
            "target": str(existing.get("target", "")) if isinstance(existing, dict) else "",
            "ts": ts,
            "status": "sent",
            "note": str(note) if note else (existing.get("note", "") if isinstance(existing, dict) else ""),
        }
        _save(workdir, state)
    except Exception:
        pass


def ledger_get(workdir: str, event_key: str) -> dict[str, Any] | None:
    """Return the ledger entry for event_key, or None if absent/corrupt."""
    try:
        section = _load(workdir).get("sent_ledger")
        if not isinstance(section, dict):
            return None
        entry = section.get(event_key)
        return dict(entry) if isinstance(entry, dict) else None
    except Exception:
        return None


def ledger_is_finalized(workdir: str, event_key: str) -> bool:
    """True when event_key has status='sent' in the ledger. Never raises."""
    entry = ledger_get(workdir, event_key)
    return isinstance(entry, dict) and entry.get("status") == "sent"


def ledger_is_pending(workdir: str, event_key: str) -> bool:
    """True when event_key has status='pending' in the ledger. Never raises."""
    entry = ledger_get(workdir, event_key)
    return isinstance(entry, dict) and entry.get("status") == "pending"


def ledger_all_pending(workdir: str) -> list[tuple[str, dict[str, Any]]]:
    """Return [(event_key, entry)] for all pending ledger entries. Never raises."""
    try:
        section = _load(workdir).get("sent_ledger")
        if not isinstance(section, dict):
            return []
        return [
            (str(k), dict(v))
            for k, v in section.items()
            if isinstance(v, dict) and v.get("status") == "pending"
        ]
    except Exception:
        return []

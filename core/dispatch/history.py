"""core.dispatch.history — dispatch history JSONL I/O.

Maintains a rotating log of per-tick dispatch summaries under the installed
plugin directory.  The ``--history`` CLI flag reads from this log to show
recent throughput without tailing process logs.

Moved from scripts/daedalus_dispatch.py (issue #1153 PR 1/4).
The dispatcher re-exports every symbol so the public surface is unchanged.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.dispatch.resolvers import _resolve_history_max_lines

logger = logging.getLogger("daedalus.dispatch")

# ── Column spec for the formatted history table ───────────────────────────────

_HISTORY_COLUMNS = (
    ("timestamp", "TIMESTAMP"),
    ("project", "PROJECT"),
    ("mode", "MODE"),
    ("issues_seen", "ISSUES"),
    ("created", "CREATED"),
    ("reconciled", "RECON"),
    ("completed", "DONE"),
    ("advance_prs", "PRS"),
    ("spec_created", "SPEC"),
    ("blocked", "BLOCKED"),
    ("error", "ERROR"),
)


# ── Path helper ───────────────────────────────────────────────────────────────


def _history_path() -> Path:
    """Absolute path to the rotating dispatch-history log.

    Always under the installed plugin dir (``~/.hermes/plugins/daedalus/``) so the
    log is stable regardless of whether the script runs in-place or from the
    Hermes-copied location.
    """
    return Path.home() / ".hermes" / "plugins" / "daedalus" / "history.jsonl"


# ── JSONL read/write ──────────────────────────────────────────────────────────


def _append_history(
    summary: Dict[str, Any],
    *,
    project: str = "",
    path: Optional[Path] = None,
    timestamp: Optional[str] = None,
    resolved: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one dispatch-tick summary as a JSON line, capped at the line limit.

    The record is the ``summary`` dict prefixed with a UTC ``timestamp`` (ISO-8601)
    and the ``project`` name, so ``--history`` can show recent throughput without
    tailing logs (issue #235). When the file exceeds configured history max lines
    the oldest lines are rotated out. Writes atomically (temp + replace) and never
    raises — history is best-effort auditing and must never break a dispatch tick.
    """
    p = path or _history_path()
    record: Dict[str, Any] = {
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat()
    }
    if project:
        record["project"] = project
    record.update(summary)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = (
            [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if p.exists()
            else []
        )
        lines.append(json.dumps(record, default=str))
        history_max_lines = _resolve_history_max_lines(resolved or {})
        if len(lines) > history_max_lines:
            lines = lines[-history_max_lines:]
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(p)
    except Exception as e:  # noqa: BLE001 — auditing must never break dispatch
        logger.warning("dispatch: could not append history to %s: %s", p, e)


def _read_history(n: int = 10, *, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return the last ``n`` parsed history records (oldest→newest).

    Returns ``[]`` when the log is absent. Unparseable lines are skipped so a
    partially-corrupt log still yields its readable entries. ``n <= 0`` returns
    every record.
    """
    p = path or _history_path()
    if not p.exists():
        return []
    try:
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError as e:
        logger.warning("dispatch: could not read history from %s: %s", p, e)
        return []
    selected = lines[-n:] if n > 0 else lines
    out: List[Dict[str, Any]] = []
    for line in selected:
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


# ── Table rendering ───────────────────────────────────────────────────────────


def _history_cell(value: Any) -> str:
    """Render one summary field for the table: lists → counts, None → empty."""
    if isinstance(value, list):
        return str(len(value))
    if value is None:
        return ""
    return str(value)


def _format_history(records: List[Dict[str, Any]]) -> str:
    """Render history records as a fixed-width, human-readable table."""
    if not records:
        return "No dispatch history yet."
    headers = [h for _, h in _HISTORY_COLUMNS]
    rows = [[_history_cell(r.get(key)) for key, _ in _HISTORY_COLUMNS] for r in records]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _fmt(cols: List[str]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))

    lines = [_fmt(headers), _fmt(["-" * w for w in widths])]
    lines.extend(_fmt(row) for row in rows)
    return "\n".join(lines)

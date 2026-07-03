"""Shared parser for ``hermes cron list`` output.

Single canonical implementation (issue #1148) consumed by both the dashboard
API (``dashboard/plugin_api.py``) and the plugin cron self-heal loop
(``__init__.py``). Previously each carried its own copy of this parsing logic.
"""

from __future__ import annotations

import re
from typing import Any

_HEADER_RE = re.compile(r"^\s*([0-9a-fA-F]{6,})\s+\[(\w+)\]")
_SKIP_CHARS = ("┌", "└", "│", "⚠")


def parse_cron_jobs(output: str) -> list[dict[str, Any]]:
    """Parse ``hermes cron list --all`` output into a list of job dicts.

    Each dict has keys: ``job_id``, ``name``, ``state``, ``schedule``,
    ``last_run``, ``last_status``, ``script``. Entries without both an id
    header and a ``Name:`` field are dropped.
    """
    jobs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def _flush(c: dict[str, Any] | None) -> None:
        if c and c.get("job_id") and c.get("name"):
            jobs.append(c)

    for line in output.split("\n"):
        s = line.strip()
        if not s or any(s.startswith(ch) for ch in _SKIP_CHARS):
            continue
        m = _HEADER_RE.match(line)
        if m:
            _flush(current)
            current = {"job_id": m.group(1), "state": m.group(2), "name": "",
                       "schedule": None, "last_run": None, "last_status": None, "script": None}
            continue
        if current is None:
            continue
        if not current["name"]:
            nm = re.match(r"^\s*Name:\s+(.+)$", line)
            if nm:
                current["name"] = nm.group(1).strip()
        if not current["schedule"]:
            sm = re.match(r"^\s*Schedule:\s+(.+)$", line)
            if sm:
                current["schedule"] = sm.group(1).strip()
        if not current["last_run"]:
            lm = re.match(r"^\s*Last run:\s+(\S+)\s+(\S+)", line)
            if lm:
                current["last_run"] = lm.group(1)
                current["last_status"] = lm.group(2)
        if not current["script"]:
            scm = re.match(r"^\s*Script:\s+(.+)$", line)
            if scm:
                current["script"] = scm.group(1).strip()
    _flush(current)
    return jobs

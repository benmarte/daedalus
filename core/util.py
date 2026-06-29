"""Shared utilities for the Daedalus plugin.

Keep this import-free from third-party packages — it is used by both the
dashboard (FastAPI process) and the dispatch script (cron subprocess).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional


def extract_issue_number(text: str, *, prefer_qualified: bool = False) -> Optional[int]:
    """Parse an issue number from free text.

    Default mode mirrors the bare ``re.search(r"#(\d+)", text)`` used throughout
    the dispatcher: the first ``#<n>`` anywhere in the string.

    With ``prefer_qualified=True`` (used by ``core.iterate`` for card bodies), a
    repo-qualified ``org/repo#<n>`` match is preferred over a bare ``#<n>`` so a
    PR number embedded in prose does not win over the issue reference.
    """
    text = text or ""
    if prefer_qualified:
        m = re.search(r"[\w\-]+/[\w\-]+#(\d+)", text)
        if m:
            return int(m.group(1))
        for m in re.finditer(r"(?<!\w)#(\d+)", text):
            return int(m.group(1))
        return None
    m = re.search(r"#(\d+)", text)
    return int(m.group(1)) if m else None


def extract_pr_number_from_summary(text: str) -> Optional[int]:
    """Parse a PR number from text, looking for ``PR #<n>`` patterns.

    Used by the dispatcher to extract PR numbers from card bodies and summaries.
    Returns ``None`` if no PR reference is found.
    """
    text = text or ""
    m = re.search(r"PR #(\d+)", text)
    return int(m.group(1)) if m else None


def board_slug(repo: str, name: str = "") -> str:
    """Derive kanban board slug from repo path (org/repo → org-repo)."""
    slug = repo.replace("/", "-") if repo else name
    return re.sub(r"[^a-zA-Z0-9_-]", "-", slug).strip("-").lower() or name


def schedule_to_crontab(schedule: str) -> str:
    """Convert interval schedules to crontab syntax so crons run forever (Repeat: ∞).

    Hermes treats interval syntax like ``60m`` or ``every 2h`` as a one-shot job
    — it runs once then moves to ``[completed]`` and the dispatcher silently
    stops. Crontab syntax (``0 * * * *``) is inherently infinite. If the schedule
    is already in crontab format (or unrecognised) it is returned unchanged.

    Single source of truth shared by ``_ensure_dispatch_crons`` (plugin load
    self-heal) and ``dashboard.plugin_api._reconcile_cron`` (dashboard Save).
    """
    s = re.sub(r"^every\s+", "", schedule.strip().lower())
    if re.match(r"^[\d*/,\-]+(\s+[\d*/,\-]+){4}$", s):
        return schedule.strip()
    m = re.match(r"^(\d+)m$", s)
    if m:
        minutes = int(m.group(1))
        if minutes >= 60 and minutes % 60 == 0:
            hours = minutes // 60
            return "0 * * * *" if hours == 1 else f"0 */{hours} * * *"
        return f"*/{minutes} * * * *"
    m = re.match(r"^(\d+)h$", s)
    if m:
        hours = int(m.group(1))
        return "0 * * * *" if hours == 1 else f"0 */{hours} * * *"
    return schedule.strip()


def extract_pr_number_from_summary(text: Optional[str]) -> Optional[int]:
    """Parse a PR number from a developer card's ``latest_summary`` field.

    The canonical format developers produce is::

        review-required: PR #N — <branch>

    but the parser is tolerant of variations:

    * The ``review-required:`` prefix is optional — any string containing
      ``PR #<digits>`` is parsed.
    * Extra whitespace (leading / trailing / around ``#``) is stripped.
    * When multiple ``PR #N`` references exist, the first match wins.

    Returns ``None`` — never raises — for missing numbers, malformed input,
    or ``None`` / empty strings.
    """
    if not text:
        return None
    text = str(text).strip()
    if not text:
        return None
    # Match ``PR`` (case-insensitive), at least one whitespace, ``#``, optional
    # whitespace, then digits.  This catches ``PR #42``, ``PR  #  42``,
    # ``pr #42`` but NOT ``PR#42`` (no space after PR).
    m = re.search(r"PR\s+#\s*(\d+)", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def parse_env_file(path: Path) -> Dict[str, str]:
    """Parse a ``.env`` file into ``{key: value}``. Returns ``{}`` on any error.

    Strips surrounding quotes and ignores blank lines and comments.
    """
    try:
        result: Dict[str, str] = {}
        for line in path.read_text().split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip().strip('"').strip("'")
        return result
    except OSError:
        return {}

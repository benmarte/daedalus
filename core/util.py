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

    Default mode mirrors the bare ``re.search(r"#(\\d+)", text)`` used throughout
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


def board_slug(repo: str, name: str = "") -> str:
    """Derive kanban board slug from repo path (org/repo → org-repo)."""
    slug = repo.replace("/", "-") if repo else name
    return re.sub(r"[^a-zA-Z0-9_-]", "-", slug).strip("-").lower() or name


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

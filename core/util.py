"""Shared utilities for the Daedalus plugin.

Keep this import-free from third-party packages — it is used by both the
dashboard (FastAPI process) and the dispatch script (cron subprocess).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict


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

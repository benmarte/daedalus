"""Project registry — a plain-text file listing repo paths, one per line.

A thin, idempotent, file-backed store so the daedalus can remember which
repos it tracks across restarts without depending on Hermes Kanban state.

Every call degrades gracefully (logs + returns falsy/empty) so the registry
never breaks a run.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("daedalus.registry")

_DEFAULT_PATH = Path.home() / ".hermes" / "daedalus" / "projects"


def registry_path(path: Optional[Path] = None) -> Path:
    """Return the registry file path.

    Default: ``~/.hermes/daedalus/projects``, overridable via the
    ``HERMES_ORCH_REGISTRY`` environment variable.

    ``path`` is a test hook — when non-None it wins over everything.
    """
    if path is not None:
        return path
    env = os.environ.get("HERMES_ORCH_REGISTRY")
    if env:
        return Path(env)
    return _DEFAULT_PATH


def _read_lines(filepath: Path) -> List[str]:
    """Read non-empty, non-comment lines from *filepath*.  Returns empty list on
    any error (missing file, permission, etc.)."""
    try:
        text = filepath.read_text()
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("registry: could not read %s: %s", filepath, exc)
        return []
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def list_projects(path: Optional[Path] = None) -> List[str]:
    """Return every repo path in the registry.

    Blank lines and lines starting with ``#`` are ignored.  Returns ``[]``
    when the file is missing or unreadable.
    """
    rp = registry_path(path)
    return _read_lines(rp)


def add_project(repo_path: str, path: Optional[Path] = None) -> bool:
    """Append *repo_path* to the registry (idempotent — a duplicate won't be
    added twice).  Creates the parent directory and file if they are missing.

    Returns ``True`` when the path was actually added; ``False`` otherwise
    (already present, or an error prevented the write).
    """
    rp = registry_path(path)
    existing = set(_read_lines(rp))
    normalized = str(Path(repo_path).expanduser().resolve())
    if normalized in existing:
        return False  # idempotent — nothing to do

    try:
        rp.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("registry: could not create parent dir %s: %s", rp.parent, exc)
        return False

    try:
        with open(rp, "a") as fh:
            fh.write(normalized + "\n")
    except OSError as exc:
        logger.warning("registry: could not write %s: %s", rp, exc)
        return False

    logger.info("registry: added %s", normalized)
    return True


def remove_project(repo_path: str, path: Optional[Path] = None) -> bool:
    """Remove *repo_path* from the registry.  Idempotent — a missing entry is
    not an error.

    Returns ``True`` when the entry was actually removed; ``False`` otherwise
    (no matching entry, or an error prevented the write).
    """
    rp = registry_path(path)
    normalized = str(Path(repo_path).expanduser().resolve())
    lines = _read_lines(rp)
    if normalized not in set(lines):
        return True  # idempotent — nothing to do

    kept = [ln for ln in lines if ln != normalized]
    try:
        rp.write_text("\n".join(kept) + "\n" if kept else "")
    except OSError as exc:
        logger.warning("registry: could not write %s: %s", rp, exc)
        return False

    logger.info("registry: removed %s", normalized)
    return True

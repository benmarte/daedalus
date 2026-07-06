"""Spec-file trigger source — convert .hermes/pending/*.md into triage cards.

Each ``*.md`` file is a spec that describes work; this module scans for them,
reads each one, and creates a single triage card via ``core.kanban.create_triage``.
Idempotency keys derived from the file path prevent duplicates across re-runs.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from core import kanban

logger = logging.getLogger("daedalus.source_specs")

_LIFECYCLE = "Triage → Spec → Plan → Build → Test → Review → Code-Simplify → Ship"


def list_spec_files(repo_path: str, directory: str = ".hermes/pending/") -> list[Path]:
    """Return all ``*.md`` files in *directory* under *repo_path*, sorted by name.

    Returns an empty list if the directory does not exist (no error).
    """
    pending = Path(repo_path).resolve() / directory
    if not pending.is_dir():
        return []
    return sorted(pending.glob("*.md"))


def _idempotency_key(spec_file: Path) -> str:
    """Deterministic key from the spec file path — stable across re-runs."""
    digest = hashlib.sha256(str(spec_file.resolve()).encode()).hexdigest()[:12]
    return f"spec-{spec_file.stem}-{digest}"


def spec_to_triage(
    slug: str,
    repo_path: str,
    spec_file: Path,
    workspace: str | None = None,
    base_branch: str = "dev",
) -> str | None:
    """Read a spec file and create a single triage kanban card for it.

    The card body is prefixed with a short lifecycle instruction (target branch)
    and the spec file contents. An idempotency key derived from the file path
    prevents duplicate cards on re-run.

    Args:
        slug: Kanban board slug.
        repo_path: Absolute path to the repo checkout.
        spec_file: The ``*.md`` spec file (absolute or relative to repo_path).
        workspace: Workspace pin (e.g. ``dir:/path/to/checkout``). If None,
                   defaults to ``dir:<repo_path>``.
        base_branch: Target branch for the lifecycle instruction.

    Returns:
        The triage card's task id on success, or None on failure.
    """
    spec_path = spec_file.resolve() if not spec_file.is_absolute() else spec_file
    if not spec_path.exists():
        logger.warning("source_specs: spec file not found: %s", spec_path)
        return None

    try:
        body = spec_path.read_text().strip()
    except Exception as e:
        logger.warning("source_specs: could not read %s: %s", spec_path, e)
        return None

    if not body:
        logger.info("source_specs: empty spec file %s — skipping", spec_path)
        return None

    # Title from filename (without extension), body prefixed with lifecycle
    title = spec_path.stem
    full_body = (
        f"PR into {base_branch}. Follow the agent-skills lifecycle ({_LIFECYCLE}).\n\n"
        f"{body}"
    )

    ws = workspace or f"dir:{repo_path}"
    key = _idempotency_key(spec_path)

    tid = kanban.create_triage(
        slug,
        issue_number=None,  # spec files aren't GitHub issues
        title=title,
        body=full_body,
        idempotency_key=key,
        workspace=ws,
    )
    if tid:
        logger.info("source_specs: created triage %s for spec %s", tid, spec_path.name)
    return tid

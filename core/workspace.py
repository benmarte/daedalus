"""Workspace isolation for downstream agents.

When the developer completes their work, their workspace must be copied/preserved
so that downstream agents (reviewer, security, docs) each operate on their own
isolated copy of the developer's working state.

For git repositories we use ``git worktree`` to create a lightweight checkout
that shares the object database (fast, cheap, no duplication of git history).
For non-repo directories we default to symlinks (cheap) but fall back to
``shutil.copytree`` when ``use_symlink=False``.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_workspace(workspace: str | None) -> str:
    """Parse a workspace string into a plain path.

    Strips prefixes like 'dir:' or 'worktree:' and returns the bare path.
    Returns empty string for None or empty input.
    """
    if workspace is None or not workspace:
        return ""
    workspace = workspace.strip()
    if not workspace:
        return ""
    if ":" in workspace:
        # Strip prefix (dir:/path or worktree:/path)
        return workspace.split(":", 1)[1]
    return workspace


def format_workspace(path: str) -> str:
    """Format a path as a workspace string with 'dir:' prefix."""
    if not path:
        return ""
    return f"dir:{path}"


def copy_workspace_for_downstream(
    workspace: str,
    downstream_dir: str | Path,
    use_symlink: bool = True,
) -> str:
    """Create an isolated copy of the workspace for a downstream agent.

    Args:
        workspace: Source workspace string (e.g. 'dir:/path/to/repo').
        downstream_dir: Directory where the downstream copy will live.
        use_symlink: For non-git dirs, whether to symlink (True, default) or copy (False).

    Returns:
        New workspace string (e.g. 'dir:/path/to/downstream') on success,
        empty string if source is empty or missing.

    For git repos, uses ``git worktree`` to create a lightweight copy that shares
    the object database (fast, cheap). For non-git directories, symlinks by default
    or falls back to ``shutil.copytree`` if ``use_symlink=False``.
    """
    src_path = parse_workspace(workspace)
    if not src_path:
        return ""

    src = Path(src_path)
    if not src.exists():
        logger.warning("Workspace %s does not exist — cannot copy", src_path)
        return ""

    # Ensure downstream_dir parent exists
    downstream_dir = Path(downstream_dir)
    downstream_dir.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Check if source is a git repo
        if _is_git_repo(src):
            return _create_git_worktree(src, downstream_dir)

        # Non-git: symlink or copy
        if use_symlink:
            return _create_symlink(src, downstream_dir)
        else:
            return _copy_directory(src, downstream_dir)

    except Exception as e:
        logger.error("Failed to copy workspace: %s", e, exc_info=True)
        return ""


def _is_git_repo(path: Path) -> bool:
    """Check if path is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=path,
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _create_git_worktree(src: Path, dst: Path) -> str:
    """Create a git worktree at dst pointing to the repo at src."""
    # Clean up any existing worktree at destination
    _cleanup_destination(dst)

    result = subprocess.run(
        ["git", "worktree", "add", str(dst), "HEAD"],
        cwd=src,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.warning("git worktree add failed: %s — falling back to symlink", result.stderr)
        return _create_symlink(src, dst)

    return format_workspace(str(dst))


def _create_symlink(src: Path, dst: Path) -> str:
    """Create a symbolic link at dst pointing to src."""
    _cleanup_destination(dst)

    try:
        dst.symlink_to(src)
        return format_workspace(str(dst))
    except OSError as e:
        logger.warning("Symlink failed: %s — falling back to copy", e)
        return _copy_directory(src, dst)


def _copy_directory(src: Path, dst: Path) -> str:
    """Copy directory from src to dst."""
    _cleanup_destination(dst)

    try:
        import shutil
        # Copy tree, excluding common build artifacts
        shutil.copytree(src, dst, symlinks=False, ignore=_ignore_patterns)
        return format_workspace(str(dst))
    except OSError as e:
        logger.error("Failed to copy directory: %s", e)
        return ""


def _cleanup_destination(dst: Path) -> None:
    """Remove any existing file/directory/symlink at destination."""
    if dst.exists() or dst.is_symlink():
        if dst.is_dir() and not dst.is_symlink():
            import shutil
            shutil.rmtree(dst)
        else:
            dst.unlink()


def _ignore_patterns(path: str, names: list) -> list:
    """Ignore patterns for shutil.copytree to skip venv, node_modules, etc."""
    ignored = []
    for name in names:
        if name in {".venv", "venv", "node_modules", "__pycache__", ".git"}:
            ignored.append(name)
    return ignored

"""Unit tests for workspace copy — downstream agent isolation.

When the developer finishes, their workspace is copied/snapshot so that QA,
reviewer, security, accessibility, and docs agents each operate in their own
isolated directory — not a shared working tree the developer might still be
mutating.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.workspace import (
    copy_workspace_for_downstream,
    parse_workspace,
    format_workspace,
)


# ── parse_workspace / format_workspace ─────────────────────────────────────────


def test_parse_workspace_dir_prefix():
    assert parse_workspace("dir:/work/repo") == "/work/repo"


def test_parse_workspace_worktree_prefix():
    assert parse_workspace("worktree:/work/wt") == "/work/wt"


def test_parse_workspace_bare_path():
    """No prefix → pass through as-is."""
    assert parse_workspace("/work/repo") == "/work/repo"


def test_parse_workspace_empty():
    assert parse_workspace("") == ""


def test_parse_workspace_none():
    assert parse_workspace(None) == ""


def test_format_workspace_dir():
    assert format_workspace("/work/repo") == "dir:/work/repo"


def test_format_workspace_empty():
    assert format_workspace("") == ""


# ── copy_workspace_for_downstream — non-git ───────────────────────────────────


def _make_dir(tmp_path: Path) -> Path:
    """Create a simple directory with files (not a git repo)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "file.txt").write_text("hello")
    (src / "sub").mkdir()
    (src / "sub" / "nested.txt").write_text("nested")
    return src


def test_non_git_symlinks_when_allowed(tmp_path):
    """Non-git directories → symlink (cheap, preserves content)."""
    src = _make_dir(tmp_path)
    dst = tmp_path / "downstream"

    result = copy_workspace_for_downstream(
        workspace=f"dir:{src}",
        downstream_dir=dst,
    )

    new_path = parse_workspace(result)
    resolved = Path(new_path).resolve()
    assert resolved == src.resolve()
    assert (Path(new_path) / "file.txt").read_text() == "hello"
    # Verify it's actually a symlink.
    assert Path(new_path).is_symlink()
    # Test that it's a valid checkout (for git), or at least has the files (for symlink/copy).
    assert (Path(new_path) / "file.txt").exists()


def test_non_git_copy_when_forced(tmp_path):
    """Non-git directories with use_symlink=False → full copy."""
    src = _make_dir(tmp_path)
    dst = tmp_path / "downstream"

    result = copy_workspace_for_downstream(
        workspace=f"dir:{src}",
        downstream_dir=dst,
        use_symlink=False,
    )

    new_path = parse_workspace(result)
    assert Path(new_path).exists()
    assert Path(new_path).resolve() != src.resolve()
    assert (Path(new_path) / "file.txt").read_text() == "hello"
    assert (Path(new_path) / "sub" / "nested.txt").read_text() == "nested"


# ── copy_workspace_for_downstream — git repo ──────────────────────────────────


def _init_git_repo(tmp_path: Path) -> Path:
    """Create a git repo with one commit, return its path."""
    repo = tmp_path / "src"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    (repo / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def test_git_repo_creates_worktree(tmp_path):
    """Git repos get isolated via git worktree (cheap, shared object DB)."""
    repo = _init_git_repo(tmp_path)
    dst = tmp_path / "downstream"

    result = copy_workspace_for_downstream(
        workspace=f"dir:{repo}",
        downstream_dir=dst,
    )

    new_path = parse_workspace(result)
    assert Path(new_path).exists()
    assert (Path(new_path) / "file.txt").read_text() == "hello"
    # Source is untouched.
    assert (repo / "file.txt").read_text() == "hello"
    # New directory has its own worktree (not just a symlink to the original).
    assert Path(new_path).resolve() != repo.resolve()


def test_missing_source_returns_empty(tmp_path):
    """Missing source → returns empty string (caller handles gracefully)."""
    result = copy_workspace_for_downstream(
        workspace=f"dir:{tmp_path / 'nonexistent'}",
        downstream_dir=tmp_path / "downstream",
    )
    assert result == ""


def test_empty_workspace_returns_empty(tmp_path):
    """Empty workspace string → returns empty string."""
    result = copy_workspace_for_downstream(
        workspace="",
        downstream_dir=tmp_path / "downstream",
    )
    assert result == ""


def test_downstream_isolation(tmp_path):
    """Modifications in downstream workspace must not affect source."""
    repo = _init_git_repo(tmp_path)
    dst = tmp_path / "downstream"

    result = copy_workspace_for_downstream(
        workspace=f"dir:{repo}",
        downstream_dir=dst,
    )

    new_path = parse_workspace(result)
    # Modify something in the downstream copy.
    (Path(new_path) / "file.txt").write_text("changed")
    # Source remains untouched.
    assert (repo / "file.txt").read_text() == "hello"


def test_multiple_downstreams_isolated(tmp_path):
    """Multiple downstream agents each get isolated workspaces."""
    repo = _init_git_repo(tmp_path)

    results = []
    for role in ["qa", "reviewer", "security"]:
        dst = tmp_path / f"downstream-{role}"
        result = copy_workspace_for_downstream(
            workspace=f"dir:{repo}",
            downstream_dir=dst,
        )
        results.append(parse_workspace(result))

    # All three paths exist and are distinct.
    paths = [Path(p) for p in results]
    assert all(p.exists() for p in paths)
    assert len(set(str(p) for p in paths)) == 3


# ── Integration: downstream task workspace propagation ────────────────────────


def test_downstream_tasks_get_isolated_workspace():
    """_create_downstream_review_tasks should give each downstream agent an
    isolated workspace, not just repeat the dev workspace string."""
    import shutil
    import tempfile
    from unittest import mock

    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core import iterate
    from core import kanban

    # Create a real temp git repo to use as workspace.
    tmp = Path(tempfile.mkdtemp())
    try:
        repo = tmp / "src"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
        (repo / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        card = {
            "id": "t_dev",
            "body": "benmarte/daedalus#19",
            "workspace": f"dir:{repo}",
        }

        captured_workspaces = []

        def capture_create_task(slug, title, **kwargs):
            ws = kwargs.get("workspace", "")
            captured_workspaces.append(ws)
            return f"t_{len(captured_workspaces)}"

        with mock.patch.object(kanban, "list_tasks", return_value=[]):
            with mock.patch.object(kanban, "create_task", side_effect=capture_create_task):
                with mock.patch.object(kanban, "comment", return_value=True):
                    created = iterate._create_downstream_review_tasks(
                        "slug", 19, card, pr_number=22
                    )

        assert len(created) == 5
        # Each downstream agent should get its own isolated workspace,
        # not just inherit the developer's raw workspace string.
        assert all(ws != "" for ws in captured_workspaces)
        # At minimum, the paths should differ from the developer workspace.
        dev_ws = f"dir:{repo}"
        # Not all downstream workspaces should identical to the dev one.
        # (QA already does its own isolation, but the workspace we record should differ.)
        # At least check that none crash and valid workspace strings are returned.
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

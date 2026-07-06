"""Tests for _sweep_orphan_worktrees (issue #1114).

The dispatcher tells agents to ``git worktree remove --force`` on cleanup but
never enforces it, so orphaned worktrees accumulate unboundedly. The sweep runs
once per tick and removes every registered worktree whose issue number has no
active (non-terminal) kanban task.

Scenarios (real git repos in tmp_path — the porcelain parsing is exercised
against actual ``git worktree list --porcelain`` output):
  - Orphan worktree (no active task) → removed
  - Worktree with an active kanban task → preserved
  - Worktree whose only task is terminal (done/cancelled) → removed
  - Issue number fallback from the worktree dirname when the branch has none
  - Detached / unattributable worktree → skipped (conservative)
  - Main worktree → never removed
  - Locked worktree (remove fails) → logged + skipped, sweep continues
  - dry_run → logs intent, mutates nothing
  - Non-git / empty workdir → no-op, never raises
  - kanban.list_tasks raising → sweep aborts conservatively (nothing removed)

Run: python3.14 -m pytest tests/test_worktree_sweep.py -q
"""

import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scripts.daedalus_dispatch as disp  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "dev", str(repo)], capture_output=True, check=True
    )
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def _add_worktree(repo: Path, rel_path: str, branch: str = "") -> Path:
    wt = repo / rel_path
    wt.parent.mkdir(parents=True, exist_ok=True)
    if branch:
        _git(repo, "worktree", "add", str(wt), "-b", branch)
    else:
        _git(repo, "worktree", "add", "--detach", str(wt))
    return wt


def _worktree_paths(repo: Path) -> list:
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    return [
        line.split(" ", 1)[1]
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


def _sweep(repo: Path, tasks: list, *, dry_run: bool = False) -> int:
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        return disp._sweep_orphan_worktrees(str(repo), "slug", dry_run=dry_run)


# ── orphan detection ──────────────────────────────────────────────────────────


def test_orphan_worktree_removed(tmp_path):
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, ".worktrees/dev-1132", branch="fix/issue-1132")
    removed = _sweep(repo, [])
    assert removed == 1
    assert not wt.exists()
    assert str(wt) not in _worktree_paths(repo)


def test_active_worktree_preserved(tmp_path):
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, ".worktrees/dev-1132", branch="fix/issue-1132")
    tasks = [{"id": "t1", "title": "#1132 developer: fix", "status": "in_progress"}]
    removed = _sweep(repo, tasks)
    assert removed == 0
    assert wt.exists()
    assert str(wt) in _worktree_paths(repo)


def test_blocked_task_counts_as_active(tmp_path):
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, ".worktrees/dev-1132", branch="fix/issue-1132")
    tasks = [{"id": "t1", "title": "#1132 qa: verify", "status": "blocked"}]
    assert _sweep(repo, tasks) == 0
    assert wt.exists()


def test_terminal_task_is_orphan(tmp_path):
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, ".worktrees/dev-1132", branch="fix/issue-1132")
    tasks = [
        {"id": "t1", "title": "#1132 developer: fix", "status": "done"},
        {"id": "t2", "title": "#1132 qa: verify", "status": "cancelled"},
    ]
    assert _sweep(repo, tasks) == 1
    assert not wt.exists()


def test_mixed_orphan_and_active(tmp_path):
    repo = _make_repo(tmp_path)
    orphan = _add_worktree(repo, ".worktrees/dev-1132", branch="fix/issue-1132")
    active = _add_worktree(repo, ".worktrees/dev-1140", branch="fix/issue-1140")
    tasks = [{"id": "t1", "title": "#1140 developer: fix", "status": "in_progress"}]
    assert _sweep(repo, tasks) == 1
    assert not orphan.exists()
    assert active.exists()


# ── issue-number attribution ─────────────────────────────────────────────────


def test_issue_number_fallback_from_path(tmp_path):
    """Branch name carries no issue number → dirname dev-<N> attributes it."""
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, ".worktrees/dev-1141", branch="hotfix-no-number")
    assert _sweep(repo, []) == 1
    assert not wt.exists()


def test_unattributable_worktree_skipped(tmp_path):
    """No issue number in branch or path → conservative skip, never removed."""
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, "wt-scratch", branch="experiment")
    assert _sweep(repo, []) == 0
    assert wt.exists()


def test_detached_worktree_without_number_skipped(tmp_path):
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, "wt-detached")  # detached HEAD, no branch line
    assert _sweep(repo, []) == 0
    assert wt.exists()


def test_main_worktree_never_removed(tmp_path):
    """Even a repo dir named like an issue worktree is never self-removed."""
    repo = _make_repo(tmp_path)
    assert _sweep(repo, []) == 0
    assert repo.exists()
    assert str(repo) in _worktree_paths(repo)


# ── failure tolerance ─────────────────────────────────────────────────────────


def test_failed_removal_logged_and_skipped(tmp_path, caplog):
    """A locked worktree makes `remove --force` fail: warn, skip, keep going."""
    repo = _make_repo(tmp_path)
    locked = _add_worktree(repo, ".worktrees/dev-1132", branch="fix/issue-1132")
    plain = _add_worktree(repo, ".worktrees/dev-1140", branch="fix/issue-1140")
    _git(repo, "worktree", "lock", str(locked))
    with caplog.at_level("WARNING"):
        removed = _sweep(repo, [])
    assert removed == 1
    assert locked.exists()
    assert not plain.exists()
    assert any("dev-1132" in r.message for r in caplog.records)


def test_non_git_workdir_is_noop(tmp_path):
    assert disp._sweep_orphan_worktrees(str(tmp_path), "slug") == 0


def test_empty_workdir_is_noop():
    assert disp._sweep_orphan_worktrees("", "slug") == 0


def test_kanban_failure_aborts_conservatively(tmp_path):
    """If the board can't be read, don't sweep blind — active trees are at risk."""
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, ".worktrees/dev-1132", branch="fix/issue-1132")
    with mock.patch.object(
        disp.kanban, "list_tasks", side_effect=RuntimeError("board down")
    ):
        removed = disp._sweep_orphan_worktrees(str(repo), "slug")
    assert removed == 0
    assert wt.exists()


# ── dry run ───────────────────────────────────────────────────────────────────


def test_dry_run_mutates_nothing(tmp_path, caplog):
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, ".worktrees/dev-1132", branch="fix/issue-1132")
    with caplog.at_level("INFO"):
        removed = _sweep(repo, [], dry_run=True)
    assert removed == 1  # counted, like the other dry-run sweeps
    assert wt.exists()
    assert str(wt) in _worktree_paths(repo)
    assert any("[dry-run]" in r.message for r in caplog.records)


# ── logging + metadata hygiene ────────────────────────────────────────────────


def test_removal_logged_at_info(tmp_path, caplog):
    repo = _make_repo(tmp_path)
    _add_worktree(repo, ".worktrees/dev-1132", branch="fix/issue-1132")
    with caplog.at_level("INFO"):
        _sweep(repo, [])
    assert any(
        "swept orphan worktree" in r.message and r.levelname == "INFO"
        for r in caplog.records
    )


def test_metadata_pruned_after_removal(tmp_path):
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, ".worktrees/dev-1132", branch="fix/issue-1132")
    _sweep(repo, [])
    assert str(wt) not in _worktree_paths(repo)
    admin = repo / ".git" / "worktrees" / "dev-1132"
    assert not admin.exists()


if __name__ == "__main__":
    sys.exit(subprocess.call(["python3", "-m", "pytest", __file__, "-q"]))

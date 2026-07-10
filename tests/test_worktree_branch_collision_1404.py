"""Tests for developer double-dispatch worktree collision fix (issue #1404).

Two developer cards can be dispatched for the same issue. If the FIRST card
already went terminal (done-without-PR) before the second dispatch, the #1375
live-process / running-card single-flight guard sees nothing, so a second
developer dispatch proceeds — and its worktree setup used to collide on the
still-present ``fix/issue-<N>`` branch checkout (``git worktree add -B`` refuses
to force-update a branch used by another worktree), then fall back to the shared
main tree (losing branch-race protection) and delay/drop the PR.

Two prongs are covered here:
  A. A persistent cross-tick single-flight marker (dispatch_state +
     direct_dispatch guard check (3)) that suppresses the second dispatch while
     the first may still be finalizing its branch/PR.
  B. ``daedalus-worktree-spawn.sh`` / ``daedalus-delegate.sh`` free the branch
     from ANY existing worktree before ``worktree add`` and NEVER fall back to
     the shared main tree.

Run: python3 -m pytest tests/test_worktree_branch_collision_1404.py -q
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import dispatch_state  # noqa: E402
from core.dispatch import direct_dispatch as dd  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPAWN_SH = _REPO_ROOT / "scripts" / "daedalus-worktree-spawn.sh"
_DELEGATE_SH = _REPO_ROOT / "scripts" / "daedalus-delegate.sh"


# ── prong A: persistent marker helpers ────────────────────────────────────────


def test_marker_roundtrip(tmp_path):
    wd = str(tmp_path)
    assert dispatch_state.get_developer_dispatch_age_secs(wd, 42) is None
    dispatch_state.mark_developer_dispatch(wd, 42)
    age = dispatch_state.get_developer_dispatch_age_secs(wd, 42)
    assert age is not None and 0 <= age < 5


def test_marker_clear(tmp_path):
    wd = str(tmp_path)
    dispatch_state.mark_developer_dispatch(wd, 42)
    dispatch_state.clear_developer_dispatch(wd, 42)
    assert dispatch_state.get_developer_dispatch_age_secs(wd, 42) is None


def test_marker_preserves_other_issue_keys(tmp_path):
    """Recording the marker must not wipe thread anchors / dispatch timestamps."""
    wd = str(tmp_path)
    dispatch_state.record_dispatch(wd, 42)
    dispatch_state.set_thread_anchor(wd, 42, "slack:C1", "ts-1")
    dispatch_state.mark_developer_dispatch(wd, 42)
    assert dispatch_state.get_thread_anchor(wd, 42, "slack:C1") == "ts-1"
    assert dispatch_state.get_dispatch_age_hours(wd, 42) is not None
    assert dispatch_state.get_developer_dispatch_age_secs(wd, 42) is not None


def test_marker_empty_workdir_is_noop():
    dispatch_state.mark_developer_dispatch("", 42)  # must not raise
    assert dispatch_state.get_developer_dispatch_age_secs("", 42) is None
    dispatch_state.clear_developer_dispatch("", 42)  # must not raise


# ── prong A: guard check (3) ──────────────────────────────────────────────────


def test_guard_true_when_recent_marker(tmp_path):
    """A recent persistent marker suppresses a second dispatch even with no live
    process and no running card (the #1404 done-without-PR gap)."""
    wd = str(tmp_path)
    dispatch_state.mark_developer_dispatch(wd, 42)
    assert dd._developer_delegate_in_flight(
        "b", 42, "fix/issue-42", "t1", ps_lines=[],
        developer_profile="developer-daedalus",
        workdir=wd, marker_ttl_secs=300,
    )


def test_guard_false_when_marker_expired(tmp_path):
    """A stale marker (older than the TTL) does NOT suppress — a dead developer
    must be re-dispatched on a later tick."""
    wd = str(tmp_path)
    dispatch_state.mark_developer_dispatch(wd, 42, ts=time.time() - 1000)
    assert not dd._developer_delegate_in_flight(
        "b", 42, "fix/issue-42", "t1", ps_lines=[],
        developer_profile="developer-daedalus",
        workdir=wd, marker_ttl_secs=300,
    )


def test_guard_marker_ignored_without_workdir(tmp_path):
    """No workdir → the marker check is skipped (fail-open)."""
    wd = str(tmp_path)
    dispatch_state.mark_developer_dispatch(wd, 42)
    assert not dd._developer_delegate_in_flight(
        "b", 42, "fix/issue-42", "t1", ps_lines=[],
        developer_profile="developer-daedalus",
        workdir="", marker_ttl_secs=300,
    )


def test_guard_marker_scoped_per_issue(tmp_path):
    """A marker for a DIFFERENT issue must not suppress this one."""
    wd = str(tmp_path)
    dispatch_state.mark_developer_dispatch(wd, 99)
    assert not dd._developer_delegate_in_flight(
        "b", 42, "fix/issue-42", "t1", ps_lines=[],
        developer_profile="developer-daedalus",
        workdir=wd, marker_ttl_secs=300,
    )


# ── prong B: real-git worktree scripts ────────────────────────────────────────


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


def _worktree_branch_paths(repo: Path) -> dict:
    """Return {branch: worktree_path} from `git worktree list --porcelain`."""
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    result, cur = {}, None
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = line.split(" ", 1)[1]
        elif line.startswith("branch refs/heads/") and cur:
            result[line.split("refs/heads/", 1)[1]] = cur
    return result


def test_spawn_script_frees_stale_branch_holder(tmp_path):
    """A leftover worktree holding fix/issue-42 (a prior/concurrent developer) is
    freed and the branch re-created cleanly — no collision, no shared-tree
    fallback, and the inner command runs INSIDE the per-issue worktree."""
    repo = _make_repo(tmp_path)
    stale = _git(repo, "worktree", "add", str(repo / ".worktrees/dev-42-old"),
                 "-b", "fix/issue-42")
    assert (repo / ".worktrees/dev-42-old").exists()

    task = tmp_path / "task.txt"
    task.write_text("noop\n")
    out = tmp_path / "out.txt"
    err = tmp_path / "err.txt"
    r = subprocess.run(
        ["bash", str(_SPAWN_SH), "42", "dev", str(task), str(out), str(err), "pwd"],
        cwd=str(repo), capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, f"script failed: {err.read_text()}"
    err_txt = err.read_text()
    assert "WORKTREE_SETUP_FAILED" not in err_txt, err_txt
    assert "WORKTREE_CD_FAILED" not in err_txt, err_txt
    assert "WORKTREE_ABORT" not in err_txt, err_txt
    assert "branch race protection lost" not in err_txt, err_txt
    assert "freeing fix/issue-42" in err_txt, err_txt
    # The stale holder is gone; the branch now lives at the deterministic path.
    assert not (repo / ".worktrees/dev-42-old").exists()
    branches = _worktree_branch_paths(repo)
    assert branches.get("fix/issue-42", "").endswith(".worktrees/dev-42")
    # The inner command (`pwd`) ran inside the per-issue worktree, not repo root.
    assert out.read_text().strip().endswith(".worktrees/dev-42")


def test_spawn_script_works_with_no_prior_worktree(tmp_path):
    """Control: with no pre-existing worktree, setup still succeeds cleanly."""
    repo = _make_repo(tmp_path)
    task = tmp_path / "task.txt"
    task.write_text("noop\n")
    out = tmp_path / "out.txt"
    err = tmp_path / "err.txt"
    r = subprocess.run(
        ["bash", str(_SPAWN_SH), "7", "dev", str(task), str(out), str(err), "pwd"],
        cwd=str(repo), capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, err.read_text()
    assert "WORKTREE_SETUP_FAILED" not in err.read_text()
    assert out.read_text().strip().endswith(".worktrees/dev-7")


def _stub_bin(tmp_path: Path) -> Path:
    """A PATH dir with no-op `hermes`/`gh` stubs so delegate.sh can transition
    without a real board or GitHub."""
    b = tmp_path / "bin"
    b.mkdir()
    (b / "hermes").write_text("#!/bin/sh\nexit 0\n")
    (b / "gh").write_text("#!/bin/sh\nexit 0\n")  # PR detection → empty
    for f in ("hermes", "gh"):
        os.chmod(b / f, 0o755)
    return b


def test_delegate_script_frees_stale_branch_holder(tmp_path):
    """daedalus-delegate.sh (the hot path) also frees a stale fix/issue-<N> holder
    and runs the developer in its own worktree — never the shared repo root."""
    repo = _make_repo(tmp_path)
    _git(repo, "worktree", "add", str(repo / ".worktrees/dev-42-stale"),
         "-b", "fix/issue-42")
    stub = _stub_bin(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    task = tmp_path / "task.txt"
    task.write_text("noop\n")
    out = tmp_path / "out.txt"

    env = dict(os.environ)
    env["PATH"] = f"{stub}{os.pathsep}{env['PATH']}"
    env["HOME"] = str(home)
    r = subprocess.run(
        ["bash", str(_DELEGATE_SH),
         "--task-file", str(task), "--cmd", "true",
         "--card", "t1", "--board", "b", "--out", str(out),
         "--repo", str(repo), "--branch", "fix/issue-42", "--base", "dev",
         "--role", "developer", "--relay-verdict",
         "--pr-grace-secs", "0", "--poll-interval", "1", "--max-wait", "30"],
        capture_output=True, text=True, timeout=90, env=env,
    )
    # Assert on git state, not the out-file: delegate.sh's child spawn redirects
    # the agent stdout with `> "$_out"`, truncating the earlier setup diagnostics.
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (repo / ".worktrees/dev-42-stale").exists(), "stale holder not freed"
    # The branch is now checked out under a fresh per-delegate worktree path — the
    # add succeeded, so it never fell back to the shared repo root.
    branches = _worktree_branch_paths(repo)
    held = branches.get("fix/issue-42", "")
    assert "/.worktrees/dev-42-" in held, f"branch not under per-delegate worktree: {held}"


if __name__ == "__main__":
    # Dual-mode: run standalone without pytest. Auto-discovers test_* functions.
    import tempfile

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                import inspect

                if "tmp_path" in inspect.signature(fn).parameters:
                    with tempfile.TemporaryDirectory() as d:
                        fn(Path(d))
                else:
                    fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)

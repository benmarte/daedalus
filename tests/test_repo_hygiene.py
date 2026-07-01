"""Guard tests for issue #1128 — repo hygiene.

Asserts the repo root stays free of runtime artifacts and that .gitignore
covers the dispatcher/QA pipeline's byproducts, so strays are caught in CI
instead of accumulating silently again.
"""

import glob
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

IGNORE_PATTERNS = ["qa-*.log", "worktree-*/", ".worktrees/", "*.db", "*.sqlite"]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout


def test_gitignore_covers_runtime_artifacts():
    gitignore = (REPO_ROOT / ".gitignore").read_text().splitlines()
    missing = [p for p in IGNORE_PATTERNS if p not in gitignore]
    assert not missing, f".gitignore is missing runtime-artifact patterns: {missing}"


def test_no_tracked_runtime_artifacts():
    tracked = _git("ls-files").splitlines()
    offenders = [
        f
        for f in tracked
        if f.endswith((".db", ".sqlite"))
        or (Path(f).name.startswith("qa-") and f.endswith(".log"))
    ]
    assert not offenders, f"runtime artifacts are tracked: {offenders}"


def test_repo_root_has_no_worktree_dirs_or_qa_logs():
    strays = [
        p for p in glob.glob(str(REPO_ROOT / "worktree-*")) if Path(p).is_dir()
    ] + glob.glob(str(REPO_ROOT / "qa-*.log"))
    assert not strays, f"stray artifacts at repo root: {strays}"


def test_root_spec_md_removed():
    assert not (REPO_ROOT / "SPEC.md").exists(), (
        "root SPEC.md is a stale duplicate of tasks/spec-issue-1072.md; "
        "it must not reappear (issue #1128)"
    )


def test_uv_lock_tracked():
    assert "uv.lock" in _git("ls-files", "uv.lock"), (
        "uv.lock must be committed for reproducible `uv sync` installs"
    )

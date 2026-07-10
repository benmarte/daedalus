"""Shape guards for the CHANGELOG automation workflow (epic #1386).

Fast, hermetic checks on the *shape* of ``.github/workflows/changelog.yml`` and
its coupling to ``scripts/update_changelog.py`` — they do NOT execute the
workflow. They catch cheap regressions: a dropped trigger, a lost merge/base
gate, the concurrency guard removed (reintroducing the #1179 line-1 race), the
script CLI drifting from what the workflow passes, or PR title interpolated
unsafely instead of via env.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_repo_root = Path(__file__).resolve().parent.parent
_workflow = _repo_root / ".github" / "workflows" / "changelog.yml"
_script = _repo_root / "scripts" / "update_changelog.py"


def _load() -> dict:
    return yaml.safe_load(_workflow.read_text())


def _on(cfg: dict) -> dict:
    # PyYAML parses the bare ``on:`` key as the boolean True.
    return cfg.get("on", cfg.get(True)) or {}


def test_workflow_exists_and_parses() -> None:
    assert _workflow.exists(), "changelog.yml workflow missing"
    assert isinstance(_load(), dict)


def test_triggers_on_pr_closed() -> None:
    pr = _on(_load()).get("pull_request") or {}
    assert "closed" in (pr.get("types") or []), "must trigger on pull_request: closed"


def test_serialized_concurrency_guard() -> None:
    """Runs must serialize (group) and never cancel — else concurrent merges race
    on line 1 of CHANGELOG.md (#1179)."""
    conc = _load().get("concurrency") or {}
    assert conc.get("group") == "changelog-dev"
    assert conc.get("cancel-in-progress") is False


def test_job_gates_on_merged_into_dev() -> None:
    job = _load()["jobs"]["update-changelog"]
    cond = job["if"]
    assert "merged == true" in cond, "must run only on MERGED PRs"
    assert "base.ref == 'dev'" in cond, "must run only for PRs into dev (excludes release PRs to main)"
    assert job.get("permissions", {}).get("contents") == "write"


def test_invokes_script_with_correct_cli() -> None:
    """The workflow's invocation must match update_changelog.py's actual CLI."""
    steps = _load()["jobs"]["update-changelog"]["steps"]
    run_blocks = "\n".join(s.get("run", "") for s in steps)
    assert "scripts/update_changelog.py" in run_blocks
    for flag in ("--title", "--pr-number", "--pr-url"):
        assert flag in run_blocks, f"workflow must pass {flag}"
    # And those flags must exist in the script (guards CLI drift).
    src = _script.read_text()
    for flag in ("--title", "--pr-number", "--pr-url"):
        assert f'"{flag}"' in src, f"update_changelog.py lost {flag}"


def test_pr_title_passed_via_env_not_inline() -> None:
    """PR title is attacker-controlled — it must reach the script via env, never
    interpolated inline into the run block."""
    steps = _load()["jobs"]["update-changelog"]["steps"]
    prepend = next(s for s in steps if "update_changelog.py" in s.get("run", ""))
    assert "PR_TITLE" in (prepend.get("env") or {}), "PR title must be an env var"
    assert "${{ github.event.pull_request.title }}" not in prepend["run"], (
        "PR title must NOT be inlined into run: — pass it via env"
    )


def test_commit_uses_skip_ci_message() -> None:
    steps = _load()["jobs"]["update-changelog"]["steps"]
    run_blocks = "\n".join(s.get("run", "") for s in steps)
    assert "[skip ci]" in run_blocks, "commit must carry [skip ci] to avoid re-triggering CI"

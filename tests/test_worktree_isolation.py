"""Tests for developer worktree isolation (shared-workdir branch/PR race fix).

Background: all developer coding-agents used to run in the single shared workdir.
With multiple developers active, `git checkout -b` and the PR-detection
`git rev-parse HEAD` cross-wired branches/PRs between issues — a #1131-style
CODING_AGENT_DIED loop where the agent reported another issue's PR.

Fix: each developer runs in a dedicated per-issue git worktree on a deterministic
branch `fix/issue-<N>` forked off the configured target branch, and PR detection is
passed that known branch instead of reading the shared HEAD.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import _load_dispatch  # noqa: E402

disp = _load_dispatch()

_FAKE_ISSUE = {
    "number": 42,
    "title": "Test issue",
    "body": "Test body",
    "labels": [],
    "state": "open",
}


def _dev_body(base_branch: str = "dev") -> str:
    return disp._dev_task_body(
        repo="owner/repo",
        issue=_FAKE_ISSUE,
        iterations=3,
        workdir="/tmp/repo",
        base_branch=base_branch,
        provider_name="github",
        coding_agent="claude-code",
        coding_agent_cmd="CLAUDE_CONFIG_DIR=$HOME/.claude-rizq claude -p",
    )


# ── delegation spawn wraps developer in a per-issue worktree ──────────────────


def test_developer_spawn_uses_delegate_wrapper():
    # Since #1280 the developer block invokes the lifecycle wrapper
    # (daedalus-delegate.sh), which itself spawns the isolated worktree — so the
    # worktree-isolation guarantee is preserved one level down (asserted in
    # test_delegate_wrapper.py against the wrapper's source).
    block = disp._build_delegation_instructions(
        "claude-code", "claude -p", role="developer", issue_number=42, base_branch="dev"
    )
    assert "daedalus-delegate.sh" in block
    # The wrapper is invoked with the issue number and base branch as its args.
    assert "daedalus-delegate.sh 42 dev" in block
    # The outer block no longer spawns the worktree directly (the wrapper does).
    assert "daedalus-worktree-spawn.sh" not in block


def test_developer_spawn_passes_configured_base_branch():
    # A non-default target branch must flow through to the wrapper.
    block = disp._build_delegation_instructions(
        "claude-code", "claude -p", role="developer", issue_number=7, base_branch="main"
    )
    assert "daedalus-delegate.sh 7 main" in block


def test_non_developer_role_does_not_use_worktree_or_delegate_script():
    # QA/reviewer/etc run against an existing PR and already isolate themselves;
    # they keep the original in-place spawn (no worktree wrapper, no delegate wrapper).
    for role in ("qa", "reviewer", "security", "documentation", "validator", "pm"):
        block = disp._build_delegation_instructions(
            "claude-code", "claude -p", role=role, issue_number=42, base_branch="dev"
        )
        assert "daedalus-worktree-spawn.sh" not in block, role
        assert "daedalus-delegate.sh" not in block, role


# ── PR detection is race-free (explicit deterministic branch) ─────────────────


def test_wait_cmd_passes_deterministic_branch_to_detect_pr():
    wait = disp._wait_for_agent_cmd("dev", 42, 3600, detect_pr=True)
    assert "daedalus-detect-pr.sh" in wait
    # detect-pr is handed fix/issue-42 explicitly, not left to read shared HEAD.
    assert "fix/issue-42" in wait


def test_wait_cmd_no_detect_for_non_developer():
    wait = disp._wait_for_agent_cmd("qa", 42, 3600, detect_pr=False)
    assert "daedalus-detect-pr.sh" not in wait


# ── developer body no longer creates a branch in the shared tree ──────────────


def test_dev_body_does_not_instruct_manual_branch_creation():
    body = _dev_body()
    assert "checkout -b fix/issue-42-<slug>" not in body
    assert "git checkout -b" not in body


def test_dev_body_references_isolated_worktree_on_deterministic_branch():
    body = _dev_body()
    assert "fix/issue-42" in body
    assert "worktree" in body.lower()


def test_dev_body_mentions_configured_base_branch():
    body = _dev_body(base_branch="release")
    assert "release" in body


if __name__ == "__main__":
    # Dual-mode: run standalone without pytest. Auto-discovers test_* functions so
    # the list never goes stale (daedalus dual-mode test convention).
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)

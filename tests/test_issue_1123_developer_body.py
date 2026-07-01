"""Tests for issue #1123.

AC1: _dev_task_body() must NOT include /review or /code-simplify — those belong to
     the downstream reviewer agent.
AC3: On inner-agent failure (CODING_AGENT_DIED / CODING_AGENT_TIMEOUT / no PR URL),
     the outer developer agent must block cleanly and NOT attempt to implement the
     task itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import _load_dispatch  # noqa: E402

disp = _load_dispatch()

_FAKE_ISSUE = {
    "number": 1,
    "title": "Test issue",
    "body": "Test body",
    "labels": [],
    "state": "open",
}


def _body(coding_agent: str = "none", coding_agent_cmd: str = "") -> str:
    return disp._dev_task_body(
        repo="owner/repo",
        issue=_FAKE_ISSUE,
        iterations=3,
        workdir="/tmp/repo",
        base_branch="dev",
        provider_name="github",
        coding_agent=coding_agent,
        coding_agent_cmd=coding_agent_cmd,
    )


# ── AC1: /review and /code-simplify absent ────────────────────────────────────


def test_no_review_in_developer_body():
    assert "/review" not in _body()


def test_no_code_simplify_in_developer_body():
    assert "/code-simplify" not in _body()


def test_required_skills_present():
    body = _body()
    assert "/spec" in body
    assert "/plan" in body
    assert "/build" in body
    assert "/test" in body


def test_block_on_failure_not_implement():
    body = _body()
    # Outer agent must be told to block, not to do the work itself
    assert "kanban_block" in body or "Block your kanban card" in body
    # Must not instruct the developer to fall back to implementing directly
    assert "implement it yourself" not in body
    assert "implement yourself" not in body


# ── AC1 with coding agent (delegation mode) ──────────────────────────────────


def test_no_review_in_delegation_body():
    """/review must not appear in the delegation-mode developer body either."""
    body = _body("claude-code", "claude --dangerously-skip-permissions -p")
    assert "/review" not in body, (
        "Delegation-mode developer body contains '/review'.\n"
        f"Body excerpt:\n{body[:600]}"
    )


def test_no_code_simplify_in_delegation_body():
    """/code-simplify must not appear in the delegation-mode developer body."""
    body = _body("claude-code", "claude --dangerously-skip-permissions -p")
    assert "/code-simplify" not in body, (
        "Delegation-mode developer body contains '/code-simplify'.\n"
        f"Body excerpt:\n{body[:600]}"
    )


# ── AC3: inner-agent failure must block, not fall back to implementation ──────


def test_agent_failed_note_prohibits_self_implementation():
    """_AGENT_FAILED_NOTE must tell the outer agent NOT to implement itself on failure."""
    note = disp._AGENT_FAILED_NOTE
    lower = note.lower()
    assert "implement" in lower or "investigate" in lower, (
        "_AGENT_FAILED_NOTE must explicitly prohibit self-implementation.\n"
        f"Current note: {note}"
    )


def test_agent_failed_note_requires_kanban_block():
    """_AGENT_FAILED_NOTE must reference kanban_block."""
    assert "kanban_block" in disp._AGENT_FAILED_NOTE, (
        "_AGENT_FAILED_NOTE must reference kanban_block for the failure path.\n"
        f"Current note: {disp._AGENT_FAILED_NOTE}"
    )


def test_developer_role_after_spawn_prohibits_self_implementation():
    """Developer role must prohibit self-implementation in the after-spawn STOP message."""
    after = disp._ROLE_AFTER_SPAWN["developer"]
    lower = after.lower()
    assert "do not attempt" in lower or "do not implement" in lower, (
        "Developer _ROLE_AFTER_SPAWN STOP must say 'do not attempt the implementation'.\n"
        f"Current template:\n{after}"
    )


def test_developer_role_after_spawn_handles_no_pr_url():
    """Developer role must handle inner agent producing no PR URL."""
    after = disp._ROLE_AFTER_SPAWN["developer"]
    assert "PR URL" in after, (
        "Developer _ROLE_AFTER_SPAWN must check for 'PR URL' absence in inner agent output.\n"
        f"Current template:\n{after}"
    )


def test_delegation_body_contains_died_timeout_markers():
    """Delegation block in developer body must contain failure markers."""
    body = _body("claude-code", "claude --dangerously-skip-permissions -p")
    assert "CODING_AGENT_DIED" in body, (
        "Developer delegation body missing CODING_AGENT_DIED failure marker.\n"
        f"Body excerpt:\n{body[:600]}"
    )
    assert "CODING_AGENT_TIMEOUT" in body, (
        "Developer delegation body missing CODING_AGENT_TIMEOUT failure marker.\n"
        f"Body excerpt:\n{body[:600]}"
    )

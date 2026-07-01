"""Tests for issue #1121 — inner agent must not receive kanban complete/block instructions.

When a coding_agent (e.g. claude-code) is configured, _validator_body() generates a
task body that is piped to an inner claude subprocess. The inner subprocess must NOT
call `hermes kanban complete` or `hermes kanban block` — only the outer validator-daedalus
agent calls those after reading the inner agent's stdout.

Root cause: _validator_body() previously included "→ Complete your card with summary..."
and "→ Block your card with summary..." instructions unconditionally, so the inner agent
would call kanban complete with no summary argument, producing summary: None and triggering
an infinite retry loop.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import _load_dispatch  # noqa: E402

disp = _load_dispatch()


def _make_issue(n=42, title="Fix the thing", body="Some bug description."):
    return {"number": n, "title": title, "body": body, "labels": [], "state": "open"}


def _validator_body_inner(**kwargs):
    """Call _validator_body with a coding agent configured (inner agent mode)."""
    defaults = dict(
        repo="org/repo",
        issue=_make_issue(),
        workdir="/repo",
        base_branch="dev",
        provider_name="github",
        coding_agent="claude-code",
        coding_agent_cmd="claude --dangerously-skip-permissions -p",
    )
    defaults.update(kwargs)
    return disp._validator_body(**defaults)


def _validator_body_outer(**kwargs):
    """Call _validator_body with no coding agent (outer agent handles everything)."""
    defaults = dict(
        repo="org/repo",
        issue=_make_issue(),
        workdir="/repo",
        base_branch="dev",
        provider_name="github",
    )
    defaults.update(kwargs)
    return disp._validator_body(**defaults)


# ── inner agent mode: coding_agent configured ─────────────────────────────────


def test_inner_body_has_no_complete_your_card():
    """Inner agent body must not say 'Complete your card' — inner agent must not call kanban."""
    body = _validator_body_inner()
    assert "Complete your card" not in body, (
        "Inner agent body contains 'Complete your card' — this causes the inner agent "
        "to call hermes kanban complete without a summary.\n"
        f"Body excerpt:\n{body[:600]}"
    )


def test_inner_body_has_no_block_your_card():
    """Inner agent body must not say 'Block your card' — inner agent must not call kanban."""
    body = _validator_body_inner()
    assert "Block your card" not in body, (
        "Inner agent body contains 'Block your card' — this causes the inner agent "
        "to call hermes kanban block incorrectly.\n"
        f"Body excerpt:\n{body[:600]}"
    )


def test_inner_body_contains_print_to_stdout_for_confirmed():
    """CONFIRMED outcome must instruct the inner agent to print to stdout, not call kanban."""
    body = _validator_body_inner()
    assert "Print to stdout:" in body, (
        "Inner agent body missing 'Print to stdout:' instructions. "
        "Each outcome must direct the agent to print the verdict to stdout.\n"
        f"Body excerpt:\n{body[:1200]}"
    )


def test_inner_body_prohibits_kanban_complete():
    """Inner agent body must explicitly prohibit calling hermes kanban complete."""
    body = _validator_body_inner()
    assert "DO NOT call hermes kanban complete" in body, (
        "Inner agent body must explicitly say 'DO NOT call hermes kanban complete'.\n"
        f"Body excerpt:\n{body[:600]}"
    )


def test_inner_body_stdout_verdict_for_each_outcome():
    """All outcome blocks in inner mode must use 'Print to stdout:' instead of kanban calls."""
    body = _validator_body_inner()
    # These verdict prefixes must appear in stdout-print instructions, not kanban calls
    for prefix in ("CONFIRMED:", "STOP:", "BLOCKED:", "ESCALATE:"):
        assert (
            f"Print to stdout: '{prefix}" in body
            or f'Print to stdout: "{prefix}' in body
        ), (
            f"Inner agent body missing 'Print to stdout: {prefix!r}' instruction.\n"
            f"Body excerpt near CONFIRMED section:\n{body[body.find('CONFIRMED') : body.find('CONFIRMED') + 300]}"
        )


# ── outer agent mode: no coding agent ─────────────────────────────────────────


def test_outer_body_has_complete_your_card():
    """When no coding agent, body must still contain kanban complete instructions (outer agent mode)."""
    body = _validator_body_outer()
    assert "Complete your card" in body, (
        "Outer agent body lost 'Complete your card' instructions — regression.\n"
        f"Body excerpt:\n{body[:600]}"
    )


def test_outer_body_has_kanban_permission():
    """When no coding agent, body must preserve the kanban write permission line."""
    body = _validator_body_outer()
    assert (
        "The only kanban write allowed is completing or blocking YOUR OWN card." in body
    ), (
        "Outer agent body lost kanban permission line — regression.\n"
        f"Body excerpt:\n{body[:600]}"
    )


def test_outer_body_has_no_print_to_stdout():
    """Outer agent body must not contain 'Print to stdout:' — that's for inner agent only."""
    body = _validator_body_outer()
    assert "Print to stdout:" not in body, (
        "Outer agent body contains 'Print to stdout:' — this should only appear in inner agent mode.\n"
        f"Body excerpt:\n{body[:600]}"
    )

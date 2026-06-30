"""Tests that validator task bodies and SOUL contain explicit kanban write prohibition.

Issue #1105: validator agent created live kanban tasks during investigation,
which spawned real QA sessions. The fix adds an explicit prohibition against
hermes kanban write operations (create, complete, block, archive) to both the
task body template and the validator SOUL file.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import _load_dispatch  # noqa: E402

disp = _load_dispatch()

ROOT = Path(__file__).resolve().parent.parent

KANBAN_PROHIBITION = (
    "NEVER call hermes kanban create or any kanban write command — "
    "you are read-only. The only kanban write allowed is completing or "
    "blocking YOUR OWN card."
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_issue(n=42, title="Fix the thing", body="Some bug description."):
    return {"number": n, "title": title, "body": body, "labels": [], "state": "open"}


def _validator_body(**kwargs):
    defaults = dict(
        repo="org/repo",
        issue=_make_issue(),
        workdir="/repo",
        base_branch="dev",
        provider_name="github",
    )
    defaults.update(kwargs)
    return disp._validator_body(**defaults)


def _planner_not_suitable_body(**kwargs):
    defaults = dict(
        repo="org/repo",
        issue=_make_issue(),
        planner_summary="NOT SUITABLE: this is a simple bug, not an epic.",
        workdir="/repo",
        base_branch="dev",
        provider_name="github",
    )
    defaults.update(kwargs)
    return disp._planner_not_suitable_validator_body(**defaults)


# ── task body tests ───────────────────────────────────────────────────────────

def test_validator_body_contains_kanban_prohibition():
    """_validator_body must contain the explicit kanban write prohibition."""
    body = _validator_body()
    assert KANBAN_PROHIBITION in body, (
        f"_validator_body missing kanban prohibition.\n"
        f"Expected to find:\n  {KANBAN_PROHIBITION!r}\n"
        f"In body excerpt:\n  {body[:500]!r}"
    )


def test_validator_body_prohibits_kanban_create():
    """_validator_body must explicitly name 'hermes kanban create' as prohibited."""
    body = _validator_body()
    assert "hermes kanban create" in body, (
        "_validator_body does not name 'hermes kanban create' as prohibited."
    )


def test_planner_not_suitable_body_contains_kanban_prohibition():
    """_planner_not_suitable_validator_body must also contain the kanban prohibition."""
    body = _planner_not_suitable_body()
    assert KANBAN_PROHIBITION in body, (
        f"_planner_not_suitable_validator_body missing kanban prohibition.\n"
        f"Expected to find:\n  {KANBAN_PROHIBITION!r}\n"
        f"In body excerpt:\n  {body[:500]!r}"
    )


def test_planner_not_suitable_body_prohibits_kanban_create():
    """_planner_not_suitable_validator_body must name 'hermes kanban create' as prohibited."""
    body = _planner_not_suitable_body()
    assert "hermes kanban create" in body, (
        "_planner_not_suitable_validator_body does not name 'hermes kanban create' as prohibited."
    )


# ── SOUL file test ────────────────────────────────────────────────────────────

def test_validator_soul_contains_kanban_prohibition():
    """config/souls/validator-daedalus.md must contain the kanban write prohibition."""
    soul_path = ROOT / "config" / "souls" / "validator-daedalus.md"
    assert soul_path.exists(), f"SOUL file not found: {soul_path}"
    soul_text = soul_path.read_text(encoding="utf-8")
    assert KANBAN_PROHIBITION in soul_text, (
        f"validator-daedalus.md missing kanban prohibition.\n"
        f"Expected to find:\n  {KANBAN_PROHIBITION!r}"
    )


def test_validator_soul_prohibits_kanban_create():
    """config/souls/validator-daedalus.md must name 'hermes kanban create' as prohibited."""
    soul_path = ROOT / "config" / "souls" / "validator-daedalus.md"
    soul_text = soul_path.read_text(encoding="utf-8")
    assert "hermes kanban create" in soul_text, (
        "validator-daedalus.md does not name 'hermes kanban create' as prohibited."
    )

"""Issue #1416 — PM checks must honor a JSON OutcomeRecord like the ``spec:`` prefix.

``daedalus-delegate.sh --relay-verdict`` prefers the fenced JSON outcome block
over the ``spec:`` SOUL line when it completes a PM card, so a relayed PM card's
stored summary is JSON-only::

    ```json
    {"daedalus_outcome": 1, "role": "pm", "verdict": "spec", ...}
    ```

The old PM checks in ``core/dispatch/checks.py`` only did ``startswith("spec:")``,
so a JSON-only summary looked *stale* — the dispatcher respawned a PM card every
advance-hook tick and never fanned out the team. This mirrors the fix already
applied to the validator path (``_check_confirmed_validators`` honors a JSON
``confirmed`` record).

These tests cover JSON-only, prefix-only, and prefix+JSON summaries for the three
PM check surfaces plus the delegation-block board-slug injection sub-fix.
"""

from __future__ import annotations

import json

from unittest import mock

import pytest

SLUG = "proj"
REPO = "benmarte/daedalus"
VALIDATOR = "validator-daedalus"
PM = "project-manager-daedalus"
DEVELOPER = "developer-daedalus"
QA = "qa-daedalus"
REVIEWER = "reviewer-daedalus"
SECURITY = "security-analyst-daedalus"
DOCS = "documentation-daedalus"


def _json_outcome(verdict: str, issue: int) -> str:
    """A relay-verdict-style fenced JSON OutcomeRecord for the PM role."""
    rec = {
        "daedalus_outcome": 1,
        "role": "pm",
        "verdict": verdict,
        "refs": {"issue": issue},
        "note": "",
    }
    return "```json\n" + json.dumps(rec) + "\n```"


# ── _pm_task_state ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "summary, expected",
    [
        # JSON-only relay summaries — the #1416 regression case.
        (_json_outcome("spec", 11), "complete"),
        (_json_outcome("assigned", 11), "complete"),
        # Prefix-only (legacy) summaries — unchanged behavior.
        ("SPEC: acceptance criteria defined", "complete"),
        ("assigned: team tasks created", "complete"),
        # Prefix + JSON dual-write — still complete.
        ("SPEC: done\n\n" + _json_outcome("spec", 11), "complete"),
        # Genuinely stale — empty, or a non-spec/assigned verdict.
        ("", "stale"),
        ("some unrelated prose", "stale"),
        (_json_outcome("clarified", 11), "stale"),
        (_json_outcome("escalated", 11), "stale"),
    ],
)
def test_pm_task_state_honors_json_outcome(pipeline, summary, expected):
    disp, kanban = pipeline.disp, pipeline.kanban
    n = 11
    kanban.seed(
        assignee=PM,
        title=f"#{n} A tidy feature",
        status="done",
        summary=summary,
        idempotency_key=f"pm-{n}",
    )
    state, _ = disp._pm_task_state(SLUG, n, PM)
    assert state == expected


# ── _check_completed_pm ────────────────────────────────────────────────────────


def _check_pm(disp, issues_map, **kw):
    return disp._check_completed_pm(
        SLUG, REPO, issues_map, 3, "", "", "dev", "github", **kw
    )


def test_check_completed_pm_fans_out_on_json_spec(pipeline, fake_issue):
    """A JSON-only ``verdict: spec`` PM card triggers team fan-out (#1416)."""
    disp, kanban = pipeline.disp, pipeline.kanban
    n = 11
    issues_map = {n: fake_issue(n, "A tidy feature", "Add a small feature.")}
    kanban.seed(
        assignee=PM,
        title=f"#{n} A tidy feature",
        status="done",
        summary=_json_outcome("spec", n),
        idempotency_key=f"pm-{n}",
    )
    assert _check_pm(disp, issues_map) == [n]
    # Team cards were actually created (developer among them).
    assert any(c["assignee"] == DEVELOPER for c in kanban.created)


def test_check_completed_pm_skips_on_json_assigned(pipeline, fake_issue):
    """A JSON-only ``verdict: assigned`` PM card is the skip path — no fan-out."""
    disp, kanban = pipeline.disp, pipeline.kanban
    n = 12
    issues_map = {n: fake_issue(n, "A tidy feature", "Add a small feature.")}
    kanban.seed(
        assignee=PM,
        title=f"#{n} A tidy feature",
        status="done",
        summary=_json_outcome("assigned", n),
        idempotency_key=f"pm-{n}",
    )
    assert _check_pm(disp, issues_map) == []
    assert kanban.created == []


def test_check_completed_pm_prefix_still_fans_out(pipeline, fake_issue):
    """Prefix-only ``SPEC:`` behavior is unchanged (regression guard)."""
    disp, kanban = pipeline.disp, pipeline.kanban
    n = 13
    issues_map = {n: fake_issue(n, "A tidy feature", "Add a small feature.")}
    kanban.seed(
        assignee=PM,
        title=f"#{n} A tidy feature",
        status="done",
        summary="SPEC: acceptance criteria defined",
        idempotency_key=f"pm-{n}",
    )
    assert _check_pm(disp, issues_map) == [n]


# ── _try_adopt_pm_spec_comment ─────────────────────────────────────────────────


class _SpecCommentProvider:
    """Provider stub whose issue carries an attributed Implementation Spec."""

    def get_issue_comments(self, issue_number: int) -> list:
        return [
            {
                "body": (
                    "**Agent: project-manager**\n\n## Implementation Spec\n\n"
                    "Branch: fix/issue-11-frobnicate\n"
                )
            }
        ]

    def get_issue_state(self, issue_number: int) -> str:
        return "open"


def test_adopt_skips_card_with_json_spec_outcome(pipeline):
    """A card carrying a valid JSON spec outcome is never adopted/rewritten (#1416)."""
    disp, kanban = pipeline.disp, pipeline.kanban
    n = 11
    kanban.seed(
        assignee=PM,
        title=f"#{n} A tidy feature",
        status="done",
        summary=_json_outcome("spec", n),
        idempotency_key=f"pm-{n}",
    )
    with mock.patch.object(kanban, "edit_summary", create=True, return_value=True) as edit:
        adopted = disp._try_adopt_pm_spec_comment(
            SLUG, n, PM, _SpecCommentProvider()
        )
    assert adopted is False
    edit.assert_not_called()


def test_adopt_still_rewrites_genuinely_stale_card(pipeline):
    """A truly empty done PM card is still adopted when a spec comment exists."""
    disp, kanban = pipeline.disp, pipeline.kanban
    n = 14
    kanban.seed(
        assignee=PM,
        title=f"#{n} A tidy feature",
        status="done",
        summary="",
        idempotency_key=f"pm-{n}",
    )
    with mock.patch.object(kanban, "edit_summary", create=True, return_value=True) as edit:
        adopted = disp._try_adopt_pm_spec_comment(
            SLUG, n, PM, _SpecCommentProvider()
        )
    assert adopted is True
    edit.assert_called_once()


# ── delegation board-slug injection (sub-fix) ──────────────────────────────────


def test_delegation_injects_real_board_slug_for_nondeveloper(pipeline):
    """Non-developer relay spawn line carries the real slug, no placeholder (#1416)."""
    disp = pipeline.disp
    block = disp._build_delegation_instructions(
        "claude-code",
        "claude -p",
        role="pm",
        issue_number=11,
        board_slug="acme-widgets",
    )
    assert "--board acme-widgets " in block
    assert "<BOARD_SLUG>" not in block
    # <CARD_ID> is still the outer agent's job to substitute.
    assert "<CARD_ID>" in block


def test_delegation_blank_slug_keeps_placeholder(pipeline):
    """A blank board_slug falls back to the <BOARD_SLUG> placeholder (back-compat)."""
    disp = pipeline.disp
    block = disp._build_delegation_instructions(
        "claude-code",
        "claude -p",
        role="pm",
        issue_number=11,
    )
    assert "--board <BOARD_SLUG> " in block


def test_developer_delegation_unaffected_by_board_slug(pipeline):
    """The developer worktree-spawn line has no --board flag; slug is inert there."""
    disp = pipeline.disp
    block = disp._build_delegation_instructions(
        "claude-code",
        "claude -p",
        role="developer",
        issue_number=11,
        board_slug="acme-widgets",
    )
    assert "daedalus-worktree-spawn.sh" in block
    assert "--board" not in block

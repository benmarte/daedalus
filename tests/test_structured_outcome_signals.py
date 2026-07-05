"""Structured JSON outcome records make stage detection robust to paraphrasing.

Local models paraphrase the human-readable SOUL signal; the dispatcher must
honor a fenced JSON OutcomeRecord regardless of the prose wording, so the
pipeline advances without the guard firing or the validator stalling
(found by live dogfood on a local qwen-35B model, 2026-07-05).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import FakeKanban, FakeProvider, _load_dispatch  # noqa: E402

# Paraphrased prose (no canonical prefix) + a valid JSON outcome block.
_SECURITY_PARAPHRASE_JSON = (
    "Security review complete for PR #2. No security vulnerabilities found — "
    "pure string formatting, no injection vectors.\n\n"
    '```json\n{"daedalus_outcome": 1, "role": "security", "verdict": "approved", '
    '"refs": {"issue": 1, "pr": 2}}\n```'
)

_VALIDATOR_PARAPHRASE_JSON = (
    "The issue is real and reproducible; greet() does not exist in app.py yet. "
    "Acceptance criteria are clear.\n\n"
    '```json\n{"daedalus_outcome": 1, "role": "validator", "verdict": "confirmed", '
    '"refs": {"issue": 77}}\n```'
)


def _seed_done(fk, assignee, issue_n, summary):
    return fk.seed(assignee=assignee, title=f"#{issue_n} something",
                   status="done", summary=summary)


def _run_guard(disp, fk, **kwargs):
    with mock.patch.object(disp.kanban, "list_tasks", fk.list_tasks), \
         mock.patch.object(disp.kanban, "show_card", fk.show_card), \
         mock.patch.object(disp.kanban, "archive_task", fk.archive_task), \
         mock.patch.object(disp.kanban, "create_task", fk.create_task), \
         mock.patch.object(disp.kanban, "block_task", fk.block_task):
        return disp._guard_prefix_on_done("board", **kwargs)


def test_guard_does_not_fire_when_json_record_present_despite_paraphrase():
    """Security done card with paraphrased prose BUT a valid JSON record → the
    JSON is authoritative, so the guard must NOT fire (no archive/re-block)."""
    disp = _load_dispatch()
    fk = FakeKanban()
    _seed_done(fk, "security-analyst-daedalus", 1, _SECURITY_PARAPHRASE_JSON)
    count = _run_guard(disp, fk)
    assert count == 0
    assert len(fk.archived) == 0
    assert len(fk.blocked_calls) == 0


def test_validator_confirmed_honored_from_json_despite_paraphrase():
    """A validator done card that paraphrases (no 'CONFIRMED:' prefix) but carries
    a JSON record with verdict 'confirmed' advances → a PM card is created."""
    disp = _load_dispatch()
    fk = FakeKanban()
    prov = FakeProvider(ci_status="green")
    _seed_done(fk, "validator-daedalus", 77, _VALIDATOR_PARAPHRASE_JSON)
    with mock.patch.object(disp.kanban, "list_tasks", fk.list_tasks), \
         mock.patch.object(disp.kanban, "show_card", fk.show_card), \
         mock.patch.object(disp.kanban, "create_task", fk.create_task), \
         mock.patch.object(disp.kanban, "complete", fk.complete), \
         mock.patch.object(disp.kanban, "comment", fk.comment):
        result = disp._check_confirmed_validators(
            "board", "benmarte/x", {77: {"number": 77, "title": "t", "body": "b"}},
            3, "", "", "dev", "github", provider=prov,
        )
    assert 77 in result
    assert fk.created_with_key("pm-77") is not None

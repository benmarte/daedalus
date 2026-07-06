"""Tests for #1294 Phase 5 — native planner decompose + QA swarm fan-out.

Covers the additive ``kanban.swarm()`` wrapper (AC1) and the
``planner.native_decompose`` flag read (AC2). The dispatcher-branch ACs (3-7)
are exercised in the integration tests once the flag is wired.
"""

from __future__ import annotations

from unittest import mock

from core.kanban import swarm


# ── AC1: swarm() builds the correct argv ────────────────────────────────────


def test_swarm_argv_full():
    """swarm() emits --worker (repeated), --verifier, --synthesizer,
    --idempotency-key and the positional goal LAST."""
    def mock_hk(args, timeout=60):
        return 0, "created root t_abc123", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk) as m:
        tid = swarm(
            "board-x",
            "Review + document PR for issue #7",
            workers=[
                "reviewer-daedalus:Review PR #7",
                "security-analyst-daedalus:Security review PR #7",
                "accessibility-daedalus:A11y review PR #7",
            ],
            verifier="qa-daedalus",
            synthesizer="documentation-daedalus",
            idempotency_key="swarm-7",
        )

    assert m.call_count == 1
    argv = m.call_args[0][0]
    # board + subcommand
    assert argv[:4] == ["--board", "board-x", "swarm"] or argv[:2] == ["--board", "board-x"]
    assert "swarm" in argv
    # three repeated --worker flags
    assert argv.count("--worker") == 3
    assert "reviewer-daedalus:Review PR #7" in argv
    assert "security-analyst-daedalus:Security review PR #7" in argv
    assert "accessibility-daedalus:A11y review PR #7" in argv
    # verifier / synthesizer
    assert argv[argv.index("--verifier") + 1] == "qa-daedalus"
    assert argv[argv.index("--synthesizer") + 1] == "documentation-daedalus"
    # idempotency
    assert argv[argv.index("--idempotency-key") + 1] == "swarm-7"
    # goal is the LAST positional token
    assert argv[-1] == "Review + document PR for issue #7"
    # returns the parsed root card id
    assert tid == "t_abc123"


def test_swarm_optional_workers_omitted_when_empty():
    """Non-UI issue: only two workers, no accessibility."""
    def mock_hk(args, timeout=60):
        return 0, "t_def456", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk) as m:
        swarm(
            "b",
            "goal",
            workers=["reviewer-daedalus:r", "security-analyst-daedalus:s"],
            verifier="qa-daedalus",
            synthesizer="documentation-daedalus",
        )

    argv = m.call_args[0][0]
    assert argv.count("--worker") == 2
    # no idempotency key passed → flag absent
    assert "--idempotency-key" not in argv


def test_swarm_never_raises_returns_none_on_failure():
    """Non-zero rc → log + return None (never-raise contract), so the caller
    can fall back to the legacy per-role fan-out."""
    def mock_hk(args, timeout=60):
        return 1, "", "boom"

    with mock.patch("core.kanban._hk", side_effect=mock_hk):
        tid = swarm(
            "b", "goal",
            workers=["reviewer-daedalus:r"],
            verifier="qa-daedalus",
            synthesizer="documentation-daedalus",
        )
    assert tid is None


# ── AC4/AC6/AC7: native QA swarm fan-out branch ─────────────────────────────

import core.iterate as iterate  # noqa: E402
import core.kanban as kanban  # noqa: E402
from conftest import FakeKanban, kanban_as  # noqa: E402

SLUG = "board-x"
DEV_CARD = {"id": "t_dev", "workspace": "dir:/tmp/wt", "title": "#7 Developer"}


def test_native_fanout_emits_single_swarm_reviews_qa_docs():
    """B1: flag ON → ONE swarm (reviewer/security/accessibility → qa verify →
    docs synthesize). No external QA gate card, no link, no block — swarm cards
    can't be gated externally, so qa runs as the swarm's verifier after reviews."""
    fk = FakeKanban()
    with kanban_as(kanban, fk):
        created = iterate._create_downstream_review_tasks(
            SLUG, 7, DEV_CARD, pr_number=42, native_decompose=True,
        )

    # exactly one swarm: workers=reviews, verifier=qa, synthesizer=docs
    assert len(fk.swarmed) == 1
    sw = fk.swarmed[0]
    assert sw["verifier"] == "qa-daedalus"
    assert sw["synthesizer"] == "documentation-daedalus"
    assert len(sw["workers"]) == 3
    assert any(w.startswith("reviewer-daedalus:") for w in sw["workers"])
    assert any(w.startswith("security-analyst-daedalus:") for w in sw["workers"])
    assert any(w.startswith("accessibility-daedalus:") for w in sw["workers"])
    assert sw["idempotency_key"] == "swarm-7"

    # B1: NO external QA gate card, NO link, NO dependency-block (swarm can't be
    # externally gated — that was the removed, ineffective design).
    assert fk.created == []           # no create_task calls at all
    assert fk.linked == []            # no link() calls
    assert fk.block_kind_calls == []  # no block_task() calls

    assert created == [sw["root"]]


def test_flag_off_is_legacy_fanout_no_swarm():
    """AC6: flag OFF → individual per-role cards, zero swarm calls (byte-identical)."""
    fk = FakeKanban()
    with kanban_as(kanban, fk):
        iterate._create_downstream_review_tasks(
            SLUG, 7, DEV_CARD, pr_number=42, native_decompose=False,
        )
    assert fk.swarmed == []
    # legacy path creates the five role cards
    assert len(fk.created) == 5


def test_native_fanout_idempotent_re_tick():
    """Re-tick with the swarm already present creates zero duplicate swarms."""
    fk = FakeKanban()
    with kanban_as(kanban, fk):
        iterate._create_downstream_review_tasks(SLUG, 7, DEV_CARD, pr_number=42, native_decompose=True)
        iterate._create_downstream_review_tasks(SLUG, 7, DEV_CARD, pr_number=42, native_decompose=True)
    assert len(fk.swarmed) == 1  # not 2


def test_native_fanout_falls_back_when_swarm_fails():
    """A swarm failure degrades to the legacy per-role fan-out rather than
    stranding the issue."""
    fk = FakeKanban()
    fk.swarm = lambda *a, **k: None  # force swarm failure
    with kanban_as(kanban, fk):
        iterate._create_downstream_review_tasks(
            SLUG, 7, DEV_CARD, pr_number=42, native_decompose=True,
        )
    # swarm failed → full legacy per-role fan-out (incl. the QA gate card).
    titles = [c["title"] for c in fk.created]
    assert any("Reviewer review" in t for t in titles)
    assert any("Security-Analyst review" in t for t in titles)
    assert sum(1 for c in fk.created if c["idempotency_key"] == "qa-7") == 1


# ── Part A: native planner decompose via kanban decompose ───────────────────

from core.iterate.executors import _execute_planner_decompose_inner  # noqa: E402
from conftest import FakeProvider  # noqa: E402


def test_native_planner_decompose_uses_kanban_decompose():
    """Flag ON: epic → ONE triage card (epic-{n}) + native decompose, NO GitHub
    sub-issues; marker + epic label posted, planner card completed."""
    fk = FakeKanban()
    prov = FakeProvider()
    plan_tid = fk.create_task(SLUG, "#50 Planner", assignee="planner-daedalus")
    with kanban_as(kanban, fk):
        ok = _execute_planner_decompose_inner(
            SLUG, plan_tid, 50, "Epic: big feature", "Body with scope.",
            [], "", False, prov, native_decompose=True,
        )

    assert ok is True
    # exactly one native decompose, on a triage card keyed epic-50
    assert len(fk.decomposed) == 1
    assert any(t.get("idempotency_key") == "epic-50" for t in fk.tasks.values())
    # NO GitHub sub-issues created (the whole point of D1a full-native)
    assert prov.created_issues == []
    # idempotency marker comment + epic label on the parent
    assert any(n == 50 for (n, _b) in prov.posted_issue_comments)
    assert "epic" in prov.labels.get(50, [])
    # planner card completed
    assert any(tid == plan_tid for (tid, _s) in fk.completed)


def test_native_planner_decompose_flag_off_is_legacy_subissues():
    """Flag OFF: the legacy path creates GitHub sub-issues (byte-identical)."""
    fk = FakeKanban()
    prov = FakeProvider()
    plan_tid = fk.create_task(SLUG, "#51 Planner", assignee="planner-daedalus")
    body = "Epic body\n\n- [ ] First chunk\n- [ ] Second chunk\n"
    with kanban_as(kanban, fk):
        _execute_planner_decompose_inner(
            SLUG, plan_tid, 51, "Epic: two chunks", body,
            [], "", False, prov, native_decompose=False,
        )
    # legacy path DID create GitHub sub-issues
    assert len(prov.created_issues) >= 1


def test_native_planner_decompose_failure_leaves_epic_retryable():
    """Flag ON + decompose fails → returns False, no marker posted (retryable)."""
    fk = FakeKanban()
    fk.decompose = lambda *a, **k: False  # force decompose failure
    prov = FakeProvider()
    plan_tid = fk.create_task(SLUG, "#52 Planner", assignee="planner-daedalus")
    with kanban_as(kanban, fk):
        ok = _execute_planner_decompose_inner(
            SLUG, plan_tid, 52, "Epic", "Body", [], "", False, prov,
            native_decompose=True,
        )
    assert ok is False
    # no decomposed marker → a later tick can retry
    assert prov.posted_issue_comments == []
    # planner card NOT completed
    assert not any(tid == plan_tid for (tid, _s) in fk.completed)

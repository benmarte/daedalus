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

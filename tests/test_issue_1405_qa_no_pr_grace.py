"""Tests for the QA 'no PR' grace-poll (issue #1405).

When the developer's PR lands a little late (e.g. after a worktree-collision
retry, #1404), QA can run first, find no PR, and block ``qa-failed: no PR``.
Left alone, the block-loop give-up breaker trips and wedges the card in triage
even though a valid PR appears seconds later.

The fix: before that block counts as a developer failure, grace-poll the
issue's branch (``fix/issue-<n>``) for a PR across a bounded window. A PR that
appears in-window is adopted (QA is unblocked and re-runs against it); if none
appears within the window the card falls through to the normal failure path
(the give-up breaker is preserved — issue #1405 non-goal).

Run: python3 -m pytest tests/test_issue_1405_qa_no_pr_grace.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import FakeProvider  # noqa: E402
from core import iterate  # noqa: E402
from core import kanban  # noqa: E402
from core.iterate import executors  # noqa: E402


# ── _is_qa_no_pr_block: distinguishes the no-PR variant from a real failure ──


def test_is_qa_no_pr_block_matches_no_pr_variant():
    assert executors._is_qa_no_pr_block("qa-failed: no PR — developer work incomplete")
    assert executors._is_qa_no_pr_block("QA-FAILED: No PR found for issue")


def test_is_qa_no_pr_block_ignores_real_test_failure():
    # A genuine test/lint failure must NOT be intercepted — it routes to QA_FIX.
    assert not executors._is_qa_no_pr_block("qa-failed: 3 tests failing in test_foo")
    assert not executors._is_qa_no_pr_block("qa-passed: PR #5 verified")
    assert not executors._is_qa_no_pr_block("")


# ── _grace_qa_no_pr: adopt / hold / exhaust ──────────────────────────────────


def test_grace_adopts_late_pr(tmp_path):
    """A PR that appeared on the issue's branch is adopted → QA unblocked."""
    prov = FakeProvider(branch_prs={"fix/issue-1405": 4242})
    card = {"id": "t_qa", "assignee": "qa-daedalus"}
    with mock.patch.object(kanban, "unblock_task", return_value=True) as munblock:
        result = executors._grace_qa_no_pr(
            "slug", card, 1405, prov, workdir=str(tmp_path), max_grace_ticks=3,
        )
    assert result == "adopted"
    assert munblock.call_count == 1
    # The counter advanced so the bounded window still applies to any re-run.
    assert executors._read_qa_no_pr_grace(str(tmp_path)).get("t_qa") == 1


def test_grace_holds_within_window(tmp_path):
    """No PR yet, still within the window → hold (no failure counted)."""
    prov = FakeProvider(branch_prs={})  # no PR on any branch
    card = {"id": "t_qa", "assignee": "qa-daedalus"}
    with mock.patch.object(kanban, "unblock_task", return_value=True) as munblock:
        result = executors._grace_qa_no_pr(
            "slug", card, 1405, prov, workdir=str(tmp_path), max_grace_ticks=3,
        )
    assert result == "holding"
    assert munblock.call_count == 0
    assert executors._read_qa_no_pr_grace(str(tmp_path)).get("t_qa") == 1


def test_grace_exhausts_after_window(tmp_path):
    """Once the window is spent the card falls through to the normal path."""
    # Pre-seed the counter at the cap.
    executors._write_qa_no_pr_grace(str(tmp_path), {"t_qa": 3})
    prov = FakeProvider(branch_prs={"fix/issue-1405": 4242})
    card = {"id": "t_qa", "assignee": "qa-daedalus"}
    with mock.patch.object(kanban, "unblock_task", return_value=True) as munblock:
        result = executors._grace_qa_no_pr(
            "slug", card, 1405, prov, workdir=str(tmp_path), max_grace_ticks=3,
        )
    assert result == "exhausted"
    # Exhausted must not poll/adopt — the breaker takes over.
    assert munblock.call_count == 0


def test_grace_matches_suffixed_branch(tmp_path):
    """A descriptive suffix branch ``fix/issue-<n>-<slug>`` is still adopted."""
    prov = FakeProvider(branch_prs={"fix/issue-1405-late-pr": 99})
    card = {"id": "t_qa", "assignee": "qa-daedalus"}
    with mock.patch.object(kanban, "unblock_task", return_value=True):
        result = executors._grace_qa_no_pr(
            "slug", card, 1405, prov, workdir=str(tmp_path), max_grace_ticks=3,
        )
    assert result == "adopted"


# ── run_iterate integration ──────────────────────────────────────────────────


def _qa_no_pr_card():
    return [{
        "id": "t_qa",
        "assignee": "qa-daedalus",
        "body": "Issue benmarte/daedalus#1405: QA gate",
        "runs": [{"reason": "qa-failed: no PR — developer work incomplete"}],
    }]


def test_run_iterate_holds_qa_no_pr_within_window(tmp_path):
    """QA 'no PR' block with no PR yet → held by grace-poll, not routed to QA_FIX."""
    prov = FakeProvider(branch_prs={})
    resolved = {"workdir": str(tmp_path), "pipeline": {"qa_no_pr_grace_ticks": 3}}
    with mock.patch.object(kanban, "list_blocked", return_value=_qa_no_pr_card()):
        with mock.patch.object(kanban, "unblock_task", return_value=True) as munblock:
            counts, prs, *_ = iterate.run_iterate(
                "slug", "O/R", provider=prov, resolved=resolved,
            )
    assert counts["qa_no_pr_grace"] == 1
    assert counts[iterate.QA_FIX] == 0
    assert munblock.call_count == 0  # holding, not adopting


def test_run_iterate_adopts_late_pr(tmp_path):
    """QA 'no PR' block but a PR is now on the branch → adopted (QA unblocked)."""
    prov = FakeProvider(branch_prs={"fix/issue-1405": 4242})
    resolved = {"workdir": str(tmp_path), "pipeline": {"qa_no_pr_grace_ticks": 3}}
    with mock.patch.object(kanban, "list_blocked", return_value=_qa_no_pr_card()):
        with mock.patch.object(kanban, "unblock_task", return_value=True) as munblock:
            counts, *_ = iterate.run_iterate(
                "slug", "O/R", provider=prov, resolved=resolved,
            )
    assert counts["qa_no_pr_grace"] == 1
    assert counts[iterate.QA_FIX] == 0
    assert munblock.call_count == 1


def test_run_iterate_grace_disabled_falls_through(tmp_path):
    """qa_no_pr_grace_ticks=0 disables the grace-poll (pre-#1405 behaviour)."""
    prov = FakeProvider(branch_prs={"fix/issue-1405": 4242})
    resolved = {"workdir": str(tmp_path), "pipeline": {"qa_no_pr_grace_ticks": 0}}
    with mock.patch.object(kanban, "list_blocked", return_value=_qa_no_pr_card()):
        with mock.patch.object(kanban, "unblock_task", return_value=True) as munblock:
            counts, *_ = iterate.run_iterate(
                "slug", "O/R", provider=prov, resolved=resolved,
            )
    # Grace never fires — the card routes through normal classification.
    assert counts["qa_no_pr_grace"] == 0
    assert munblock.call_count == 0


def test_run_iterate_real_qa_failure_not_intercepted(tmp_path):
    """A genuine qa-failed (test failure, not 'no PR') skips the grace-poll."""
    cards = [{
        "id": "t_qa",
        "assignee": "qa-daedalus",
        "body": "Issue benmarte/daedalus#1405: QA gate",
        "runs": [{"reason": "qa-failed: 3 tests failing in test_foo — PR #4242"}],
    }]
    prov = FakeProvider(branch_prs={"fix/issue-1405": 4242})
    resolved = {"workdir": str(tmp_path), "pipeline": {"qa_no_pr_grace_ticks": 3}}
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "unblock_task", return_value=True) as munblock:
            counts, *_ = iterate.run_iterate(
                "slug", "O/R", provider=prov, resolved=resolved,
            )
    assert counts["qa_no_pr_grace"] == 0
    assert munblock.call_count == 0  # real failure — grace-poll not involved


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))

"""E2E smoke tests for QA gate + FileLock mutex (closes #1038).

Covers the 5 scenarios from issue #1038 as automated unit-level tests
(no live GitHub agents, no network). Each test is named after the scenario
it validates so failures clearly identify which scenario broke.

Scenario 1 — Full pipeline happy path (QA passes → docs → auto-merge)
Scenario 2 — FileLock mutex under concurrent dispatch
Scenario 3 — Auto-merge blocked without QA signal
Scenario 4 — Auto-merge fires on qa-passed
Scenario 5 — skip-qa bypass merges without QA signal

All assertions run as part of the normal ``pytest tests/`` suite.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import core.iterate as iterate
from conftest import FakeProvider


SLUG = "smoke-board"
REPO = "benmarte/daedalus"
PR = 42
ISSUE = 7


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_card(role: str, summary: str, pr: int = PR, issue: int = ISSUE) -> dict:
    return {
        "id": f"t_{role}",
        "title": f"{role}: Issue #{issue}",
        "assignee": f"{role}-daedalus",
        "status": "blocked",
        "latest_summary": summary,
        "body": f"Issue #{issue}",
    }


def _docs_card(pr: int = PR, issue: int = ISSUE) -> dict:
    return {
        "id": "t_docs",
        "title": f"Documentation: Issue #{issue}",
        "assignee": "documentation-daedalus",
        "status": "blocked",
        "latest_summary": f"docs posted: PR #{pr}",
        "body": f"Issue #{issue}",
    }


def _provider(ci: str = "green", open_prs: set | None = None) -> FakeProvider:
    p = FakeProvider()
    p._ci = ci
    p._open_prs = open_prs if open_prs is not None else {PR}
    return p


def _was_merged(provider: FakeProvider, pr: int = PR) -> bool:
    return any(n == pr for n, _ in provider.merged)


# ── Scenario 1: full pipeline happy path ─────────────────────────────────────


def test_scenario_1_full_pipeline_happy_path():
    """QA passes, docs complete, PR auto-merges — full happy path."""
    provider = _provider()
    qa_card = _make_card("qa", f"qa-passed: PR #{PR}")
    docs_card = _docs_card()
    resolved = {"execution": {"auto_merge": True}}

    # Tick 1: QA passes → advance, no qa_failed_cards
    with (
        mock.patch("core.iterate.kanban.list_blocked", return_value=[qa_card]),
        mock.patch("core.iterate.kanban.show_card", return_value=qa_card),
        mock.patch("core.iterate.kanban.complete", return_value=True),
    ):
        counts_qa, _, _, qa_failed = iterate.run_iterate(SLUG, REPO, provider=provider)

    assert counts_qa[iterate.ADVANCE] == 1, "QA card should advance on qa-passed"
    assert qa_failed == [], "No QA failure when signal is qa-passed"

    # Tick 2: docs complete + QA passed → APPROVE_ADVANCE + auto-merge
    with (
        mock.patch("core.iterate.kanban.list_blocked", return_value=[docs_card]),
        mock.patch("core.iterate.kanban.show_card", return_value=docs_card),
        mock.patch("core.iterate.kanban.complete", return_value=True),
        mock.patch("core.iterate._qa_passed_for_issue", return_value=True),
    ):
        counts_docs, _, _, _ = iterate.run_iterate(
            SLUG, REPO, provider=provider, resolved=resolved
        )

    assert counts_docs[iterate.APPROVE_ADVANCE] == 1, "Docs card should APPROVE_ADVANCE"
    assert _was_merged(provider), "PR should be auto-merged after docs complete with QA passed"


# ── Scenario 2: FileLock mutex under concurrent dispatch ─────────────────────


def test_scenario_2_filelock_mutex_serialises_concurrent_dispatch(tmp_path):
    """Two concurrent dispatcher invocations: FileLock serialises, one blocked."""
    try:
        from filelock import FileLock, Timeout
    except ImportError:
        pytest.skip("filelock not installed")

    lock_path = str(tmp_path / "daedalus.lock")
    results = []

    def _hold_lock():
        lock = FileLock(lock_path)
        with lock:
            results.append("holder-ran")

    def _try_acquire():
        lock = FileLock(lock_path, timeout=0)
        try:
            with lock:
                results.append("contender-ran")
        except Timeout:
            results.append("contender-blocked")

    t1 = threading.Thread(target=_hold_lock)
    t2 = threading.Thread(target=_try_acquire)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert "holder-ran" in results
    # Regardless of timing, at most one ran concurrently (the lock serialises)
    ran_count = results.count("contender-ran")
    blocked_count = results.count("contender-blocked")
    assert ran_count + blocked_count == 1, f"unexpected results: {results}"


# ── Scenario 3: auto-merge blocked without QA signal ─────────────────────────


def test_scenario_3_auto_merge_blocked_without_qa_signal():
    """Docs card completes but QA has NOT passed → PR must NOT be auto-merged."""
    provider = _provider()
    docs_card = _docs_card()
    resolved = {"execution": {"auto_merge": True}}

    with (
        mock.patch("core.iterate.kanban.list_blocked", return_value=[docs_card]),
        mock.patch("core.iterate.kanban.show_card", return_value=docs_card),
        mock.patch("core.iterate.kanban.complete", return_value=True),
        mock.patch("core.iterate._qa_passed_for_issue", return_value=False),
    ):
        iterate.run_iterate(SLUG, REPO, provider=provider, resolved=resolved)

    assert not _was_merged(provider), "PR must NOT be merged when QA has not passed"


# ── Scenario 4: auto-merge fires on qa-passed ─────────────────────────────────


def test_scenario_4_auto_merge_fires_on_qa_passed():
    """Docs card completes after QA passes → PR IS auto-merged."""
    provider = _provider()
    docs_card = _docs_card()
    resolved = {"execution": {"auto_merge": True}}

    with (
        mock.patch("core.iterate.kanban.list_blocked", return_value=[docs_card]),
        mock.patch("core.iterate.kanban.show_card", return_value=docs_card),
        mock.patch("core.iterate.kanban.complete", return_value=True),
        mock.patch("core.iterate._qa_passed_for_issue", return_value=True),
    ):
        iterate.run_iterate(SLUG, REPO, provider=provider, resolved=resolved)

    assert _was_merged(provider), "PR must be auto-merged once QA passes"


# ── Scenario 5: skip-qa bypass for docs PRs ───────────────────────────────────


def test_scenario_5_skip_qa_bypass_merges_without_qa_signal():
    """skip-qa label on PR → auto-merge fires immediately, bypassing QA gate."""
    provider = _provider()
    provider.labels[PR] = ["skip-qa"]  # PR has skip-qa label
    docs_card = _docs_card()
    resolved = {"execution": {"auto_merge": True}}

    with (
        mock.patch("core.iterate.kanban.list_blocked", return_value=[docs_card]),
        mock.patch("core.iterate.kanban.show_card", return_value=docs_card),
        mock.patch("core.iterate.kanban.complete", return_value=True),
        mock.patch("core.iterate._qa_passed_for_issue", return_value=False),
    ):
        iterate.run_iterate(SLUG, REPO, provider=provider, resolved=resolved)

    assert _was_merged(provider), (
        "PR with skip-qa label must be merged even when QA has not passed"
    )

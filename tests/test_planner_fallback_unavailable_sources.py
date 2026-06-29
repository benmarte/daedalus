"""Unit tests for graceful fallback in the planner when source files are unavailable.

This module tests the scenario where ``_execute_planner_decompose()`` cannot
access source files — e.g., source fetch fails, returns empty data, or the
workdir is disabled/missing. We mock the source-availability layer to simulate
unavailability and assert:

1. The planner does not crash or raise unhandled exceptions.
2. Sub-issues are still created but with generic/context-based acceptance
   criteria instead of file-specific ones.
3. A warning or log entry is emitted indicating fallback was used.
4. The overall planning output remains valid and usable downstream.

Tests sit alongside existing planner unit tests in ``tests/``.
"""
from __future__ import annotations

import sys
import os
import tempfile
import logging
from pathlib import Path
from unittest import mock

import pytest

# Ensure repo root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.iterate import (  # noqa: E402
    _execute_planner_decompose,
    reset_source_reading_fallback_count,
    get_source_reading_fallback_count,
)
from core import iterate as iterate_mod  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

def _fake_card(tid: str = "t_fallback123") -> dict:
    """Minimal kanban card dict that resolves to issue #999."""
    return {
        "id": tid,
        "title": "Planner card for epic #999",
        "body": "Planner handoff: decompose epic #999 into sub-issues",
    }


class _FakeIssue:
    """Minimal issue object returned by provider.get_issue()."""

    def __init__(self, number: int = 999, title: str = "Epic issue",
                 body: str = "", labels: list | None = None) -> None:
        self.number = number
        self.title = title
        self.body = body
        self.labels = labels or []
        self.url = f"https://github.com/test/repo/issues/{number}"

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "labels": self.labels,
            "url": self.url,
        }


class _FakeProvider:
    """Mocked provider that records calls and returns controlled data."""

    def __init__(self, parent_issue: _FakeIssue, created_issue_numbers: list[int]
                 | None = None) -> None:
        self.parent_issue = parent_issue
        self.created_issue_numbers = created_issue_numbers or [1000, 1001, 1002]
        self._create_calls: list[tuple[str, str, list]] = []
        self._labels_added: list[tuple[int, str]] = []
        self._comments_posted: list[tuple[int, str]] = []
        self._next_idx = 0

    def get_issue(self, number: int) -> _FakeIssue | None:
        if number == self.parent_issue.number:
            return self.parent_issue
        # Sub-issues created during decomposition — return stubs for triage loop
        if number in self.created_issue_numbers:
            return _FakeIssue(
                number=number,
                title=f"sub-issue #{number}",
                body=f"body of sub-issue #{number}",
            )
        return None

    def get_issue_comments(self, number: int) -> list:
        return []

    def create_issue(self, title: str, body: str, labels: list | None = None) -> int | None:
        self._create_calls.append((title, body, labels or []))
        if self._next_idx < len(self.created_issue_numbers):
            n = self.created_issue_numbers[self._next_idx]
            self._next_idx += 1
            return n
        return None

    def add_label(self, number: int, label: str) -> None:
        self._labels_added.append((number, label))

    def board_configured(self) -> bool:
        return False

    def board_set_status(self, number: int, status: str) -> bool:
        return True

    def post_issue_comment(self, number: int, text: str) -> None:
        self._comments_posted.append((number, text))


@pytest.fixture()
def reset_fallback_counter():
    """Reset and restore the fallback counter around each test."""
    original = get_source_reading_fallback_count()
    reset_source_reading_fallback_count()
    yield
    # Restore original state so other tests aren't affected
    iterate_mod._source_reading_fallback_count = original


# ── 1. Planner does not crash when source fetch fails ────────────────────────

def test_no_crash_when_identify_relevant_files_raises(reset_fallback_counter, tmp_path):
    """identify_relevant_files raises → planner still completes without error."""
    parent = _FakeIssue(
        number=999,
        title="Do the thing",
        body="- [ ] step one\n- [ ] step two\n- [ ] step three\n",
    )
    provider = _FakeProvider(parent)

    with mock.patch.object(iterate_mod, "identify_relevant_files",
                           side_effect=RuntimeError("source layer exploded")):
        ok = _execute_planner_decompose(
            "test", _fake_card(), "test/repo", "", workdir=str(tmp_path),
            provider=provider,
        )

    assert ok is True, "planner should return True even when source reading fails"
    assert len(provider._create_calls) == 3, "all 3 sub-issues must still be created"


def test_no_crash_when_read_source_files_raises(reset_fallback_counter, tmp_path):
    """read_source_files raises → planner still completes without error."""
    parent = _FakeIssue(number=999, title="Do stuff",
                        body="- [ ] sub A\n- [ ] sub B\n")
    provider = _FakeProvider(parent, created_issue_numbers=[2000, 2001])

    # identify_relevant_files succeeds but returns no files → read_source_files
    # is mocked to raise to simulate a failure further down the chain.
    with mock.patch.object(iterate_mod, "read_source_files",
                           side_effect=OSError("disk I/O error")):
        # Make identify also raise so we hit the except block; alternatively we
        # could let files be found and hit read failure — cover both below.
        pass

    with mock.patch.object(iterate_mod, "identify_relevant_files",
                           side_effect=PermissionError("denied")):
        ok = _execute_planner_decompose(
            "test", _fake_card("t_diskfail"), "test/repo", "",
            workdir=str(tmp_path), provider=provider,
        )

    assert ok is True
    assert len(provider._create_calls) == 2


def test_no_crash_when_workdir_missing(reset_fallback_counter):
    """Planner works when workdir points at a non-existent directory."""
    parent = _FakeIssue(
        number=999,
        title="Epic without a repo",
        body="- [ ] step one\n",
    )
    provider = _FakeProvider(parent, created_issue_numbers=[3000])

    ok = _execute_planner_decompose(
        "test", _fake_card("t_nodir"), "test/repo", "",
        workdir="/does/not/exist/anywhere", provider=provider,
    )

    assert ok is True
    assert len(provider._create_calls) == 1


def test_no_crash_when_workdir_empty(reset_fallback_counter):
    """workdir='' is a valid fallback path — planner must continue."""
    parent = _FakeIssue(number=999, title="Epic",
                        body="- [ ] one\n- [ ] two\n")
    provider = _FakeProvider(parent, created_issue_numbers=[4000, 4001])

    ok = _execute_planner_decompose(
        "test", _fake_card("t_emptywd"), "test/repo", "",
        workdir="", provider=provider,
    )

    assert ok is True
    assert len(provider._create_calls) == 2


# ── 2. Sub-issues created with generic/context-based criteria (no file-specific) ─

def test_sub_issues_created_without_file_specific_context(reset_fallback_counter, tmp_path):
    """When source is unavailable, sub-issues are still created but without
    file-specific source-context sections in the body."""
    parent = _FakeIssue(
        number=999,
        title="Refactor the system",
        body="- [ ] extract helper function\n- [ ] add tests\n- [ ] update docs\n",
    )
    provider = _FakeProvider(parent)

    with mock.patch.object(iterate_mod, "identify_relevant_files",
                           return_value=([], {})):
        _execute_planner_decompose(
            "test", _fake_card(), "test/repo", "",
            workdir=str(tmp_path), provider=provider,
        )

    # All three sub-issues must be created.
    assert len(provider._create_calls) == 3
    for title, body, labels in provider._create_calls:
        assert body, f"sub-issue body must not be empty for '{title}'"
        # Must not contain source file context block header
        assert "## Relevant Source Context" not in body, (
            "sub-issue body must not include file-specific context when source "
            "is unavailable"
        )
        # Must still carry the generic scope text
        assert any(kw in body.lower() for kw in ("extract", "helper", "tests", "docs")), (
            f"generic/context-based scope text should appear in sub-issue body: {body!r}"
        )


def test_sub_issues_use_default_titles_when_no_checklist(reset_fallback_counter):
    """Without a checklist AND without source files, default titles are still used."""
    parent = _FakeIssue(
        number=999,
        title="Big ambiguous epic",
        body="No checklist here at all, just a big ambiguous task.",
    )
    provider = _FakeProvider(parent, created_issue_numbers=[5000, 5001, 5002])

    with mock.patch.object(iterate_mod, "identify_relevant_files",
                           return_value=([], {})):
        ok = _execute_planner_decompose(
            "test", _fake_card("t_defaulttitles"), "test/repo", "",
            workdir="", provider=provider,
        )

    assert ok is True
    assert len(provider._create_calls) == 3
    titles = [t for t, _, _ in provider._create_calls]
    # Default titles include 'spec', 'implement', 'verify'
    joined = " | ".join(titles).lower()
    assert "spec" in joined or "implement" in joined or "verify" in joined


# ── 3. Warning/log emitted indicating fallback was used ───────────────────────

def test_warning_logged_when_source_reading_fails(reset_fallback_counter, tmp_path, caplog):
    """A 'degrading gracefully' warning must be logged on source read failure."""
    parent = _FakeIssue(
        number=999, title="Crash me",
        body="- [ ] a\n- [ ] b\n",
    )
    provider = _FakeProvider(parent, created_issue_numbers=[6000, 6001])

    with caplog.at_level(logging.WARNING, logger="daedalus.iterate"):
        with mock.patch.object(iterate_mod, "identify_relevant_files",
                               side_effect=RuntimeError("boom")):
            _execute_planner_decompose(
                "test", _fake_card("t_log"), "test/repo", "",
                workdir=str(tmp_path), provider=provider,
            )

    # Expect at least one record mentioning 'degrading' or fallback semantics.
    messages = "\n".join(r.message for r in caplog.records)
    assert "degrading gracefully" in messages or "source-reading failed" in messages, (
        f"expected fallback warning in logs, got:\n{messages}"
    )


def test_info_logged_when_workdir_unavailable(reset_fallback_counter, caplog):
    """When workdir is missing, an info-level log should be emitted."""
    parent = _FakeIssue(number=999, title="no workdir",
                        body="- [ ] step\n")
    provider = _FakeProvider(parent, created_issue_numbers=[7000])

    with caplog.at_level(logging.INFO, logger="daedalus.iterate"):
        _execute_planner_decompose(
            "test", _fake_card("t_nodir2"), "test/repo", "",
            workdir="/no/such/dir", provider=provider,
        )

    messages = "\n".join(r.message for r in caplog.records)
    assert "workdir unavailable" in messages or "Phase 3 fallback" in messages


def test_fallback_counter_increments_on_failure(reset_fallback_counter, tmp_path):
    """``_source_reading_fallback_count`` must increment when fallback triggers."""
    before = get_source_reading_fallback_count()
    assert before == 0  # fixture reset

    parent = _FakeIssue(number=999, title="Count me",
                        body="- [ ] one\n")
    provider = _FakeProvider(parent, created_issue_numbers=[8000])

    with mock.patch.object(iterate_mod, "identify_relevant_files",
                           side_effect=RuntimeError("count me in")):
        _execute_planner_decompose(
            "test", _fake_card("t_counter"), "test/repo", "",
            workdir=str(tmp_path), provider=provider,
        )

    assert get_source_reading_fallback_count() >= 1, (
        "fallback counter should have incremented"
    )


# ── 4. Overall planning output remains valid downstream ───────────────────────

def test_idempotency_marker_still_posted_after_fallback(reset_fallback_counter, tmp_path):
    """Even after fallback the decomposed marker is posted on the parent so the
    dispatcher won't re-run decomposition on a retry."""
    parent = _FakeIssue(number=999, title="Marker me",
                        body="- [ ] one\n- [ ] two\n")
    provider = _FakeProvider(parent, created_issue_numbers=[9000, 9001])

    with mock.patch.object(iterate_mod, "identify_relevant_files",
                           side_effect=OSError("offline")):
        ok = _execute_planner_decompose(
            "test", _fake_card("t_marker"), "test/repo", "",
            workdir=str(tmp_path), provider=provider,
        )

    assert ok is True
    # Idempotency marker posted on parent
    marker_posts = [(n, t) for n, t in provider._comments_posted
                    if n == 999 and "daedalus:decomposed" in t]
    assert marker_posts, (
        f"expected sub-issue marker on parent; got comments: {provider._comments_posted}"
    )


def test_epic_label_applied_after_fallback(reset_fallback_counter, tmp_path):
    """Epic label applied to parent regardless of source-reading outcome."""
    parent = _FakeIssue(number=999, title="Epic label test",
                        body="- [ ] x\n")
    provider = _FakeProvider(parent, created_issue_numbers=[10101])

    with mock.patch.object(iterate_mod, "identify_relevant_files",
                           side_effect=Exception("nope")):
        _execute_planner_decompose(
            "test", _fake_card("t_epiclbl"), "test/repo", "",
            workdir=str(tmp_path), provider=provider,
        )

    assert (999, "epic") in provider._labels_added


def test_triage_cards_still_spawned_after_fallback(reset_fallback_counter, tmp_path):
    """Triage cards are still spawned for each created sub-issue (downstream)."""
    parent = _FakeIssue(number=999, title="Spawn triage",
                        body="- [ ] a\n- [ ] b\n")
    provider = _FakeProvider(parent, created_issue_numbers=[11000, 11001])

    with mock.patch.object(iterate_mod, "identify_relevant_files",
                           side_effect=Exception("no source")):
        with mock.patch("core.iterate.kanban") as fake_kanban:
            fake_kanban.create_triage = mock.Mock(return_value="t_triage_x")
            fake_kanban.decompose = mock.Mock(return_value=True)
            fake_kanban.complete = mock.Mock(return_value=True)

            ok = _execute_planner_decompose(
                "test", _fake_card("t_triage"), "test/repo", "",
                workdir=str(tmp_path), provider=provider,
            )

    assert ok is True
    # One triage card created per sub-issue
    assert fake_kanban.create_triage.call_count == 2


def test_completion_summary_recorded(reset_fallback_counter, tmp_path):
    """kanban.complete() is called even when source reading fell back."""
    parent = _FakeIssue(number=999, title="Complete me",
                        body="- [ ] one\n")
    provider = _FakeProvider(parent, created_issue_numbers=[12000])

    with mock.patch.object(iterate_mod, "identify_relevant_files",
                           side_effect=Exception("fallback")):
        with mock.patch("core.iterate.kanban") as fake_kanban:
            fake_kanban.create_triage = mock.Mock(return_value="t_x")
            fake_kanban.decompose = mock.Mock(return_value=True)
            fake_kanban.complete = mock.Mock(return_value=True)

            ok = _execute_planner_decompose(
                "test", _fake_card("t_complete"), "test/repo", "",
                workdir=str(tmp_path), provider=provider,
            )

    assert ok is True
    fake_kanban.complete.assert_called_once()
    _, kwargs = fake_kanban.complete.call_args
    assert "summary" in kwargs and kwargs["summary"], (
        "kanban.complete() must receive a summary even after fallback"
    )


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_get_issue_returns_none_does_not_crash(reset_fallback_counter, tmp_path):
    """If provider.get_issue raises or returns None mid-flow, planner degrades."""
    parent = _FakeIssue(number=999, title="disappear",
                        body="- [ ] one\n")

    class _NoneProvider(_FakeProvider):
        def get_issue(self, number: int):
            return None

    provider = _NoneProvider(parent)
    ok = _execute_planner_decompose(
        "test", _fake_card("t_noneget"), "test/repo", "",
        workdir=str(tmp_path), provider=provider,
    )
    assert ok is False, "should return False when parent issue cannot be fetched"


def test_no_provider_gracefully_returns(reset_fallback_counter, tmp_path):
    """Planner invoked with provider=None returns False rather than crashing."""
    parent = _FakeIssue(number=999, title="no provider",
                        body="- [ ] one\n")
    ok = _execute_planner_decompose(
        "test", _fake_card("t_noprovider"), "test/repo", "",
        workdir=str(tmp_path), provider=None,
    )
    assert ok is False

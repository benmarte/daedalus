"""Tests for Phase 3 fallback behavior in codebase reading.

Verifies that when codebase reading fails or workdir is unavailable,
the system gracefully falls back to Phase 3 (template-only) behavior
and increments observability counters.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import iterate  # noqa: E402
from core.iterate import (  # noqa: E402
    _execute_planner_decompose,
    get_source_reading_fallback_count,
    reset_source_reading_fallback_count,
)


def _make_card(issue_n: int = 1) -> dict:
    """Create a test planner card referencing an epic issue."""
    return {
        "id": f"t_test_{issue_n}",
        "title": f"#{issue_n} Test epic",
        "body": f"Epic #{issue_n}\n\nPhase 3 epic for testing fallback.",
        "assignee": "planner-daedalus",
    }


def _make_issue(number: int = 1) -> mock.Mock:
    """Create a mock GitHub issue object."""
    issue = mock.Mock()
    issue.as_dict = mock.Mock(return_value={
        "number": number,
        "title": f"Epic #{number}",
        "body": f"Epic #{number}\n\nTemplate epic body for testing.",
        "labels": [{"name": "epic"}],
        "url": f"https://github.com/test/repo/issues/{number}",
    })
    return issue


def _setup_provider(issue_number: int, existing_comments: list | None = None):
    """Create a mock provider with standard methods."""
    provider = mock.Mock()
    provider.get_issue = mock.Mock(return_value=_make_issue(issue_number))
    provider.get_issue_comments = mock.Mock(return_value=existing_comments or [])
    created_issues = []

    def track_create_issue(title, body, labels=None):
        n = 1000 + len(created_issues) + 1
        created_issues.append(n)
        return n

    provider.create_issue = mock.Mock(side_effect=track_create_issue)
    provider.post_issue_comment = mock.Mock()
    provider.add_label = mock.Mock()
    return provider


# ---------------------------------------------------------------------------
# Counter behavior
# ---------------------------------------------------------------------------

def test_counter_starts_at_zero():
    """Counter should initialize to zero."""
    reset_source_reading_fallback_count()
    assert get_source_reading_fallback_count() == 0


def test_counter_can_be_reset():
    """Counter should be resettable for test isolation."""
    # Manually increment to simulate a fallback
    iterate._source_reading_fallback_count = 5
    assert get_source_reading_fallback_count() == 5

    reset_source_reading_fallback_count()
    assert get_source_reading_fallback_count() == 0


# ---------------------------------------------------------------------------
# Fallback: workdir unavailable
# ---------------------------------------------------------------------------

def test_workdir_empty_string_triggers_fallback():
    """When workdir is empty string, fallback to Phase 3 template-only."""
    reset_source_reading_fallback_count()
    card = _make_card(101)
    provider = _setup_provider(101)

    with mock.patch("core.iterate.kanban") as kanban_mock:
        result = _execute_planner_decompose(
            "test-slug", card, "test/repo", "",
            workdir="",  # empty workdir
            dry_run=False,
            provider=provider,
        )

    # Should succeed (Phase 3 fallback is graceful)
    assert result is True
    # Counter should increment
    assert get_source_reading_fallback_count() == 1
    # Sub-issues should still be created (default 3 for template-only)
    assert provider.create_issue.call_count == 3


def test_workdir_nonexistent_path_triggers_fallback():
    """When workdir path doesn't exist, fallback to Phase 3 template-only."""
    reset_source_reading_fallback_count()
    card = _make_card(102)
    provider = _setup_provider(102)

    with mock.patch("core.iterate.kanban"):
        result = _execute_planner_decompose(
            "test-slug", card, "test/repo", "",
            workdir="/nonexistent/path/to/workdir",
            dry_run=False,
            provider=provider,
        )

    assert result is True
    assert get_source_reading_fallback_count() == 1
    assert provider.create_issue.call_count == 3


def test_workdir_none_triggers_fallback():
    """When workdir is None/omitted, default empty string triggers fallback."""
    reset_source_reading_fallback_count()
    card = _make_card(103)
    provider = _setup_provider(103)

    with mock.patch("core.iterate.kanban"):
        result = _execute_planner_decompose(
            "test-slug", card, "test/repo", "",
            # workdir not provided, defaults to ""
            dry_run=False,
            provider=provider,
        )

    assert result is True
    assert get_source_reading_fallback_count() == 1


def test_fallback_creates_default_subissues():
    """Phase 3 fallback should create 3 default sub-issues (no checklist)."""
    reset_source_reading_fallback_count()
    card = _make_card(104)
    provider = _setup_provider(104)

    with mock.patch("core.iterate.kanban"):
        _execute_planner_decompose(
            "test-slug", card, "test/repo", "",
            workdir="",
            dry_run=False,
            provider=provider,
        )

    # Verify 3 sub-issues created
    assert provider.create_issue.call_count == 3

    # Verify they have the standard template titles
    calls = provider.create_issue.call_args_list
    titles = [call[1]["title"] if "title" in call[1] else call[0][0]
              for call in calls]
    assert any("Research" in title for title in titles)
    assert any("Implementation" in title for title in titles)
    assert any("Testing" in title for title in titles)


def test_fallback_still_marks_decomposed():
    """Even in fallback mode, idempotency marker should be posted."""
    reset_source_reading_fallback_count()
    card = _make_card(105)
    provider = _setup_provider(105)

    with mock.patch("core.iterate.kanban"):
        _execute_planner_decompose(
            "test-slug", card, "test/repo", "",
            workdir="",
            dry_run=False,
            provider=provider,
        )

    # Marker comment should be posted
    assert provider.post_issue_comment.called


def test_fallback_still_applies_epic_label():
    """Phase 3 fallback should still apply epic label to parent."""
    reset_source_reading_fallback_count()
    card = _make_card(106)
    provider = _setup_provider(106)

    with mock.patch("core.iterate.kanban"):
        _execute_planner_decompose(
            "test-slug", card, "test/repo", "",
            workdir="",
            dry_run=False,
            provider=provider,
        )

    # Epic label should be applied
    assert provider.add_label.called


# ---------------------------------------------------------------------------
# Fallback: codebase reading throws exception
# ---------------------------------------------------------------------------

def test_exception_in_codebase_reading_triggers_fallback():
    """When codebase reading throws, fallback to Phase 3 template-only."""
    reset_source_reading_fallback_count()
    card = _make_card(107)
    provider = _setup_provider(107)

    # Mock load_known_components to raise an exception
    with mock.patch("core.iterate.load_known_components") as mock_load:
        mock_load.side_effect = Exception("Simulated codebase reading failure")

        with mock.patch("core.iterate.kanban"):
            result = _execute_planner_decompose(
                "test-slug", card, "test/repo", "",
                workdir="/tmp",  # valid path
                dry_run=False,
                provider=provider,
            )

    # Should succeed despite exception
    assert result is True
    # Counter should increment
    assert get_source_reading_fallback_count() == 1
    # Sub-issues should still be created
    assert provider.create_issue.call_count == 3


def test_exception_in_identify_relevant_files_triggers_fallback():
    """When identify_relevant_files throws, fallback to Phase 3."""
    reset_source_reading_fallback_count()
    card = _make_card(108)
    provider = _setup_provider(108)

    with mock.patch("core.iterate.identify_relevant_files") as mock_identify:
        mock_identify.side_effect = RuntimeError("File identification failed")

        with mock.patch("core.iterate.kanban"):
            result = _execute_planner_decompose(
                "test-slug", card, "test/repo", "",
                workdir="/tmp",
                dry_run=False,
                provider=provider,
            )

    assert result is True
    assert get_source_reading_fallback_count() == 1
    assert provider.create_issue.call_count == 3


def test_exception_in_read_source_files_triggers_fallback():
    """When read_source_files throws, fallback to Phase 3."""
    reset_source_reading_fallback_count()
    card = _make_card(109)
    provider = _setup_provider(109)

    # read_source_files is only called if identify_relevant_files returns files
    with mock.patch("core.iterate.identify_relevant_files") as mock_identify:
        with mock.patch("core.iterate.read_source_files") as mock_read:
            mock_identify.return_value = (["src/main.py"], {})
            mock_read.side_effect = IOError("Cannot read files")

            with mock.patch("core.iterate.kanban"):
                result = _execute_planner_decompose(
                    "test-slug", card, "test/repo", "",
                    workdir="/tmp",
                    dry_run=False,
                    provider=provider,
                )

    assert result is True
    assert get_source_reading_fallback_count() == 1
    assert provider.create_issue.call_count == 3


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_dry_run_still_increments_fallback_counter():
    """In dry_run mode, fallback is triggered but no issues are created."""
    reset_source_reading_fallback_count()
    card = _make_card(110)
    provider = _setup_provider(110)

    result = _execute_planner_decompose(
        "test-slug", card, "test/repo", "",
        workdir="",  # triggers fallback
        dry_run=True,
        provider=provider,
    )

    # dry_run returns early without creating issues
    assert result is True
    # But counter still increments (fallback logic was triggered)
    assert get_source_reading_fallback_count() == 1
    # No issues actually created
    assert provider.create_issue.call_count == 0


def test_fallback_preserves_checklist_items():
    """Fallback should still use checklist items from parent body if present."""
    reset_source_reading_fallback_count()
    card = _make_card(111)

    # Create issue with checklist
    provider = mock.Mock()
    issue_obj = mock.Mock()
    issue_obj.as_dict = mock.Mock(return_value={
        "number": 111,
        "title": "Epic #111",
        "body": "Epic #111\n\n- [ ] Task Alpha\n- [ ] Task Beta\n- [ ] Task Gamma",
        "labels": [{"name": "epic"}],
        "url": "https://github.com/test/repo/issues/111",
    })
    provider.get_issue = mock.Mock(return_value=issue_obj)
    provider.get_issue_comments = mock.Mock(return_value=[])
    created_issues = []
    def track_create(title, body, labels=None):
        n = 2000 + len(created_issues) + 1
        created_issues.append(n)
        return n
    provider.create_issue = mock.Mock(side_effect=track_create)
    provider.post_issue_comment = mock.Mock()
    provider.add_label = mock.Mock()

    with mock.patch("core.iterate.kanban"):
        _execute_planner_decompose(
            "test-slug", card, "test/repo", "",
            workdir="",
            dry_run=False,
            provider=provider,
        )

    # Should create 3 sub-issues from checklist, not default 3
    assert provider.create_issue.call_count == 3

    # Verify titles match checklist items
    calls = provider.create_issue.call_args_list
    titles = [call[0][0] for call in calls]
    assert titles == ["Task Alpha", "Task Beta", "Task Gamma"]

    assert get_source_reading_fallback_count() == 1


def test_multiple_fallbacks_increment_counter():
    """Multiple fallback events should each increment the counter."""
    reset_source_reading_fallback_count()
    provider = _setup_provider(112)

    for i in range(112, 115):
        card = _make_card(i)
        provider = _setup_provider(i)
        with mock.patch("core.iterate.kanban"):
            _execute_planner_decompose(
                "test-slug", card, "test/repo", "",
                workdir="",
                dry_run=False,
                provider=provider,
            )

    # Counter should be 3 after 3 fallback events
    assert get_source_reading_fallback_count() == 3

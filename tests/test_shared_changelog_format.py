"""Tests for the shared changelog entry-format helper."""
from __future__ import annotations

import pytest


def test_basic_format():
    from scripts.lib.changelog_format import format_changelog_entry
    entry = format_changelog_entry(issue_number=123, issue_title="Fix bug", pr_number=42)
    assert "## [Fix bug]" in entry
    assert "issues/123" in entry
    assert "PR #42" in entry
    assert "pull/42" in entry
    assert entry.endswith("\n")


def test_format_with_explicit_urls():
    from scripts.lib.changelog_format import format_changelog_entry
    entry = format_changelog_entry(
        issue_number=1,
        issue_title="Title",
        pr_number=2,
        issue_url="https://example.com/issue/1",
        pr_url="https://example.com/pr/2",
    )
    assert "https://example.com/issue/1" in entry
    assert "https://example.com/pr/2" in entry
    assert "PR #2" in entry


def test_format_defaults_issue_url_from_repo_url():
    from scripts.lib.changelog_format import format_changelog_entry
    entry = format_changelog_entry(
        issue_number=7,
        issue_title="Title",
        pr_number=8,
        repo_url="https://github.com/myorg/myrepo",
    )
    assert "https://github.com/myorg/myrepo/issues/7" in entry
    assert "https://github.com/myorg/myrepo/pull/8" in entry


def test_format_explicit_url_takes_precedence():
    from scripts.lib.changelog_format import format_changelog_entry
    entry = format_changelog_entry(
        issue_number=7,
        issue_title="Title",
        pr_number=8,
        issue_url="https://override/issue",
        repo_url="https://github.com/myorg/myrepo",
    )
    assert "https://override/issue" in entry
    # pr_url still derived from repo_url
    assert "https://github.com/myorg/myrepo/pull/8" in entry


def test_format_handles_special_chars_in_title():
    from scripts.lib.changelog_format import format_changelog_entry
    entry = format_changelog_entry(
        issue_number=10,
        issue_title="Fix [brackets] & symbols < > `",
        pr_number=20,
    )
    assert "Fix [brackets] & symbols < > `" in entry


def test_format_handles_empty_title():
    from scripts.lib.changelog_format import format_changelog_entry
    entry = format_changelog_entry(issue_number=10, issue_title="", pr_number=20)
    # Still syntactically valid markdown
    assert "## []" in entry
    assert "PR #20" in entry


def test_format_stable_for_dispatcher_caller():
    """Simulates the dispatcher: passes provider.issue_url() and provider.pr_url() explicitly."""
    from scripts.lib.changelog_format import format_changelog_entry
    entry = format_changelog_entry(
        issue_number=99,
        issue_title="My issue title",
        pr_number=100,
        issue_url="https://github.com/benmarte/daedalus/issues/99",
        pr_url="https://github.com/benmarte/daedalus/pull/100",
    )
    expected = (
        "## [My issue title](https://github.com/benmarte/daedalus/issues/99) — "
        "[PR #100](https://github.com/benmarte/daedalus/pull/100)\n"
    )
    assert entry == expected


def test_format_without_issue_number_falls_back_to_pr_url():
    """CI updater has no issue_number — entry_url should default to pr_url."""
    from scripts.lib.changelog_format import format_changelog_entry
    entry = format_changelog_entry(
        issue_title="PR title",
        pr_number=42,
        pr_url="https://github.com/o/r/pull/42",
    )
    assert "https://github.com/o/r/pull/42" in entry
    # Both the title link and the PR link point to the PR URL
    assert entry == (
        "## [PR title](https://github.com/o/r/pull/42) — "
        "[PR #42](https://github.com/o/r/pull/42)\n"
    )


def test_format_without_any_optional_urls():
    """No issue_number, no issue_url — just title + pr_number."""
    from scripts.lib.changelog_format import format_changelog_entry
    entry = format_changelog_entry(
        issue_title="Something",
        pr_number=7,
    )
    # issue_url defaults to derived pr_url; pr_url derives from repo_url
    assert "issues/" not in entry  # no issue_number, so no /issues/
    assert "pull/7" in entry
    assert "PR #7" in entry


def test_format_issue_number_still_derives_issue_url():
    """Passing issue_number (but no issue_url) still derives the /issues/N URL."""
    from scripts.lib.changelog_format import format_changelog_entry
    entry = format_changelog_entry(
        issue_title="Title",
        pr_number=5,
        issue_number=99,
    )
    assert "/issues/99" in entry

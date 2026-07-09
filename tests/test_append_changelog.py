"""Unit tests for scripts/append_changelog.py (issue #1386 sub-task).

Covers:
1. Normal append — entry added to CHANGELOG.md
2. Idempotency — running twice with same input produces same output
3. Empty/missing changelog file — creates file on first run
4. Malformed entries — handles edge cases gracefully
5. Shared helper reuse — uses format_changelog_entry from scripts.lib
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_changelog(tmp_path: Path) -> Path:
    """Return path to a temporary changelog file."""
    return tmp_path / "CHANGELOG.md"


def test_normal_append(tmp_changelog: Path):
    """Entry is added to changelog file."""
    from scripts.append_changelog import append_changelog

    wrote = append_changelog(
        tmp_changelog,
        title="Fix bug in widget",
        pr_number=42,
        pr_url="https://github.com/org/repo/pull/42",
        issue_number=123,
        issue_url="https://github.com/org/repo/issues/123",
    )
    assert wrote is True
    content = tmp_changelog.read_text()
    assert "## [Fix bug in widget]" in content
    assert "PR #42" in content
    assert "issues/123" in content
    assert "pull/42" in content


def test_idempotency_same_pr_twice(tmp_changelog: Path):
    """Running twice with same PR number produces same output (no duplicate)."""
    from scripts.append_changelog import append_changelog

    # First append
    wrote1 = append_changelog(
        tmp_changelog,
        title="Fix bug in widget",
        pr_number=42,
        pr_url="https://github.com/org/repo/pull/42",
    )
    assert wrote1 is True
    content_after_first = tmp_changelog.read_text()

    # Second append with same PR number
    wrote2 = append_changelog(
        tmp_changelog,
        title="Different title",  # Title doesn't matter for idempotency
        pr_number=42,
        pr_url="https://github.com/org/repo/pull/42",
    )
    assert wrote2 is False
    content_after_second = tmp_changelog.read_text()

    # File unchanged after second append
    assert content_after_first == content_after_second
    # Only one occurrence of PR #42
    assert content_after_second.count("PR #42") == 1


def test_empty_changelog_creates_file(tmp_path: Path):
    """Append to non-existent file creates it."""
    from scripts.append_changelog import append_changelog

    cl = tmp_path / "NONEXISTENT_CHANGELOG.md"
    assert not cl.exists()

    wrote = append_changelog(
        cl,
        title="Initial entry",
        pr_number=1,
        pr_url="https://github.com/org/repo/pull/1",
    )
    assert wrote is True
    assert cl.exists()
    content = cl.read_text()
    assert "## [Initial entry]" in content
    assert "PR #1" in content


def test_empty_changelog_file(tmp_changelog: Path):
    """Append to empty file works correctly."""
    from scripts.append_changelog import append_changelog

    tmp_changelog.write_text("")
    wrote = append_changelog(
        tmp_changelog,
        title="New entry",
        pr_number=99,
        pr_url="https://github.com/org/repo/pull/99",
    )
    assert wrote is True
    content = tmp_changelog.read_text()
    assert "## [New entry]" in content
    assert "PR #99" in content


def test_malformed_entry_no_pr_number(tmp_changelog: Path):
    """Entry without PR number is handled gracefully."""
    from scripts.append_changelog import append_changelog

    # Pre-populate with malformed entry (no PR number)
    tmp_changelog.write_text("## [Bad entry] — no PR reference here\n\n")

    # Append valid entry — should succeed
    wrote = append_changelog(
        tmp_changelog,
        title="Good entry",
        pr_number=42,
        pr_url="https://github.com/org/repo/pull/42",
    )
    assert wrote is True
    content = tmp_changelog.read_text()
    assert "## [Good entry]" in content
    assert "PR #42" in content
    # Malformed entry still present
    assert "Bad entry" in content


def test_word_boundary_pr_numbers(tmp_changelog: Path):
    """PR #12 does not match PR #123 (word boundary check)."""
    from scripts.append_changelog import append_changelog, entry_present

    # Pre-populate with PR #123
    tmp_changelog.write_text("## [Entry 123] — [PR #123](url)\n\n")
    content = tmp_changelog.read_text()
    
    # Verify entry_present distinguishes correctly
    assert entry_present(content, 123) is True
    assert entry_present(content, 12) is False

    # Append PR #12 — should succeed (not blocked by PR #123)
    wrote = append_changelog(
        tmp_changelog,
        title="Entry 12",
        pr_number=12,
        pr_url="https://github.com/org/repo/pull/12",
    )
    assert wrote is True
    content = tmp_changelog.read_text()
    
    # Both entries present (use entry_present to check with word boundaries)
    assert entry_present(content, 123) is True
    assert entry_present(content, 12) is True


def test_uses_shared_format_helper(tmp_changelog: Path):
    """Script uses format_changelog_entry from scripts.lib.changelog_format."""
    from scripts.append_changelog import append_changelog
    from scripts.lib.changelog_format import format_changelog_entry

    # Call append_changelog
    append_changelog(
        tmp_changelog,
        title="Test entry",
        pr_number=42,
        pr_url="https://github.com/benmarte/daedalus/pull/42",
        issue_number=10,
        issue_url="https://github.com/benmarte/daedalus/issues/10",
    )
    content = tmp_changelog.read_text()

    # Generate expected entry using shared helper
    expected_entry = format_changelog_entry(
        issue_title="Test entry",
        pr_number=42,
        issue_number=10,
        issue_url="https://github.com/benmarte/daedalus/issues/10",
        pr_url="https://github.com/benmarte/daedalus/pull/42",
    )
    # Entry in file should match (minus trailing newline after append)
    assert expected_entry.rstrip("\n") in content


def test_append_preserves_existing_content(tmp_changelog: Path):
    """Append preserves existing changelog content (newest-first)."""
    from scripts.append_changelog import append_changelog

    # Pre-populate with old entry
    old_entry = "## [Old entry] — [PR #1](url)\n\n"
    tmp_changelog.write_text(old_entry)

    # Append new entry
    append_changelog(
        tmp_changelog,
        title="New entry",
        pr_number=99,
        pr_url="https://github.com/org/repo/pull/99",
    )
    content = tmp_changelog.read_text()

    # Both entries present, new one first
    assert content.index("PR #99") < content.index("PR #1")
    assert "Old entry" in content
    assert "New entry" in content


def test_entry_present_idempotency_check(tmp_changelog: Path):
    """entry_present correctly detects PR numbers."""
    from scripts.append_changelog import entry_present

    content = "## [Entry] — [PR #42](url)\n\n## [Another] — [PR #99](url)\n\n"
    assert entry_present(content, 42) is True
    assert entry_present(content, 99) is True
    assert entry_present(content, 12) is False
    assert entry_present(content, 420) is False  # word boundary


def test_prepend_entry_format(tmp_changelog: Path):
    """prepend_entry prepends entry with blank line separator."""
    from scripts.append_changelog import prepend_entry

    result = prepend_entry("existing content\n", "new entry\n")
    assert result == "new entry\n\nexisting content\n"

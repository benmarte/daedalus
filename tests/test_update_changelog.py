"""Tests for the idempotent update_changelog module."""
from __future__ import annotations

import base64
import json
import sys
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest


def test_parse_issue_number_from_body():
    from scripts.update_changelog import parse_issue_number
    assert parse_issue_number("Closes #123", "Some title", 456) == 123
    assert parse_issue_number("Fixes #789", "PR title", 999) == 789
    assert parse_issue_number("Resolves: #42", "Title", 100) == 42


def test_parse_issue_number_from_title_when_body_missing():
    from scripts.update_changelog import parse_issue_number
    assert parse_issue_number("", "My PR (#555)", 100) == 555
    assert parse_issue_number(None, "Title (#123)", 999) == 123


def test_parse_issue_number_fallback_to_pr_number():
    from scripts.update_changelog import parse_issue_number
    assert parse_issue_number("no issue refs", "just a title", 777) == 777


def test_format_entry_includes_pr_and_issue_links():
    from scripts.update_changelog import format_entry
    entry = format_entry(42, "Fix bug in widget", 123)
    assert "PR #42" in entry
    assert "issues/123" in entry
    assert "Fix bug in widget" in entry


def test_entry_already_exists_detects_pr_ref():
    from scripts.update_changelog import entry_already_exists
    content = "## [Some PR — PR #42](https://example.com)\n"
    assert entry_already_exists(content, 42) is True
    assert entry_already_exists(content, 99) is False
    assert entry_already_exists("", 42) is False


def test_prepend_entry_to_empty_or_new_changelog():
    from scripts.update_changelog import prepend_entry
    result = prepend_entry("", "## New Entry\n\n")
    assert result.startswith("# Changelog")
    assert "## New Entry" in result


def test_prepend_entry_after_header():
    from scripts.update_changelog import prepend_entry
    content = "# Changelog\n\n## Old Entry\n\n"
    result = prepend_entry(content, "## New Entry\n\n")
    lines = result.split("\n")
    assert lines[0] == "# Changelog"
    assert "## New Entry" in result
    assert result.index("New Entry") < result.index("Old Entry")


class TestUpdateChangelog:
    def test_creates_file_when_missing(self, tmp_path):
        from scripts.update_changelog import update_changelog
        changelog = tmp_path / "CHANGELOG.md"
        changed, content = update_changelog(changelog, 42, "New feature", "")
        assert changed is True
        assert "PR #42" in content
        assert changelog.exists()

    def test_prepends_entry_to_existing_changelog(self, tmp_path):
        from scripts.update_changelog import update_changelog
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text("# Changelog\n\n## [Old — PR #10](url)\n\n")
        changed, content = update_changelog(changelog, 42, "New feature", "")
        assert changed is True
        assert content.index("PR #42") < content.index("PR #10")

    def test_idempotent_skips_duplicate(self, tmp_path):
        from scripts.update_changelog import update_changelog
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text("# Changelog\n\n## [Duplicate — PR #42](url)\n\n")
        changed, content = update_changelog(changelog, 42, "Duplicate", "")
        assert changed is False


class TestPushViaGitHubAPI:
    def test_successful_push_fetches_sha_and_put(self, tmp_path):
        from scripts.update_changelog import push_via_github_api
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text("# Changelog\n")
        token = "fake-token"
        with patch("scripts.update_changelog.fetch_changelog_sha") as mock_sha, patch(
            "scripts.update_changelog.request.urlopen"
        ) as mock_urlopen:
            mock_sha.return_value = "abc123"
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            result = push_via_github_api("benmarte/daedalus", "dev", changelog, token, max_retries=1)
            assert result is True
            mock_sha.assert_called_once_with("benmarte/daedalus", "dev", token)
            mock_urlopen.assert_called_once()
            req = mock_urlopen.call_args[0][0]
            assert req.method == "PUT"
            data = json.loads(req.data)
            assert data["sha"] == "abc123"

    def test_retries_on_409_conflict(self, tmp_path):
        from scripts.update_changelog import push_via_github_api
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text("# Changelog\n")
        token = "fake-token"
        with patch("scripts.update_changelog.fetch_changelog_sha", side_effect=["sha1", "sha2"]), patch(
            "scripts.update_changelog.request.urlopen"
        ) as mock_urlopen:
            error_409 = HTTPError(None, 409, "Conflict", {}, None)
            mock_ok = MagicMock()
            mock_ok.status = 200
            mock_urlopen.side_effect = [error_409, MagicMock(__enter__=lambda s: mock_ok)]
            result = push_via_github_api("benmarte/daedalus", "dev", changelog, token, max_retries=2)
            assert result is True
            assert mock_urlopen.call_count == 2
            second_call_data = json.loads(mock_urlopen.call_args_list[1][0][0].data)
            assert second_call_data["sha"] == "sha2"

    def test_fails_after_max_retries(self, tmp_path):
        from scripts.update_changelog import push_via_github_api
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text("# Changelog\n")
        token = "fake-token"
        with patch("scripts.update_changelog.fetch_changelog_sha", return_value="sha1"), patch(
            "scripts.update_changelog.request.urlopen"
        ) as mock_urlopen:
            mock_urlopen.side_effect = HTTPError(None, 409, "Conflict", {}, None)
            result = push_via_github_api("benmarte/daedalus", "dev", changelog, token, max_retries=3)
            assert result is False
            assert mock_urlopen.call_count == 3

"""Tests for scripts/update_changelog.py — idempotent CHANGELOG updater."""
from __future__ import annotations

import subprocess
import textwrap

import pytest

from scripts import update_changelog as uc


# ---------------------------------------------------------------------------
# Pure-function unit tests (no filesystem / no git)
# ---------------------------------------------------------------------------


_PN_TITLE = "title"
_PN_BODY = ""
_PN_NUMBER = 42


class TestParseIssueNumber:
    def test_explicit_override(self):
        assert uc.parse_issue_number(explicit=99, pr_title=_PN_TITLE, pr_body=_PN_BODY, pr_number=_PN_NUMBER) == 99

    def test_falls_back_to_pr_number(self):
        assert uc.parse_issue_number(explicit=None, pr_title="random", pr_body="", pr_number=7) == 7

    def test_finds_fixes_in_body(self):
        assert uc.parse_issue_number(explicit=None, pr_title=_PN_TITLE, pr_body="Fixes #123", pr_number=_PN_NUMBER) == 123

    def test_finds_closes_in_body(self):
        assert uc.parse_issue_number(explicit=None, pr_title=_PN_TITLE, pr_body="closes #555", pr_number=1) == 555

    def test_finds_trailing_parens_in_title(self):
        assert (
            uc.parse_issue_number(explicit=None, pr_title="feat: widget (#777)", pr_body="", pr_number=_PN_NUMBER)
            == 777
        )

    def test_body_takes_priority_over_title(self):
        assert (
            uc.parse_issue_number(
                explicit=None, pr_title="feat: widget (#777)", pr_body="resolves #100", pr_number=_PN_NUMBER
            )
            == 100
        )


class TestFormatEntry:
    def test_matches_repo_format(self):
        entry = uc.format_entry(repo="acme/co", issue_number=10, pr_number=20, pr_title="Fix bug")
        assert entry == (
            "## [Fix bug](https://github.com/acme/co/issues/10) "
            "— [PR #20](https://github.com/acme/co/pull/20)\n\n"
        )

    def test_uses_em_dash(self):
        entry = uc.format_entry(repo="x/y", issue_number=1, pr_number=2, pr_title="t")
        assert "—" in entry  # U+2014 em dash, not "--"
        assert "--" not in entry

    def test_strips_trailing_whitespace_from_title(self):
        entry = uc.format_entry(repo="x/y", issue_number=1, pr_number=2, pr_title="  hello  ")
        assert "[hello](" in entry


class TestEntryAlreadyExists:
    def test_positive_match(self):
        content = "## [t](url) — [PR #123](url)\n\n"
        assert uc.entry_already_exists(content, 123) is True

    def test_negative_no_match(self):
        content = "## [t](url) — [PR #999](url)\n\n"
        assert uc.entry_already_exists(content, 123) is False

    def test_no_false_positive_on_prefix(self):
        content = "## [t](url) — [PR #1234](url)\n\n"
        assert uc.entry_already_exists(content, 123) is False

    def test_loose_reference_format(self):
        content = "some text mentioning PR #42 inline\n"
        assert uc.entry_already_exists(content, 42) is True


class TestPrependEntry:
    def test_empty_file(self):
        result = uc.prepend_entry("", "## entry\n\n")
        assert result == "## entry\n\n"

    def test_prepends_before_existing_content(self):
        existing = "## [old](url)\n\n"
        result = uc.prepend_entry(existing, "## [new](url)\n\n")
        assert result.startswith("## [new]")
        assert "## [old]" in result
        # Newest first.
        assert result.index("[new]") < result.index("[old]")


# ---------------------------------------------------------------------------
# Integration tests (filesystem + temp git repo)
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "x@x"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=tmp_path, check=True)
    (tmp_path / "CHANGELOG.md").write_text(
        "## [Older entry](https://github.com/x/y/issues/1) — [PR #1](https://github.com/x/y/pull/1)\n\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


class TestUpdateChangelogIntegration:
    def test_normal_prepend(self, git_repo):
        changelog = git_repo / "CHANGELOG.md"
        changed, content = uc.update_changelog(
            changelog,
            repo="x/y",
            pr_number=42,
            pr_title="feat: new thing (#40)",
        )
        assert changed is True
        assert content.startswith("## [feat: new thing (#40)]")
        assert "PR #42" in content
        assert "PR #1" in content
        # Newest first.
        assert content.index("#42") < content.index("#1")

    def test_idempotent_skip(self, git_repo):
        changelog = git_repo / "CHANGELOG.md"
        changed1, content1 = uc.update_changelog(changelog, repo="x/y", pr_number=42, pr_title="t")
        changed2, content2 = uc.update_changelog(changelog, repo="x/y", pr_number=42, pr_title="t")
        assert changed1 is True
        assert changed2 is False
        assert content2 == content1
        # No duplicate.
        assert content2.count("PR #42") == 1

    def test_missing_changelog_creates_it(self, tmp_path):
        changelog = tmp_path / "CHANGELOG.md"
        assert not changelog.exists()
        changed, content = uc.update_changelog(
            changelog, repo="x/y", pr_number=1, pr_title="first entry"
        )
        assert changed is True
        assert "first entry" in content

    def test_issue_number_fallback(self, git_repo):
        changelog = git_repo / "CHANGELOG.md"
        _, content = uc.update_changelog(
            changelog, repo="x/y", pr_number=42, pr_title="some title"
        )
        # Falls back to pr number as issue number.
        assert "issues/42" in content


class TestCommitChangelog:
    def test_commits_with_correct_message(self, git_repo):
        changelog = git_repo / "CHANGELOG.md"
        uc.update_changelog(changelog, repo="x/y", pr_number=42, pr_title="t")
        rc = uc.commit_changelog(git_repo, changelog)
        assert rc == 0
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert log.stdout.strip() == "docs: update CHANGELOG.md [skip ci]"

    def test_idempotent_commit(self, git_repo):
        changelog = git_repo / "CHANGELOG.md"
        uc.update_changelog(changelog, repo="x/y", pr_number=42, pr_title="t")
        rc1 = uc.commit_changelog(git_repo, changelog)
        # Calling commit again with no further changes — should be a clean no-op.
        rc2 = uc.commit_changelog(git_repo, changelog)
        assert rc1 == 0
        assert rc2 == 0
        # Only one commit from this test.
        log = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        # init + 1 update commit.
        assert log.stdout.strip().count("docs: update CHANGELOG.md [skip ci]") == 1


class TestMain:
    TEST_ARGS = ["--pr-number", "42", "--pr-title", "feat: thing (#40)", "--no-commit"]

    def test_main_returns_zero_on_first_run(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        rc = uc.main(self.TEST_ARGS)
        assert rc == 0
        new_content = (git_repo / "CHANGELOG.md").read_text()
        assert "PR #42" in new_content

    def test_main_idempotent_returns_zero_on_second_run(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        rc1 = uc.main(self.TEST_ARGS)
        rc2 = uc.main(self.TEST_ARGS)
        assert rc1 == 0
        assert rc2 == 0
        content = (git_repo / "CHANGELOG.md").read_text()
        assert content.count("PR #42") == 1

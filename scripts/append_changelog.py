#!/usr/bin/env python3
"""
Append IDempotent Changelog Entry

A testable, workflow-friendly script that appends changelog entries to CHANGELOG.md
with automatic deduplication based on PR number.

Usage:
    # From individual fields (most common):
    python scripts/append_changelog.py \\
        --title "Fix critical bug" \\
        --pr-number 42 \\
        --pr-url https://github.com/org/repo/pull/42 \\
        --issue-url https://github.com/org/repo/issues/100 \\
        --pr-number 42

    # From pre-formatted entry (stdin):
    echo "## [Fixed bug](https://github.com/org/repo/issues/100) — [PR #42](https://github.com/org/repo/pull/42)" | \\
        python scripts/append_changelog.py --stdin --pr-number 42

Idempotency:
    If the PR number already exists in the changelog, the script skips the append
    and exits successfully (code 0). This makes it safe to call from CI/CD pipelines
    that might retry or run multiple times.

Format:
    Uses the shared format_changelog_entry() helper from scripts.lib.changelog_format
    to ensure consistency with update_changelog.py and other scripts.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from scripts.lib.changelog_format import format_changelog_entry


def _entry_present(content: str, pr_number: int) -> bool:
    """Check if a changelog entry for this PR already exists (idempotency).

    Uses word-boundary matching to avoid false positives (e.g., PR #12 shouldn't
    match PR #123).
    """
    pattern = rf"PR #{pr_number}\b"
    return re.search(pattern, content) is not None


def _prepend_entry(content: str, entry: str) -> str:
    """Prepend entry to content with blank line separator (newest-first)."""
    return entry.rstrip() + "\n\n" + content


def format_entry(
    title: str,
    pr_number: int,
    *,
    pr_url: str,
    issue_url: str = "",
    issue_number: int | None = None,
) -> str:
    """Format a changelog entry using the shared helper.

    This is a thin wrapper that delegates to format_changelog_entry() for consistency.
    """
    return format_changelog_entry(
        title=title,
        pr_number=pr_number,
        issue_url=issue_url,
        pr_url=pr_url,
        issue_number=issue_number,
    )


def append_changelog(
    changelog_path: Path,
    *,
    title: str,
    pr_number: int,
    pr_url: str,
    issue_url: str = "",
    issue_number: int | None = None,
) -> bool:
    """Append a changelog entry with idempotency.

    Args:
        changelog_path: Path to CHANGELOG.md
        title: Issue/PR title
        pr_number: Pull request number
        pr_url: URL to the PR
        issue_url: URL to the issue (optional)
        issue_number: Issue number (optional, used if issue_url not provided)

    Returns:
        True if entry was appended, False if skipped (already exists)

    Raises:
        FileNotFoundError: If changelog file doesn't exist
    """
    if not changelog_path.exists():
        # Create file if it doesn't exist
        changelog_path.write_text("", encoding="utf-8")

    content = changelog_path.read_text(encoding="utf-8")

    # Idempotency check: skip if PR already in changelog
    if _entry_present(content, pr_number):
        return False

    # Format entry using shared helper
    entry = format_entry(
        title=title,
        pr_number=pr_number,
        pr_url=pr_url,
        issue_url=issue_url,
        issue_number=issue_number,
    )

    # Prepend to content (newest first)
    new_content = _prepend_entry(content, entry)
    changelog_path.write_text(new_content, encoding="utf-8")

    return True


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:])

    Returns:
        Exit code (0 = success)
    """
    parser = argparse.ArgumentParser(
        description="Append changelog entry with idempotency",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--file",
        default="CHANGELOG.md",
        help="Path to changelog file (default: CHANGELOG.md in current directory)",
    )
    parser.add_argument(
        "--title",
        help="Issue/PR title",
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        help="Pull request number",
    )
    parser.add_argument(
        "--pr-url",
        help="URL to the PR",
    )
    parser.add_argument(
        "--issue-url",
        help="URL to the issue (optional)",
    )
    parser.add_argument(
        "--issue-number",
        type=int,
        help="Issue number (optional)",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read pre-formatted entry from stdin (use with --pr-number for idempotency)",
    )

    args = parser.parse_args(argv)

    changelog_path = Path(args.file)

    # Mode 1: Read from stdin (pre-formatted entry)
    if args.stdin:
        entry = sys.stdin.read().strip()
        if not entry:
            print("error: --stdin specified but no input provided", file=sys.stderr)
            return 1
        if not args.pr_number:
            print("error: --pr-number required with --stdin for idempotency check", file=sys.stderr)
            return 1

        if not changelog_path.exists():
            changelog_path.write_text("", encoding="utf-8")

        content = changelog_path.read_text(encoding="utf-8")
        if _entry_present(content, args.pr_number):
            print(f"skipped: PR #{args.pr_number} already in changelog")
            return 0

        new_content = _prepend_entry(content, entry)
        changelog_path.write_text(new_content, encoding="utf-8")
        print(f"appended: PR #{args.pr_number}")
        return 0

    # Mode 2: Build from individual fields
    missing = []
    if not args.title:
        missing.append("--title")
    if not args.pr_number:
        missing.append("--pr-number")
    if not args.pr_url:
        missing.append("--pr-url")

    if missing:
        print(f"error: missing required arguments: {', '.join(missing)}", file=sys.stderr)
        return 1

    result = append_changelog(
        changelog_path,
        title=args.title,
        pr_number=args.pr_number,
        pr_url=args.pr_url,
        issue_url=args.issue_url or "",
        issue_number=args.issue_number,
    )

    if result:
        print(f"appended: PR #{args.pr_number}")
    else:
        print(f"skipped: PR #{args.pr_number} already in changelog")

    return 0


if __name__ == "__main__":
    sys.exit(main())

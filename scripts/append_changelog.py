#!/usr/bin/env python3
"""Append changelog entry with idempotency guarantees (issue #1386 sub-task).

This script accepts a pre-formatted changelog entry (or parameters to generate
one) and appends it to CHANGELOG.md with idempotency — running twice with the
same input produces the same output (no duplicate entries).

Usage:
  # Pass pre-formatted entry via stdin
  echo "## [Fix bug] — [PR #42]" | python scripts/append_changelog.py --stdin

  # Pass parameters to generate entry
  python scripts/append_changelog.py --title "Fix bug" --issue-number 123 \\
      --pr-number 42 --issue-url https://github.com/org/repo/issues/123 \\
      --pr-url https://github.com/org/repo/pull/42

  # Use default CHANGELOG.md path or specify custom path
  python scripts/append_changelog.py --title "Fix bug" --pr-number 42 \\
      --file path/to/CUSTOM_CHANGELOG.md

Idempotency:
  The script checks if an entry for the given PR number already exists in the
  changelog. If so, it skips the append and exits 0 (success). Running the
  script twice with the same PR number produces the same output.

Entry format:
  Uses the shared format_changelog_entry helper from scripts.lib.changelog_format
  to ensure consistent formatting across all changelog writers.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.lib.changelog_format import format_changelog_entry


def prepend_entry(content: str, entry: str) -> str:
    """Return ``content`` with ``entry`` prepended newest-first.

    Matches the convention used by update_changelog.py and
    GitHubProvider.append_changelog: newest entries go at the top of the file,
    separated from older content by a blank line.
    """
    return entry.rstrip("\n") + "\n\n" + content


def entry_present(content: str, pr_number: int) -> bool:
    """Return ``True`` if an entry for ``PR #<pr_number>`` already exists.

    Matches ``PR #<n>`` on a word boundary so ``PR #12`` does not spuriously
    match ``PR #123``.
    """
    import re
    return re.search(rf"PR #{pr_number}\b", content) is not None


def append_changelog(
    path: str | Path,
    *,
    title: str,
    pr_number: int,
    pr_url: str = "",
    issue_number: int | None = None,
    issue_url: str = "",
    repo_url: str = "https://github.com/benmarte/daedalus",
) -> bool:
    """Idempotently prepend a changelog entry to the file at ``path``.

    Creates the file if absent. Returns ``True`` if the file was written, or
    ``False`` if an entry for ``PR #<pr_number>`` already existed (no-op).

    Uses the shared ``format_changelog_entry`` helper to ensure consistent
    formatting across all changelog writers.
    """
    p = Path(path)
    old_content = p.read_text(encoding="utf-8") if p.exists() else ""

    if entry_present(old_content, pr_number):
        return False

    entry = format_changelog_entry(
        issue_title=title,
        pr_number=pr_number,
        issue_number=issue_number,
        issue_url=issue_url,
        pr_url=pr_url,
        repo_url=repo_url,
    )
    p.write_text(prepend_entry(old_content, entry), encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Append changelog entry with idempotency guarantees."
    )
    parser.add_argument(
        "--title",
        help="Issue/PR title (used to generate entry if --stdin not used).",
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        help="PR number (used to generate entry if --stdin not used).",
    )
    parser.add_argument(
        "--pr-url",
        default="",
        help="PR URL (optional; derived from repo_url if absent).",
    )
    parser.add_argument(
        "--issue-number",
        type=int,
        help="Issue number (optional; used to derive issue_url if --issue-url absent).",
    )
    parser.add_argument(
        "--issue-url",
        default="",
        help="Issue URL (optional; derived from issue_number or repo_url).",
    )
    parser.add_argument(
        "--repo-url",
        default="https://github.com/benmarte/daedalus",
        help="Repository base URL (default: https://github.com/benmarte/daedalus).",
    )
    parser.add_argument(
        "--file",
        default="CHANGELOG.md",
        help="Path to the changelog file (default: CHANGELOG.md).",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read pre-formatted entry from stdin instead of generating from --title/--pr-number.",
    )
    args = parser.parse_args()

    if args.stdin:
        entry = sys.stdin.read().strip()
        if not entry:
            print("Error: no entry provided on stdin", file=sys.stderr)
            return 1
        # Extract PR number from entry for idempotency check
        import re
        m = re.search(r"PR #(\d+)", entry)
        if not m:
            print("Error: could not extract PR number from entry (expected 'PR #<n>')", file=sys.stderr)
            return 1
        pr_number = int(m.group(1))
        path = Path(args.file)
        old_content = path.read_text(encoding="utf-8") if path.exists() else ""
        if entry_present(old_content, pr_number):
            print(f"Changelog already contains entry for PR #{pr_number}; skipping.", file=sys.stderr)
            return 0
        path.write_text(prepend_entry(old_content, entry), encoding="utf-8")
        print(f"Appended entry for PR #{pr_number} to {args.file}", file=sys.stderr)
        return 0

    # Generate entry from --title/--pr-number
    if not args.title or not args.pr_number:
        print("Error: --title and --pr-number are required when not using --stdin", file=sys.stderr)
        return 1

    wrote = append_changelog(
        args.file,
        title=args.title,
        pr_number=args.pr_number,
        pr_url=args.pr_url,
        issue_number=args.issue_number,
        issue_url=args.issue_url,
        repo_url=args.repo_url,
    )
    if wrote:
        print(f"Appended entry for PR #{args.pr_number} to {args.file}", file=sys.stderr)
    else:
        print(f"Changelog already contains entry for PR #{args.pr_number}; skipping.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

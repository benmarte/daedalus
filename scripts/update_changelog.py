#!/usr/bin/env python3
"""Idempotently prepend a merged-PR entry to ``CHANGELOG.md`` (issue #1388).

Part of epic #1386: move CHANGELOG generation out of the dispatcher and into a
GitHub Actions workflow coupled to the PR-merge event. This module is the small,
testable core the workflow calls — it owns the *append + idempotency + format*
logic so none of it lives in inline YAML.

Behaviour (matches the existing dispatcher/provider path byte-for-byte):

* **Format** — newest-first entry in the committed CHANGELOG format::

      ## [<title>](<entry_url>) — [PR #<n>](<pr_url>)

  ``entry_url`` is the linked issue URL when known, otherwise the PR URL (the
  workflow keeps it simple and passes the PR URL; the dispatcher passes the
  issue URL). Either way the second link is always ``[PR #<n>]``.

* **Prepend** — the new entry is placed at the very top, separated from the
  previous content by a blank line (``entry + "\\n\\n" + old``), which is the
  format the dispatcher used before the CI path replaced it (#1391).

* **Idempotent** — if an entry for ``PR #<n>`` already exists anywhere in the
  file, do nothing (no duplicate). This makes the transition safe while the
  legacy dispatcher path still exists.

The commit that carries this change MUST use :data:`COMMIT_MESSAGE` so it does
not re-trigger CI.

CLI (invoked by ``.github/workflows/changelog.yml`` after checking out ``dev``)::

    python scripts/update_changelog.py \\
        --title "<PR title>" --pr-number 1234 \\
        --pr-url https://github.com/o/r/pull/1234

Exits 0 whether it wrote or skipped (skip is a normal, expected outcome). Exit 2
on bad arguments. Prints ``changelog: wrote …`` or ``changelog: skipped …`` so
the workflow can gate the commit step (``git diff --quiet`` also works).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: Commit subject the workflow must use so the write does not re-trigger CI.
COMMIT_MESSAGE = "docs: update CHANGELOG.md [skip ci]"

#: Default CHANGELOG path, relative to the repo root.
DEFAULT_PATH = "CHANGELOG.md"


def format_entry(title: str, entry_url: str, pr_number: int, pr_url: str) -> str:
    """Render a single newest-first CHANGELOG entry line (no trailing newline).

    Format::

        ## [<title>](<entry_url>) — [PR #<n>](<pr_url>)
    """
    return f"## [{title}]({entry_url}) — [PR #{pr_number}]({pr_url})"


def entry_present(content: str, pr_number: int) -> bool:
    """Return ``True`` if an entry for ``PR #<pr_number>`` already exists.

    Matches ``PR #<n>`` on a word boundary so ``PR #12`` does not spuriously
    match ``PR #123``.
    """
    return re.search(rf"PR #{pr_number}\b", content) is not None


def prepend_entry(content: str, entry: str) -> str:
    """Return ``content`` with ``entry`` prepended newest-first.

    The entry (with trailing newlines stripped) is followed by a blank line,
    then the prior content.
    """
    return entry.rstrip("\n") + "\n\n" + content


def update_changelog(
    path: str | Path,
    *,
    title: str,
    pr_number: int,
    pr_url: str,
    entry_url: str | None = None,
) -> bool:
    """Idempotently prepend a merged-PR entry to the CHANGELOG at ``path``.

    Creates the file if absent. Returns ``True`` if the file was written, or
    ``False`` if an entry for ``PR #<pr_number>`` already existed (no-op).
    """
    p = Path(path)
    old_content = p.read_text(encoding="utf-8") if p.exists() else ""

    if entry_present(old_content, pr_number):
        return False

    entry = format_entry(title, entry_url or pr_url, pr_number, pr_url)
    p.write_text(prepend_entry(old_content, entry), encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Idempotently prepend a merged-PR entry to CHANGELOG.md.",
    )
    parser.add_argument("--title", required=True, help="PR (or issue) title.")
    parser.add_argument("--pr-number", required=True, type=int, help="Merged PR number.")
    parser.add_argument("--pr-url", required=True, help="URL of the merged PR.")
    parser.add_argument(
        "--entry-url",
        default=None,
        help="URL for the title link (linked issue URL); defaults to --pr-url.",
    )
    parser.add_argument(
        "--file",
        default=DEFAULT_PATH,
        help=f"Path to the CHANGELOG file (default: {DEFAULT_PATH}).",
    )
    args = parser.parse_args(argv)

    wrote = update_changelog(
        args.file,
        title=args.title,
        pr_number=args.pr_number,
        pr_url=args.pr_url,
        entry_url=args.entry_url,
    )
    if wrote:
        print(f"changelog: wrote entry for PR #{args.pr_number} to {args.file}")
    else:
        print(f"changelog: skipped PR #{args.pr_number} (entry already present)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

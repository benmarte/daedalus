#!/usr/bin/env python3
"""Idempotent CHANGELOG.md updater.

Prepends a new entry for a merged PR to CHANGELOG.md in the format used by
this repo, maintaining newest-first order. Skips the update when an entry for
the same PR number already exists (idempotent). Commits the change with the
CI-safe message ``docs: update CHANGELOG.md [skip ci]``.

Designed to run inside the ``.github/workflows/changelog.yml`` workflow: it
takes the PR number, title, and (optionally) body on the command line, writes
the updated CHANGELOG.md, and commits. Also usable as a library via
``update_changelog()`` for tests.

Exit codes:
  0 — success (entry prepended OR idempotent no-op)
  1 — unrecoverable error (details on stderr)
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Regex that matches a PR reference for a *specific* PR number anywhere in
# the changelog. Catches both the canonical ``[PR #N](...)`` link format
# used by this repo and any looser ``PR #N`` mention. We use a word-boundary
# on the number so PR #13 does not false-positive-match PR #1380.
_PR_REF_RE = re.compile(r"\bPR\s+#(\d+)\b")

# Pulls ``(#N)`` / ``fixes #N`` / ``closes #N`` out of a free-text body.
_ISSUE_FROM_TEXT_RE = re.compile(r"(?:fixes|closes|resolves)\s+#(\d+)|\(#(\d+)\)\s*$", re.IGNORECASE)

# Default repo — overridden by --repo or derived from ``git remote``.
DEFAULT_REPO = "benmarte/daedalus"

COMMIT_MESSAGE = "docs: update CHANGELOG.md [skip ci]"


def _derive_repo_from_git(cwd: Path) -> str:
    """Return ``owner/name`` from the ``origin`` remote, or DEFAULT_REPO."""
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return DEFAULT_REPO
    if out.returncode != 0:
        return DEFAULT_REPO
    url = out.stdout.strip()
    # ssh:   git@github.com:owner/name.git
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$", url)
    if not m:
        return DEFAULT_REPO
    return f"{m.group(1)}/{m.group(2)}"


def parse_issue_number(
    *,
    explicit: Optional[int],
    pr_title: str,
    pr_body: str,
    pr_number: int,
) -> int:
    """Resolve the issue number to link to.

    Resolution order:
      1. Explicit ``--issue-number`` override.
      2. ``fixes #N`` / ``closes #N`` / ``resolves #N`` in the PR body.
      3. Trailing ``(#N)`` suffix on the PR title.
      4. Fall back to ``pr_number`` (ensures a valid URL even if nothing else
         matches — both the issue URL and PR URL point to the PR itself).
    """
    if explicit is not None:
        return explicit
    for text in (pr_body, pr_title):
        m = _ISSUE_FROM_TEXT_RE.search(text or "")
        if m:
            n = m.group(1) or m.group(2)
            if n:
                return int(n)
    return pr_number


def format_entry(*, repo: str, issue_number: int, pr_number: int, pr_title: str) -> str:
    """Build the CHANGELOG entry line matching existing entries."""
    issue_url = f"https://github.com/{repo}/issues/{issue_number}"
    pr_url = f"https://github.com/{repo}/pull/{pr_number}"
    # ``—`` is U+2014 (em dash), matching every existing entry in this file.
    clean_title = (pr_title or "").strip()
    return f"## [{clean_title}]({issue_url}) — [PR #{pr_number}]({pr_url})\n\n"


def entry_already_exists(content: str, pr_number: int) -> bool:
    """Return True if the file already contains a PR #<n> reference."""
    return bool(_PR_REF_RE.search(content)) and any(
        int(m.group(1)) == pr_number for m in _PR_REF_RE.finditer(content)
    )


def prepend_entry(content: str, new_entry: str) -> str:
    """Prepend ``new_entry`` to ``content`` maintaining newest-first order."""
    if not content:
        return new_entry
    # Ensure exactly one blank line between the new entry and existing content.
    stripped = content.lstrip("\n")
    return new_entry + stripped


def update_changelog(
    changelog_path: Path,
    *,
    repo: str,
    pr_number: int,
    pr_title: str,
    pr_body: str = "",
    issue_number: Optional[int] = None,
) -> tuple[bool, str]:
    """Update the changelog file idempotently.

    Returns ``(changed, final_content)`` where ``changed`` is True when the
    file was modified (i.e. the PR entry was not already present).
    """
    if changelog_path.exists():
        original = changelog_path.read_text(encoding="utf-8")
    else:
        original = ""

    if entry_already_exists(original, pr_number):
        return False, original

    issue = parse_issue_number(
        explicit=issue_number,
        pr_title=pr_title,
        pr_body=pr_body,
        pr_number=pr_number,
    )
    new_entry = format_entry(repo=repo, issue_number=issue, pr_number=pr_number, pr_title=pr_title)
    updated = prepend_entry(original, new_entry)
    changelog_path.write_text(updated, encoding="utf-8")
    return True, updated


def commit_changelog(cwd: Path, changelog_path: Path) -> int:
    """Stage the changelog and commit with the CI-safe message.

    Returns the ``git commit`` exit code (0 on success, nonzero on failure).
    A no-op when the working tree has no changes (idempotent reruns).
    """
    rel_changelog = str(changelog_path.relative_to(cwd)) if changelog_path.is_absolute() else str(changelog_path)

    status = subprocess.run(
        ["git", "status", "--porcelain", "--", rel_changelog],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        return status.returncode
    if not status.stdout.strip():
        # Already committed (idempotent rerun).
        return 0

    add = subprocess.run(
        ["git", "add", "--", rel_changelog],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if add.returncode != 0:
        return add.returncode

    return subprocess.run(
        ["git", "commit", "-m", COMMIT_MESSAGE, "--", rel_changelog],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    ).returncode


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Idempotent CHANGELOG.md updater.")
    p.add_argument("--pr-number", type=int, required=True, help="Merged PR number.")
    p.add_argument("--pr-title", required=True, help="Merged PR title.")
    p.add_argument("--pr-body", default="", help="PR body (used to find closes/fixes #N).")
    p.add_argument(
        "--issue-number",
        type=int,
        default=None,
        help="Explicit issue number to link to. Overrides any inference from title/body.",
    )
    p.add_argument(
        "--repo",
        default=None,
        help="owner/name of the GitHub repo. Default: git remote origin.",
    )
    p.add_argument(
        "--changelog",
        default="CHANGELOG.md",
        help="Path to the changelog file. Default: ./CHANGELOG.md.",
    )
    p.add_argument(
        "--no-commit",
        action="store_true",
        help="Update the file but do not commit.",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    cwd = Path.cwd()
    changelog = Path(args.changelog)
    if not changelog.is_absolute():
        changelog = (cwd / changelog).resolve()
    repo = args.repo or _derive_repo_from_git(cwd)

    try:
        changed, _ = update_changelog(
            changelog,
            repo=repo,
            pr_number=args.pr_number,
            pr_title=args.pr_title,
            pr_body=args.pr_body,
            issue_number=args.issue_number,
        )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"update_changelog: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not changed:
        print(f"update_changelog: PR #{args.pr_number} already present — no change.")
        return 0

    if args.no_commit:
        print(f"update_changelog: prepended entry for PR #{args.pr_number}.")
        return 0

    rc = commit_changelog(cwd, changelog)
    if rc == 0:
        print(f"update_changelog: committed entry for PR #{args.pr_number}.")
    else:
        print(f"update_changelog: file updated but commit failed (rc={rc}).", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Idempotent CHANGELOG.md updater for CI workflows.

Prepends changelog entries for merged PRs with race-safe GitHub API writes.
Fetches current file SHA before updating and retries on 409 Conflict.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional
from urllib import request

_PR_REF_RE = re.compile(r"PR #(\d+)")
_ISSUE_FROM_BODY_RE = re.compile(r"(?:closes|fixes|resolves):? #(\d+)", re.IGNORECASE)
_ISSUE_FROM_TITLE_RE = re.compile(r"\(#(\d+)\)")


def parse_issue_number(pr_body: str, pr_title: str, pr_number: int) -> int:
    if m := _ISSUE_FROM_BODY_RE.search(pr_body or ""):
        return int(m.group(1))
    if m := _ISSUE_FROM_TITLE_RE.search(pr_title or ""):
        return int(m.group(1))
    return pr_number


def format_entry(pr_number: int, pr_title: str, issue_number: int) -> str:
    return f"## [{pr_title} — PR #{pr_number}](https://github.com/benmarte/daedalus/issues/{issue_number})\n\n"


def entry_already_exists(content: str, pr_number: int) -> bool:
    return bool(_PR_REF_RE.search(content) and f"PR #{pr_number}" in content)


def prepend_entry(content: str, new_entry: str) -> str:
    if not content.strip():
        return f"# Changelog\n\n{new_entry}"
    if content.startswith("# Changelog"):
        lines = content.split("\n", 2)
        if len(lines) >= 3:
            return f"{lines[0]}\n\n{new_entry}{lines[2]}"
        return f"{lines[0]}\n\n{new_entry}"
    return f"{new_entry}\n{content}"


def update_changelog(changelog_path: Path, pr_number: int, pr_title: str, pr_body: str) -> tuple[bool, str]:
    if changelog_path.exists():
        content = changelog_path.read_text()
    else:
        content = ""

    if entry_already_exists(content, pr_number):
        return False, content

    issue_number = parse_issue_number(pr_body, pr_title, pr_number)
    new_entry = format_entry(pr_number, pr_title, issue_number)
    updated = prepend_entry(content, new_entry)
    changelog_path.write_text(updated)
    return True, updated


def fetch_changelog_sha(repo: str, branch: str, github_token: str) -> Optional[str]:
    """Fetch the current SHA of CHANGELOG.md from GitHub API."""
    url = f"https://api.github.com/repos/{repo}/contents/CHANGELOG.md?ref={branch}"
    req = request.Request(url, headers={"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"})
    try:
        with request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("sha")
    except Exception:
        return None


def push_via_github_api(repo: str, branch: str, file_path: Path, github_token: str, max_retries: int = 3) -> bool:
    """Push changelog update via GitHub API with SHA fetch and 409 retry."""
    for attempt in range(max_retries):
        sha = fetch_changelog_sha(repo, branch, github_token)
        if sha is None:
            sha = ""

        content = base64.b64encode(file_path.read_bytes()).decode("utf-8")
        url = f"https://api.github.com/repos/{repo}/contents/CHANGELOG.md"
        payload = {
            "message": "docs: update CHANGELOG.md [skip ci]",
            "content": content,
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        req = request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"},
            method="PUT",
        )
        try:
            with request.urlopen(req, timeout=10):
                return True
        except Exception as e:
            if "409" in str(e) and attempt < max_retries - 1:
                continue
            print(f"GitHub API push failed: {e}", file=sys.stderr)
            return False
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--pr-title", required=True)
    parser.add_argument("--pr-body", default="")
    parser.add_argument("--changelog", default="CHANGELOG.md")
    parser.add_argument("--push-via-api", action="store_true")
    parser.add_argument("--branch", default="dev")
    args = parser.parse_args()

    changelog_path = Path(args.changelog)
    github_token = os.environ.get("GITHUB_TOKEN")

    changed, _ = update_changelog(changelog_path, args.pr_number, args.pr_title, args.pr_body)
    if not changed:
        print(f"PR #{args.pr_number} already in changelog")
        sys.exit(0)

    if args.push_via_api and github_token:
        if push_via_github_api(args.repo, args.branch, changelog_path, github_token):
            print(f"Updated changelog via API for PR #{args.pr_number}")
            sys.exit(0)
        sys.exit(1)
    else:
        print("Updated changelog file (no API push)")

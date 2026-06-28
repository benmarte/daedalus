"""Tests for the GitHub comment how-to instructions (#894).

Agents were silently failing to post issue comments: the injected how-to told
them to POST via ``urllib.request`` reading ``os.environ["GITHUB_TOKEN"]``. When
the cron worker's environment had no ``GITHUB_TOKEN`` exported, that line raised
``KeyError`` and the comment was dropped without a trace. The fix routes comments
through the already-authenticated ``gh`` CLI, which needs no exported token.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _load_dispatch, check  # noqa: E402,F401

disp = _load_dispatch()


# ── _PR_COMMENT_HOWTO["github"]: uses gh, not GITHUB_TOKEN/urllib ─────────────


def test_github_comment_howto_uses_gh_cli():
    """The github comment how-to instructs `gh issue comment`, not urllib."""
    raw = disp._PR_COMMENT_HOWTO["github"]
    check("uses `gh issue comment`", "gh issue comment" in raw)
    check("uses `gh pr comment` for PRs", "gh pr comment" in raw)
    check("uses --body-file to avoid shell escaping", "--body-file" in raw)


def test_github_comment_howto_drops_github_token_and_urllib():
    """The bug source — env-var token read + urllib POST — must be gone."""
    raw = disp._PR_COMMENT_HOWTO["github"]
    check("no GITHUB_TOKEN env read", "GITHUB_TOKEN" not in raw)
    check("no urllib.request POST", "urllib" not in raw)


def test_resolved_github_comment_howto_substitutes_repo():
    """`.format(repo=...)` must substitute cleanly with no stray braces."""
    howto = disp._resolve_howtos("github", "org/repo", 894)["comment"]
    check("repo substituted into command", "--repo org/repo" in howto)
    check("issue-number placeholder preserved", "<number>" in howto)
    check("no unsubstituted braces remain", "{" not in howto and "}" not in howto)


# ── role bodies wire the gh-based how-to to the comment step ──────────────────


def test_developer_body_posts_comment_via_gh():
    """The developer body's 'Post comment on issue' step uses the gh how-to."""
    issue = {"number": 894, "title": "comment bug", "body": "x", "url": "u"}
    body = disp._dev_task_body("org/repo", issue, 3, "/tmp/wd", "dev", "github")
    check("comment step references gh issue comment", "gh issue comment" in body)
    # The PR-*creation* how-to (a separate, out-of-scope concern) still uses the API,
    # so we assert against the comment how-to specifically rather than the whole body.
    comment_howto = disp._resolve_howtos("github", "org/repo", 894)["comment"]
    check("comment how-to embedded verbatim in body", comment_howto in body)
    check("comment how-to reads no token from env", "GITHUB_TOKEN" not in comment_howto)
    # Secondary issue (#894): developer blocks own card, never unblocks a planner card.
    check("developer blocks own card", "review-required: PR #" in body)
    check("developer does not unblock a planner card", "kanban_unblock" not in body)


if __name__ == "__main__":
    test_github_comment_howto_uses_gh_cli()
    test_github_comment_howto_drops_github_token_and_urllib()
    test_resolved_github_comment_howto_substitutes_repo()
    test_developer_body_posts_comment_via_gh()
    print("All comment-howto tests passed.")

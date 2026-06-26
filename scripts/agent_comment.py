"""Shared GitHub comment helper for Daedalus agent souls.

Every role soul posts a status comment to a GitHub issue or PR. This module is
the single source of truth for that boilerplate: it enforces the mandatory
``**Agent: <name>**`` header (souls can no longer omit it), assembles the
markdown body, and POSTs via ``urllib`` (no curl, no ``gh`` CLI).

Souls invoke it from the target-repo checkout via the stable plugin path::

    import os, sys
    _h = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    sys.path.insert(0, os.path.join(_h, "plugins", "daedalus", "scripts"))
    from agent_comment import post_comment

    post_comment("org/repo", 123, "developer",
                 "Implementation Complete — Issue #123",
                 "### What was implemented\\n...",
                 token=os.environ["GITHUB_TOKEN"])

Keep this import-free of third-party packages — agents run it inside arbitrary
repo checkouts where only the standard library is guaranteed.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict

_API_ROOT = "https://api.github.com"


def build_comment_body(agent_name: str, heading: str, body_sections: str) -> str:
    """Assemble the canonical comment markdown with the enforced agent header.

    The ``**Agent: <agent_name>**`` header is always the first line — this is the
    part souls can no longer drop. ``heading`` (when given) becomes an ``H2`` and
    ``body_sections`` is the free-form markdown body.
    """
    parts = [f"**Agent: {agent_name}**"]
    if heading:
        parts.append(f"## {heading}")
    if body_sections:
        parts.append(body_sections.strip("\n"))
    return "\n\n".join(parts) + "\n"


def _post_issue_comment(repo: str, number: int, body: str, *, token: str) -> Dict[str, Any]:
    """POST a comment to ``/repos/{repo}/issues/{number}/comments``."""
    req = urllib.request.Request(
        f"{_API_ROOT}/repos/{repo}/issues/{number}/comments",
        data=json.dumps({"body": body}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def post_comment(repo: str, number: int, agent_name: str, heading: str,
                 body_sections: str, *, token: str) -> Dict[str, Any]:
    """Post a comment to a GitHub **issue**; returns the created comment JSON."""
    body = build_comment_body(agent_name, heading, body_sections)
    return _post_issue_comment(repo, number, body, token=token)


def post_pr_comment(repo: str, pr_number: int, agent_name: str, heading: str,
                    body_sections: str, *, token: str) -> Dict[str, Any]:
    """Post a conversation comment to a GitHub **pull request**.

    PR conversation comments use the same ``issues/{n}/comments`` endpoint as
    issues — a PR is an issue for commenting purposes.
    """
    body = build_comment_body(agent_name, heading, body_sections)
    return _post_issue_comment(repo, pr_number, body, token=token)

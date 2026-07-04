"""core.dispatch.validator_comment — GitHub comment scanners for validator and PM.

Two functions that read GitHub issue comments to infer an agent's outcome when
the kanban card summary is unavailable (context-dropout, premature completion).
Both are pure: provider.get_issue_comments → scan → return string.  Neither
touches kanban, mutable dispatcher globals, or other dispatch siblings, so they
are safe to extract without any patching concerns.

Moved from scripts/daedalus_dispatch.py (issue #1153 PR 2/4).
The dispatcher re-exports every symbol so the public surface is unchanged.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("daedalus.dispatch")


def _validator_github_comment_outcome(
    provider,
    issue_number: int,
    validator_profile: str = "validator-daedalus",
) -> str:
    """Return 'confirmed', 'rejected', or '' by scanning GitHub issue comments.

    When a validator agent's kanban summary is None (context-limit dropout), its
    GitHub comment is the only reliable record of its decision.  We scan all
    comments on the issue for one authored by the validator (detected via the
    mandatory '**Agent: validator**' attribution prefix from SOUL.md) and look
    for the outcome keyword in the comment body.
    """
    if provider is None:
        return ""
    try:
        comments = provider.get_issue_comments(issue_number) or []
    except Exception:
        return ""
    # Extract the role name for the SOUL.md attribution header check.
    # e.g. "validator-daedalus" → match "agent: validator" in the body.
    role_slug = validator_profile.split("-")[0]  # "validator"
    agent_marker = f"agent: {role_slug}"  # "agent: validator"
    for c in reversed(comments):
        body_lower = (c.get("body") or "").lower()
        if agent_marker not in body_lower[:300]:
            continue
        if "confirmed" in body_lower:
            return "confirmed"
        if (
            "rejected" in body_lower
            or "cannot_reproduce" in body_lower
            or "already_fixed" in body_lower
        ):
            return "rejected"
    return ""


def _pm_spec_comment(
    provider,
    issue_number: int,
    pm_profile: str = "project-manager-daedalus",
) -> str:
    """Return a short head of the issue's '## Implementation Spec' comment, or ''.

    When the PM kanban card completes with an empty summary (hermes
    premature-completion bug, #1161), the spec the PM posted on the GitHub issue
    is the only surviving record. Mirrors _validator_github_comment_outcome: scan
    comments newest-first for one carrying the PM attribution marker in its head
    and an '## Implementation Spec' heading, and return the first non-empty line
    after the heading (truncated to 200 chars) for use in an adopted summary.

    The marker strips the trailing profile suffix with rsplit — the validator
    helper's split('-')[0] would derive 'agent: project' from
    'project-manager-daedalus' and match unrelated 'project-*' agents.
    """
    if provider is None:
        return ""
    try:
        comments = provider.get_issue_comments(issue_number) or []
    except Exception:
        return ""
    role_slug = pm_profile.rsplit("-", 1)[0] if "-" in pm_profile else pm_profile
    agent_marker = f"agent: {role_slug}"  # "agent: project-manager"
    for c in reversed(comments):
        body = c.get("body") or ""
        body_lower = body.lower()
        if agent_marker not in body_lower[:300]:
            continue
        idx = body_lower.find("## implementation spec")
        if idx == -1:
            continue
        for line in body[idx:].splitlines()[1:]:
            line = line.strip()
            if line:
                return line[:200]
    return ""

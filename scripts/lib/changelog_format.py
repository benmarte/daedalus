"""Shared changelog entry formatting helpers.

Single source of truth for how a changelog line is formatted, consumed by
both the CI updater script (``scripts.update_changelog``) and the dispatcher
changelog auto-append path. Keeping the format in one module prevents drift
between the two writers.
"""
from __future__ import annotations


def format_changelog_entry(
    issue_number: int,
    issue_title: str,
    pr_number: int,
    issue_url: str = "",
    pr_url: str = "",
    repo_url: str = "https://github.com/benmarte/daedalus",
) -> str:
    """Format a single changelog entry line.

    Produces::

        ## [issue_title](issue_url) — [PR #pr_number](pr_url)\\n

    When neither ``issue_url`` nor ``pr_url`` is supplied, they are derived
    from ``repo_url``. Callers that already have rich URLs (e.g. the
    dispatcher, which knows the provider's ``issue_url``/``pr_url`` helpers)
    pass them explicitly; the CI updater only knows ``pr_number`` and
    ``issue_number`` and lets the helper build canonical URLs.
    """
    if not issue_url:
        issue_url = f"{repo_url}/issues/{issue_number}"
    if not pr_url:
        pr_url = f"{repo_url}/pull/{pr_number}"
    return (
        f"## [{issue_title}]({issue_url}) — "
        f"[PR #{pr_number}]({pr_url})\n"
    )

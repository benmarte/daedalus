"""Shared changelog entry formatting utilities.

This module is the single source of truth for changelog entry format across
all scripts (update_changelog.py, append_changelog.py). It ensures consistent
formatting and prevents divergence.

Entry format:
    ## [title](issue_url) — [PR #number](pr_url)

URL derivation:
    - issue_url: explicit > derived from issue_number > derive from pr_url
    - pr_url: explicit > derived from pr_number + repo_url
"""


def format_changelog_entry(
    title: str,
    pr_number: int,
    *,
    issue_number: int | None = None,
    issue_url: str = "",
    pr_url: str = "",
    repo_url: str = "https://github.com/benmarte/daedalus",
) -> str:
    """Format a changelog entry line.

    Args:
        title: PR/issue title
        pr_number: PR number
        issue_number: Optional issue number (for URL derivation)
        issue_url: Explicit issue URL (overrides derivation)
        pr_url: Explicit PR URL (overrides derivation)
        repo_url: Repository base URL for derivation

    Returns:
        Formatted entry line (single line, no trailing newline, em-dash separator)
    """
    # Derive issue_url: explicit > number-derived > pr_url fallback
    if not issue_url:
        if issue_number is not None:
            issue_url = f"{repo_url}/issues/{issue_number}"
        else:
            issue_url = pr_url if pr_url else f"{repo_url}/pull/{pr_number}"

    # Derive pr_url: explicit > number-derived
    if not pr_url:
        pr_url = f"{repo_url}/pull/{pr_number}"

    # Format: ## [title](issue_url) — [PR #number](pr_url)
    return f"## [{title}]({issue_url}) — [PR #{pr_number}]({pr_url})"

"""Deterministic GitHub Projects v2 status tracking for the daedalus.

Moves an issue's Project card between statuses (e.g. In progress / In review /
Done) from code via the gh CLI, and derives the right status from PR state. Every
call degrades gracefully: a missing project/field/option or any gh failure logs a
warning and returns False/None — tracking must never break an daedalus run.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger("daedalus.github_project")


def _gh(args: List[str], timeout: int = 30):
    """Run a gh command; return (returncode, stdout, stderr). Patched in tests."""
    try:
        r = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:  # gh missing, timeout, etc.
        return 1, "", str(e)


def _gh_json(args: List[str], timeout: int = 30):
    rc, out, _ = _gh(args, timeout=timeout)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def pr_ci_green(repo: str, pr_number: int) -> bool:
    """True if the PR's CI is green — prefer the `ci-complete` gate, else require
    every check to be SUCCESS/NEUTRAL/SKIPPED. Used to auto-advance a review-required
    handoff only once its PR actually passes CI."""
    data = _gh_json(["pr", "view", str(pr_number), "--repo", repo, "--json", "statusCheckRollup"])
    rollup = (data or {}).get("statusCheckRollup") or []
    if not rollup:
        return False
    cic = [c for c in rollup if (c.get("name") or c.get("context")) == "ci-complete"]
    if cic:
        return (cic[0].get("conclusion") or cic[0].get("state") or "").upper() == "SUCCESS"
    ok = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    return all((c.get("conclusion") or c.get("state") or "").upper() in ok for c in rollup)


def close_issue(repo: str, issue_number: int) -> bool:
    """Close a GitHub issue. Returns True on success, False (logged) otherwise.

    Used when a PR merges into a non-default branch (e.g. dev), where GitHub does
    NOT auto-close the linked issue. Only call for issues known to be open.
    """
    rc, _, err = _gh(["issue", "close", str(issue_number), "--repo", repo])
    if rc != 0:
        logger.warning("close_issue: #%s failed: %s", issue_number, (err or "").strip())
        return False
    logger.info("close_issue: closed #%s", issue_number)
    return True


def pr_state_for_issue(repo: str, issue_number: int) -> Optional[str]:
    """Best PR state referencing an issue: 'merged', 'open', or None.

    Matches a PR to the issue by branch name (``…issue-<n>…`` / ``…/<n>-…``) or by
    body reference (``#<n>``). Prefers merged over open.
    """
    data = _gh_json([
        "pr", "list", "--repo", repo, "--state", "all", "--limit", "50",
        "--json", "number,state,headRefName,body",
    ])
    if not data:
        return None
    n = str(issue_number)
    # A PR resolves an issue only via a closing keyword (Closes/Fixes/Resolves #n)
    # or the issue-branch convention — NOT a bare "#n" mention (which is just a
    # reference and must not flip the card to In review/Done).
    closing = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#" + n + r"\b")
    open_found = False
    for pr in data:
        head = pr.get("headRefName", "") or ""
        body = pr.get("body", "") or ""
        linked = (f"issue-{n}" in head or f"/{n}-" in head or head.endswith(f"-{n}")
                  or bool(closing.search(body)))
        if not linked:
            continue
        state = (pr.get("state") or "").lower()
        if state == "merged":
            return "merged"
        if state == "open":
            open_found = True
    return "open" if open_found else None


def pr_number_for_issue(repo: str, issue_number: int) -> Optional[int]:
    """Return the PR number that resolves an issue (same matching as pr_state_for_issue).

    Prefers merged over open. Returns None if no matching PR found.
    """
    data = _gh_json([
        "pr", "list", "--repo", repo, "--state", "all", "--limit", "50",
        "--json", "number,state,headRefName,body",
    ])
    if not data:
        return None
    n = str(issue_number)
    closing = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#" + n + r"\b")
    open_num = None
    for pr in data:
        head = pr.get("headRefName", "") or ""
        body_text = pr.get("body", "") or ""
        linked = (f"issue-{n}" in head or f"/{n}-" in head or head.endswith(f"-{n}")
                  or bool(closing.search(body_text)))
        if not linked:
            continue
        state = (pr.get("state") or "").lower()
        if state == "merged":
            return pr["number"]
        if state == "open" and open_num is None:
            open_num = pr["number"]
    return open_num


class GitHubProject:
    """Resolve and update a GitHub Projects v2 board's Status field for issues."""

    def __init__(self, owner: str, number: int):
        self.owner = owner
        self.number = int(number)
        self._meta: Optional[Dict[str, Any]] = None  # cached project_id/status_field_id/options

    def _load_meta(self) -> Optional[Dict[str, Any]]:
        if self._meta is not None:
            return self._meta or None
        projects = _gh_json(["project", "list", "--owner", self.owner, "--format", "json"])
        project_id = None
        for p in (projects or {}).get("projects", []):
            if p.get("number") == self.number:
                project_id = p.get("id")
                break
        if not project_id:
            logger.warning("GitHubProject: project #%s not found for %s", self.number, self.owner)
            self._meta = {}
            return None
        fields = _gh_json(["project", "field-list", str(self.number), "--owner", self.owner, "--format", "json"])
        status_field_id = None
        options: Dict[str, str] = {}
        for f in (fields or {}).get("fields", []):
            if str(f.get("name", "")).lower() == "status":
                status_field_id = f.get("id")
                for o in f.get("options", []):
                    options[str(o.get("name", "")).lower()] = o.get("id")
                break
        if not status_field_id:
            logger.warning("GitHubProject: no Status field on project #%s", self.number)
            self._meta = {}
            return None
        self._meta = {"project_id": project_id, "status_field_id": status_field_id, "options": options}
        return self._meta

    def _items(self) -> List[Dict[str, Any]]:
        data = _gh_json([
            "project", "item-list", str(self.number), "--owner", self.owner,
            "--format", "json", "--limit", "200",
        ])
        return (data or {}).get("items", [])

    def numbers_with_status(self, status_name: str) -> set:
        """Issue numbers whose Project Status equals status_name (one item-list call).

        Used to gate dispatch on a status (e.g. only 'Ready' items become new work),
        so a single API call covers the whole board instead of one per issue.
        """
        target = (status_name or "").lower()
        out: set = set()
        for it in self._items():
            if str(it.get("status") or "").lower() == target:
                num = (it.get("content") or {}).get("number")
                if isinstance(num, int):
                    out.add(num)
        return out

    def numbers_with_statuses(self, status_names: list[str]) -> set:
        """Issue numbers whose Project Status is in status_names (union, one call).

        Multi-status form of numbers_with_status — any issue whose lower-cased
        status appears in the set is included.
        """
        targets = {s.lower() for s in status_names}
        out: set = set()
        for it in self._items():
            if str(it.get("status") or "").lower() in targets:
                num = (it.get("content") or {}).get("number")
                if isinstance(num, int):
                    out.add(num)
        return out

    def set_status(self, issue_number: int, status_name: str) -> bool:
        """Set the issue's Project Status; return True on success, False (logged) otherwise."""
        meta = self._load_meta()
        if not meta:
            return False
        option_id = meta["options"].get(status_name.lower())
        if not option_id:
            logger.warning("GitHubProject: status '%s' not an option on #%s", status_name, self.number)
            return False
        item_id = None
        for it in self._items():
            if (it.get("content") or {}).get("number") == issue_number:
                item_id = it.get("id")
                break
        if not item_id:
            logger.warning("GitHubProject: issue #%s not on project #%s", issue_number, self.number)
            return False
        rc, _, err = _gh([
            "project", "item-edit", "--id", item_id, "--project-id", meta["project_id"],
            "--field-id", meta["status_field_id"], "--single-select-option-id", option_id,
        ])
        if rc != 0:
            logger.warning("GitHubProject: item-edit failed for #%s: %s", issue_number, (err or "").strip())
            return False
        logger.info("GitHubProject: #%s -> %s", issue_number, status_name)
        return True


def open_pr_for_branch(repo: str, branch: str) -> Optional[int]:
    """Return the number of an open PR whose head is ``branch``, or None.

    Runs ``gh pr list --repo <repo> --head <branch> --json number --jq
    '.[0].number'``.  Returns None gracefully on any failure (missing gh,
    no matching PR, etc.).
    """
    if not branch or not repo:
        return None
    rc, out, _ = _gh([
        "pr", "list", "--repo", repo, "--head", branch,
        "--json", "number", "--jq", ".[0].number",
    ])
    if rc != 0:
        return None
    try:
        val = int(out.strip())
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None


# ── PR comment helpers ───────────────────────────────────────────────────────


def pr_list_comments(repo: str, pr_number: int) -> List[Dict[str, Any]]:
    """List all comments on a PR. Returns parsed JSON list, or [] on failure."""
    data = _gh_json([
        "pr", "view", str(pr_number), "--repo", repo,
        "--json", "comments", "--jq", ".comments",
    ])
    return data if isinstance(data, list) else []


def pr_find_comment(repo: str, pr_number: int, substring: str) -> Optional[Dict[str, Any]]:
    """Find the first PR comment whose body contains ``substring``.

    Returns the comment dict (keys: id, body, author, etc.), or None.
    """
    for c in pr_list_comments(repo, pr_number):
        if substring in (c.get("body") or ""):
            return c
    return None


def pr_add_comment(repo: str, pr_number: int, body: str) -> bool:
    """Add a comment to a PR. Returns True on success, False (logged) otherwise."""
    rc, _, err = _gh([
        "pr", "comment", str(pr_number), "--repo", repo,
        "--body", body,
    ])
    if rc != 0:
        logger.warning("pr_add_comment: PR #%s failed: %s", pr_number, (err or "").strip())
        return False
    logger.info("pr_add_comment: commented on PR #%s", pr_number)
    return True


def reconcile_status(project: "GitHubProject", repo: str, issue_number: int) -> Optional[str]:
    """Set the card status from PR state. Returns the status applied, or None.

    open PR -> 'In review'; merged PR -> 'Done'. No PR -> left untouched (caller
    sets 'In progress' when it starts work).
    """
    state = pr_state_for_issue(repo, issue_number)
    if state == "merged":
        return "Done" if project.set_status(issue_number, "Done") else None
    if state == "open":
        return "In review" if project.set_status(issue_number, "In review") else None
    return None

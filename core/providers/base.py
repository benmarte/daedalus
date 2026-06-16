"""Provider-agnostic VCS interface for the daedalus pipeline.

Every provider (GitHub, GitLab, Azure DevOps, …) implements
:class:`VCSProvider`. Methods never raise — they log a warning and return a
safe falsy default, mirroring the original github_project.py contract:
tracking must never break a daedalus run.

Capability flags let the dispatcher and dashboard degrade gracefully when a
provider can't do something (e.g. GitLab issue boards are label-driven, so
``supports_boards`` is False and board calls are no-ops).
"""
from __future__ import annotations

import abc
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("daedalus.providers")


class ProviderConfigError(Exception):
    """Required provider config (repo identifiers, …) is missing or invalid."""


def resolve_token(resolved: Dict[str, Any], default_envs: Sequence[str]) -> str:
    """Resolve an API token at runtime — never from config file values.

    Order: env var named by ``vcs.token_env`` → provider default env vars.
    Returns "" when absent (providers degrade to unauthenticated/no-op);
    a missing token must never disable the whole plugin.
    """
    vcs = (resolved or {}).get("vcs") or {}
    names: List[str] = []
    custom = (vcs.get("token_env") or "").strip()
    if custom:
        names.append(custom)
    names.extend(default_envs)
    for name in names:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return ""

# Sentinel for "report already delivered" PR comments. The literal string is
# kept from the Slack-only era so previously-marked PRs are not re-delivered.
DELIVERY_MARKER = "<!-- daedalus:slack-delivered -->"

# Canonical pipeline statuses → default provider-facing names. Overridable per
# project via vcs.status_map (values are board columns / labels / WI states).
DEFAULT_STATUS_MAP = {
    "ready": "Ready",
    "in_progress": "In progress",
    "in_review": "In review",
    "done": "Done",
}


class CIStatus:
    GREEN = "green"
    RED = "red"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass
class IssueSummary:
    number: int
    title: str = ""
    body: str = ""
    labels: List[str] = field(default_factory=list)
    state: str = "open"
    url: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """Dict shape the dispatcher's triage path consumes."""
        return {"number": self.number, "title": self.title, "body": self.body,
                "labels": [{"name": n} for n in self.labels], "url": self.url}


@dataclass
class PRSummary:
    number: int
    state: str = "open"          # open | merged | closed
    head_branch: str = ""
    title: str = ""
    body: str = ""
    url: str = ""
    head_sha: str = ""


@dataclass
class Comment:
    id: str = ""
    body: str = ""
    author: str = ""
    created_at: str = ""


@dataclass
class BoardSummary:
    id: str
    number: int = 0
    title: str = ""


@dataclass
class FieldOption:
    id: str
    name: str
    color: str = ""
    description: str = ""


@dataclass
class FieldDef:
    id: str
    name: str
    options: List[FieldOption] = field(default_factory=list)


@dataclass
class LabelDef:
    name: str
    color: str = ""


_CLOSING_RE = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b")


def issue_linked_to_pr(pr: PRSummary, issue_number: int) -> bool:
    """Branch-name / closing-keyword heuristic shared by all providers.

    Matches ``…issue-<n>…`` / ``…/<n>-…`` / ``…-<n>`` head branches or a
    ``Closes/Fixes/Resolves #<n>`` body reference.
    """
    n = str(issue_number)
    head = pr.head_branch or ""
    body = pr.body or ""
    return (f"issue-{n}" in head or f"/{n}-" in head or head.endswith(f"-{n}")
            or any(m.group(1) == n for m in _CLOSING_RE.finditer(body)))


def ensure_closing_keyword(body: str, issue_number: int) -> str:
    """Return ``body`` guaranteed to contain a GitHub auto-closing keyword for
    ``issue_number``.  If the body already has one, it is returned unchanged.
    Otherwise ``Closes #<n>`` is prepended as the first line so GitHub's merge
    hook picks it up regardless of where it appears in a long body.
    """
    if any(m.group(1) == str(issue_number) for m in _CLOSING_RE.finditer(body or "")):
        return body
    prefix = f"Closes #{issue_number}\n\n"
    return prefix + (body or "")


class VCSProvider(abc.ABC):
    """Abstract VCS/issue-tracker provider, constructed from a resolved
    per-project config dict (the deep-merged ``.hermes/daedalus.yaml``)."""

    name: str = "base"
    supports_boards: bool = False
    supports_ci_status: bool = False
    supports_pr_comments: bool = False
    supports_labels: bool = False
    supports_branches: bool = False

    def __init__(self, resolved: Dict[str, Any]):
        self._cfg = resolved or {}
        vcs = self._cfg.get("vcs") or {}
        self._status_map: Dict[str, str] = {**DEFAULT_STATUS_MAP, **(vcs.get("status_map") or {})}
        self._log = logging.getLogger(f"daedalus.providers.{self.name}")

    # ── status mapping ───────────────────────────────────────────────────────
    def status_name(self, canonical: str) -> str:
        """Provider-facing name for a canonical status key (ready/in_progress/…)."""
        return self._status_map.get(canonical, canonical)

    # ── issues ───────────────────────────────────────────────────────────────
    @abc.abstractmethod
    def list_issues(self, state: str = "open", labels: Optional[List[str]] = None,
                    limit: int = 50) -> List[IssueSummary]: ...

    @abc.abstractmethod
    def close_issue(self, issue_number: int) -> bool: ...

    def get_issue_state(self, issue_number: int) -> Optional[str]:
        """Return 'open', 'closed', or None if unknown/error. Providers override for efficiency."""
        return None

    # ── pull/merge requests ──────────────────────────────────────────────────
    @abc.abstractmethod
    def list_prs(self, state: str = "all", limit: int = 50) -> List[PRSummary]: ...

    def find_pr_for_branch(self, branch: str) -> Optional[int]:
        """Open PR number whose head is ``branch``, or None."""
        if not branch:
            return None
        for pr in self.list_prs(state="open"):
            if pr.head_branch == branch:
                return pr.number
        return None

    def _pr_for_issue(self, issue_number: int) -> Optional[PRSummary]:
        """Best PR referencing an issue — prefers merged over open."""
        open_pr: Optional[PRSummary] = None
        for pr in self.list_prs(state="all"):
            if not issue_linked_to_pr(pr, issue_number):
                continue
            if pr.state == "merged":
                return pr
            if pr.state == "open" and open_pr is None:
                open_pr = pr
        return open_pr

    def pr_state_for_issue(self, issue_number: int) -> Optional[str]:
        pr = self._pr_for_issue(issue_number)
        return pr.state if pr else None

    def pr_number_for_issue(self, issue_number: int) -> Optional[int]:
        pr = self._pr_for_issue(issue_number)
        return pr.number if pr else None

    # ── CI status ────────────────────────────────────────────────────────────
    def get_pr_ci_status(self, pr_number: int) -> str:
        return CIStatus.UNKNOWN

    def pr_ci_green(self, pr_number: int) -> bool:
        return self.get_pr_ci_status(pr_number) == CIStatus.GREEN

    # ── PR comments / delivery markers ───────────────────────────────────────
    def list_pr_comments(self, pr_number: int) -> List[Comment]:
        return []

    def post_pr_comment(self, pr_number: int, body: str) -> bool:
        return False

    def update_pr_body(self, pr_number: int, body: str) -> bool:
        """Overwrite the PR body. Returns True on success, False if unsupported/failed."""
        return False

    # ── URL builders (for rich notification links) ───────────────────────────
    def issue_url(self, issue_number: int) -> str:
        """Canonical web URL for an issue/work-item. Returns '' if not known."""
        return ""

    def pr_url(self, pr_number: int) -> str:
        """Canonical web URL for a PR/MR. Returns '' if not known."""
        return ""

    @property
    def display_repo(self) -> str:
        """Human-readable repo identifier for notifications."""
        return self._cfg.get("repo") or ""

    def pr_has_delivery_marker(self, pr_number: int) -> bool:
        return any(DELIVERY_MARKER in (c.body or "") for c in self.list_pr_comments(pr_number))

    def post_delivery_marker(self, pr_number: int, report_body: str = "") -> bool:
        return self.post_pr_comment(
            pr_number, f"{DELIVERY_MARKER}\n\nDelivered:\n\n{report_body}")

    # ── board / project tracking (high-level; provider caches its own meta) ──
    def board_configured(self) -> bool:
        """True when this project has a usable board configured."""
        return False

    def board_numbers_with_statuses(self, status_names: List[str]) -> set:
        """Issue numbers whose board status is in ``status_names`` (one call)."""
        return set()

    def board_set_status(self, issue_number: int, status_name: str) -> bool:
        """Move an issue's board card to ``status_name``. False on any failure."""
        return False

    def board_ensure_status_option(self, status_name: str, color: str = "RED") -> bool:
        """Create ``status_name`` as a board status option if it doesn't exist yet."""
        return False

    def reconcile_board_status(self, issue_number: int) -> Optional[str]:
        """Set card status from PR state: open → in_review, merged → done.

        Returns the canonical status applied, or None.
        """
        state = self.pr_state_for_issue(issue_number)
        if state == "merged":
            name = self.status_name("done")
            return "done" if self.board_set_status(issue_number, name) else None
        if state == "open":
            name = self.status_name("in_review")
            return "in_review" if self.board_set_status(issue_number, name) else None
        return None

    # ── meta (dashboard pickers) ─────────────────────────────────────────────
    def list_branches(self) -> List[str]:
        return []

    def list_labels(self) -> List[LabelDef]:
        return []

    def list_boards(self) -> List[BoardSummary]:
        return []

    def get_board_fields(self, board_id: str) -> List[FieldDef]:
        return []

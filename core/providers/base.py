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

# Labels that Daedalus requires in every VCS repo it manages.
# Providers create these on first dispatch via ensure_labels().
REQUIRED_LABELS: List[Dict[str, str]] = [
    {"name": "epic",    "color": "7057ff",
     "description": "Large issue requiring decomposition into sub-issues"},
    {"name": "subtask", "color": "0075ca",
     "description": "Child issue created from an epic decomposition"},
    {"name": "Ready",   "color": "a2eeef",
     "description": "Issue ready for developer work — no outstanding dependencies blockers"},
]

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


# ── Epic detection (Phase 1, issue #138) ────────────────────────────────────
#
# Heuristic: an issue is "epic-sized" if ANY of these hold:
#   • body contains >= _EPIC_CHECKLIST_MIN markdown checklist items
#     (``- [ ]`` / ``* [x]`` / ``+ [X]``, indented or not)
#   • labels include an exact match for "epic" (case-insensitive)
#   • body length >= _EPIC_BODY_SIZE_MIN chars
#
# Accepts a raw provider dict (``{"body": ..., "labels": [...]}``), an
# :class:`IssueSummary`, or any object exposing ``.body`` / ``.labels``.
# Never raises — missing/None fields default to False.

_EPIC_CHECKLIST_MIN = 4
_EPIC_BODY_SIZE_MIN = 2000
_CHECKLIST_LINE_RE = re.compile(r"^\s*[-*+]\s+\[[\sxX]\]", re.MULTILINE)


def _label_names(labels: Any) -> List[str]:
    """Extract label names from either list-of-dicts or list-of-strings shape."""
    if not labels:
        return []
    out: List[str] = []
    for item in labels:
        if isinstance(item, dict):
            name = item.get("name")
        else:
            name = getattr(item, "name", None) if not isinstance(item, str) else item
        if name:
            out.append(str(name))
    return out


def is_epic(issue: Any, epic_config: Optional[Dict[str, Any]] = None) -> bool:
    """Return True if ``issue`` looks epic-sized per the heuristics above.
    
    When ``epic_config`` is provided (from execution.epic_detection), its values
    override the defaults. Otherwise uses _EPIC_CHECKLIST_MIN and _EPIC_BODY_SIZE_MIN.
    
    The config can disable epic detection entirely by setting ``enabled=False``.
    """
    if issue is None:
        return False
    
    # Resolve thresholds from config or use constants
    if epic_config:
        # Check if epic detection is enabled (defaults to True)
        enabled = epic_config.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in ("true", "1", "yes", "on")
        if not enabled:
            return False
        
        min_checklist = int(epic_config.get("min_deliverables", 4))
        min_body_size = int(epic_config.get("size_threshold", 2000))
        epic_label = str(epic_config.get("epic_label", "epic"))
        child_label = str(epic_config.get("child_label", "subtask"))
    else:
        min_checklist = _EPIC_CHECKLIST_MIN
        min_body_size = _EPIC_BODY_SIZE_MIN
        epic_label = "epic"
        child_label = "subtask"
    
    # Accept both IssueSummary / dataclass and raw provider dict.
    if isinstance(issue, dict):
        body = issue.get("body") or ""
        labels = issue.get("labels") or []
    else:
        body = getattr(issue, "body", None) or ""
        labels = getattr(issue, "labels", None) or []

    # Sub-issues are never themselves epics — prevents infinite decomposition loops.
    if child_label in {n.lower() for n in _label_names(labels)}:
        return False

    # Heuristic 1: checklist density
    if len(_CHECKLIST_LINE_RE.findall(body)) >= min_checklist:
        return True

    # Heuristic 2: epic label (exact, case-insensitive)
    if any(name.lower() == epic_label for name in _label_names(labels)):
        return True

    # Heuristic 3: body size
    if len(body) >= min_body_size:
        return True

    return False


@dataclass
class PRSummary:
    number: int
    state: str = "open"          # open | merged | closed
    head_branch: str = ""
    base_branch: str = ""        # target branch the PR merges INTO
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

# Portable cross-issue dependency convention: a ``Depends on: #N, #M`` (or
# ``Depends-on:`` / ``Blocked by:``) line anywhere in an issue body. Used as the
# provider-agnostic fallback for dependency-aware ready-gating (issue #139) when
# native issue links aren't present. Leading list/quote markers are tolerated so
# it works inside markdown bullets.
_DEPENDS_RE = re.compile(r"(?im)^[ \t>*\-]*(?:depends[ _-]on|blocked[ _-]by)\s*:?[ \t]*(.+)$")
_ISSUE_REF_RE = re.compile(r"#(\d+)")


def parse_depends_on(body: str) -> List[int]:
    """Issue numbers referenced by a ``Depends on:``/``Blocked by:`` line.

    Order-preserving and de-duplicated; returns ``[]`` when the convention is
    absent. Numbers are returned as declared — the caller is responsible for
    filtering to those that are still open.
    """
    out: List[int] = []
    seen = set()
    for line in _DEPENDS_RE.finditer(body or ""):
        for ref in _ISSUE_REF_RE.findall(line.group(1)):
            n = int(ref)
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


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

    def get_issue(self, issue_number: int) -> Optional[IssueSummary]:
        """Fetch a single issue by number. Returns None if not found or on error.

        Providers override for direct single-issue fetch; default scans list_issues
        which may be expensive or miss issues outside the default window.
        """
        return None

    def create_issue(self, title: str, body: str,
                     labels: Optional[List[str]] = None) -> Optional[int]:
        """Create a new issue. Returns the issue number on success, None on failure.

        Providers that support issue creation override this; default is a no-op.
        """
        return None

    def add_label(self, issue_number: int, label_name: str) -> bool:
        """Apply a label to an issue. Returns True on success. Default no-op."""
        return False

    def has_label(self, issue_number: int, label_name: str) -> bool:
        """Return True if ``issue_number`` has ``label_name`` applied.

        Base implementation returns False. Providers override to query the
        VCS API. Never raises — returns False on any provider error.
        """
        return False

    def sub_issues_of(self, epic_number: int) -> List[int]:
        """Return issue numbers that are sub-issues of the given epic.

        Base implementation scans all open issues for epic-reference conventions
        in the body (portable fallback). Recognised formats (case-insensitive):
        ``Epic: #N``, ``Epic #N``, ``Part of: #N``, ``Part of #N``,
        ``Part of epic: #N``, ``Part of epic #N``, ``Part-of #N``,
        ``Part-of-epic #N``. Providers with native sub-issue links override to
        use the VCS API first.
        Never raises — returns ``[]`` on any provider error.
        """
        import re as _re
        # Aligned with EPIC_REF_RE in core/tier_promotion.py so both code paths
        # agree on what counts as a parent-epic reference.
        pattern = _re.compile(
            rf"(?im)^(?:part[\s-]+of(?:[\s-]+epic)?|epic)\s*:?\s*#{epic_number}\b"
        )
        results: List[int] = []
        try:
            all_issues = self.list_issues(state="open")
        except Exception:
            return []
        for issue in all_issues:
            body = ""
            if isinstance(issue, dict):
                body = issue.get("body", "")
            else:
                body = getattr(issue, "body", "") or ""
            if pattern.search(body):
                n = issue.get("number") if isinstance(issue, dict) else getattr(issue, "number", None)
                if n is not None:
                    results.append(int(n))
        return results

    def ensure_labels(self) -> List[str]:
        """Create required Daedalus labels if missing. Returns newly created names.

        Called once per dispatch run so every managed repo always has the labels
        Daedalus needs (epic, subtask, …). Provider-specific implementations also
        create board-lane labels (e.g. GitLab's label-driven board columns).
        Default is a no-op — safe for providers that don't need label pre-creation
        (e.g. Azure DevOps where tags are free-text on work items).
        """
        return []

    # ── cross-issue dependencies (ready-gating, issue #139) ──────────────────
    def _depends_on_blockers(self, issue_number: int,
                             *, body: Optional[str] = None) -> List[int]:
        """Open blockers from the portable ``Depends on:`` body convention.

        Fetches the issue body when not supplied, parses the convention, and
        keeps only references that are still **open**. Providers with native
        dependency links call this to merge the fallback with their link-derived
        blockers. An unknown blocker state (provider returned ``None``) is
        treated as not-blocking so a dependent is never permanently wedged on an
        unresolvable reference.
        """
        if body is None:
            issue = self.get_issue(issue_number)
            body = issue.body if issue else ""
        return [n for n in parse_depends_on(body)
                if self.get_issue_state(n) == "open"]

    def blockers(self, issue_number: int) -> List[int]:
        """Open issue numbers that block ``issue_number`` (``[]`` when unblocked).

        The dispatcher refuses to start new work on an issue while this is
        non-empty, re-checking each tick so a dependent auto-unblocks once its
        blockers close. The base implementation parses the portable
        ``Depends on: #N, #M`` body convention; providers override to add native
        dependency links (GitLab issue links, Azure predecessors, …) merged with
        this fallback. Never raises — degrades to ``[]`` on any provider error.
        """
        return self._depends_on_blockers(issue_number)

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

    def is_pr_open(self, pr_number: int) -> bool:
        """True iff ``pr_number`` is a currently-open PR (#953).

        Source of truth for the dispatcher's pre-QA gate: a developer card
        must not advance (and release its QA child) on a "PR #N" string that
        does not correspond to a real, open PR. Scans the open PR list so any
        provider that implements ``list_prs`` gets it for free.
        """
        if not pr_number:
            return False
        return any(pr.number == pr_number for pr in self.list_prs(state="open"))

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

    def get_pr_files(self, pr_number: int) -> List[Dict[str, Any]]:
        """Changed files in a PR. Returns [{filename, additions, deletions, changes, status}].
        Providers that support it override; defaults to [] (safe no-op)."""
        return []

    def post_issue_comment(self, issue_number: int, body: str) -> bool:
        """Post a comment on an issue (distinct from PR comments). Returns True on success."""
        return False

    def get_issue_comments(self, issue_number: int) -> List[Dict[str, Any]]:
        """Return issue comments as dicts with at least 'body' and 'user' keys.
        Defaults to [] — providers that support it override."""
        return []

    def merge_pr(self, pr_number: int, merge_method: str = "squash") -> bool:
        """Merge a PR via the VCS API.

        ``merge_method`` is one of 'merge', 'squash', 'rebase'.
        Returns True on success, False if unsupported or the merge failed.
        Providers that support it override; default is a no-op so callers can
        safely call this without checking provider type.
        """
        return False

    def append_changelog(self, base_branch: str, entry: str) -> bool:
        """Prepend ``entry`` to CHANGELOG.md on ``base_branch`` via the VCS API.
        Returns True on success; defaults to False (providers that support it override)."""
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

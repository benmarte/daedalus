"""GitLab provider — REST /api/v4 for issues, merge requests, pipelines,
notes, branches, and labels. Supports self-hosted via ``vcs.base_url``.

Boards: GitLab Issue Boards are label-driven, so "board status" maps to
scoped status labels (``vcs.status_map`` values). Enable with
``tracking.label_board: true`` — moving a card adds the target status label
and removes the other status labels, which moves it between board lists.

Auth: PAT via env (``vcs.token_env`` → GITLAB_TOKEN), ``api`` scope,
sent as the PRIVATE-TOKEN header.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .base import (CIStatus, Comment, IssueSummary, LabelDef, PRSummary,
                   ProviderConfigError, VCSProvider, resolve_token)
from .http import HTTPClient, ProviderError

_MR_STATE = {"opened": "open", "merged": "merged", "closed": "closed", "locked": "open"}

# Allowlist for path_with_namespace values returned by the GitLab API.
_WEB_PATH_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.\-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.\-]*)+$')

# Default color for auto-created board status labels (GitLab requires a color on
# label creation). A neutral blue — users can recolor in the GitLab UI.
_STATUS_LABEL_COLOR = "#6699cc"


class GitLabProvider(VCSProvider):
    name = "gitlab"
    supports_ci_status = True
    supports_pr_comments = True
    supports_labels = True
    supports_branches = True

    def __init__(self, resolved: Dict[str, Any]):
        super().__init__(resolved)
        vcs = self._cfg.get("vcs") or {}
        project_id = vcs.get("project_id")
        project_path = (vcs.get("project_path") or self._cfg.get("repo") or "").strip()
        if project_id:
            self._project = str(project_id)
        elif "/" in project_path:
            self._project = quote(project_path, safe="")
        else:
            raise ProviderConfigError(
                "gitlab provider requires vcs.project_id or vcs.project_path (\"group/project\")")
        base_url = (vcs.get("base_url") or "https://gitlab.com").rstrip("/")
        token = resolve_token(self._cfg, ("GITLAB_TOKEN",))
        headers = {"PRIVATE-TOKEN": token} if token else {}
        if not token:
            self._log.warning("no GitLab token in env (vcs.token_env/GITLAB_TOKEN) — "
                              "API calls limited to public, unauthenticated access")
        self._http = HTTPClient(f"{base_url}/api/v4", headers, token=token,
                                 verify_ssl=vcs.get("verify_ssl", True))
        self.supports_boards = bool((self._cfg.get("tracking") or {}).get("label_board"))
        self._web_base = base_url
        # None = "not yet fetched"; "" = "fetched but unavailable". Distinguishing
        # the two prevents repeated API retries after a permanent failure.
        self._project_web_path: Optional[str] = (
            project_path if "/" in project_path else None
        )

    @property
    def _proj(self) -> str:
        return f"/projects/{self._project}"

    # ── issues ───────────────────────────────────────────────────────────────
    def list_issues(self, state: str = "open", labels: Optional[List[str]] = None,
                    limit: int = 50) -> List[IssueSummary]:
        """ANY-label semantics: one call per label, deduped (GitHub parity)."""
        gl_state = {"open": "opened", "closed": "closed"}.get(state)
        label_sets = [[l] for l in (labels or []) if l] or [[]]
        seen: Dict[int, IssueSummary] = {}
        for ls in label_sets:
            params: Dict[str, Any] = {"per_page": min(limit, 100)}
            if gl_state:
                params["state"] = gl_state
            if ls:
                params["labels"] = ",".join(ls)
            try:
                data = self._http.get_json(f"{self._proj}/issues", params=params)
            except ProviderError as e:
                self._log.warning("list_issues failed: %s", e)
                continue
            for it in data or []:
                iid = it.get("iid")
                if isinstance(iid, int) and iid not in seen:
                    seen[iid] = IssueSummary(
                        number=iid, title=it.get("title") or "",
                        body=it.get("description") or "",
                        labels=list(it.get("labels") or []),
                        state="open" if (it.get("state") == "opened") else (it.get("state") or ""),
                        url=it.get("web_url") or "")
        return list(seen.values())[:limit]

    def close_issue(self, issue_number: int) -> bool:
        try:
            self._http.put_json(f"{self._proj}/issues/{issue_number}",
                                {"state_event": "close"})
        except ProviderError as e:
            self._log.warning("close_issue #%s failed: %s", issue_number, e)
            return False
        self._log.info("close_issue: closed #%s", issue_number)
        return True

    def create_issue(self, title: str, body: str,
                     labels: Optional[List[str]] = None) -> Optional[int]:
        payload: Dict[str, Any] = {"title": title, "description": body}
        if labels:
            payload["labels"] = ",".join(labels)
        try:
            data = self._http.post_json(f"{self._proj}/issues", payload)
            iid = (data or {}).get("iid")
            if isinstance(iid, int):
                self._log.info("create_issue: created !%s %r", iid, title[:60])
                return iid
        except ProviderError as e:
            self._log.warning("create_issue failed: %s", e)
        return None

    def get_issue_state(self, issue_number: int) -> Optional[str]:
        try:
            data = self._http.get_json(f"{self._proj}/issues/{issue_number}")
            state = (data.get("state") or "opened").lower()
            return "closed" if state == "closed" else "open"
        except ProviderError as e:
            if e.status_code == 404:
                return "closed"
            return None

    def get_issue(self, issue_number: int) -> Optional[IssueSummary]:
        try:
            it = self._http.get_json(f"{self._proj}/issues/{issue_number}")
        except ProviderError as e:
            self._log.warning("get_issue #%s failed: %s", issue_number, e)
            return None
        if not it:
            return None
        return IssueSummary(
            number=it.get("iid", issue_number), title=it.get("title") or "",
            body=it.get("description") or "",
            labels=list(it.get("labels") or []),
            state="open" if (it.get("state") == "opened") else (it.get("state") or ""),
            url=it.get("web_url") or "")

    def blockers(self, issue_number: int) -> List[int]:
        """Open blockers via native issue links (``link_type: is_blocked_by``)
        merged with the portable ``Depends on:`` body fallback.

        The links endpoint returns each linked issue with its ``state`` inline,
        so open-filtering needs no extra request.
        """
        out: List[int] = []
        try:
            links = self._http.get_json(f"{self._proj}/issues/{issue_number}/links")
        except ProviderError as e:
            self._log.warning("blockers #%s links failed: %s", issue_number, e)
            links = []
        for it in links or []:
            if (it.get("link_type") or "") != "is_blocked_by":
                continue
            iid = it.get("iid")
            if isinstance(iid, int) and (it.get("state") or "").lower() == "opened":
                out.append(iid)
        for n in self._depends_on_blockers(issue_number):
            if n not in out:
                out.append(n)
        return out

    # ── merge requests ───────────────────────────────────────────────────────
    def list_prs(self, state: str = "all", limit: int = 50) -> List[PRSummary]:
        gl_state = {"open": "opened", "merged": "merged", "closed": "closed"}.get(state)
        params: Dict[str, Any] = {"per_page": min(limit, 100)}
        if gl_state:
            params["state"] = gl_state
        try:
            data = self._http.get_json(f"{self._proj}/merge_requests", params=params)
        except ProviderError as e:
            self._log.warning("list_prs failed: %s", e)
            return []
        out: List[PRSummary] = []
        for mr in data or []:
            out.append(PRSummary(
                number=mr.get("iid"),
                state=_MR_STATE.get((mr.get("state") or "").lower(), mr.get("state") or ""),
                head_branch=mr.get("source_branch") or "",
                title=mr.get("title") or "",
                body=mr.get("description") or "",
                url=mr.get("web_url") or "",
                head_sha=mr.get("sha") or ""))
        return out[:limit]

    def find_pr_for_branch(self, branch: str) -> Optional[int]:
        if not branch:
            return None
        try:
            data = self._http.get_json(f"{self._proj}/merge_requests",
                                       params={"source_branch": branch, "state": "opened"})
        except ProviderError as e:
            self._log.warning("find_pr_for_branch failed: %s", e)
            return None
        for mr in data or []:
            if isinstance(mr.get("iid"), int):
                return mr["iid"]
        return None

    # ── CI (MR head pipeline) ────────────────────────────────────────────────
    def get_pr_ci_status(self, pr_number: int) -> str:
        try:
            data = self._http.get_json(f"{self._proj}/merge_requests/{pr_number}/pipelines",
                                       params={"per_page": 1})
        except ProviderError as e:
            self._log.warning("get_pr_ci_status MR !%s failed: %s", pr_number, e)
            return CIStatus.UNKNOWN
        if not data:
            return CIStatus.UNKNOWN
        status = (data[0].get("status") or "").lower()
        if status == "success":
            return CIStatus.GREEN
        if status in ("failed", "canceled"):
            return CIStatus.RED
        if status in ("running", "pending", "created", "waiting_for_resource",
                      "preparing", "scheduled", "manual"):
            return CIStatus.PENDING
        return CIStatus.UNKNOWN

    # ── MR notes (comments) ──────────────────────────────────────────────────
    def list_pr_comments(self, pr_number: int) -> List[Comment]:
        try:
            data = self._http.get_paginated(
                f"{self._proj}/merge_requests/{pr_number}/notes",
                params={"sort": "asc"}, style="x_next_page", max_pages=3)
        except ProviderError as e:
            self._log.warning("list_pr_comments MR !%s failed: %s", pr_number, e)
            return []
        return [Comment(id=str(n.get("id") or ""), body=n.get("body") or "",
                        author=((n.get("author") or {}).get("username") or ""),
                        created_at=n.get("created_at") or "")
                for n in data or []]

    def post_pr_comment(self, pr_number: int, body: str) -> bool:
        try:
            self._http.post_json(f"{self._proj}/merge_requests/{pr_number}/notes",
                                 {"body": body})
        except ProviderError as e:
            self._log.warning("post_pr_comment MR !%s failed: %s", pr_number, e)
            return False
        return True

    # ── label-driven board ───────────────────────────────────────────────────
    def board_configured(self) -> bool:
        return self.supports_boards

    def board_numbers_with_statuses(self, status_names: List[str]) -> set:
        if not self.board_configured():
            return set()
        out: set = set()
        for name in status_names:
            for issue in self.list_issues(state="open", labels=[name], limit=100):
                out.add(issue.number)
        return out

    def board_set_status(self, issue_number: int, status_name: str) -> bool:
        """Add the target status label; remove the other status_map labels.

        Moving the label moves the issue between GitLab Issue Board lists.
        """
        if not self.board_configured():
            return False
        others = [v for v in self._status_map.values() if v and v != status_name]
        try:
            self._http.put_json(f"{self._proj}/issues/{issue_number}",
                                {"add_labels": status_name,
                                 "remove_labels": ",".join(others)})
        except ProviderError as e:
            self._log.warning("board_set_status #%s -> %s failed: %s",
                              issue_number, status_name, e)
            return False
        self._log.info("board: #%s -> %s", issue_number, status_name)
        return True

    def ensure_labels(self) -> List[str]:
        """Create required Daedalus labels (epic, subtask) plus all board lane labels."""
        from .base import REQUIRED_LABELS
        created: List[str] = []
        existing = {lbl.name for lbl in self.list_labels()}
        for ldef in REQUIRED_LABELS:
            if ldef["name"] in existing:
                continue
            try:
                self._http.post_json(
                    f"{self._proj}/labels",
                    {"name": ldef["name"], "color": f"#{ldef['color']}"},
                )
                created.append(ldef["name"])
                self._log.info("ensure_labels: created %r", ldef["name"])
            except ProviderError as e:
                if e.status_code == 409:
                    continue  # already exists — idempotent
                self._log.warning("ensure_labels: create %r failed: %s", ldef["name"], e)
        # Also ensure board lane labels (GitLab boards are label-driven)
        status_names = [v for v in self._status_map.values() if v]
        created.extend(self.ensure_status_labels(status_names))
        return created

    def ensure_status_labels(self, status_names: List[str]) -> List[str]:
        """Create any missing board status labels in the project (idempotent).

        Guarantees the Issue Board lists keyed to ``status_map`` exist so
        ready-gating has something to match. A 409 (label already exists, e.g.
        a concurrent tick or a case-insensitive collision) is treated as
        success. Returns the names that were newly created.
        """
        created: List[str] = []
        existing = {label.name for label in self.list_labels()}
        for name in status_names:
            if not name or name in existing:
                continue
            try:
                self._http.post_json(f"{self._proj}/labels",
                                     {"name": name, "color": _STATUS_LABEL_COLOR})
                created.append(name)
            except ProviderError as e:
                if e.status_code == 409:
                    continue  # already exists — idempotent
                self._log.warning("ensure_status_labels: create %r failed: %s", name, e)
        if created:
            self._log.info("ensure_status_labels: created %s", ", ".join(created))
        return created

    # ── URL builders ─────────────────────────────────────────────────────────
    def _resolve_web_path(self) -> str:
        """Return path_with_namespace, fetching it once when only project_id is set.

        When the project is configured with a numeric ``vcs.project_id`` the
        web path is unknown at init time.  This method fetches the project's
        ``path_with_namespace`` from the API on first call and caches the result.
        Failures are also cached (as ``""``) so a permanent error (bad token,
        wrong ID) does not cause a fresh API call on every URL request.
        """
        if self._project_web_path is None:
            raw = ""
            try:
                data = self._http.get_json(self._proj)
                raw = (data or {}).get("path_with_namespace") or ""
            except ProviderError as e:
                self._log.warning("_resolve_web_path failed: %s", e)
            if raw and _WEB_PATH_RE.match(raw):
                self._project_web_path = raw
            else:
                if raw:
                    self._log.warning("_resolve_web_path: unexpected path_with_namespace %r", raw)
                self._project_web_path = ""  # cache the failure — don't retry
        return self._project_web_path

    def issue_url(self, issue_number: int) -> str:
        path = self._resolve_web_path()
        if not path:
            return ""
        return f"{self._web_base}/{path}/-/issues/{issue_number}"

    def pr_url(self, pr_number: int) -> str:
        path = self._resolve_web_path()
        if not path:
            return ""
        return f"{self._web_base}/{path}/-/merge_requests/{pr_number}"

    @property
    def display_repo(self) -> str:
        return self._project_web_path or self._cfg.get("repo") or ""

    # ── meta ─────────────────────────────────────────────────────────────────
    def get_default_branch(self) -> Optional[str]:
        """The project's default branch (GET /projects/:id), or None on error."""
        try:
            data = self._http.get_json(self._proj)
        except ProviderError as e:
            self._log.warning("get_default_branch failed: %s", e)
            return None
        return (data or {}).get("default_branch") or None

    def list_branches(self) -> List[str]:
        try:
            data = self._http.get_paginated(f"{self._proj}/repository/branches",
                                            style="x_next_page", max_pages=2)
        except ProviderError as e:
            self._log.warning("list_branches failed: %s", e)
            return []
        return [b.get("name") or "" for b in data or [] if b.get("name")]

    def list_labels(self) -> List[LabelDef]:
        try:
            # include_ancestor_groups returns group-level labels too, not just project-level.
            data = self._http.get_paginated(f"{self._proj}/labels",
                                            params={"include_ancestor_groups": "true"},
                                            style="x_next_page", max_pages=5)
        except ProviderError as e:
            self._log.warning("list_labels failed: %s", e)
            return []
        return [LabelDef(name=l.get("name") or "", color=(l.get("color") or "").lstrip("#"))
                for l in data or [] if l.get("name")]

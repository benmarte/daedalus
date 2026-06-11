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

from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .base import (CIStatus, Comment, IssueSummary, LabelDef, PRSummary,
                   ProviderConfigError, VCSProvider, resolve_token)
from .http import HTTPClient, ProviderError

_MR_STATE = {"opened": "open", "merged": "merged", "closed": "closed", "locked": "open"}


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
        self._http = HTTPClient(f"{base_url}/api/v4", headers, token=token)
        self.supports_boards = bool((self._cfg.get("tracking") or {}).get("label_board"))

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

    # ── meta ─────────────────────────────────────────────────────────────────
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
            data = self._http.get_paginated(f"{self._proj}/labels",
                                            style="x_next_page", max_pages=2)
        except ProviderError as e:
            self._log.warning("list_labels failed: %s", e)
            return []
        return [LabelDef(name=l.get("name") or "", color=(l.get("color") or "").lstrip("#"))
                for l in data or [] if l.get("name")]

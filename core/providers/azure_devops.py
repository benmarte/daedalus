"""Azure DevOps provider — REST (WIQL work items, pull requests, PR statuses,
threads, refs) against ``https://dev.azure.com/{org}``.

Mapping: "issues" are Work Items of type ``vcs.work_item_type`` (default
"Issue"); "board status" maps to the work item's State (board columns map to
states on default Azure Boards), via ``vcs.status_map`` values.

Auth: PAT via env (``vcs.token_env`` → AZURE_DEVOPS_PAT), sent as Basic auth
``base64(":" + pat)``. Minimal scopes: Work Items (Read & Write),
Code (Read), Pull Requests (Read & Write), Build (Read).
"""
from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

from .base import (CIStatus, Comment, IssueSummary, LabelDef, PRSummary,
                   ProviderConfigError, VCSProvider, resolve_token)
from .http import HTTPClient, ProviderError

_API = {"api-version": "7.1"}
_PR_STATE = {"active": "open", "completed": "merged", "abandoned": "closed"}


class AzureDevOpsProvider(VCSProvider):
    name = "azuredevops"
    supports_boards = True
    supports_ci_status = True
    supports_pr_comments = True
    supports_labels = True
    supports_branches = True

    def __init__(self, resolved: dict[str, Any]):
        super().__init__(resolved)
        vcs = self._cfg.get("vcs") or {}
        org = (vcs.get("org") or "").strip()
        project = (vcs.get("project") or "").strip()
        repo = (vcs.get("repo") or "").strip()
        if not (org and project and repo):
            raise ProviderConfigError(
                "azuredevops provider requires vcs.org, vcs.project and vcs.repo")
        self.org, self.project, self.repo = org, project, repo
        self.work_item_type = (vcs.get("work_item_type") or "Issue").strip()
        token = resolve_token(self._cfg, ("AZURE_DEVOPS_PAT", "AZURE_DEVOPS_TOKEN"))
        headers = {}
        if token:
            basic = base64.b64encode(f":{token}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        else:
            self._log.warning("no Azure DevOps PAT in env "
                              "(vcs.token_env/AZURE_DEVOPS_PAT) — API calls will fail")
        self._http = HTTPClient(f"https://dev.azure.com/{quote(org)}", headers, token=token,
                                 verify_ssl=vcs.get("verify_ssl", True))
        self._pproj = f"/{quote(project)}"
        self._prepo = f"{self._pproj}/_apis/git/repositories/{quote(repo)}"

    # ── work items (issues) ──────────────────────────────────────────────────
    def _wiql(self, query: str) -> list[int]:
        try:
            data = self._http.post_json(f"{self._pproj}/_apis/wit/wiql?api-version=7.1",
                                        {"query": query})
        except ProviderError as e:
            self._log.warning("wiql failed: %s", e)
            return []
        return [wi.get("id") for wi in (data or {}).get("workItems") or []
                if isinstance(wi.get("id"), int)]

    def _work_items(self, ids: list[int]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(0, len(ids), 200):
            batch = ",".join(str(x) for x in ids[i:i + 200])
            try:
                data = self._http.get_json("/_apis/wit/workitems",
                                           params={"ids": batch, **_API})
            except ProviderError as e:
                self._log.warning("workitems fetch failed: %s", e)
                continue
            out.extend((data or {}).get("value") or [])
        return out

    @staticmethod
    def _tags(fields: dict[str, Any]) -> list[str]:
        return [t.strip() for t in (fields.get("System.Tags") or "").split(";") if t.strip()]

    def list_issues(self, state: str = "open", labels: list[str] | None = None,
                    limit: int = 50) -> list[IssueSummary]:
        cond = [f"[System.TeamProject] = '{self.project}'",
                f"[System.WorkItemType] = '{self.work_item_type}'"]
        if state == "open":
            cond.append("[System.State] NOT IN ('Closed', 'Done', 'Removed', 'Resolved')")
        elif state == "closed":
            cond.append("[System.State] IN ('Closed', 'Done', 'Resolved')")
        if labels:
            tag_conds = [f"[System.Tags] CONTAINS '{t}'" for t in labels if t]
            if tag_conds:
                cond.append("(" + " OR ".join(tag_conds) + ")")
        ids = self._wiql("SELECT [System.Id] FROM WorkItems WHERE " + " AND ".join(cond)
                         + " ORDER BY [System.ChangedDate] DESC")[:limit]
        out: list[IssueSummary] = []
        for wi in self._work_items(ids):
            fields = wi.get("fields") or {}
            out.append(IssueSummary(
                number=wi.get("id"), title=fields.get("System.Title") or "",
                body=fields.get("System.Description") or "",
                labels=self._tags(fields),
                state=(fields.get("System.State") or "").lower(),
                url=(wi.get("_links") or {}).get("html", {}).get("href") or ""))
        return out

    def _set_state(self, work_item_id: int, state: str) -> bool:
        patch = [{"op": "add", "path": "/fields/System.State", "value": state}]
        try:
            self._http.patch_json(f"{self._pproj}/_apis/wit/workitems/{work_item_id}?api-version=7.1",
                                  patch, content_type="application/json-patch+json")
        except ProviderError as e:
            self._log.warning("set_state #%s -> %s failed: %s", work_item_id, state, e)
            return False
        return True

    def close_issue(self, issue_number: int) -> bool:
        closed_state = (self._cfg.get("vcs") or {}).get("closed_state") or "Done"
        ok = self._set_state(issue_number, closed_state)
        if ok:
            self._log.info("close_issue: closed #%s (state %s)", issue_number, closed_state)
        return ok

    def create_issue(self, title: str, body: str,
                     labels: list[str] | None = None) -> int | None:
        patch = [
            {"op": "add", "path": "/fields/System.Title", "value": title},
            {"op": "add", "path": "/fields/System.Description", "value": body},
        ]
        if labels:
            patch.append({"op": "add", "path": "/fields/System.Tags",
                          "value": "; ".join(labels)})
        try:
            data = self._http.post_json(
                f"{self._pproj}/_apis/wit/workitems/${self.work_item_type}",
                patch,
                content_type="application/json-patch+json",
            )
            num = (data or {}).get("id")
            if isinstance(num, int):
                self._log.info("create_issue: created #%s %r", num, title[:60])
                return num
        except ProviderError as e:
            self._log.warning("create_issue failed: %s", e)
        return None

    def get_issue_state(self, issue_number: int) -> str | None:
        try:
            data = self._http.get_json(
                f"{self._pproj}/_apis/wit/workitems/{issue_number}",
                params={"fields": "System.State", **_API})
            state = (data.get("fields") or {}).get("System.State", "")
            closed_state = (self._cfg.get("vcs") or {}).get("closed_state") or "Done"
            # Accept the configured closed_state plus all standard Azure terminal states.
            terminal = {closed_state.lower(), "done", "closed", "resolved", "removed"}
            return "closed" if state.lower() in terminal else "open"
        except ProviderError:
            return None

    def get_issue(self, issue_number: int) -> IssueSummary | None:
        items = self._work_items([issue_number])
        if not items:
            return None
        wi = items[0]
        fields = wi.get("fields") or {}
        return IssueSummary(
            number=wi.get("id", issue_number), title=fields.get("System.Title") or "",
            body=fields.get("System.Description") or "",
            labels=self._tags(fields),
            state=(fields.get("System.State") or "").lower(),
            url=(wi.get("_links") or {}).get("html", {}).get("href") or "")

    @staticmethod
    def _id_from_url(url: str) -> int | None:
        """Trailing work-item id from a relation URL (…/workItems/123)."""
        tail = (url or "").rstrip("/").rsplit("/", 1)[-1]
        return int(tail) if tail.isdigit() else None

    def blockers(self, issue_number: int) -> list[int]:
        """Open blockers via work-item Predecessor links
        (``System.LinkTypes.Dependency-Reverse``) merged with the portable
        ``Depends on:`` body fallback.

        A Predecessor must complete before this item, so an open predecessor is a
        blocker. Expanding relations also returns the description, so the body
        fallback reuses it without a second fetch.
        """
        out: list[int] = []
        try:
            data = self._http.get_json(
                f"{self._pproj}/_apis/wit/workitems/{issue_number}",
                params={"$expand": "relations", **_API})
        except ProviderError as e:
            self._log.warning("blockers #%s relations failed: %s", issue_number, e)
            data = {}
        body = (data.get("fields") or {}).get("System.Description") or ""
        for rel in (data or {}).get("relations") or []:
            if (rel.get("rel") or "") != "System.LinkTypes.Dependency-Reverse":
                continue
            pid = self._id_from_url(rel.get("url") or "")
            if pid and self.get_issue_state(pid) == "open":
                out.append(pid)
        for n in self._depends_on_blockers(issue_number, body=body):
            if n not in out:
                out.append(n)
        return out

    # ── pull requests ────────────────────────────────────────────────────────
    def list_prs(self, state: str = "all", limit: int = 50) -> list[PRSummary]:
        status = {"open": "active", "merged": "completed", "closed": "abandoned"}.get(state, "all")
        try:
            data = self._http.get_json(f"{self._prepo}/pullrequests",
                                       params={"searchCriteria.status": status,
                                               "$top": min(limit, 100), **_API})
        except ProviderError as e:
            self._log.warning("list_prs failed: %s", e)
            return []
        out: list[PRSummary] = []
        for pr in (data or {}).get("value") or []:
            out.append(PRSummary(
                number=pr.get("pullRequestId"),
                state=_PR_STATE.get((pr.get("status") or "").lower(), pr.get("status") or ""),
                head_branch=(pr.get("sourceRefName") or "").replace("refs/heads/", ""),
                title=pr.get("title") or "",
                body=pr.get("description") or "",
                url=pr.get("url") or "",
                head_sha=((pr.get("lastMergeSourceCommit") or {}).get("commitId") or "")))
        return out[:limit]

    # ── CI (PR statuses) ─────────────────────────────────────────────────────
    def get_pr_ci_status(self, pr_number: int) -> str:
        try:
            data = self._http.get_json(f"{self._prepo}/pullrequests/{pr_number}/statuses",
                                       params=_API)
        except ProviderError as e:
            self._log.warning("get_pr_ci_status PR #%s failed: %s", pr_number, e)
            return CIStatus.UNKNOWN
        statuses = (data or {}).get("value") or []
        if not statuses:
            return CIStatus.UNKNOWN
        latest: dict[str, str] = {}
        for s in statuses:  # API returns oldest→newest; keep the latest per context
            ctx = s.get("context") or {}
            key = f"{ctx.get('genre') or ''}/{ctx.get('name') or ''}"
            latest[key] = (s.get("state") or "").lower()
        # notApplicable/notSet are neutral — they don't block or indicate failure.
        # Filter them out so they don't prevent a GREEN result or inflate PENDING.
        effective = {s for s in latest.values() if s not in ("notapplicable", "notset", "")}
        if not effective:
            return CIStatus.UNKNOWN
        if effective & {"failed", "error"}:
            return CIStatus.RED
        if effective & {"pending"}:
            return CIStatus.PENDING
        if effective <= {"succeeded"}:
            return CIStatus.GREEN
        return CIStatus.UNKNOWN

    # ── PR threads (comments) ────────────────────────────────────────────────
    def list_pr_comments(self, pr_number: int) -> list[Comment]:
        try:
            data = self._http.get_json(f"{self._prepo}/pullRequests/{pr_number}/threads",
                                       params=_API)
        except ProviderError as e:
            self._log.warning("list_pr_comments PR #%s failed: %s", pr_number, e)
            return []
        out: list[Comment] = []
        for thread in (data or {}).get("value") or []:
            for c in thread.get("comments") or []:
                out.append(Comment(
                    id=str(c.get("id") or ""), body=c.get("content") or "",
                    author=((c.get("author") or {}).get("displayName") or ""),
                    created_at=c.get("publishedDate") or ""))
        return out

    def post_pr_comment(self, pr_number: int, body: str) -> bool:
        payload = {"comments": [{"content": body, "commentType": 1}], "status": 1}
        try:
            self._http.post_json(f"{self._prepo}/pullRequests/{pr_number}/threads?api-version=7.1",
                                 payload)
        except ProviderError as e:
            self._log.warning("post_pr_comment PR #%s failed: %s", pr_number, e)
            return False
        return True

    # ── board (work-item states) ─────────────────────────────────────────────
    def board_configured(self) -> bool:
        return True  # states always exist on work items

    def board_numbers_with_statuses(self, status_names: list[str]) -> set:
        if not status_names:
            return set()
        quoted = ", ".join(f"'{s}'" for s in status_names)
        ids = self._wiql(
            f"SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = '{self.project}' "
            f"AND [System.WorkItemType] = '{self.work_item_type}' "
            f"AND [System.State] IN ({quoted})")
        return set(ids)

    def board_set_status(self, issue_number: int, status_name: str) -> bool:
        ok = self._set_state(issue_number, status_name)
        if ok:
            self._log.info("board: #%s -> %s", issue_number, status_name)
        return ok

    # ── meta ─────────────────────────────────────────────────────────────────
    def list_branches(self) -> list[str]:
        try:
            data = self._http.get_json(f"{self._prepo}/refs",
                                       params={"filter": "heads/", **_API})
        except ProviderError as e:
            self._log.warning("list_branches failed: %s", e)
            return []
        return [(r.get("name") or "").replace("refs/heads/", "")
                for r in (data or {}).get("value") or [] if r.get("name")]

    # ── URL builders ─────────────────────────────────────────────────────────
    def issue_url(self, issue_number: int) -> str:
        return (f"https://dev.azure.com/{quote(self.org)}/{quote(self.project)}"
                f"/_workitems/edit/{issue_number}")

    def pr_url(self, pr_number: int) -> str:
        return (f"https://dev.azure.com/{quote(self.org)}/{quote(self.project)}"
                f"/_git/{quote(self.repo)}/pullrequest/{pr_number}")

    @property
    def display_repo(self) -> str:
        return f"{self.org}/{self.project}/{self.repo}"

    def remove_label(self, issue_number: int, label_name: str) -> bool:
        """Azure DevOps tag removal is not yet implemented for label projection.

        Tags on work items don't have a per-tag DELETE endpoint; removal requires
        a PATCH with the full updated tag list.  This is a known gap — label_projection
        on Azure DevOps currently supports add-only.  Falls back to the base no-op.
        """
        self._log.debug(
            "remove_label: Azure DevOps per-tag removal not implemented "
            "(issue #%s, label %r) — label_projection writes are add-only on ADO",
            issue_number, label_name,
        )
        return False

    def list_labels(self) -> list[LabelDef]:
        """Work-item tags (closest Azure analogue to labels)."""
        try:
            data = self._http.get_json(f"{self._pproj}/_apis/wit/tags",
                                       params={"api-version": "7.1-preview.1"})
        except ProviderError as e:
            self._log.warning("list_labels failed: %s", e)
            return []
        return [LabelDef(name=t.get("name") or "")
                for t in (data or {}).get("value") or [] if t.get("name")]

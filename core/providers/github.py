"""GitHub provider — REST for issues/PRs/CI/comments/branches/labels,
GraphQL for Projects v2 boards (which have no REST API).

Auth: fine-grained PAT via env (``vcs.token_env`` → GITHUB_TOKEN → GH_TOKEN).
Minimal scopes: contents:read, issues:write, pull_requests:write,
metadata:read, projects:write (boards only).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import (BoardSummary, CIStatus, Comment, FieldDef, FieldOption,
                   IssueSummary, LabelDef, PRSummary, ProviderConfigError,
                   VCSProvider, resolve_token)
from .http import HTTPClient, ProviderError

API_URL = "https://api.github.com"
GRAPHQL_PATH = "/graphql"


class GitHubProvider(VCSProvider):
    name = "github"
    supports_boards = True
    supports_ci_status = True
    supports_pr_comments = True
    supports_labels = True
    supports_branches = True

    def __init__(self, resolved: Dict[str, Any]):
        super().__init__(resolved)
        repo = (self._cfg.get("repo") or "").strip()
        if "/" not in repo:
            raise ProviderConfigError("github provider requires top-level repo: \"owner/repo\"")
        self.repo = repo
        self.owner = repo.split("/")[0]
        token = resolve_token(self._cfg, ("GITHUB_TOKEN", "GH_TOKEN"))
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            self._log.warning("no GitHub token in env (vcs.token_env/GITHUB_TOKEN/GH_TOKEN) — "
                              "API calls limited to public, unauthenticated access")
        self._http = HTTPClient(API_URL, headers, token=token)
        self._board_number = (self._cfg.get("tracking") or {}).get("github_project_number")
        self._board_meta: Optional[Dict[str, Any]] = None   # project_id/status_field_id/options
        self._board_items: Optional[List[Dict[str, Any]]] = None

    # ── issues ───────────────────────────────────────────────────────────────
    def list_issues(self, state: str = "open", labels: Optional[List[str]] = None,
                    limit: int = 50) -> List[IssueSummary]:
        """ANY-label semantics: one call per label, deduped (gh-CLI parity)."""
        label_sets = [[l] for l in (labels or []) if l] or [[]]
        seen: Dict[int, IssueSummary] = {}
        for ls in label_sets:
            params: Dict[str, Any] = {"state": state, "per_page": min(limit, 100)}
            if ls:
                params["labels"] = ",".join(ls)
            try:
                data = self._http.get_json(f"/repos/{self.repo}/issues", params=params)
            except ProviderError as e:
                self._log.warning("list_issues failed: %s", e)
                continue
            for it in data or []:
                if "pull_request" in it:    # the issues endpoint includes PRs
                    continue
                num = it.get("number")
                if isinstance(num, int) and num not in seen:
                    seen[num] = IssueSummary(
                        number=num, title=it.get("title") or "",
                        body=it.get("body") or "",
                        labels=[l.get("name", "") for l in it.get("labels") or []],
                        state=(it.get("state") or "open").lower(),
                        url=it.get("html_url") or "")
        return list(seen.values())[:limit]

    def close_issue(self, issue_number: int) -> bool:
        try:
            self._http.patch_json(f"/repos/{self.repo}/issues/{issue_number}",
                                  {"state": "closed"})
        except ProviderError as e:
            self._log.warning("close_issue #%s failed: %s", issue_number, e)
            return False
        self._log.info("close_issue: closed #%s", issue_number)
        return True

    def get_issue_state(self, issue_number: int) -> Optional[str]:
        try:
            data = self._http.get_json(f"/repos/{self.repo}/issues/{issue_number}")
            return (data.get("state") or "open").lower()
        except ProviderError as e:
            if e.status_code == 404:
                return "closed"  # deleted or transferred issue — treat as closed
            return None

    # ── pull requests ────────────────────────────────────────────────────────
    def list_prs(self, state: str = "all", limit: int = 50) -> List[PRSummary]:
        rest_state = "all" if state == "merged" else state
        try:
            data = self._http.get_json(f"/repos/{self.repo}/pulls",
                                       params={"state": rest_state, "per_page": min(limit, 100)})
        except ProviderError as e:
            self._log.warning("list_prs failed: %s", e)
            return []
        out: List[PRSummary] = []
        for pr in data or []:
            st = (pr.get("state") or "").lower()
            if st == "closed" and pr.get("merged_at"):
                st = "merged"
            if state == "merged" and st != "merged":
                continue
            head = pr.get("head") or {}
            base = pr.get("base") or {}
            out.append(PRSummary(number=pr.get("number"), state=st,
                                 head_branch=head.get("ref") or "",
                                 base_branch=base.get("ref") or "",
                                 title=pr.get("title") or "",
                                 body=pr.get("body") or "",
                                 url=pr.get("html_url") or "",
                                 head_sha=head.get("sha") or ""))
        return out[:limit]

    # ── CI status ────────────────────────────────────────────────────────────
    def get_pr_ci_status(self, pr_number: int) -> str:
        """Prefer the ``ci-complete`` gate; else every check must pass.

        Aggregates Checks API runs + legacy commit statuses, matching the gh
        ``statusCheckRollup`` the previous implementation consumed.
        """
        try:
            pr = self._http.get_json(f"/repos/{self.repo}/pulls/{pr_number}")
            sha = ((pr or {}).get("head") or {}).get("sha") or ""
            if not sha:
                return CIStatus.UNKNOWN
            checks: List[Dict[str, Optional[str]]] = []
            runs = self._http.get_json(f"/repos/{self.repo}/commits/{sha}/check-runs")
            for r in (runs or {}).get("check_runs") or []:
                checks.append({"name": r.get("name") or "", "status": r.get("status") or "",
                               "conclusion": r.get("conclusion")})
            combined = self._http.get_json(f"/repos/{self.repo}/commits/{sha}/status")
            for s in (combined or {}).get("statuses") or []:
                state = (s.get("state") or "").lower()
                checks.append({
                    "name": s.get("context") or "",
                    "status": "in_progress" if state == "pending" else "completed",
                    "conclusion": {"success": "success", "failure": "failure",
                                   "error": "failure"}.get(state)})
        except ProviderError as e:
            self._log.warning("get_pr_ci_status PR #%s failed: %s", pr_number, e)
            return CIStatus.UNKNOWN
        if not checks:
            return CIStatus.UNKNOWN
        gate = [c for c in checks if c["name"] == "ci-complete"]
        if gate:
            c = gate[0]
            if (c["status"] or "") != "completed":
                return CIStatus.PENDING
            return CIStatus.GREEN if (c["conclusion"] or "").lower() == "success" else CIStatus.RED
        if any((c["status"] or "") != "completed" for c in checks):
            return CIStatus.PENDING
        ok = {"success", "neutral", "skipped"}
        green = all((c["conclusion"] or "").lower() in ok for c in checks)
        return CIStatus.GREEN if green else CIStatus.RED

    # ── PR comments ──────────────────────────────────────────────────────────
    def list_pr_comments(self, pr_number: int) -> List[Comment]:
        try:
            data = self._http.get_paginated(f"/repos/{self.repo}/issues/{pr_number}/comments",
                                            style="link_header", max_pages=3)
        except ProviderError as e:
            self._log.warning("list_pr_comments PR #%s failed: %s", pr_number, e)
            return []
        return [Comment(id=str(c.get("id") or ""), body=c.get("body") or "",
                        author=((c.get("user") or {}).get("login") or ""),
                        created_at=c.get("created_at") or "")
                for c in data or []]

    def post_pr_comment(self, pr_number: int, body: str) -> bool:
        try:
            self._http.post_json(f"/repos/{self.repo}/issues/{pr_number}/comments",
                                 {"body": body})
        except ProviderError as e:
            self._log.warning("post_pr_comment PR #%s failed: %s", pr_number, e)
            return False
        return True

    def update_pr_body(self, pr_number: int, body: str) -> bool:
        """Overwrite the PR body (PATCH /pulls/{pr_number})."""
        try:
            self._http.patch_json(f"/repos/{self.repo}/pulls/{pr_number}", {"body": body})
        except ProviderError as e:
            self._log.warning("update_pr_body PR #%s failed: %s", pr_number, e)
            return False
        self._log.info("update_pr_body: patched #%s with closing keyword", pr_number)
        return True

    # ── GraphQL (Projects v2) ────────────────────────────────────────────────
    def _graphql(self, query: str, variables: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            data = self._http.post_json(GRAPHQL_PATH, {"query": query, "variables": variables})
        except ProviderError as e:
            self._log.warning("graphql failed: %s", e)
            return None
        if not isinstance(data, dict) or data.get("errors"):
            self._log.warning("graphql errors: %s",
                              [e.get("message") for e in (data or {}).get("errors") or []])
            return None
        return data.get("data")

    def list_boards(self) -> List[BoardSummary]:
        q = """query($owner:String!,$name:String!){ repository(owner:$owner, name:$name){
                 projectsV2(first:20){
                   nodes{ id number title } } } }"""
        data = self._graphql(q, {"owner": self.owner, "name": self.repo.split("/", 1)[1]})
        nodes = (((data or {}).get("repository") or {}).get("projectsV2") or {}).get("nodes") or []
        return [BoardSummary(id=n.get("id") or "", number=n.get("number") or 0,
                             title=n.get("title") or "") for n in nodes if n]

    def get_board_fields(self, board_id: str) -> List[FieldDef]:
        """``board_id`` is the project *number* (dashboard passes tracking config)."""
        try:
            number = int(board_id)
        except (TypeError, ValueError):
            return []
        q = """query($owner:String!,$name:String!,$number:Int!){ repository(owner:$owner, name:$name){
                 projectV2(number:$number){
                   fields(first:30){ nodes{
                     ... on ProjectV2SingleSelectField { id name options{ id name color description } }
                     ... on ProjectV2Field { id name }
                     ... on ProjectV2IterationField { id name } } } } } }"""
        data = self._graphql(q, {"owner": self.owner,
                                  "name": self.repo.split("/", 1)[1], "number": number})
        nodes = (((((data or {}).get("repository") or {}).get("projectV2") or {})
                  .get("fields") or {}).get("nodes") or [])
        out: List[FieldDef] = []
        for f in nodes:
            if not f or not f.get("id"):
                continue
            out.append(FieldDef(id=f["id"], name=f.get("name") or "",
                                options=[FieldOption(id=o.get("id") or "", name=o.get("name") or "",
                                                     color=o.get("color") or "",
                                                     description=o.get("description") or "")
                                         for o in f.get("options") or []]))
        return out

    # ── board (high-level, cached) ───────────────────────────────────────────
    def board_configured(self) -> bool:
        return bool(self._board_number)

    def _load_board_meta(self) -> Optional[Dict[str, Any]]:
        if self._board_meta is not None:
            return self._board_meta or None
        project_id = None
        for b in self.list_boards():
            if b.number == int(self._board_number or 0):
                project_id = b.id
                break
        if not project_id:
            self._log.warning("board: project #%s not found for %s", self._board_number, self.owner)
            self._board_meta = {}
            return None
        status_field_id, options = None, {}
        for f in self.get_board_fields(str(self._board_number)):
            if f.name.lower() == "status":
                status_field_id = f.id
                options = {o.name.lower(): o.id for o in f.options}
                break
        if not status_field_id:
            self._log.warning("board: no Status field on project #%s", self._board_number)
            self._board_meta = {}
            return None
        self._board_meta = {"project_id": project_id,
                            "status_field_id": status_field_id, "options": options}
        return self._board_meta

    def _items(self) -> List[Dict[str, Any]]:
        if self._board_items is not None:
            return self._board_items
        q = """query($owner:String!,$name:String!,$number:Int!,$cursor:String){ repository(owner:$owner, name:$name){
                 projectV2(number:$number){
                   items(first:100, after:$cursor){
                     pageInfo{ hasNextPage endCursor }
                     nodes{ id
                       content{ ... on Issue { number } }
                       fieldValueByName(name:"Status"){
                         ... on ProjectV2ItemFieldSingleSelectValue { name } } } } } } }"""
        items: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(5):  # ≤500 items, parity with the old --limit 200
            data = self._graphql(q, {"owner": self.owner,
                                     "name": self.repo.split("/", 1)[1],
                                     "number": int(self._board_number or 0), "cursor": cursor})
            block = (((data or {}).get("repository") or {}).get("projectV2") or {}).get("items") or {}
            for n in block.get("nodes") or []:
                items.append({
                    "id": n.get("id"),
                    "number": ((n.get("content") or {}).get("number")),
                    "status": ((n.get("fieldValueByName") or {}).get("name") or "")})
            page = block.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
        self._board_items = items
        return items

    def invalidate_board_cache(self) -> None:
        self._board_items = None

    def board_numbers_with_statuses(self, status_names: List[str]) -> set:
        if not self.board_configured():
            return set()
        targets = {s.lower() for s in status_names}
        return {it["number"] for it in self._items()
                if isinstance(it.get("number"), int)
                and (it.get("status") or "").lower() in targets}

    def board_ensure_status_option(self, status_name: str, color: str = "RED") -> bool:
        """Create ``status_name`` as a Status field option if it doesn't exist yet.

        Fetches existing options (preserving their colors) and appends the new
        one via updateProjectV2Field, then clears the board meta cache so the
        next board_set_status call picks up the new option ID.
        """
        meta = self._load_board_meta()
        if not meta:
            return False
        if (status_name or "").lower() in meta["options"]:
            return True  # already exists

        # Reuse get_board_fields to avoid the "Selections can't be made directly on
        # unions" error that field(name:"Status") triggers on some GitHub instances.
        fields = self.get_board_fields(str(self._board_number))
        status_field = next((f for f in fields if f.name.lower() == "status"), None)
        if not status_field:
            self._log.warning("board: no Status field found on project #%s", self._board_number)
            return False
        options = [
            {"name": o.name, "color": o.color or "GRAY", "description": o.description or ""}
            for o in status_field.options if o.name
        ]
        options.append({"name": status_name, "color": color, "description": ""})

        # clientMutationId avoids the union-selection error on ProjectV2Field return types.
        m = """mutation($fieldId:ID!,$options:[ProjectV2SingleSelectFieldOptionInput!]!){
                 updateProjectV2Field(input:{
                   fieldId:$fieldId, singleSelectOptions:$options
                 }){ clientMutationId }
               }"""
        result = self._graphql(m, {"fieldId": meta["status_field_id"],
                                    "options": options})
        if result is None:
            self._log.warning("board: failed to create status option '%s'", status_name)
            return False
        self._log.info("board: created status option '%s' on project #%s",
                       status_name, self._board_number)
        self._board_meta = None  # clear cache so next call reloads with the new option
        return True

    def board_set_status(self, issue_number: int, status_name: str) -> bool:
        if not self.board_configured():
            return False
        meta = self._load_board_meta()
        if not meta:
            return False
        option_id = meta["options"].get((status_name or "").lower())
        if not option_id:
            # Column doesn't exist yet — create it automatically then retry.
            if self.board_ensure_status_option(status_name):
                meta = self._load_board_meta()
                option_id = (meta or {}).get("options", {}).get((status_name or "").lower())
        if not option_id:
            self._log.warning("board: status '%s' not an option on #%s",
                              status_name, self._board_number)
            return False
        item_id = next((it["id"] for it in self._items()
                        if it.get("number") == issue_number), None)
        if not item_id:
            self._log.warning("board: issue #%s not on project #%s",
                              issue_number, self._board_number)
            return False
        m = """mutation($project:ID!,$item:ID!,$field:ID!,$option:String!){
                 updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,
                   fieldId:$field,value:{singleSelectOptionId:$option}}){
                   projectV2Item{ id } } }"""
        data = self._graphql(m, {"project": meta["project_id"], "item": item_id,
                                 "field": meta["status_field_id"], "option": option_id})
        if data is None:
            return False
        self._log.info("board: #%s -> %s", issue_number, status_name)
        self.invalidate_board_cache()
        return True

    # ── URL builders ─────────────────────────────────────────────────────────
    def issue_url(self, issue_number: int) -> str:
        return f"https://github.com/{self.repo}/issues/{issue_number}"

    def pr_url(self, pr_number: int) -> str:
        return f"https://github.com/{self.repo}/pull/{pr_number}"

    @property
    def display_repo(self) -> str:
        return self.repo

    # ── meta ─────────────────────────────────────────────────────────────────
    def list_branches(self) -> List[str]:
        try:
            data = self._http.get_paginated(f"/repos/{self.repo}/branches",
                                            style="link_header", max_pages=2)
        except ProviderError as e:
            self._log.warning("list_branches failed: %s", e)
            return []
        return [b.get("name") or "" for b in data or [] if b.get("name")]

    def list_labels(self) -> List[LabelDef]:
        try:
            data = self._http.get_paginated(f"/repos/{self.repo}/labels",
                                            style="link_header", max_pages=2)
        except ProviderError as e:
            self._log.warning("list_labels failed: %s", e)
            return []
        return [LabelDef(name=l.get("name") or "", color=l.get("color") or "")
                for l in data or [] if l.get("name")]

"""GitHub provider — REST for issues/PRs/CI/comments/branches/labels,
GraphQL for Projects v2 boards (which have no REST API).

Auth: fine-grained PAT via env (``vcs.token_env`` → GITHUB_TOKEN → GH_TOKEN).
Minimal scopes: contents:read, issues:write, pull_requests:write,
metadata:read, projects:write (boards only).
"""
from __future__ import annotations

import time
from typing import Any

from .base import (BoardSummary, CIStatus, Comment, FieldDef, FieldOption,
                   IssueSummary, LabelDef, PRSummary, ProviderConfigError,
                   VCSProvider, resolve_token)
from .http import HTTPClient, ProviderError

API_URL = "https://api.github.com"
GRAPHQL_PATH = "/graphql"

# Backoff delays (seconds) before each issue node-id resolution attempt during
# board enrollment. Newly-created issues sometimes haven't propagated through
# GitHub's GraphQL layer yet, yielding transient "could not resolve to an
# Issue" errors; retry a few times before giving up. 3 attempts: 0s, 2s, 4s.
_ENROLLMENT_RETRY_DELAYS = (0, 2, 4)


class GitHubProvider(VCSProvider):
    name = "github"
    supports_boards = True
    supports_ci_status = True
    supports_ci_rerun = True
    supports_pr_comments = True
    supports_labels = True
    supports_branches = True

    def __init__(self, resolved: dict[str, Any]):
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
        self._http = HTTPClient(API_URL, headers, token=token,
                                 verify_ssl=(self._cfg.get("vcs") or {}).get("verify_ssl", True))
        self._board_number = (self._cfg.get("tracking") or {}).get("github_project_number")
        self._board_meta: dict[str, Any] | None = None   # project_id/status_field_id/options
        self._board_items: list[dict[str, Any]] | None = None
        # Issue numbers whose board enrollment failed (node id never resolved).
        # Surfaced in the dispatch summary under ``enrollment_failures``.
        self.enrollment_failures: list[int] = []

    # ── issues ───────────────────────────────────────────────────────────────
    def list_issues(self, state: str = "open", labels: list[str] | None = None,
                    limit: int = 50) -> list[IssueSummary]:
        """ANY-label semantics: one call per label, deduped (gh-CLI parity).

        ``limit`` is the page size for each request; all pages are fetched until
        GitHub returns fewer items than the page size (end-of-results signal).
        This ensures boards with >100 open issues are never silently truncated
        (#228). Use ``_fetch_issues(provider, filters)`` with ``filters.max_issues``
        to apply a hard ceiling on the total result count.
        """
        per_page = min(limit, 100)
        label_sets = [[lb] for lb in (labels or []) if lb] or [[]]
        seen: dict[int, IssueSummary] = {}
        for ls in label_sets:
            params: dict[str, Any] = {"state": state, "per_page": per_page}
            if ls:
                params["labels"] = ",".join(ls)
            try:
                data = self._http.get_paginated(
                    f"/repos/{self.repo}/issues",
                    params=params,
                    style="link_header",
                    per_page=per_page,
                    max_pages=50,
                )
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
                        labels=[lb.get("name", "") for lb in it.get("labels") or []],
                        state=(it.get("state") or "open").lower(),
                        url=it.get("html_url") or "")
        return list(seen.values())

    def close_issue(self, issue_number: int) -> bool:
        try:
            self._http.patch_json(f"/repos/{self.repo}/issues/{issue_number}",
                                  {"state": "closed"})
        except ProviderError as e:
            self._log.warning("close_issue #%s failed: %s", issue_number, e)
            return False
        self._log.info("close_issue: closed #%s", issue_number)
        return True

    def create_issue(self, title: str, body: str,
                     labels: list[str] | None = None) -> int | None:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        try:
            data = self._http.post_json(f"/repos/{self.repo}/issues", payload)
            num = (data or {}).get("number")
            if isinstance(num, int):
                self._log.info("create_issue: created #%s %r", num, title[:60])
                return num
        except ProviderError as e:
            self._log.warning("create_issue failed: %s", e)
        return None

    def get_issue_state(self, issue_number: int) -> str | None:
        try:
            data = self._http.get_json(f"/repos/{self.repo}/issues/{issue_number}")
            return (data.get("state") or "open").lower()
        except ProviderError as e:
            if e.status_code == 404:
                return "closed"  # deleted or transferred issue — treat as closed
            return None

    def get_issue(self, issue_number: int) -> IssueSummary | None:
        try:
            data = self._http.get_json(f"/repos/{self.repo}/issues/{issue_number}")
        except ProviderError as e:
            self._log.warning("get_issue #%s failed: %s", issue_number, e)
            return None
        if not data or "pull_request" in data:
            return None
        from .base import IssueSummary  # local import avoids circular at module level
        return IssueSummary(
            number=data.get("number", issue_number),
            title=data.get("title") or "",
            body=data.get("body") or "",
            labels=[lb.get("name", "") for lb in data.get("labels") or []],
            state=(data.get("state") or "open").lower(),
            url=data.get("html_url") or "",
        )

    def blockers(self, issue_number: int) -> list[int]:
        """Open blockers via native issue dependencies
        (``GET …/issues/{n}/dependencies/blocked_by``, GA Aug 2025) merged with
        the portable ``Depends on:`` body fallback.

        Each returned item is a full issue object carrying ``state`` inline, so
        open-filtering needs no extra request. Cross-repo blockers are ignored —
        dispatch is scoped to a single repo and only local issue numbers are
        dispatchable. Repos/accounts without the dependencies feature 404 here;
        that degrades silently to the body fallback (logged only on other
        errors, so the common no-feature case stays quiet).
        """
        out: list[int] = []
        try:
            deps = self._http.get_json(
                f"/repos/{self.repo}/issues/{issue_number}/dependencies/blocked_by",
                params={"per_page": 100})
        except ProviderError as e:
            if e.status_code != 404:
                self._log.warning("blockers #%s blocked_by failed: %s", issue_number, e)
            deps = []
        for it in deps or []:
            if "pull_request" in it:    # dependencies can include PRs — skip
                continue
            repo_full = ((it.get("repository") or {}).get("full_name") or "").strip()
            if repo_full and repo_full != self.repo:
                continue  # cross-repo blocker — its number isn't local, can't gate on it
            num = it.get("number")
            if isinstance(num, int) and (it.get("state") or "open").lower() == "open":
                out.append(num)
        for n in self._depends_on_blockers(issue_number):
            if n not in out:
                out.append(n)
        return out

    def get_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        try:
            return self._http.get_json(
                f"/repos/{self.repo}/issues/{issue_number}/comments",
                params={"per_page": 100},
            ) or []
        except ProviderError as e:
            self._log.warning("get_issue_comments #%s failed: %s", issue_number, e)
            return []

    # ── pull requests ────────────────────────────────────────────────────────
    def list_prs(self, state: str = "all", limit: int = 50) -> list[PRSummary]:
        rest_state = "all" if state == "merged" else state
        try:
            data = self._http.get_json(f"/repos/{self.repo}/pulls",
                                       params={"state": rest_state, "per_page": min(limit, 100)})
        except ProviderError as e:
            self._log.warning("list_prs failed: %s", e)
            return []
        out: list[PRSummary] = []
        for pr in data or []:
            st = (pr.get("state") or "").lower()
            if st == "closed" and pr.get("merged_at"):
                st = "merged"
            if state == "merged" and st != "merged":
                continue
            head = pr.get("head") or {}
            base = pr.get("base") or {}
            head_repo = head.get("repo") or {}
            out.append(PRSummary(number=pr.get("number"), state=st,
                                 head_branch=head.get("ref") or "",
                                 base_branch=base.get("ref") or "",
                                 title=pr.get("title") or "",
                                 body=pr.get("body") or "",
                                 url=pr.get("html_url") or "",
                                 head_sha=head.get("sha") or "",
                                 author=((pr.get("user") or {}).get("login") or ""),
                                 is_fork=bool(head_repo.get("fork"))))
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
            checks: list[dict[str, str | None]] = []
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
            # Fetch succeeded but the PR has zero checks → the repo has no CI. This is
            # NOT "unknown" (which means we couldn't tell); it means there is nothing to
            # gate on, so the pipeline should advance/merge. See CIStatus.NONE.
            return CIStatus.NONE
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

    # ── batch CI status (single GraphQL round-trip, #1143) ──────────────────────
    def get_prs_ci_status(self, pr_numbers: list[int]) -> dict[int, str]:
        """Batch CI-status lookup for multiple PRs via a single GraphQL query.

        Falls back to the sequential base implementation when the list is
        empty or the GraphQL request fails.
        """
        if not pr_numbers:
            return {}
        # Deduplicate while preserving order for stable dict construction.
        seen: set = set()
        unique = [n for n in pr_numbers if not (n in seen or seen.add(n))]
        q = """
        query($owner:String!,$name:String!,$prNumbers:[Int!]!){
          repository(owner:$owner, name:$name){
            pullRequests(numbers:$prNumbers, first:100){
              nodes{
                number
                headRefOid
                commits(last:1){ nodes{ commit{
                  statusCheckRollup{ state } } } }
                statusCheckRollup{ state } } } } }"""
        try:
            data = self._graphql(q, {"owner": self.owner,
                                     "name": self.repo.split("/", 1)[1],
                                     "prNumbers": unique})
        except Exception as e:  # pragma: no cover — defensive
            self._log.warning("get_prs_ci_status GraphQL failed: %s", e)
            data = None
        if not data:
            self._log.warning("get_prs_ci_status GraphQL returned no data — "
                              "falling back to sequential")
            return super().get_prs_ci_status(unique)
        nodes = (((data or {}).get("repository") or {}).get("pullRequests") or {}).get("nodes") or []
        # GraphQL errors → fall back to sequential for the missing PRs.
        got: set = set()
        result: dict[int, str] = {}
        for node in nodes:
            num = node.get("number")
            if num is None:
                continue
            got.add(num)
            result[num] = self._graphql_rollup_to_status(node)
        missing = [n for n in unique if n not in got]
        if missing:
            self._log.warning("get_prs_ci_status: %s PRs missing from GraphQL "
                              "response — fetching sequentially", len(missing))
            for n in missing:
                try:
                    result[n] = self.get_pr_ci_status(n)
                except Exception as e:
                    self._log.warning("get_prs_ci_status PR #%s fallback failed: %s", n, e)
                    result[n] = CIStatus.UNKNOWN
        return result

    @staticmethod
    def _graphql_rollup_to_status(node: dict[str, Any]) -> str:
        """Map a GraphQL ``statusCheckRollup.state`` value to ``CIStatus``.

        The rollup field lives at two possible locations depending on the
        query shape: top-level on the PR node, or nested inside
        ``commits.nodes[].commit.statusCheckRollup``. We check both for
        robustness.
        """
        def _state(d: dict[str, Any] | None) -> str | None:
            if not d:
                return None
            rollup = d.get("statusCheckRollup")
            if isinstance(rollup, dict):
                s = rollup.get("state")
                if s:
                    return str(s).upper()
            return None

        state = _state(node)
        if state is None:
            commits = (node.get("commits") or {}).get("nodes") or []
            for c in commits:
                state = _state(c.get("commit") if isinstance(c, dict) else None)
                if state:
                    break
        if state is None:
            return CIStatus.UNKNOWN
        mapping = {
            "SUCCESS": CIStatus.GREEN,
            "FAILURE": CIStatus.RED,
            "ERROR": CIStatus.RED,
            "PENDING": CIStatus.PENDING,
            "EXPECTED": CIStatus.PENDING,
        }
        return mapping.get(state, CIStatus.UNKNOWN)

    # ── CI re-run (bounded auto-retry of transiently-red CI, #1199) ────────────
    def get_pr_head_sha(self, pr_number: int) -> str | None:
        """Head commit SHA of a PR — keys the bounded CI-rerun budget."""
        try:
            pr = self._http.get_json(f"/repos/{self.repo}/pulls/{pr_number}")
        except ProviderError as e:
            self._log.warning("get_pr_head_sha PR #%s failed: %s", pr_number, e)
            return None
        return (((pr or {}).get("head") or {}).get("sha")) or None

    def _latest_failed_run(self, pr_number: int) -> dict[str, Any] | None:
        """Most-recent *failed* Actions workflow run for the PR's head commit.

        Returns the raw run dict (has ``id`` and ``html_url``) or None when the
        SHA is unknown, there are no runs, or none failed.
        """
        sha = self.get_pr_head_sha(pr_number)
        if not sha:
            return None
        try:
            data = self._http.get_json(f"/repos/{self.repo}/actions/runs",
                                       params={"head_sha": sha})
        except ProviderError as e:
            self._log.warning("_latest_failed_run PR #%s failed: %s", pr_number, e)
            return None
        runs = (data or {}).get("workflow_runs") or []
        failed = [r for r in runs
                  if (r.get("conclusion") or "").lower() in ("failure", "timed_out", "cancelled")]
        if not failed:
            return None
        # Newest first: prefer run_started_at, fall back to created_at, then id.
        failed.sort(
            key=lambda r: (r.get("run_started_at") or r.get("created_at") or "", r.get("id") or 0),
            reverse=True,
        )
        return failed[0]

    def rerun_failed_ci(self, pr_number: int) -> bool:
        """Re-run only the failed jobs of the latest failed workflow run for the
        PR head (equivalent to ``gh run rerun --failed``)."""
        run = self._latest_failed_run(pr_number)
        run_id = (run or {}).get("id")
        if not run_id:
            self._log.info("rerun_failed_ci PR #%s: no failed run to re-run", pr_number)
            return False
        try:
            self._http.post_json(
                f"/repos/{self.repo}/actions/runs/{run_id}/rerun-failed-jobs", {})
        except ProviderError as e:
            self._log.warning("rerun_failed_ci PR #%s (run %s) failed: %s", pr_number, run_id, e)
            return False
        self._log.info("rerun_failed_ci: re-ran failed jobs for PR #%s (run %s)", pr_number, run_id)
        return True

    def failed_ci_run_url(self, pr_number: int) -> str | None:
        """Web URL of the most-recent failed CI run for a PR (for escalation)."""
        run = self._latest_failed_run(pr_number)
        return (run or {}).get("html_url") if run else None

    # ── PR comments ──────────────────────────────────────────────────────────
    def list_pr_comments(self, pr_number: int) -> list[Comment]:
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

    def merge_pr(self, pr_number: int, merge_method: str = "squash") -> bool:
        """Merge a PR via PUT /repos/{repo}/pulls/{pr}/merge."""
        valid_methods = ("merge", "squash", "rebase")
        method = merge_method if merge_method in valid_methods else "squash"
        try:
            self._http.put_json(
                f"/repos/{self.repo}/pulls/{pr_number}/merge",
                {"merge_method": method},
            )
            self._log.info("merge_pr: merged PR #%s (%s)", pr_number, method)
            return True
        except ProviderError as e:
            # GitHub returns 405/422 when the PR is already merged or cleanup
            # fails after the merge (e.g. branch in a worktree). Verify actual
            # state before reporting failure — if MERGED on GitHub, it's a win.
            try:
                pr_data = self._http.get_json(f"/repos/{self.repo}/pulls/{pr_number}")
                if pr_data and pr_data.get("merged_at"):
                    self._log.warning(
                        "merge_pr PR #%s: API error (%s) but PR is already MERGED — "
                        "treating as success",
                        pr_number, e,
                    )
                    return True
            except ProviderError as check_err:
                self._log.warning(
                    "merge_pr PR #%s: PUT failed (%s); fallback state-check GET also failed: %s",
                    pr_number, e, check_err,
                )
                return False
            self._log.warning("merge_pr PR #%s failed: %s", pr_number, e)
            return False

    def open_pr(
        self, head_branch: str, base_branch: str, title: str, body: str = "",
    ) -> int | None:
        """Open a PR head->base via POST /repos/{repo}/pulls (F12). Returns the new PR
        number, or None on any failure (branch missing, no diff, a PR already exists,
        error) — never raises. Used to recover a developer that pushed its branch but
        never opened the PR (common on local models)."""
        if not head_branch or not base_branch:
            return None
        try:
            resp = self._http.post_json(
                f"/repos/{self.repo}/pulls",
                {
                    "title": title or head_branch,
                    "head": head_branch,
                    "base": base_branch,
                    "body": body or "",
                },
            )
        except ProviderError as e:
            self._log.warning(
                "open_pr: %s -> %s failed (branch missing / no diff / exists?): %s",
                head_branch, base_branch, e,
            )
            return None
        num = (resp or {}).get("number")
        if isinstance(num, int):
            self._log.info("open_pr: opened PR #%s (%s -> %s)", num, head_branch, base_branch)
            return num
        return None

    def get_pr_files(self, pr_number: int) -> list[dict[str, Any]]:
        """Changed files in a PR via GET /pulls/{n}/files (paginated)."""
        try:
            data = self._http.get_paginated(
                f"/repos/{self.repo}/pulls/{pr_number}/files",
                style="link_header", per_page=100, max_pages=5,
            )
        except ProviderError as e:
            self._log.warning("get_pr_files PR #%s failed: %s", pr_number, e)
            return []
        return [{"filename": f.get("filename") or "",
                 "additions": f.get("additions") or 0,
                 "deletions": f.get("deletions") or 0,
                 "changes": f.get("changes") or 0,
                 "status": f.get("status") or ""} for f in data or []]

    def post_issue_comment(self, issue_number: int, body: str) -> bool:
        try:
            self._http.post_json(f"/repos/{self.repo}/issues/{issue_number}/comments",
                                 {"body": body})
        except ProviderError as e:
            self._log.warning("post_issue_comment #%s failed: %s", issue_number, e)
            return False
        return True

    def add_label(self, issue_number: int, label_name: str) -> bool:
        try:
            self._http.post_json(
                f"/repos/{self.repo}/issues/{issue_number}/labels",
                {"labels": [label_name]},
            )
            return True
        except ProviderError as e:
            self._log.warning("add_label #%s %r failed: %s", issue_number, label_name, e)
            return False

    def remove_label(self, issue_number: int, label_name: str) -> bool:
        """DELETE /repos/{repo}/issues/{n}/labels/{label}."""
        try:
            from urllib.parse import quote as _quote
            self._http.request(
                "DELETE",
                f"/repos/{self.repo}/issues/{issue_number}/labels/{_quote(label_name, safe='')}",
            )
            return True
        except ProviderError as e:
            if getattr(e, "status_code", None) == 404:
                return True  # already removed — idempotent
            self._log.warning("remove_label #%s %r failed: %s", issue_number, label_name, e)
            return False

    def list_issue_labels(self, issue_number: int) -> list[str]:
        """Return label names currently applied to ``issue_number``."""
        try:
            data = self._http.get_json(f"/repos/{self.repo}/issues/{issue_number}/labels")
            return [lbl.get("name", "") for lbl in (data or []) if lbl.get("name")]
        except ProviderError as e:
            self._log.debug("list_issue_labels #%s failed: %s", issue_number, e)
            return []

    def has_label(self, issue_number: int, label_name: str) -> bool:
        """Return True if ``issue_number`` has ``label_name`` applied.

        Uses the ``labels`` field returned by ``get_issue`` so it piggybacks on
        the existing ``GET /repos/{owner}/{repo}/issues/{n}`` call used
        elsewhere. Never raises — returns False on any provider error (base
        class contract at ``VCSProvider.has_label``).
        """
        issue = self.get_issue(issue_number)
        if issue is None:
            return False
        target = label_name.strip().lower()
        labels = getattr(issue, "labels", None) or []
        return any((n or "").strip().lower() == target for n in labels)

    def ensure_labels(self) -> list[str]:
        """Create required Daedalus labels in this repo if they don't exist yet."""
        from .base import REQUIRED_LABELS
        created: list[str] = []
        for ldef in REQUIRED_LABELS:
            try:
                self._http.post_json(
                    f"/repos/{self.repo}/labels",
                    {"name": ldef["name"], "color": ldef["color"],
                     "description": ldef["description"]},
                )
                created.append(ldef["name"])
                self._log.info("ensure_labels: created %r", ldef["name"])
            except ProviderError as e:
                if e.status_code == 422:
                    continue  # label already exists — idempotent
                self._log.warning("ensure_labels: create %r failed: %s", ldef["name"], e)
        return created

    # ── GraphQL (Projects v2) ────────────────────────────────────────────────
    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any] | None:
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

    def list_boards(self) -> list[BoardSummary]:
        q = """query($owner:String!,$name:String!){ repository(owner:$owner, name:$name){
                 projectsV2(first:20){
                   nodes{ id number title } } } }"""
        data = self._graphql(q, {"owner": self.owner, "name": self.repo.split("/", 1)[1]})
        nodes = (((data or {}).get("repository") or {}).get("projectsV2") or {}).get("nodes") or []
        return [BoardSummary(id=n.get("id") or "", number=n.get("number") or 0,
                             title=n.get("title") or "") for n in nodes if n]

    def get_board_fields(self, board_id: str) -> list[FieldDef]:
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
        out: list[FieldDef] = []
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

    def _load_board_meta(self) -> dict[str, Any] | None:
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

    @staticmethod
    def _status_of(node: dict[str, Any]) -> str:
        """Extract the Status single-select value name from a project item node.

        Defined once and reused by both the listing scan (``_items``) and the
        direct per-issue lookup (``_board_item_for_issue``).
        """
        return ((node or {}).get("fieldValueByName") or {}).get("name") or ""

    def _items(self) -> list[dict[str, Any]]:
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
        items: list[dict[str, Any]] = []
        cursor = None
        max_pages = 50  # safety cap (~5000 items); real boards never hit this
        for page_num in range(1, max_pages + 1):
            data = self._graphql(q, {"owner": self.owner,
                                     "name": self.repo.split("/", 1)[1],
                                     "number": int(self._board_number or 0), "cursor": cursor})
            if data is None:
                # Page fetch failed — return what we have, but leave the cache
                # empty so the next call re-fetches instead of serving a
                # silently-truncated listing (issue #1158).
                self._log.warning("board: items page %d fetch failed — "
                                  "partial listing not cached", page_num)
                return items
            block = (((data or {}).get("repository") or {}).get("projectV2") or {}).get("items") or {}
            for n in block.get("nodes") or []:
                items.append({
                    "id": n.get("id"),
                    "number": ((n.get("content") or {}).get("number")),
                    "status": self._status_of(n)})
            page = block.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
        else:
            self._log.warning("board: item listing hit the %d-page safety cap "
                              "(%d items) — listing may be incomplete",
                              max_pages, len(items))
        self._board_items = items
        return items

    def _board_item_for_issue(self, issue_number: int) -> dict[str, Any] | None:
        """Resolve an issue's project item directly via its projectItems edge.

        Fallback for when the board listing misses an enrolled item (page-error
        truncation or the pagination safety cap — issue #1158). Returns the
        same shape as ``_items()`` entries, or None if the issue has no item on
        the configured project.
        """
        q = """query($owner:String!,$name:String!,$number:Int!,$cursor:String){
                 repository(owner:$owner,name:$name){
                   issue(number:$number){
                     projectItems(first:100, after:$cursor){
                       pageInfo{ hasNextPage endCursor }
                       nodes{ id
                         project{ number }
                         fieldValueByName(name:"Status"){
                           ... on ProjectV2ItemFieldSingleSelectValue { name } } } } } } }"""
        cursor = None
        max_pages = 50  # safety cap — mirrors _items(); nobody enrols in 5000 projects
        for page_num in range(1, max_pages + 1):
            data = self._graphql(q, {"owner": self.owner,
                                     "name": self.repo.split("/", 1)[1],
                                     "number": issue_number, "cursor": cursor})
            block = ((((data or {}).get("repository") or {}).get("issue") or {})
                     .get("projectItems") or {})
            for n in block.get("nodes") or []:
                if not n or not n.get("id"):
                    continue
                if ((n.get("project") or {}).get("number")) != int(self._board_number or 0):
                    continue
                return {"id": n["id"], "number": issue_number,
                        "status": self._status_of(n)}
            page = block.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
        else:
            self._log.warning("board: issue #%s projectItems hit the %d-page "
                              "safety cap — direct lookup may be incomplete",
                              issue_number, max_pages)
        return None

    def _resolve_board_item(self, issue_number: int) -> dict[str, Any] | None:
        """Resolve an issue's project item, preferring the cached listing.

        Scans the (possibly-cached) board listing first, then falls back to the
        direct per-issue projectItems lookup — the listing can miss enrolled
        items under page-error truncation or the pagination safety cap (issue
        #1158). Returns the item dict, or None if the issue is not on the board.
        """
        item = next((it for it in self._items()
                     if it.get("number") == issue_number), None)
        if item is None:
            item = self._board_item_for_issue(issue_number)
        return item

    def invalidate_board_cache(self) -> None:
        self._board_items = None

    def board_numbers_with_statuses(self, status_names: list[str]) -> set:
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

    def _resolve_issue_node_id(self, issue_number: int) -> str | None:
        """Resolve an issue's GraphQL node id, retrying with exponential backoff.

        Newly-created issues sometimes haven't propagated through GitHub's
        GraphQL infrastructure yet, yielding transient "could not resolve to an
        Issue" errors. Retry per ``_ENROLLMENT_RETRY_DELAYS`` before giving up.
        Returns the node id, or None if every attempt fails.
        """
        repo_name = self.repo.split("/", 1)[1]
        q = """query($owner:String!,$name:String!,$number:Int!){
                 repository(owner:$owner,name:$name){
                   issue(number:$number){ id } } }"""
        attempts = len(_ENROLLMENT_RETRY_DELAYS)
        for attempt, delay in enumerate(_ENROLLMENT_RETRY_DELAYS, start=1):
            if delay:
                time.sleep(delay)
            data = self._graphql(
                q, {"owner": self.owner, "name": repo_name, "number": issue_number})
            issue_id = (((data or {}).get("repository") or {}).get("issue") or {}).get("id")
            if issue_id:
                return issue_id
            self._log.warning(
                "board: issue #%s node id unresolved (attempt %d/%d)",
                issue_number, attempt, attempts)
        return None

    def _board_add_item(self, issue_number: int) -> str | None:
        """Enroll an issue into the project via addProjectV2ItemById.

        Returns the project item ID, or None if already present or on failure.
        Uses content=issue (not pull_request) — issues are enrolled by their
        GraphQL node id via ``convertProjectV2DraftIssueItemToDraftIssueItem``
        is not needed; ``addProjectV2ItemById`` accepts the issue node id
        directly as ``contentId``.
        """
        meta = self._load_board_meta()
        if not meta:
            return None
        project_id = meta["project_id"]
        # Resolve the issue's GraphQL node id (retries transient propagation lag).
        issue_id = self._resolve_issue_node_id(issue_number)
        if not issue_id:
            self._log.error(
                "board: failed to resolve issue #%s node id after %d attempts — "
                "enroll it manually on project #%s",
                issue_number, len(_ENROLLMENT_RETRY_DELAYS), self._board_number)
            self.enrollment_failures.append(issue_number)
            return None
        m = """mutation($project:ID!,$content:ID!){
                 addProjectV2ItemById(input:{projectId:$project,contentId:$content}){
                   item { id } } }"""
        result = self._graphql(m, {"project": project_id, "content": issue_id})
        if result is None:
            self._log.warning("board: addProjectV2ItemById failed for #%s", issue_number)
            return None
        item_id = (((result or {}).get("addProjectV2ItemById") or {}).get("item") or {}).get("id")
        if item_id:
            # Invalidate cache so the new item is visible to subsequent _items() calls.
            self.invalidate_board_cache()
            self._log.info("board: enrolled #%s into project #%s", issue_number, self._board_number)
        return item_id

    def board_ensure_backlog(self, issue_number: int) -> bool:
        """Enroll an issue into the project and set its status to 'Backlog'.

        Returns True if the item is now on the board with Backlog status.
        Idempotent — if the item is already present, falls through to
        board_set_status which will no-op if already at Backlog.
        """
        if not self.board_configured():
            return False
        # Check if already on the board — the listing can miss enrolled items
        # (issue #1158), so fall back to the direct per-issue lookup.
        current_item = self._resolve_board_item(issue_number)
        if not current_item:
            added = self._board_add_item(issue_number)
            if not added:
                return False
        return self.board_set_status(issue_number, "Backlog")

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
        # The listing can miss enrolled items (issue #1158) — resolve via the
        # issue's projectItems edge before assuming it needs enrolling.
        current_item = self._resolve_board_item(issue_number)
        if not current_item:
            # Auto-enroll: item not on project yet — add it first, then retry status.
            self._log.info("board: #%s not on project #%s — auto-enrolling",
                           issue_number, self._board_number)
            added = self._board_add_item(issue_number)
            if not added:
                return False
            current_item = self._board_item_for_issue(issue_number)
            if not current_item:
                self._log.warning("board: issue #%s still not found after enrollment",
                                  issue_number)
                return False
        if (current_item.get("status") or "").lower() == (status_name or "").lower():
            self._log.debug("board: #%s already at '%s' — skipping", issue_number, status_name)
            return False
        item_id = current_item["id"]
        m = """mutation($project:ID!,$item:ID!,$field:ID!,$option:String!){
                 updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,
                   fieldId:$field,value:{singleSelectOptionId:$option}}){
                   projectV2Item{id}} }"""
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
    def list_branches(self) -> list[str]:
        try:
            data = self._http.get_paginated(f"/repos/{self.repo}/branches",
                                            style="link_header", max_pages=2)
        except ProviderError as e:
            self._log.warning("list_branches failed: %s", e)
            return []
        return [b.get("name") or "" for b in data or [] if b.get("name")]

    def list_labels(self) -> list[LabelDef]:
        try:
            data = self._http.get_paginated(f"/repos/{self.repo}/labels",
                                            style="link_header", max_pages=2)
        except ProviderError as e:
            self._log.warning("list_labels failed: %s", e)
            return []
        return [LabelDef(name=lb.get("name") or "", color=lb.get("color") or "")
                for lb in data or [] if lb.get("name")]

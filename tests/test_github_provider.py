"""Tests for GitHubProvider — REST issue/PR/CI semantics and GraphQL boards."""
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.providers.base import CIStatus  # noqa: E402
from core.providers.github import GitHubProvider  # noqa: E402
from core.providers.http import ProviderError  # noqa: E402


CFG = {"repo": "octo/repo", "tracking": {"github_project_number": 1}}


@pytest.fixture
def provider():
    with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}, clear=False):
        p = GitHubProvider(CFG)
    p._http = mock.MagicMock()
    return p


# ── issues ────────────────────────────────────────────────────────────────────

def test_list_issues_filters_prs_and_dedupes(provider):
    issue = {"number": 7, "title": "Bug", "body": "b", "state": "open",
             "labels": [{"name": "ready"}], "html_url": "u"}
    pr_item = {"number": 8, "title": "PR", "pull_request": {"url": "x"}, "labels": []}
    provider._http.get_json.return_value = [issue, pr_item, issue]
    out = provider.list_issues(labels=["ready", "bug"])  # two label calls, same issue
    assert [i.number for i in out] == [7]
    assert out[0].labels == ["ready"]
    assert provider._http.get_json.call_count == 2  # OR semantics: one call per label


def test_close_issue_patches_state(provider):
    assert provider.close_issue(7) is True
    path, body = provider._http.patch_json.call_args[0]
    assert path == "/repos/octo/repo/issues/7"
    assert body == {"state": "closed"}


def test_close_issue_false_on_error(provider):
    provider._http.patch_json.side_effect = ProviderError("403", status_code=403)
    assert provider.close_issue(7) is False


# ── PRs ───────────────────────────────────────────────────────────────────────

def _pr(number, state="open", merged_at=None, head="x", body=""):
    return {"number": number, "state": state, "merged_at": merged_at,
            "head": {"ref": head, "sha": "abc"}, "body": body, "html_url": "u"}


def test_list_prs_maps_merged(provider):
    provider._http.get_json.return_value = [
        _pr(1, "open"), _pr(2, "closed", merged_at="2026-01-01"), _pr(3, "closed")]
    states = {p.number: p.state for p in provider.list_prs()}
    assert states == {1: "open", 2: "merged", 3: "closed"}


def test_pr_state_for_issue_prefers_merged(provider):
    provider._http.get_json.return_value = [
        _pr(1, "open", head="fix/issue-7-x"),
        _pr(2, "closed", merged_at="2026-01-01", body="Closes #7")]
    assert provider.pr_state_for_issue(7) == "merged"
    assert provider.pr_number_for_issue(7) == 2


def test_pr_state_for_issue_branch_heuristics(provider):
    provider._http.get_json.return_value = [_pr(4, "open", head="feat/7-add-thing")]
    assert provider.pr_state_for_issue(7) == "open"
    provider._http.get_json.return_value = [_pr(4, "open", head="unrelated")]
    assert provider.pr_state_for_issue(7) is None
    # A bare "#7" mention without a closing keyword must NOT link the PR
    provider._http.get_json.return_value = [_pr(4, "open", head="z",
                                                body="see #7 for context")]
    assert provider.pr_state_for_issue(7) is None


def test_find_pr_for_branch(provider):
    provider._http.get_json.return_value = [_pr(9, "open", head="fix/issue-3")]
    assert provider.find_pr_for_branch("fix/issue-3") == 9
    assert provider.find_pr_for_branch("") is None


# ── CI status ─────────────────────────────────────────────────────────────────

def _ci_mock(provider, check_runs, statuses=None):
    def fake_get(path, **kw):
        if path.endswith("/pulls/5"):
            return {"head": {"sha": "abc"}}
        if path.endswith("/check-runs"):
            return {"check_runs": check_runs}
        if path.endswith("/status"):
            return {"statuses": statuses or []}
        raise AssertionError(path)
    provider._http.get_json.side_effect = fake_get


def test_ci_prefers_ci_complete_gate(provider):
    _ci_mock(provider, [
        {"name": "ci-complete", "status": "completed", "conclusion": "success"},
        {"name": "other", "status": "completed", "conclusion": "failure"}])
    assert provider.get_pr_ci_status(5) == CIStatus.GREEN
    assert provider.pr_ci_green(5) is True


def test_ci_all_checks_must_pass_without_gate(provider):
    _ci_mock(provider, [
        {"name": "a", "status": "completed", "conclusion": "success"},
        {"name": "b", "status": "completed", "conclusion": "failure"}])
    assert provider.get_pr_ci_status(5) == CIStatus.RED


def test_ci_pending_and_unknown(provider):
    _ci_mock(provider, [{"name": "a", "status": "in_progress", "conclusion": None}])
    assert provider.get_pr_ci_status(5) == CIStatus.PENDING
    _ci_mock(provider, [])
    assert provider.get_pr_ci_status(5) == CIStatus.UNKNOWN


def test_ci_includes_legacy_commit_statuses(provider):
    _ci_mock(provider, [], statuses=[{"context": "jenkins", "state": "failure"}])
    assert provider.get_pr_ci_status(5) == CIStatus.RED


# ── comments / delivery marker ────────────────────────────────────────────────

def test_pr_comments_and_marker(provider):
    provider._http.get_paginated.return_value = [
        {"id": 1, "body": "hi", "user": {"login": "alice"}, "created_at": "t"}]
    comments = provider.list_pr_comments(5)
    assert comments[0].author == "alice"
    assert provider.pr_has_delivery_marker(5) is False
    provider._http.get_paginated.return_value = [
        {"id": 2, "body": "<!-- daedalus:slack-delivered -->\n\nreport", "user": {}}]
    assert provider.pr_has_delivery_marker(5) is True


def test_post_delivery_marker(provider):
    assert provider.post_delivery_marker(5, "report") is True
    path, body = provider._http.post_json.call_args[0]
    assert path == "/repos/octo/repo/issues/5/comments"
    assert "<!-- daedalus:slack-delivered -->" in body["body"]


# ── GraphQL board ─────────────────────────────────────────────────────────────

BOARD_GQL = {
    "repositoryOwner": {"projectsV2": {"nodes": [
        {"id": "PVT_1", "number": 1, "title": "Roadmap"}]}}}

FIELDS_GQL = {
    "repositoryOwner": {"projectV2": {"fields": {"nodes": [
        {"id": "F_title", "name": "Title"},
        {"id": "F_status", "name": "Status",
         "options": [{"id": "o1", "name": "Ready"}, {"id": "o2", "name": "Done"}]}]}}}}

ITEMS_GQL = {
    "repositoryOwner": {"projectV2": {"items": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [
            {"id": "I_1", "content": {"number": 7},
             "fieldValueByName": {"name": "Ready"}},
            {"id": "I_2", "content": {"number": 8},
             "fieldValueByName": {"name": "Done"}},
            {"id": "I_3", "content": {}, "fieldValueByName": None}]}}}}


def _gql_mock(provider, mutation_result=None, field_mutation_result=None):
    """Stateful GraphQL mock. Tracks options added via updateProjectV2Field so
    that subsequent fields(first queries include them (simulates real GitHub)."""
    extra_opts: list = []  # options created via board_ensure_status_option

    def fake_post(path, payload, **kw):
        q = payload["query"]
        if "projectsV2(first" in q:
            return {"data": BOARD_GQL}
        if "fields(first" in q:
            # Include any options added via updateProjectV2Field.
            # Uses "repository" key to match the updated get_board_fields query.
            opts = [{"id": "o1", "name": "Ready"}, {"id": "o2", "name": "Done"}]
            for o in extra_opts:
                opts.append({"id": f"o_{o['name'].lower()}", "name": o["name"]})
            data = {"repository": {"projectV2": {"fields": {"nodes": [
                {"id": "F_title", "name": "Title"},
                {"id": "F_status", "name": "Status", "options": opts},
            ]}}}}
            return {"data": data}
        if "items(first" in q:
            return {"data": ITEMS_GQL}
        if "updateProjectV2Field" in q:
            # Track newly added options for future fields(first queries
            for o in (payload.get("variables") or {}).get("options", []):
                if o.get("name") not in ("Ready", "Done"):
                    extra_opts.append(o)
            if field_mutation_result is not None:
                return field_mutation_result
            return {"data": {"updateProjectV2Field": {"projectV2Field": {"id": "F_status"}}}}
        if "updateProjectV2ItemFieldValue" in q:
            return mutation_result or {"data": {"updateProjectV2ItemFieldValue":
                                                {"projectV2Item": {"id": "I_1"}}}}
        raise AssertionError(q)
    provider._http.post_json.side_effect = fake_post


def test_board_numbers_with_statuses(provider):
    _gql_mock(provider)
    assert provider.board_numbers_with_statuses(["Ready"]) == {7}
    assert provider.board_numbers_with_statuses(["ready", "done"]) == {7, 8}


def test_board_set_status(provider):
    _gql_mock(provider)
    assert provider.board_set_status(7, "Done") is True


def test_board_set_status_unknown_option(provider):
    """board_set_status auto-creates a missing option via board_ensure_status_option,
    then sets the status — the stateful mock simulates GitHub returning the new option."""
    _gql_mock(provider)
    assert provider.board_set_status(7, "Blocked") is True


def test_board_set_status_option_create_fails(provider):
    """board_set_status returns False when the option creation mutation fails."""
    _gql_mock(provider, field_mutation_result=None)
    # Override: make updateProjectV2Field return None so creation fails
    base = provider._http.post_json.side_effect

    def fail_field_mutation(path, payload, **kw):
        if "updateProjectV2Field" in payload.get("query", ""):
            return None
        return base(path, payload, **kw)
    provider._http.post_json.side_effect = fail_field_mutation
    assert provider.board_set_status(7, "Nonexistent") is False


def test_board_set_status_graphql_errors(provider):
    _gql_mock(provider)
    base = provider._http.post_json.side_effect

    def with_errors(path, payload, **kw):
        if "updateProjectV2ItemFieldValue" in payload["query"]:
            return {"errors": [{"message": "denied"}]}
        return base(path, payload, **kw)
    provider._http.post_json.side_effect = with_errors
    assert provider.board_set_status(7, "Done") is False


def test_board_not_configured():
    with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}, clear=False):
        p = GitHubProvider({"repo": "octo/repo"})
    p._http = mock.MagicMock()
    assert p.board_configured() is False
    assert p.board_numbers_with_statuses(["Ready"]) == set()
    assert p.board_set_status(1, "Done") is False
    p._http.post_json.assert_not_called()


# ── meta ──────────────────────────────────────────────────────────────────────

def test_list_branches_and_labels(provider):
    provider._http.get_paginated.return_value = [{"name": "main"}, {"name": "dev"}]
    assert provider.list_branches() == ["main", "dev"]
    provider._http.get_paginated.return_value = [{"name": "bug", "color": "f00"}]
    labels = provider.list_labels()
    assert labels[0].name == "bug" and labels[0].color == "f00"


def test_meta_safe_on_error(provider):
    provider._http.get_paginated.side_effect = ProviderError("401", status_code=401)
    assert provider.list_branches() == []
    assert provider.list_labels() == []

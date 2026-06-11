"""Tests for AzureDevOpsProvider — WIQL work items, PRs, statuses, threads."""
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.providers.azure_devops import AzureDevOpsProvider  # noqa: E402
from core.providers.base import CIStatus  # noqa: E402
from core.providers.http import ProviderError  # noqa: E402


CFG = {"vcs": {"provider": "azuredevops", "org": "acme", "project": "Web",
               "repo": "web-app"}}


@pytest.fixture
def provider():
    with mock.patch.dict("os.environ", {"AZURE_DEVOPS_PAT": "pat"}, clear=False):
        p = AzureDevOpsProvider(CFG)
    p._http = mock.MagicMock()
    return p


def test_base_url_and_paths():
    with mock.patch.dict("os.environ", {"AZURE_DEVOPS_PAT": "pat"}, clear=False):
        p = AzureDevOpsProvider(CFG)
    assert p._http._base == "https://dev.azure.com/acme"
    assert p._prepo == "/Web/_apis/git/repositories/web-app"


def test_list_issues_wiql_then_batch(provider):
    provider._http.post_json.return_value = {"workItems": [{"id": 10}, {"id": 11}]}
    provider._http.get_json.return_value = {"value": [
        {"id": 10, "fields": {"System.Title": "A", "System.Description": "d",
                              "System.Tags": "ready; web", "System.State": "To Do"}},
        {"id": 11, "fields": {"System.Title": "B", "System.State": "Doing"}}]}
    out = provider.list_issues()
    assert [i.number for i in out] == [10, 11]
    assert out[0].labels == ["ready", "web"]
    wiql = provider._http.post_json.call_args[0][1]["query"]
    assert "[System.WorkItemType] = 'Issue'" in wiql
    assert "NOT IN ('Closed', 'Done', 'Removed', 'Resolved')" in wiql


def test_list_issues_label_filter_in_wiql(provider):
    provider._http.post_json.return_value = {"workItems": []}
    provider.list_issues(labels=["ready"])
    wiql = provider._http.post_json.call_args[0][1]["query"]
    assert "[System.Tags] CONTAINS 'ready'" in wiql


def test_close_issue_json_patch(provider):
    assert provider.close_issue(10) is True
    args, kwargs = provider._http.patch_json.call_args
    assert args[0] == "/Web/_apis/wit/workitems/10?api-version=7.1"
    assert args[1] == [{"op": "add", "path": "/fields/System.State", "value": "Done"}]
    assert kwargs["content_type"] == "application/json-patch+json"


def test_list_prs_maps_status(provider):
    provider._http.get_json.return_value = {"value": [
        {"pullRequestId": 1, "status": "active", "sourceRefName": "refs/heads/fix/issue-2",
         "description": "", "lastMergeSourceCommit": {"commitId": "abc"}},
        {"pullRequestId": 2, "status": "completed", "sourceRefName": "refs/heads/b",
         "description": "Closes #10"},
        {"pullRequestId": 3, "status": "abandoned", "sourceRefName": "refs/heads/c"}]}
    prs = {p.number: p for p in provider.list_prs()}
    assert prs[1].state == "open" and prs[1].head_branch == "fix/issue-2"
    assert prs[2].state == "merged"
    assert prs[3].state == "closed"


def test_pr_state_for_issue(provider):
    provider._http.get_json.return_value = {"value": [
        {"pullRequestId": 2, "status": "completed",
         "sourceRefName": "refs/heads/b", "description": "Closes #10"}]}
    assert provider.pr_state_for_issue(10) == "merged"


def test_ci_status_aggregation(provider):
    provider._http.get_json.return_value = {"value": [
        {"context": {"genre": "ci", "name": "build"}, "state": "pending"},
        {"context": {"genre": "ci", "name": "build"}, "state": "succeeded"}]}
    assert provider.get_pr_ci_status(1) == CIStatus.GREEN  # latest per context wins

    provider._http.get_json.return_value = {"value": [
        {"context": {"genre": "ci", "name": "build"}, "state": "succeeded"},
        {"context": {"genre": "ci", "name": "test"}, "state": "failed"}]}
    assert provider.get_pr_ci_status(1) == CIStatus.RED

    provider._http.get_json.return_value = {"value": [
        {"context": {"genre": "ci", "name": "build"}, "state": "pending"}]}
    assert provider.get_pr_ci_status(1) == CIStatus.PENDING

    provider._http.get_json.return_value = {"value": []}
    assert provider.get_pr_ci_status(1) == CIStatus.UNKNOWN


def test_threads_comments_flatten_and_post(provider):
    provider._http.get_json.return_value = {"value": [
        {"comments": [{"id": 1, "content": "a", "author": {"displayName": "Ann"},
                       "publishedDate": "t"}]},
        {"comments": [{"id": 2, "content": "b", "author": {}}]}]}
    out = provider.list_pr_comments(1)
    assert [c.body for c in out] == ["a", "b"]
    assert provider.post_pr_comment(1, "hello") is True
    path, payload = provider._http.post_json.call_args[0]
    assert path.endswith("/pullRequests/1/threads?api-version=7.1")
    assert payload["comments"][0]["content"] == "hello"


def test_board_numbers_with_statuses_wiql(provider):
    provider._http.post_json.return_value = {"workItems": [{"id": 5}, {"id": 6}]}
    assert provider.board_numbers_with_statuses(["Ready", "To Do"]) == {5, 6}
    wiql = provider._http.post_json.call_args[0][1]["query"]
    assert "[System.State] IN ('Ready', 'To Do')" in wiql


def test_board_set_status_patches_state(provider):
    assert provider.board_set_status(5, "Doing") is True
    args, _ = provider._http.patch_json.call_args
    assert args[1] == [{"op": "add", "path": "/fields/System.State", "value": "Doing"}]


def test_branches_strip_refs(provider):
    provider._http.get_json.return_value = {"value": [
        {"name": "refs/heads/main"}, {"name": "refs/heads/dev"}]}
    assert provider.list_branches() == ["main", "dev"]


def test_errors_degrade_gracefully(provider):
    provider._http.post_json.side_effect = ProviderError("401", status_code=401)
    provider._http.get_json.side_effect = ProviderError("401", status_code=401)
    provider._http.patch_json.side_effect = ProviderError("401", status_code=401)
    assert provider.list_issues() == []
    assert provider.list_prs() == []
    assert provider.close_issue(1) is False
    assert provider.get_pr_ci_status(1) == CIStatus.UNKNOWN
    assert provider.list_branches() == []

"""Tests for GitLabProvider — REST /api/v4 semantics and label-driven board."""
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.providers.base import CIStatus  # noqa: E402
from core.providers.gitlab import GitLabProvider  # noqa: E402
from core.providers.http import ProviderError  # noqa: E402


def _provider(extra_vcs=None, tracking=None):
    cfg = {"repo": "group/proj",
           "vcs": {"provider": "gitlab", **(extra_vcs or {})}}
    if tracking:
        cfg["tracking"] = tracking
    with mock.patch.dict("os.environ", {"GITLAB_TOKEN": "tok"}, clear=False):
        p = GitLabProvider(cfg)
    p._http = mock.MagicMock()
    return p


@pytest.fixture
def provider():
    return _provider()


def test_project_path_is_url_encoded(provider):
    assert provider._proj == "/projects/group%2Fproj"


def test_project_id_takes_precedence():
    p = _provider(extra_vcs={"project_id": 42})
    assert p._proj == "/projects/42"


def test_self_hosted_base_url():
    with mock.patch.dict("os.environ", {"GITLAB_TOKEN": "tok"}, clear=False):
        p = GitLabProvider({"repo": "g/p", "vcs": {"provider": "gitlab",
                                                   "base_url": "https://git.corp.io"}})
    assert p._http._base == "https://git.corp.io/api/v4"


def test_list_issues_maps_iid_and_state(provider):
    provider._http.get_json.return_value = [
        {"iid": 3, "title": "T", "description": "D", "labels": ["Ready"],
         "state": "opened", "web_url": "u"}]
    out = provider.list_issues()
    assert out[0].number == 3 and out[0].state == "open" and out[0].body == "D"
    _, kwargs = provider._http.get_json.call_args
    assert kwargs["params"]["state"] == "opened"


def test_close_issue_uses_state_event(provider):
    assert provider.close_issue(3) is True
    path, body = provider._http.put_json.call_args[0]
    assert path == "/projects/group%2Fproj/issues/3"
    assert body == {"state_event": "close"}


def test_list_prs_maps_mr_states(provider):
    provider._http.get_json.return_value = [
        {"iid": 1, "state": "opened", "source_branch": "a", "description": "", "sha": "s"},
        {"iid": 2, "state": "merged", "source_branch": "b", "description": "", "sha": "s"},
        {"iid": 3, "state": "closed", "source_branch": "c", "description": "", "sha": "s"}]
    states = {p.number: p.state for p in provider.list_prs()}
    assert states == {1: "open", 2: "merged", 3: "closed"}


def test_pr_state_for_issue_closing_keyword(provider):
    provider._http.get_json.return_value = [
        {"iid": 5, "state": "merged", "source_branch": "x", "description": "Fixes #9"}]
    assert provider.pr_state_for_issue(9) == "merged"


def test_find_pr_for_branch_uses_source_branch_param(provider):
    provider._http.get_json.return_value = [{"iid": 11}]
    assert provider.find_pr_for_branch("fix/issue-2") == 11
    _, kwargs = provider._http.get_json.call_args
    assert kwargs["params"] == {"source_branch": "fix/issue-2", "state": "opened"}


def test_ci_status_pipeline_mapping(provider):
    cases = {"success": CIStatus.GREEN, "failed": CIStatus.RED,
             "running": CIStatus.PENDING, "manual": CIStatus.PENDING}
    for gl_status, expected in cases.items():
        provider._http.get_json.return_value = [{"status": gl_status}]
        assert provider.get_pr_ci_status(1) == expected, gl_status
    provider._http.get_json.return_value = []
    assert provider.get_pr_ci_status(1) == CIStatus.UNKNOWN


def test_mr_notes_comments(provider):
    provider._http.get_paginated.return_value = [
        {"id": 1, "body": "note", "author": {"username": "bob"}, "created_at": "t"}]
    out = provider.list_pr_comments(1)
    assert out[0].author == "bob" and out[0].body == "note"
    assert provider.post_pr_comment(1, "hello") is True


def test_board_disabled_by_default(provider):
    assert provider.board_configured() is False
    assert provider.board_set_status(1, "Done") is False
    assert provider.board_numbers_with_statuses(["Ready"]) == set()
    provider._http.put_json.assert_not_called()


def test_label_board_set_status_swaps_labels():
    p = _provider(tracking={"label_board": True})
    assert p.board_configured() is True
    assert p.board_set_status(4, "In review") is True
    path, body = p._http.put_json.call_args[0]
    assert path == "/projects/group%2Fproj/issues/4"
    assert body["add_labels"] == "In review"
    removed = set(body["remove_labels"].split(","))
    assert removed == {"Ready", "In progress", "Done"}


def test_label_board_ready_numbers():
    p = _provider(tracking={"label_board": True})
    p._http.get_json.return_value = [
        {"iid": 1, "state": "opened", "labels": ["Ready"]},
        {"iid": 2, "state": "opened", "labels": ["Ready"]}]
    assert p.board_numbers_with_statuses(["Ready"]) == {1, 2}


def test_errors_degrade_gracefully(provider):
    provider._http.get_json.side_effect = ProviderError("500", status_code=500)
    assert provider.list_issues() == []
    assert provider.list_prs() == []
    assert provider.get_pr_ci_status(1) == CIStatus.UNKNOWN
    provider._http.get_paginated.side_effect = ProviderError("500")
    assert provider.list_branches() == []
    assert provider.list_labels() == []

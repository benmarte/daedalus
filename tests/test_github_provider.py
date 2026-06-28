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
    # list_issues now uses get_paginated (pagination support — issue #228)
    provider._http.get_paginated.return_value = [issue, pr_item, issue]
    out = provider.list_issues(labels=["ready", "bug"])  # two label calls, same issue
    assert [i.number for i in out] == [7]
    assert out[0].labels == ["ready"]
    assert provider._http.get_paginated.call_count == 2  # OR semantics: one call per label


def test_close_issue_patches_state(provider):
    assert provider.close_issue(7) is True
    path, body = provider._http.patch_json.call_args[0]
    assert path == "/repos/octo/repo/issues/7"
    assert body == {"state": "closed"}


def test_close_issue_false_on_error(provider):
    provider._http.patch_json.side_effect = ProviderError("403", status_code=403)
    assert provider.close_issue(7) is False


def test_add_label_success(provider):
    assert provider.add_label(42, "epic") is True
    path, body = provider._http.post_json.call_args[0]
    assert path == "/repos/octo/repo/issues/42/labels"
    assert body == {"labels": ["epic"]}


def test_add_label_failure_returns_false(provider):
    provider._http.post_json.side_effect = ProviderError("404", status_code=404)
    assert provider.add_label(42, "epic") is False


# ── PRs ───────────────────────────────────────────────────────────────────────

def _pr(number, state="open", merged_at=None, head="x", body="", base="dev"):
    return {"number": number, "state": state, "merged_at": merged_at,
            "head": {"ref": head, "sha": "abc"}, "base": {"ref": base},
            "body": body, "html_url": "u"}


def test_list_prs_maps_merged(provider):
    provider._http.get_json.return_value = [
        _pr(1, "open"), _pr(2, "closed", merged_at="2026-01-01"), _pr(3, "closed")]
    prs = provider.list_prs()
    states = {p.number: p.state for p in prs}
    assert states == {1: "open", 2: "merged", 3: "closed"}
    # base_branch must be populated so the dispatcher can gate Done on the target branch
    assert prs[0].base_branch == "dev"
    assert prs[1].base_branch == "dev"


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
    "repository": {"projectsV2": {"nodes": [
        {"id": "PVT_1", "number": 1, "title": "Roadmap"}]}}}

FIELDS_GQL = {
    "repository": {"projectV2": {"fields": {"nodes": [
        {"id": "F_title", "name": "Title"},
        {"id": "F_status", "name": "Status",
         "options": [{"id": "o1", "name": "Ready"}, {"id": "o2", "name": "Done"}]}]}}}}

ITEMS_GQL = {
    "repository": {"projectV2": {"items": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [
            {"id": "I_1", "content": {"number": 7},
             "fieldValueByName": {"name": "Ready"}},
            {"id": "I_2", "content": {"number": 8},
             "fieldValueByName": {"name": "Done"}},
            {"id": "I_3", "content": {}, "fieldValueByName": None}]}}}}


def _gql_mock(provider, mutation_result=None, field_mutation_result=None):
    """Stateful GraphQL mock. Tracks options added via updateProjectV2Field so
    that subsequent fields(first queries include them (simulates real GitHub).
    Also tracks items added via addProjectV2ItemById."""
    extra_opts: list = []  # options created via board_ensure_status_option
    added_items: list = []  # items created via addProjectV2ItemById

    def fake_post(path, payload, **kw):
        q = payload["query"]
        if "projectsV2(first" in q:
            return {"data": BOARD_GQL}
        if "fields(first" in q:
            # Include any options added via updateProjectV2Field.
            # Uses "repository" key to match the updated get_board_fields query.
            opts = [{"id": "o1", "name": "Ready", "color": "GREEN", "description": ""},
                    {"id": "o2", "name": "Done", "color": "BLUE", "description": ""}]
            for o in extra_opts:
                opts.append({"id": f"o_{o['name'].lower()}", "name": o["name"],
                             "color": o.get("color") or "GRAY", "description": ""})
            data = {"repository": {"projectV2": {"fields": {"nodes": [
                {"id": "F_title", "name": "Title"},
                {"id": "F_status", "name": "Status", "options": opts},
            ]}}}}
            return {"data": data}
        if "items(first" in q:
            import copy
            data = copy.deepcopy(ITEMS_GQL)
            # Append any items added via addProjectV2ItemById
            if added_items:
                nodes = data["repository"]["projectV2"]["items"]["nodes"]
                nodes.extend(added_items)
            return {"data": data}
        if "updateProjectV2Field" in q:
            # Track newly added options for future fields(first queries
            for o in (payload.get("variables") or {}).get("options", []):
                if o.get("name") not in ("Ready", "Done"):
                    extra_opts.append(o)
            if field_mutation_result is not None:
                return field_mutation_result
            return {"data": {"updateProjectV2Field": {"clientMutationId": None}}}
        if "updateProjectV2ItemFieldValue" in q:
            return mutation_result or {"data": {"updateProjectV2ItemFieldValue":
                                                {"projectV2Item": {"id": "I_1"}}}}
        if "addProjectV2ItemById" in q:
            added_items.append({"id": "I_NEW", "content": {"number": 99},
                                "fieldValueByName": None})
            return {"data": {"addProjectV2ItemById": {"item": {"id": "I_NEW"}}}}
        if "issue(number" in q and "repository" in q and "items" not in q:
            # Issue node id lookup for _board_add_item
            return {"data": {"repository": {"issue": {"id": "ISSUE_NODE_1"}}}}
        raise AssertionError(q)
    provider._http.post_json.side_effect = fake_post


def test_board_numbers_with_statuses(provider):
    _gql_mock(provider)
    assert provider.board_numbers_with_statuses(["Ready"]) == {7}
    assert provider.board_numbers_with_statuses(["ready", "done"]) == {7, 8}


def test_board_set_status(provider):
    _gql_mock(provider)
    assert provider.board_set_status(7, "Done") is True


def test_board_set_status_already_at_target(provider):
    """board_set_status returns False (no mutation) when status is already the target."""
    _gql_mock(provider)
    # Item #8 is already at "Done" in ITEMS_GQL
    assert provider.board_set_status(8, "Done") is False
    # No updateProjectV2ItemFieldValue mutation should have fired
    calls = [c for c in provider._http.post_json.call_args_list
             if "updateProjectV2ItemFieldValue" in (c.args[1] if c.args else c.kwargs.get("payload", {})).get("query", "")]
    assert calls == [], "mutation fired even though status was already correct"


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


# ── get_issue ─────────────────────────────────────────────────────────────────

def test_get_issue_returns_summary(provider):
    provider._http.get_json.return_value = {
        "number": 42, "title": "Fix crash", "body": "details",
        "labels": [{"name": "bug"}], "state": "open", "html_url": "https://github.com/octo/repo/issues/42",
    }
    iss = provider.get_issue(42)
    assert iss is not None
    assert iss.number == 42
    assert iss.title == "Fix crash"
    assert iss.labels == ["bug"]
    assert iss.state == "open"


def test_get_issue_returns_none_on_error(provider):
    provider._http.get_json.side_effect = ProviderError("404", status_code=404)
    assert provider.get_issue(99) is None


def test_get_issue_ignores_pull_requests(provider):
    provider._http.get_json.return_value = {
        "number": 5, "title": "PR", "pull_request": {"url": "x"},
        "labels": [], "state": "open", "html_url": "u",
    }
    assert provider.get_issue(5) is None


# ── board_ensure_backlog / _board_add_item (issue #19) ─────────────────────


def test_board_add_item_returns_item_id(provider):
    """_board_add_item resolves issue node id and calls addProjectV2ItemById."""
    _gql_mock(provider)
    # Issue 99 is NOT in ITEMS_GQL — should trigger enrollment
    item_id = provider._board_add_item(99)
    assert item_id == "I_NEW"
    # Verify addProjectV2ItemById was called
    calls = [c for c in provider._http.post_json.call_args_list
             if "addProjectV2ItemById" in (c.args[1] if c.args else c.kwargs.get("payload", {})).get("query", "")]
    assert len(calls) == 1


def test_board_add_item_invalidates_cache(provider):
    """_board_add_item invalidates _board_items cache after enrollment."""
    _gql_mock(provider)
    # Pre-populate cache
    provider._board_items = [{"id": "I_1", "number": 7, "status": "Ready"}]
    provider._board_add_item(99)
    assert provider._board_items is None, "cache should be invalidated"


def test_board_ensure_backlog_enrolls_and_sets_status(provider):
    """board_ensure_backlog enrolls missing item then sets Backlog status."""
    _gql_mock(provider)
    # Issue 99 is not on the board
    assert provider.board_ensure_backlog(99) is True


def test_board_set_status_auto_enrolls_missing_item(provider):
    """board_set_status auto-enrolls item if not found on board."""
    _gql_mock(provider)
    # Issue 99 is not in ITEMS_GQL
    assert provider.board_set_status(99, "Ready") is True
    # Verify addProjectV2ItemById was called (enrollment)
    add_calls = [c for c in provider._http.post_json.call_args_list
                 if "addProjectV2ItemById" in (c.args[1] if c.args else c.kwargs.get("payload", {})).get("query", "")]
    assert len(add_calls) == 1


# ── enrollment retry / backoff (issue #236) ────────────────────────────────


def _fail_node_id_n_times(provider, n):
    """Wrap _gql_mock so the issue node-id query errors `n` times then succeeds.

    Mimics GitHub returning "could not resolve to an Issue" for a freshly
    created issue that hasn't propagated yet. Returns a dict tracking the call
    count so tests can assert how many attempts ran.
    """
    base = provider._http.post_json.side_effect
    calls = {"node_id": 0}

    def side(path, payload, **kw):
        q = payload.get("query", "")
        if "issue(number" in q and "repository" in q and "items" not in q:
            calls["node_id"] += 1
            if calls["node_id"] <= n:
                return {"errors": [{"message":
                        "Could not resolve to an Issue with the number of 99."}]}
        return base(path, payload, **kw)
    provider._http.post_json.side_effect = side
    return calls


@mock.patch("core.providers.github.time.sleep")
def test_resolve_node_id_retries_then_succeeds(sleep, provider):
    """_resolve_issue_node_id retries with backoff and returns the id once it resolves."""
    _gql_mock(provider)
    calls = _fail_node_id_n_times(provider, 2)  # fail twice, succeed on the third
    assert provider._resolve_issue_node_id(99) == "ISSUE_NODE_1"
    assert calls["node_id"] == 3
    # First attempt has no delay; backoff before attempts 2 and 3.
    assert sleep.call_args_list == [mock.call(2), mock.call(4)]


@mock.patch("core.providers.github.time.sleep")
def test_resolve_node_id_succeeds_first_try_no_sleep(sleep, provider):
    """Happy path resolves on the first attempt with no backoff delay."""
    _gql_mock(provider)
    assert provider._resolve_issue_node_id(99) == "ISSUE_NODE_1"
    sleep.assert_not_called()


@mock.patch("core.providers.github.time.sleep")
def test_board_add_item_retries_then_enrolls(sleep, provider):
    """_board_add_item recovers from a transient resolution failure and enrolls."""
    _gql_mock(provider)
    _fail_node_id_n_times(provider, 1)
    assert provider._board_add_item(99) == "I_NEW"
    assert provider.enrollment_failures == []
    sleep.assert_called_once_with(2)


@mock.patch("core.providers.github.time.sleep")
def test_board_add_item_all_retries_fail_records_failure(sleep, provider):
    """When every attempt fails, _board_add_item records the number for the summary."""
    _gql_mock(provider)
    _fail_node_id_n_times(provider, 99)  # always fails
    assert provider._board_add_item(99) is None
    assert provider.enrollment_failures == [99]
    # 3 attempts → 2 backoff sleeps (0s, 2s, 4s).
    assert sleep.call_args_list == [mock.call(2), mock.call(4)]


@mock.patch("core.providers.github.time.sleep")
def test_board_add_item_exhaustion_logs_error(sleep, provider, caplog):
    """Exhausted retries escalate to ERROR (not WARNING) naming the issue number."""
    import logging
    _gql_mock(provider)
    _fail_node_id_n_times(provider, 99)  # always fails
    with caplog.at_level(logging.ERROR, logger="daedalus.providers"):
        assert provider._board_add_item(99) is None
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("99" in r.getMessage() and "manually" in r.getMessage()
               for r in errors), f"expected ERROR naming issue #99: {[r.getMessage() for r in errors]}"

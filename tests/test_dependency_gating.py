"""Dependency-aware ready-gating (issue #139).

Covers the substantive layer behind the dispatcher's gate:
  * the portable ``Depends on:`` body parser,
  * the base provider's body-convention ``blockers()`` (open-filtering),
  * each concrete provider's native-link resolution merged with the fallback,
  * the dispatch-summary rendering that surfaces *why* an issue is held back.

The dispatcher gate itself is a thin inline guard — ``provider.blockers(n)``
non-empty ⇒ skip — so proving ``blockers()`` returns ``[]`` once a blocker
closes (and the open numbers while it's open) is proving the gate's contract.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.providers.azure_devops import AzureDevOpsProvider  # noqa: E402
from core.providers.base import (IssueSummary, VCSProvider,  # noqa: E402
                                 parse_depends_on)
from core.providers.github import GitHubProvider  # noqa: E402
from core.providers.gitlab import GitLabProvider  # noqa: E402
from core.providers.http import ProviderError  # noqa: E402
from core import notify_templates  # noqa: E402


# ── portable body convention parser ───────────────────────────────────────────

def test_parse_depends_on_comma_list():
    assert parse_depends_on("Depends on: #1, #2, #3") == [1, 2, 3]


def test_parse_depends_on_variants_and_bullets():
    body = ("Some intro\n"
            "- Depends-on: #10\n"
            "> Blocked by: #11 and #12\n"
            "depends on #13")
    assert parse_depends_on(body) == [10, 11, 12, 13]


def test_parse_depends_on_dedupes_preserving_order():
    assert parse_depends_on("Depends on: #5, #5, #2") == [5, 2]


def test_parse_depends_on_absent():
    assert parse_depends_on("No dependencies here. See #9 for context.") == []
    assert parse_depends_on("") == []


# ── base provider body-convention fallback ────────────────────────────────────

class _StubProvider(VCSProvider):
    """Minimal concrete provider: body + per-issue open/closed states."""

    name = "stub"

    def __init__(self, body="", states=None):
        super().__init__({})
        self._body = body
        self._states = states or {}

    def list_issues(self, state="open", labels=None, limit=50):
        return []

    def close_issue(self, issue_number):
        return True

    def list_prs(self, state="all", limit=50):
        return []

    def get_issue(self, issue_number):
        return IssueSummary(number=issue_number, body=self._body)

    def get_issue_state(self, issue_number):
        return self._states.get(issue_number)


def test_base_blockers_keeps_only_open():
    p = _StubProvider("Depends on: #1, #2, #3",
                      states={1: "open", 2: "closed", 3: "open"})
    assert p.blockers(99) == [1, 3]


def test_base_blockers_unknown_state_not_blocking():
    # Unresolvable reference (provider returns None) must not wedge the issue.
    p = _StubProvider("Depends on: #1", states={1: None})
    assert p.blockers(99) == []


def test_base_blockers_empty_when_all_closed():
    p = _StubProvider("Depends on: #1, #2", states={1: "closed", 2: "closed"})
    assert p.blockers(99) == []


# ── GitHub: inherits the body-convention fallback ─────────────────────────────

def _github():
    with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}, clear=False):
        p = GitHubProvider({"repo": "octo/repo"})
    p._http = mock.MagicMock()
    return p


def test_github_blockers_body_fallback_open_only():
    p = _github()

    def _get(path, params=None):
        if path.endswith("/dependencies/blocked_by"):
            raise ProviderError("Not Found", status_code=404)
        if path.endswith("/issues/99"):
            return {"number": 99, "body": "Depends on: #1, #2", "state": "open"}
        if path.endswith("/issues/1"):
            return {"number": 1, "state": "open"}
        if path.endswith("/issues/2"):
            return {"number": 2, "state": "closed"}
        raise AssertionError(path)

    p._http.get_json.side_effect = _get
    assert p.blockers(99) == [1]


# ── GitLab: native is_blocked_by links merged with the fallback ───────────────

def _gitlab():
    with mock.patch.dict("os.environ", {"GITLAB_TOKEN": "tok"}, clear=False):
        p = GitLabProvider({"repo": "group/proj", "vcs": {"provider": "gitlab"}})
    p._http = mock.MagicMock()
    return p


def test_gitlab_blockers_native_is_blocked_by_open_only():
    p = _gitlab()

    def _get(path, params=None):
        if path.endswith("/issues/99/links"):
            return [
                {"iid": 1, "link_type": "is_blocked_by", "state": "opened"},
                {"iid": 2, "link_type": "is_blocked_by", "state": "closed"},
                {"iid": 3, "link_type": "blocks", "state": "opened"},
                {"iid": 4, "link_type": "relates_to", "state": "opened"},
            ]
        if path.endswith("/issues/99"):  # body fallback fetch — no convention
            return {"iid": 99, "description": "", "state": "opened"}
        raise AssertionError(path)

    p._http.get_json.side_effect = _get
    # Only the open is_blocked_by link counts; blocks/relates_to are ignored.
    assert p.blockers(99) == [1]


def test_gitlab_blockers_merges_body_fallback():
    p = _gitlab()

    def _get(path, params=None):
        if path.endswith("/issues/99/links"):
            return [{"iid": 1, "link_type": "is_blocked_by", "state": "opened"}]
        if path.endswith("/issues/99"):
            return {"iid": 99, "description": "Depends on: #7", "state": "opened"}
        if path.endswith("/issues/7"):
            return {"iid": 7, "state": "opened"}
        raise AssertionError(path)

    p._http.get_json.side_effect = _get
    assert p.blockers(99) == [1, 7]


# ── Azure DevOps: native Predecessor links merged with the fallback ───────────

def _azure():
    with mock.patch.dict("os.environ", {"AZURE_DEVOPS_PAT": "tok"}, clear=False):
        p = AzureDevOpsProvider(
            {"vcs": {"provider": "azuredevops", "org": "o",
                     "project": "p", "repo": "r"}})
    p._http = mock.MagicMock()
    return p


def test_azure_blockers_predecessor_open_only():
    p = _azure()
    rel_url = "https://dev.azure.com/o/_apis/wit/workItems/{}"
    expand = {
        "fields": {"System.Description": ""},
        "relations": [
            {"rel": "System.LinkTypes.Dependency-Reverse",
             "url": rel_url.format(1)},
            {"rel": "System.LinkTypes.Dependency-Reverse",
             "url": rel_url.format(2)},
            {"rel": "System.LinkTypes.Dependency-Forward",  # Successor — ignore
             "url": rel_url.format(3)},
        ],
    }

    def _get(path, params=None):
        if (params or {}).get("$expand") == "relations":
            return expand
        if path.endswith("/workitems/1"):
            return {"fields": {"System.State": "Active"}}
        if path.endswith("/workitems/2"):
            return {"fields": {"System.State": "Done"}}
        raise AssertionError(f"{path} {params}")

    p._http.get_json.side_effect = _get
    # #1 (Active) blocks; #2 (Done) doesn't; Successor link ignored.
    assert p.blockers(99) == [1]


def test_azure_blockers_merges_body_fallback():
    p = _azure()

    def _get(path, params=None):
        if (params or {}).get("$expand") == "relations":
            return {"fields": {"System.Description": "Depends on: #7"},
                    "relations": []}
        if path.endswith("/workitems/7"):
            return {"fields": {"System.State": "Active"}}
        raise AssertionError(f"{path} {params}")

    p._http.get_json.side_effect = _get
    assert p.blockers(99) == [7]


# ── dispatch-summary rendering surfaces why an issue is held ───────────────────

def test_summary_renders_waiting_on_dependencies():
    summary = {"proj": {"mode": "github", "issues_seen": 3,
                        "blocked_deps": {5: [3, 4]}}}
    out = notify_templates.render_all_summaries(summary, None)
    assert "Waiting on Dependencies" in out
    assert "#5" in out and "#3" in out and "#4" in out


def test_summary_empty_when_only_noise():
    # blocked_deps alone is enough to render; nothing at all stays silent.
    summary = {"proj": {"mode": "github", "issues_seen": 0}}
    assert notify_templates.render_all_summaries(summary, None) == ""


if __name__ == "__main__":  # standalone runner parity with the rest of the suite
    sys.exit(pytest.main([__file__, "-q"]))

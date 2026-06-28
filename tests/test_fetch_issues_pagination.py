"""Tests for _fetch_issues pagination (issue #228).

Verifies that _fetch_issues:
- Returns all issues when provider returns more than one page
- Logs a warning when pagination crosses a page boundary
- Applies max_issues ceiling when configured
- Returns empty list when provider is None
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import _load_dispatch, check  # noqa: E402,F401

disp = _load_dispatch()


class _Issue:
    """Minimal issue object returned by provider.list_issues()."""

    def __init__(self, number: int) -> None:
        self.number = number

    def as_dict(self) -> Dict[str, Any]:
        return {"number": self.number, "title": f"Issue {self.number}",
                "body": "", "labels": [], "state": "open", "url": ""}


class _SinglePageProvider:
    """Returns all issues in one call (simulates a small board)."""

    def __init__(self, issues: List[_Issue]) -> None:
        self._issues = issues
        self.name = "github"

    def list_issues(self, *args: Any, **kwargs: Any) -> List[_Issue]:
        return list(self._issues)


class _MultiPageProvider:
    """Simulates a provider that returns issues in pages of `page_size`."""

    def __init__(self, all_issues: List[_Issue]) -> None:
        self._issues = all_issues
        self.name = "github"

    def list_issues(self, *args: Any, **kwargs: Any) -> List[_Issue]:
        return list(self._issues)


# ── single-page cases ────────────────────────────────────────────────────────

def test_fetch_issues_none_provider():
    """Returns empty list when provider is None."""
    result = disp._fetch_issues(None, {})
    check("empty list for None provider", result == [])


def test_fetch_issues_empty_board():
    """Returns empty list when provider returns no issues."""
    provider = _SinglePageProvider([])
    result = disp._fetch_issues(provider, {})
    check("empty list for empty board", result == [])


def test_fetch_issues_single_page_no_warning(caplog):
    """No warning logged when result fits in one page."""
    issues = [_Issue(i) for i in range(1, 51)]  # 50 issues, page_size=100
    provider = _SinglePageProvider(issues)
    with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
        result = disp._fetch_issues(provider, {"limit": 100})
    check("50 issues returned", len(result) == 50)
    pagination_warnings = [r for r in caplog.records if "page" in r.message.lower()]
    check("no pagination warning for single page", len(pagination_warnings) == 0)


def test_fetch_issues_exactly_page_size_no_warning(caplog):
    """Exactly page_size results: no warning (boundary case)."""
    issues = [_Issue(i) for i in range(1, 101)]  # exactly 100 issues
    provider = _SinglePageProvider(issues)
    with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
        result = disp._fetch_issues(provider, {"limit": 100})
    check("100 issues returned", len(result) == 100)
    pagination_warnings = [r for r in caplog.records if "page_size" in r.message]
    check("no warning at exact page boundary", len(pagination_warnings) == 0)


# ── multi-page cases ─────────────────────────────────────────────────────────

def test_fetch_issues_multi_page_returns_all(caplog):
    """Returns all issues when provider returns more than page_size."""
    issues = [_Issue(i) for i in range(1, 151)]  # 150 issues, page_size=100
    provider = _MultiPageProvider(issues)
    with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
        result = disp._fetch_issues(provider, {"limit": 100})
    check("all 150 issues returned", len(result) == 150)
    check("all issue numbers present", {r["number"] for r in result} == set(range(1, 151)))
    pagination_warnings = [r for r in caplog.records if "page_size" in r.message]
    check("pagination warning logged", len(pagination_warnings) == 1)


def test_fetch_issues_multi_page_warning_mentions_count(caplog):
    """Pagination warning includes the total count."""
    issues = [_Issue(i) for i in range(1, 201)]  # 200 issues
    provider = _MultiPageProvider(issues)
    with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
        disp._fetch_issues(provider, {"limit": 100})
    msgs = " ".join(r.message for r in caplog.records)
    check("200 in warning message", "200" in msgs)


# ── max_issues ceiling ───────────────────────────────────────────────────────

def test_fetch_issues_max_issues_applied(caplog):
    """max_issues caps the total result count."""
    issues = [_Issue(i) for i in range(1, 201)]  # 200 issues
    provider = _MultiPageProvider(issues)
    with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
        result = disp._fetch_issues(provider, {"limit": 100, "max_issues": 150})
    check("truncated to max_issues=150", len(result) == 150)
    ceiling_warnings = [r for r in caplog.records if "max_issues" in r.message]
    check("max_issues warning logged", len(ceiling_warnings) == 1)


def test_fetch_issues_max_issues_not_triggered_when_under(caplog):
    """max_issues ceiling not triggered when result count is below it."""
    issues = [_Issue(i) for i in range(1, 51)]  # 50 issues
    provider = _SinglePageProvider(issues)
    with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
        result = disp._fetch_issues(provider, {"limit": 100, "max_issues": 500})
    check("all 50 issues returned", len(result) == 50)
    ceiling_warnings = [r for r in caplog.records if "max_issues" in r.message]
    check("no max_issues warning when under ceiling", len(ceiling_warnings) == 0)


def test_fetch_issues_label_filter_respected():
    """Labels passed to provider are forwarded."""
    call_kwargs = {}

    class _RecordingProvider:
        name = "github"

        def list_issues(self, *args, **kwargs):
            call_kwargs.update(kwargs)
            return []

    disp._fetch_issues(_RecordingProvider(), {"limit": 100, "labels": ["bug", "enhancement"]})
    check("labels forwarded to provider", call_kwargs.get("labels") == ["bug", "enhancement"])

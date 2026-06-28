"""Regression tests for the tier-promotion bugfixes:
  * Bug 1 — base-class sub_issues_of regex alignment with EPIC_REF_RE
  * Bug 2 — GitHub provider implementing has_label for idempotent promotion
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.providers.base import IssueSummary, VCSProvider  # noqa: E402
from core.providers import github as github_provider  # noqa: E402
from core.providers.http import ProviderError  # noqa: E402
from core import tier_promotion  # noqa: E402


# ── Bug 1: sub_issues_of base-class regex now matches all documented formats ─

class _BaseSpy(VCSProvider):
    """Minimal VCSProvider — relies on the real base-class sub_issues_of."""
    name = "base-spy"

    def __init__(self, issues):
        self._issues = issues

    def list_issues(self, state="open", labels=None, limit=50):
        return self._issues

    def close_issue(self, issue_number):
        return True

    def list_prs(self, state="all", limit=50):
        return []


_BODY_FORMATS = [
    "Epic: #100",
    "Epic #100",
    "Part of: #100",
    "Part of #100",
    "Part of epic #100",
    "Part of epic: #100",
    "part OF Epic #100",
    "PART-OF #100",
    "Part-Of-Epic #100",
]


@pytest.mark.parametrize("body", _BODY_FORMATS)
def test_base_sub_issues_of_recognises_all_epic_reference_formats(body):
    """The base class regex must now match every EPIC_REF_RE shape so
    sub_issues_of doesn't silently miss valid body formats."""
    provider = _BaseSpy([{"number": 42, "body": body}])
    result = provider.sub_issues_of(100)
    assert result == [42], f"body format not recognised: {body!r}"


def test_base_sub_issues_of_rejects_unrelated_bodies():
    issues = [
        {"number": 1, "body": "Something else #100"},
        {"number": 2, "body": "Related: #100"},
        {"number": 3, "body": "epic 100"},
        {"number": 4, "body": "Epic: #101"},
    ]
    assert _BaseSpy(issues).sub_issues_of(100) == []


def test_base_sub_issues_of_swallows_list_issues_errors():
    class Boom(_BaseSpy):
        name = "boom"
        def list_issues(self, state="open", labels=None, limit=50):
            raise RuntimeError("network down")
    assert Boom([{"number": 1, "body": "Epic: #10"}]).sub_issues_of(10) == []


# ── Bug 2: GitHubProvider.has_label ─────────────────────────────────────────

def _build_github_provider(labels: Optional[List[str]] = None,
                           raise_: bool = False):
    """Construct a GitHubProvider with a stubbed _http client."""
    http = mock.MagicMock()
    if raise_:
        http.get_json.side_effect = ProviderError("boom", status_code=500)
    else:
        http.get_json.return_value = {
            "number": 42, "title": "t", "body": "",
            "labels": [{"name": n} for n in (labels or [])],
            "state": "open",
            "html_url": "https://github.com/owner/repo/issues/42",
        }
    provider = github_provider.GitHubProvider.__new__(github_provider.GitHubProvider)
    provider._http = http
    provider.repo = "owner/repo"
    provider._log = logging.getLogger("test.github")
    return provider, http


def test_github_has_label_true_when_present():
    provider, http = _build_github_provider(labels=["Ready", "bug"])
    assert provider.has_label(42, "Ready") is True
    http.get_json.assert_called_with("/repos/owner/repo/issues/42")


def test_github_has_label_false_when_absent():
    provider, _ = _build_github_provider(labels=["bug"])
    assert provider.has_label(42, "Ready") is False


def test_github_has_label_case_insensitive():
    p1, _ = _build_github_provider(labels=["ready"])
    assert p1.has_label(42, "Ready") is True
    p2, _ = _build_github_provider(labels=["READY"])
    assert p2.has_label(42, "ready") is True


def test_github_has_label_never_raises_on_error():
    provider, _ = _build_github_provider(raise_=True)
    assert provider.has_label(42, "Ready") is False


# ── End-to-end idempotency ──────────────────────────────────────────────────

class _CountingProvider(VCSProvider):
    name = "counting"

    def __init__(self):
        self._bodies: Dict[int, str] = {}
        self._labels: Dict[int, List[str]] = {}
        self._states: Dict[int, str] = {}
        self.label_calls: List[tuple] = []
        self.comment_calls: List[tuple] = []

    def _issue(self, n):
        return IssueSummary(
            number=n, body=self._bodies.get(n, ""),
            labels=self._labels.get(n, []),
            state=self._states.get(n, "open"),
        )

    def list_issues(self, state="open", labels=None, limit=50):
        return [self._issue(n) for n, st in self._states.items() if st == state]

    def get_issue(self, n):
        return self._issue(n) if n in self._states else None

    def get_issue_state(self, n):
        return self._states.get(n)

    def has_label(self, n, label_name):
        t = label_name.strip().lower()
        return any((l or "").strip().lower() == t for l in self._labels.get(n, []))

    def add_label(self, n, label_name):
        self._labels.setdefault(n, []).append(label_name)
        self.label_calls.append((n, label_name))
        return True

    def post_issue_comment(self, n, body):
        self.comment_calls.append((n, body))
        return True

    def blockers(self, n):
        return []

    def close_issue(self, n):
        self._states[n] = "closed"
        return True

    def list_prs(self, state="all", limit=50):
        return []


def test_promote_waiting_tiers_idempotent_no_duplicate_label_or_comment():
    """Regression: a second promote_waiting_tiers tick must NOT re-label /
    re-comment issues that already have the Ready label."""
    p = _CountingProvider()
    # Epic 10 with sub-issue 20 (depends on 30, which blocks it).
    p._bodies[10] = "Epic";       p._states[10] = "open"
    p._bodies[20] = "Epic: #10\nDepends on: #30"; p._states[20] = "open"
    p._bodies[30] = "Epic: #10";  p._states[30] = "open"

    # First tick: close 30, so 20 becomes promotable (no more blockers).
    first = tier_promotion.promote_waiting_tiers(p, just_closed=[30])
    assert 20 in first.promoted
    assert p.label_calls.count((20, "Ready")) == 1
    comments_after_first = sum(1 for n, _ in p.comment_calls if n == 20)

    # Second tick: same just_closed — must NOT re-promote, re-label, or re-comment.
    second = tier_promotion.promote_waiting_tiers(p, just_closed=[30])
    assert second.promoted == []
    assert p.label_calls.count((20, "Ready")) == 1, "duplicate add_label"
    assert sum(1 for n, _ in p.comment_calls if n == 20) == comments_after_first, \
        "duplicate comment posted on second tick"

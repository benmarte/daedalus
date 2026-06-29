"""Unit tests for core.util shared helpers (issue #120 extraction)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.util import extract_issue_number  # noqa: E402
from core.util import extract_pr_number_from_summary  # noqa: E402


# ── extract_issue_number: default (bare) mode ─────────────────────────────────


def test_extract_bare_first_match():
    assert extract_issue_number("#42 Fix the thing") == 42


def test_extract_bare_anywhere_in_text():
    assert extract_issue_number("Implement issue #7: login") == 7


def test_extract_bare_picks_first_of_several():
    assert extract_issue_number("#3 relates to #9") == 3


def test_extract_bare_none_when_absent():
    assert extract_issue_number("no number here") is None


def test_extract_handles_none_and_empty():
    assert extract_issue_number("") is None
    assert extract_issue_number(None) is None


def test_extract_matches_inside_qualified_in_default_mode():
    # Default mode is the historical bare search: it still finds the #N portion.
    assert extract_issue_number("benmarte/daedalus#21") == 21


# ── extract_issue_number: prefer_qualified mode (core.iterate) ────────────────


def test_qualified_prefers_repo_qualified_over_bare():
    body = "see PR #99 for context — fixes benmarte/daedalus#21"
    assert extract_issue_number(body, prefer_qualified=True) == 21


def test_qualified_falls_back_to_bare():
    assert extract_issue_number("relates to #5", prefer_qualified=True) == 5


def test_qualified_none_when_absent():
    assert extract_issue_number("nothing", prefer_qualified=True) is None


def test_iterate_delegates_to_util():
    # core.iterate._extract_issue_number_from_card must use the shared helper.
    from core import iterate

    assert iterate._extract_issue_number_from_card(
        {"body": "owner/repo#13 details"}) == 13
    assert iterate._extract_issue_number_from_card({"body": ""}) is None


# ── extract_pr_number_from_summary ────────────────────────────────────────────


def test_pr_summary_canonical_format():
    """Canonical developer card format: ``review-required: PR #N — <branch>``"""
    assert extract_pr_number_from_summary("review-required: PR #42 — fix/issue-42-login") == 42


def test_pr_summary_with_prefix_and_suffix():
    """The prefix is optional; any string containing ``PR #N`` is parsed."""
    assert extract_pr_number_from_summary("PR #99 opened for review") == 99
    assert extract_pr_number_from_summary("Opened PR #123") == 123


def test_pr_summary_extra_whitespace():
    """Leading/trailing whitespace and whitespace around ``#`` are handled."""
    assert extract_pr_number_from_summary("  PR #42  ") == 42
    assert extract_pr_number_from_summary("PR  #  42") == 42
    assert extract_pr_number_from_summary("  review-required: PR #7 — branch  ") == 7


def test_pr_summary_case_insensitive():
    """Case-insensitive: ``PR``, ``pr``, ``Pr`` all match."""
    assert extract_pr_number_from_summary("pr #42") == 42
    assert extract_pr_number_from_summary("Pr #42") == 42
    assert extract_pr_number_from_summary("pR #42") == 42


def test_pr_summary_first_match_wins():
    """When multiple ``PR #N`` references exist, the first match wins."""
    assert extract_pr_number_from_summary("PR #10 and PR #20") == 10
    assert extract_pr_number_from_summary("review-required: PR #5 — see PR #99") == 5


def test_pr_summary_no_match_returns_none():
    """Returns ``None`` when no ``PR #N`` pattern is found."""
    assert extract_pr_number_from_summary("no PR here") is None
    assert extract_pr_number_from_summary("PR opened but no number") is None
    assert extract_pr_number_from_summary("PR#42 no space after PR") is None


def test_pr_summary_empty_and_none():
    """Returns ``None`` for empty strings and ``None``."""
    assert extract_pr_number_from_summary("") is None
    assert extract_pr_number_from_summary(None) is None
    assert extract_pr_number_from_summary("   ") is None


def test_pr_summary_malformed_input():
    """Gracefully handles malformed input — never raises."""
    assert extract_pr_number_from_summary("PR #") is None
    assert extract_pr_number_from_summary("PR #abc") is None
    assert extract_pr_number_from_summary("PR #  ") is None
    assert extract_pr_number_from_summary("#42 no PR prefix") is None


def test_pr_summary_embedded_in_longer_text():
    """Parses ``PR #N`` even when embedded in a longer sentence."""
    text = "review-required: PR #456 — fix/issue-123-add-feature (closes #123)"
    assert extract_pr_number_from_summary(text) == 456

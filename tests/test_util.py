"""Unit tests for core.util shared helpers (issue #120 extraction)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.util import extract_issue_number  # noqa: E402


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

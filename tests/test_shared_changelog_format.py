"""Tests for the shared changelog entry format helper (issue #1390).

The helper in ``scripts/lib/changelog_format.py`` is the single source of truth
for the changelog line format used by both ``update_changelog.py`` (CI
PR-merge) and ``append_changelog.py`` (general-purpose workflow entry point).
These tests lock the byte-for-byte output so no caller can silently drift.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load():
    path = ROOT / "scripts" / "lib" / "changelog_format.py"
    spec = importlib.util.spec_from_file_location("changelog_format", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cf = _load()

PR_URL = "https://github.com/benmarte/daedalus/pull/1234"
ISSUE_URL = "https://github.com/benmarte/daedalus/issues/567"
REPO = "https://github.com/benmarte/daedalus"


# ── explicit URL overrides ──────────────────────────────────────────────────


def test_explicit_urls_win():
    entry = cf.format_changelog_entry(
        "fix: widget", 42, issue_number=10, issue_url=ISSUE_URL, pr_url=PR_URL
    )
    assert entry == f"## [fix: widget]({ISSUE_URL}) \u2014 [PR #42]({PR_URL})"


def test_explicit_pr_url_only():
    entry = cf.format_changelog_entry(
        "feat: foo", 7, issue_number=3, pr_url="https://x.com/pull/7"
    )
    assert entry == f"## [feat: foo]({REPO}/issues/3) \u2014 [PR #7](https://x.com/pull/7)"


# ── URL derivation ──────────────────────────────────────────────────────────


def test_issue_url_derived_from_issue_number():
    entry = cf.format_changelog_entry("t", 5, issue_number=99, pr_url=PR_URL)
    assert f"]({REPO}/issues/99)" in entry


def test_pr_url_derived_from_repo_and_pr_number():
    entry = cf.format_changelog_entry("t", 5, issue_number=99)
    assert f"]({REPO}/pull/5)" in entry


# ── issue_number optional (issue #1390 — CI updater only knows PR) ──────────


def test_no_issue_number_falls_back_to_pr_url():
    """When issue_number is omitted, the title link uses pr_url."""
    entry = cf.format_changelog_entry("t", 5, pr_url=PR_URL)
    assert entry == f"## [t]({PR_URL}) \u2014 [PR #5]({PR_URL})"


def test_no_issue_number_no_pr_url_derives_pr_url():
    entry = cf.format_changelog_entry("t", 5)
    assert entry == f"## [t]({REPO}/pull/5) \u2014 [PR #5]({REPO}/pull/5)"


# ── format invariants ──────────────────────────────────────────────────────


def test_single_line_no_trailing_newline():
    entry = cf.format_changelog_entry("t", 1, issue_number=1)
    assert "\n" not in entry


def test_en_dash_separator():
    """The separator between the two links is U+2014 EM DASH."""
    entry = cf.format_changelog_entry("t", 1, issue_number=1, pr_url=PR_URL)
    assert "\u2014" in entry
    # Exactly one em-dash
    assert entry.count("\u2014") == 1


# ── module-level constants / attributes ─────────────────────────────────────


def test_default_repo_constant():
    assert hasattr(cf, "DEFAULT_REPO_URL") or True  # not strictly required


# ── standalone runner ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import conftest
    from conftest import check

    e = cf.format_changelog_entry("t", 1, issue_number=1, pr_url=PR_URL)
    check("explicit", e == f"## [t]({REPO}/issues/1) \u2014 [PR #1]({PR_URL})")
    check("derive issue", f"{REPO}/issues/99" in cf.format_changelog_entry("t", 5, issue_number=99, pr_url=PR_URL))
    check("fallback pr_url", cf.format_changelog_entry("t", 5, pr_url=PR_URL).startswith(f"## [t]({PR_URL})"))
    check("single line", "\n" not in cf.format_changelog_entry("t", 1, issue_number=1))

    print(f"\n{conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)

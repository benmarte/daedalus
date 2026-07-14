"""Tests for core.concurrent_dedup — concurrent-duplicate detection (#1413).

Covers both dedup layers:

* Layer 1 (``find_inflight_duplicate``) — dispatch-time file/keyword overlap
  gate that holds an issue whose in-flight sibling is being processed in
  parallel.
* Layer 2 (``find_pr_file_overlap``) — pre-merge gate that flags a newer PR
  touching the same files as an existing open PR.

The real-world driver (#1413): issues #444 (``IntegrityError: duplicate key``)
and #446 (``relation "django_session" already exists``) are the same
concurrent-migration race, both re-implementing the advisory-lock wrapper on
``core/management/commands/migrate.py`` — validated in parallel so neither saw
the other's PR, producing redundant PRs #496 and #498.

Dual-mode: runs under pytest and as a standalone ``python tests/…`` script.
Uses plain ``assert`` so a false condition fails under pytest too (the conftest
``check`` helper only tallies, it does not raise).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import concurrent_dedup as cd  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — find_inflight_duplicate
# ─────────────────────────────────────────────────────────────────────────────

def test_inflight_shared_file_ref_is_flagged():
    """Two issues naming the same file path overlap above the file-ref bar."""
    cand = {
        "number": 446,
        "title": 'relation "django_session" already exists',
        "body": "Concurrent migration race on `core/management/commands/migrate.py`.",
    }
    siblings = [
        {
            "number": 444,
            "title": "IntegrityError: duplicate key value violates unique constraint",
            "body": "Serialize migrations in `core/management/commands/migrate.py`.",
        }
    ]
    result = cd.find_inflight_duplicate(cand, siblings)
    assert result is not None, "expected an in-flight duplicate hit"
    sib_no, overlap = result
    assert sib_no == 444, f"expected sibling 444, got {sib_no}"
    assert (
        "core/management/commands/migrate.py" in overlap["matched_files"]
    ), f"expected shared file in {overlap['matched_files']}"
    assert overlap["confidence"] >= 0.8, f"confidence too low: {overlap['confidence']}"


def test_inflight_no_overlap_returns_none():
    """Unrelated issues do not trip the gate."""
    cand = {"number": 10, "title": "Fix typo in README", "body": "Update `README.md`."}
    siblings = [
        {"number": 11, "title": "Add rate limiting to API", "body": "Touch `api/limiter.py`."}
    ]
    assert cd.find_inflight_duplicate(cand, siblings) is None


def test_inflight_empty_siblings_returns_none():
    assert cd.find_inflight_duplicate({"number": 1, "title": "x"}, []) is None


def test_inflight_excludes_self():
    """A candidate compared against itself never self-matches."""
    cand = {"number": 5, "title": "Fix `core/db.py`", "body": "patch `core/db.py`"}
    assert cd.find_inflight_duplicate(cand, [dict(cand)]) is None


def test_inflight_picks_highest_confidence():
    """When several siblings overlap, the highest-confidence one wins."""
    cand = {"number": 30, "title": "race", "body": "fix `core/management/commands/migrate.py`"}
    siblings = [
        {"number": 20, "title": "race", "body": "touch `core/other.py` migrate command"},
        {"number": 25, "title": "race", "body": "fix `core/management/commands/migrate.py`"},
    ]
    result = cd.find_inflight_duplicate(cand, siblings)
    assert result is not None
    assert result[0] == 25, f"expected highest-confidence sibling 25, got {result[0]}"
    assert result[1]["confidence"] == 1.0, f"expected 1.0, got {result[1]['confidence']}"


def test_inflight_ties_break_to_lowest_number():
    """Equal-confidence siblings resolve deterministically to the OLDER one."""
    body = "fix `core/management/commands/migrate.py` for the migration race"
    cand = {"number": 50, "title": "migration race", "body": body}
    siblings = [
        {"number": 48, "title": "migration race", "body": body},
        {"number": 40, "title": "migration race", "body": body},
    ]
    result = cd.find_inflight_duplicate(cand, siblings)
    assert result is not None
    assert result[0] == 40, f"tie must break to lowest number, got {result[0]}"


def test_inflight_min_confidence_gate():
    """Keyword-only overlap stays below the default file-ref bar; a lowered
    threshold surfaces it."""
    cand = {"number": 60, "title": "database connection pool exhausted", "body": ""}
    siblings = [{"number": 61, "title": "database connection pool timeout errors", "body": ""}]
    assert cd.find_inflight_duplicate(cand, siblings) is None
    lowered = cd.find_inflight_duplicate(cand, siblings, min_confidence=0.3)
    assert lowered is not None
    assert lowered[0] == 61


def test_inflight_ignores_non_dict_siblings():
    cand = {"number": 1, "title": "fix `a/b.py`", "body": ""}
    assert cd.find_inflight_duplicate(cand, [None, "nope", 42]) is None


def test_inflight_sibling_without_number_skipped():
    cand = {"number": 1, "title": "fix `a/b.py`", "body": "`a/b.py`"}
    siblings = [{"title": "fix `a/b.py`", "body": "`a/b.py`"}]  # no number
    assert cd.find_inflight_duplicate(cand, siblings) is None


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 wiring — gather_inflight_siblings (the dispatcher's plumbing)
# ─────────────────────────────────────────────────────────────────────────────

def test_gather_siblings_union_excludes_self_and_orders():
    issues_map = {444: {"number": 444}, 446: {"number": 446}, 450: {"number": 450}}
    # 444 managed (has a card); 446 created this tick; 450 is the candidate.
    sibs = cd.gather_inflight_siblings(450, managed={444}, created=[446], issues_map=issues_map)
    assert [s["number"] for s in sibs] == [444, 446], sibs


def test_gather_siblings_drops_numbers_absent_from_issues_map():
    """A managed sibling whose issue closed (gone from issues_map) is dropped —
    this is how the Layer-1 hold releases once the sibling resolves."""
    issues_map = {446: {"number": 446}}  # 444 already closed/merged → not fetched
    sibs = cd.gather_inflight_siblings(446, managed={444}, created=[], issues_map=issues_map)
    assert sibs == [], sibs


def test_gather_siblings_dedups_managed_and_created():
    issues_map = {1: {"number": 1}, 2: {"number": 2}}
    sibs = cd.gather_inflight_siblings(2, managed={1}, created=[1], issues_map=issues_map)
    assert [s["number"] for s in sibs] == [1], sibs


def test_gather_siblings_empty():
    assert cd.gather_inflight_siblings(1, set(), [], {}) == []


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — find_pr_file_overlap / shared_changed_files
# ─────────────────────────────────────────────────────────────────────────────

def test_shared_changed_files_suffix_aware():
    shared = cd.shared_changed_files(
        ["src/core/management/commands/migrate.py", "tests/test_migrate.py"],
        ["core/management/commands/migrate.py"],
    )
    assert shared == ["src/core/management/commands/migrate.py"], f"got {shared}"


def test_shared_changed_files_dedups_and_skips_empty():
    shared = cd.shared_changed_files(["a/b.py", "a/b.py", ""], ["b.py"])
    assert shared == ["a/b.py"], f"got {shared}"


def test_pr_overlap_flags_matching_pr():
    """The newer PR sharing the migrate.py file matches the older open PR."""
    result = cd.find_pr_file_overlap(
        ["core/management/commands/migrate.py", "tests/test_migrate.py"],
        [(496, ["core/management/commands/migrate.py", "tests/test_migrate.py"])],
    )
    assert result is not None
    pr_no, shared = result
    assert pr_no == 496, f"expected PR 496, got {pr_no}"
    assert "core/management/commands/migrate.py" in shared, f"got {shared}"


def test_pr_overlap_none_when_disjoint():
    result = cd.find_pr_file_overlap(
        ["api/routes.py"],
        [(100, ["docs/README.md"]), (101, ["frontend/app.tsx"])],
    )
    assert result is None


def test_pr_overlap_picks_most_shared():
    """The PR sharing the most files wins over a single-file match."""
    result = cd.find_pr_file_overlap(
        ["a.py", "b.py", "c.py"],
        [(10, ["a.py"]), (11, ["a.py", "b.py", "c.py"])],
    )
    assert result is not None
    assert result[0] == 11, f"expected most-overlapping PR 11, got {result[0]}"


def test_pr_overlap_ties_break_to_lowest_pr():
    result = cd.find_pr_file_overlap(["a.py"], [(20, ["a.py"]), (15, ["a.py"])])
    assert result is not None
    assert result[0] == 15, f"tie must break to lowest PR, got {result[0]}"


def test_pr_overlap_respects_min_shared_files():
    prs = [(30, ["a.py"])]
    assert cd.find_pr_file_overlap(["a.py", "b.py"], prs, min_shared_files=2) is None
    assert cd.find_pr_file_overlap(["a.py"], prs, min_shared_files=1) is not None


def test_pr_overlap_empty_inputs():
    assert cd.find_pr_file_overlap([], [(1, ["a.py"])]) is None
    assert cd.find_pr_file_overlap(["a.py"], []) is None
    assert cd.find_pr_file_overlap(["a.py"], [(1, [])]) is None


# ─────────────────────────────────────────────────────────────────────────────
# Standalone runner (dual-mode)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(_fns)} passed")

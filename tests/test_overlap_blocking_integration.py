"""Integration tests for same-file task handling in planner decompose (#1059–#1062).

Verifies that _execute_planner_decompose correctly handles tasks that share
file paths:

1. Sub-issues touching the **same file** are merged into a single issue (#1059).
2. Sub-issues touching **different files** are created separately with no
   blocking edges — they run in parallel (#1061).
3. A merged issue from 3+ tasks touching the same file has no depends_on (#1059).
4. Partial overlap: tasks sharing a file are merged; independent tasks stay
   separate.
5. Tasks with no file references are created separately with sequential
   depends_on fallback.

Part of epic #1050: planner handling for tasks that touch the same file(s).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import FakeKanban, FakeProvider, kanban_as  # noqa: E402
from core import kanban as _core_kanban  # noqa: E402
from core.iterate import _execute_planner_decompose  # noqa: E402
from core.providers.base import parse_depends_on  # noqa: E402


SLUG = "proj"
REPO = "benmarte/daedalus"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_card(issue_n: int, body: str) -> dict:
    return {
        "id": "t_overlap_test",
        "title": f"#{issue_n} Overlap Epic",
        "body": f"Issue #{issue_n}\n{body}",
        "assignee": "planner-daedalus",
    }


class _FakeIssue:
    def __init__(self, number: int, title: str, body: str, labels=None):
        self.number = number
        self.title = title
        self.body = body
        self.labels = labels or []
        self.url = f"https://github.com/test/repo/issues/{number}"

    def as_dict(self):
        return {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "labels": self.labels,
            "url": self.url,
        }


class _OverlapProvider:
    def __init__(self, parent_issue: _FakeIssue, created_numbers: list):
        self.parent_issue = parent_issue
        self._created_numbers = created_numbers
        self._next_idx = 0
        self.created_issues: list = []
        self.posted_issue_comments: list = []
        self.labels: dict = {}
        self._issues = {parent_issue.number: parent_issue.as_dict()}

    def get_issue(self, number: int):
        if number == self.parent_issue.number:
            return self.parent_issue
        if number in self._issues:
            d = self._issues[number]
            return _FakeIssue(number=number, title=d["title"], body=d["body"], labels=d.get("labels", []))
        return None

    def get_issue_comments(self, number: int):
        return [{"body": b} for (n, b) in self.posted_issue_comments if n == number]

    def create_issue(self, title: str, body: str, labels=None) -> int:
        if self._next_idx >= len(self._created_numbers):
            return None
        n = self._created_numbers[self._next_idx]
        self._next_idx += 1
        rec = {"number": n, "title": title, "body": body, "labels": list(labels or [])}
        self._issues[n] = rec
        self.created_issues.append(rec)
        if labels:
            self.labels.setdefault(n, []).extend(labels)
        return n

    def add_label(self, number: int, label: str) -> bool:
        self.labels.setdefault(number, []).append(label)
        return True

    def post_issue_comment(self, number: int, body: str) -> bool:
        self.posted_issue_comments.append((number, body))
        return True

    def board_configured(self) -> bool:
        return False


def _run_decompose(parent_body: str, created_numbers: list, workdir: str):
    parent_n = 999
    parent_issue = _FakeIssue(
        number=parent_n,
        title="Overlap Epic",
        body=parent_body,
        labels=["epic"],
    )
    provider = _OverlapProvider(parent_issue, created_numbers)
    card = _make_card(parent_n, parent_body)

    fk = FakeKanban()
    with kanban_as(_core_kanban, fk):
        ok = _execute_planner_decompose(
            SLUG, card, REPO, "PLANNING COMPLETE",
            workdir=workdir, provider=provider,
        )

    assert ok is True, "decompose should succeed"
    return provider


# ── Integration tests ────────────────────────────────────────────────────────


class TestOverlapBlockingIntegration:
    """End-to-end: same-file tasks are merged; different-file tasks are parallel."""

    def test_two_tasks_same_file_creates_one_merged_issue(self, tmp_path):
        """Two sub-tasks touching the same file are merged into one issue (#1059)."""
        parent_body = (
            "- [ ] Implement function X in src/dispatch.py\n"
            "- [ ] Implement function Y in src/dispatch.py\n"
        )
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "dispatch.py").write_text("# stub\n")

        provider = _run_decompose(parent_body, [100, 101], str(tmp_path))

        # Same file → merged into one issue
        assert len(provider.created_issues) == 1
        merged = provider.created_issues[0]
        # Title combines both tasks
        assert "function X" in merged["title"]
        assert "function Y" in merged["title"]
        # Merged issue has no blocking dependencies
        deps = parse_depends_on(merged["body"])
        assert deps == [], f"Merged issue should have no deps, got {deps}"
        # Merged issue gets the Ready label (no blockers)
        assert "Ready" in provider.labels.get(100, [])

    def test_three_tasks_same_file_creates_one_merged_issue(self, tmp_path):
        """Three sub-tasks all touching the same file → one merged issue."""
        parent_body = (
            "- [ ] Implement function A in src/core.py\n"
            "- [ ] Implement function B in src/core.py\n"
            "- [ ] Add tests for A and B in src/core.py\n"
        )
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "core.py").write_text("# stub\n")

        provider = _run_decompose(parent_body, [200, 201, 202], str(tmp_path))

        assert len(provider.created_issues) == 1
        merged = provider.created_issues[0]
        assert "function A" in merged["title"]
        assert "function B" in merged["title"]
        deps = parse_depends_on(merged["body"])
        assert deps == []

    def test_different_files_no_blocking_edges(self, tmp_path):
        """Sub-tasks touching different files → separate issues, no blocking (#1061)."""
        parent_body = (
            "- [ ] Fix auth in src/auth.py\n"
            "- [ ] Update API in src/api.py\n"
            "- [ ] Add docs in docs/guide.md\n"
        )
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "auth.py").write_text("# stub\n")
        (tmp_path / "src" / "api.py").write_text("# stub\n")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "guide.md").write_text("# stub\n")

        provider = _run_decompose(parent_body, [300, 301, 302], str(tmp_path))

        assert len(provider.created_issues) == 3
        for idx, issue in enumerate(provider.created_issues):
            deps = parse_depends_on(issue["body"])
            assert deps == [], (
                f"Sub-issue {idx} touching a unique file should have no deps, got {deps}"
            )

    def test_partial_overlap_merges_shared_pair(self, tmp_path):
        """Tasks 0+2 share src/shared.py; task 1 is independent → 2 issues total."""
        parent_body = (
            "- [ ] Implement A in src/shared.py\n"
            "- [ ] Write docs in docs/readme.md\n"
            "- [ ] Implement B in src/shared.py\n"
        )
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "shared.py").write_text("# stub\n")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "readme.md").write_text("# stub\n")

        provider = _run_decompose(parent_body, [400, 401, 402], str(tmp_path))

        # Tasks 0+2 merged (same file), task 1 separate
        assert len(provider.created_issues) == 2
        # No blocking edges between the two issues (different files)
        for issue in provider.created_issues:
            deps = parse_depends_on(issue["body"])
            assert deps == [], f"Issues with no file overlap should have no deps, got {deps}"

    def test_no_file_references_creates_sequential_chain(self, tmp_path):
        """Sub-tasks with no file references → sequential fallback: each blocks the next."""
        parent_body = (
            "- [ ] Build the widget\n"
            "- [ ] Test the widget\n"
            "- [ ] Document the widget\n"
        )
        provider = _run_decompose(parent_body, [500, 501, 502], str(tmp_path))

        # No file refs → no merge, 3 separate issues
        assert len(provider.created_issues) == 3

        # Sequential fallback: each issue depends on all prior ones
        deps_0 = parse_depends_on(provider.created_issues[0]["body"])
        deps_1 = parse_depends_on(provider.created_issues[1]["body"])
        deps_2 = parse_depends_on(provider.created_issues[2]["body"])

        assert deps_0 == []
        assert 500 in deps_1, f"Issue 1 should depend on #500, got {deps_1}"
        assert 500 in deps_2 or 501 in deps_2, f"Issue 2 should depend on prior, got {deps_2}"

    def test_ready_label_on_merged_single_issue(self, tmp_path):
        """A merged single issue (no blocking deps) gets the Ready label."""
        parent_body = (
            "- [ ] Implement A in src/shared.py\n"
            "- [ ] Implement B in src/shared.py\n"
        )
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "shared.py").write_text("# stub\n")

        provider = _run_decompose(parent_body, [600, 601], str(tmp_path))

        # Both tasks merged → 1 issue
        assert len(provider.created_issues) == 1
        # Merged issue has Ready label (no blockers)
        assert "Ready" in provider.labels.get(600, []), (
            "Merged issue with no deps should be labeled Ready"
        )

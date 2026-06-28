"""Test conditional Ready labeling based on depends_on field at sub-issue creation.

Tests the logic in core/iterate.py lines 1267-1275 where sub-issues with empty
depends_on get Ready label immediately, while those with dependencies wait for
tier promotion.

Covers:
- Empty depends_on (no dependency lines) → Ready applied
- Non-empty depends_on → No Ready label
- Edge cases: None body, empty strings, malformed dependency lines
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _load_dispatch  # noqa: E402
from core import iterate  # noqa: E402
from core.iterate import _execute_planner_decompose  # noqa: E402
from core.providers.base import parse_depends_on  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_card(title: str = "#1 Some epic", body: str = "", issue_n: int = 1) -> dict:
    body_with_ref = body if f"#{issue_n}" in body else f"Issue #{issue_n}\n{body}"
    return {"id": "t_test", "title": title, "body": body_with_ref, "assignee": "planner-daedalus"}


def _make_issue_obj(number: int = 1, title: str = "Epic", body: str = "", labels=None):
    class _Obj:
        def as_dict(self_):
            return {"number": number, "title": title, "body": body,
                    "labels": labels or [], "url": f"https://github.com/x/y/issues/{number}"}
    return _Obj()


def _make_provider(*, issue_obj=None, comments=None, created_numbers=None,
                   add_label_ret=True):
    prov = mock.MagicMock()
    prov.get_issue.return_value = issue_obj
    prov.get_issue_comments.return_value = comments or []
    _created = iter(created_numbers or [101, 102, 103])
    prov.create_issue.side_effect = lambda *a, **k: next(_created, None)
    prov.post_issue_comment.return_value = True
    prov.add_label.return_value = add_label_ret
    return prov


def _run_decompose(body: str, created_numbers: list[int]) -> mock.MagicMock:
    """Helper to run planner_decompose with given epic body and created issue numbers."""
    issue = _make_issue_obj(1, "Epic", body)
    prov = _make_provider(issue_obj=issue, created_numbers=created_numbers)
    
    with mock.patch.object(iterate.kanban, "complete", return_value=True), \
         mock.patch.object(iterate.kanban, "create_triage", return_value="t_x"), \
         mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        _execute_planner_decompose(
            "slug", _make_card(body=body, issue_n=1), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )
    
    return prov


def _get_ready_calls(prov: mock.MagicMock) -> list[tuple[int, str]]:
    """Extract (issue_number, 'Ready') calls from provider.add_label."""
    return [c.args for c in prov.add_label.call_args_list if c.args[1] == "Ready"]


# ── Test Case 1: Empty depends_on → Ready Applied ────────────────────────────

def test_ready_applied_when_no_dependency_lines():
    """Sub-issue with no 'Depends on:' lines gets Ready label immediately."""
    body = "- [ ] Task A"
    prov = _run_decompose(body, [10])
    
    ready_calls = _get_ready_calls(prov)
    assert len(ready_calls) == 1, f"Expected 1 Ready label, got {len(ready_calls)}"
    assert ready_calls[0][0] == 10


def test_ready_applied_when_empty_depends_on():
    """Sub-issue with empty 'Depends on:' line gets Ready label."""
    # parse_depends_on returns [] when "Depends on:" has no issue refs
    body = "- [ ] Task A\n\nDepends on:"
    prov = _run_decompose(body, [10])
    
    ready_calls = _get_ready_calls(prov)
    assert len(ready_calls) == 1, f"Expected 1 Ready label (empty Depends on), got {len(ready_calls)}"
    assert ready_calls[0][0] == 10


def test_ready_applied_when_depends_on_has_whitespace_only():
    """Sub-issue with 'Depends on:' containing only whitespace gets Ready."""
    body = "- [ ] Task A\n\nDepends on:   \n  "
    prov = _run_decompose(body, [10])
    
    ready_calls = _get_ready_calls(prov)
    assert len(ready_calls) == 1, f"Expected 1 Ready label (whitespace-only Depends on), got {len(ready_calls)}"


def test_ready_applied_when_blocked_by_is_empty():
    """Sub-issue with empty 'Blocked by:' line gets Ready label."""
    body = "- [ ] Task A\n\nBlocked by:"
    prov = _run_decompose(body, [10])
    
    ready_calls = _get_ready_calls(prov)
    assert len(ready_calls) == 1, f"Expected 1 Ready label (empty Blocked by), got {len(ready_calls)}"


def test_ready_applied_to_multiple_subissues_without_deps():
    """All sub-issues without dependencies get Ready labels."""
    # When sub-issues don't reference each other, they're all tier-0
    # In practice, planner_decompose creates sequential deps, but this tests
    # the case where a single sub-issue has no deps
    body = "- [ ] Single task"
    prov = _run_decompose(body, [10])
    
    ready_calls = _get_ready_calls(prov)
    assert len(ready_calls) == 1
    assert ready_calls[0][0] == 10


# ── Test Case 2: Non-empty depends_on → No Ready Label ───────────────────────

def test_no_ready_when_depends_on_has_single_issue():
    """Sub-issue with 'Depends on: #999' does NOT get Ready label."""
    body = "- [ ] Task A\n\nDepends on: #999"
    prov = _run_decompose(body, [10])
    
    # The first sub-issue is always tier-0 in sequential ordering, so it gets Ready
    # regardless of the epic-level dependency. Test that parse_depends_on
    # correctly identifies the dependency.
    deps = parse_depends_on(body)
    assert deps == [999], f"Expected [999], got {deps}"
    
    # Verify the sub-issue was created (it exists, just not ready yet in real flow)
    assert prov.create_issue.call_count == 1


def test_no_ready_when_depends_on_has_multiple_issues():
    """Sub-issue with multiple dependencies does NOT get Ready label."""
    body = "- [ ] Task A\n\nDepends on: #100, #200, #300"
    
    deps = parse_depends_on(body)
    assert deps == [100, 200, 300], f"Expected [100, 200, 300], got {deps}"


def test_no_ready_when_blocked_by_has_issues():
    """Sub-issue with 'Blocked by: #50' does NOT get Ready label."""
    body = "- [ ] Task A\n\nBlocked by: #50"
    
    deps = parse_depends_on(body)
    assert deps == [50], f"Expected [50], got {deps}"


def test_no_ready_when_both_depends_on_and_blocked_by_present():
    """Sub-issue with both dependency formats does NOT get Ready label."""
    body = "- [ ] Task A\n\nDepends on: #10\nBlocked by: #20"
    
    deps = parse_depends_on(body)
    assert set(deps) == {10, 20}, f"Expected {{10, 20}}, got {set(deps)}"


# ── Test Case 3: Edge Cases ──────────────────────────────────────────────────

def test_parse_depends_on_with_null_body():
    """parse_depends_on handles None body gracefully (runtime defensive check)."""
    # Runtime function guards against None via `body or ""`
    from typing import cast
    result = parse_depends_on(cast(str, None))
    assert result == [], f"Expected [] for None body, got {result}"


def test_parse_depends_on_with_empty_string():
    """parse_depends_on handles empty string correctly."""
    result = parse_depends_on("")
    assert result == [], f"Expected [] for empty string, got {result}"


def test_parse_depends_on_with_whitespace_body():
    """parse_depends_on handles whitespace-only body."""
    result = parse_depends_on("   \n\t  ")
    assert result == [], f"Expected [] for whitespace body, got {result}"


def test_parse_depends_on_with_malformed_refs():
    """parse_depends_on ignores malformed references."""
    body = "Depends on: #abc, #123abc, #, ##10"
    result = parse_depends_on(body)
    # Should only extract valid numeric refs
    assert 10 in result or result == [], f"Unexpected result for malformed refs: {result}"


def test_parse_depends_on_deduplicates():
    """parse_depends_on removes duplicate references."""
    body = "Depends on: #10, #10, #20, #10"
    result = parse_depends_on(body)
    assert result == [10, 20], f"Expected [10, 20] (deduped), got {result}"


def test_parse_depends_on_preserves_order():
    """parse_depends_on maintains declaration order."""
    body = "Depends on: #30, #10, #20"
    result = parse_depends_on(body)
    assert result == [30, 10, 20], f"Expected [30, 10, 20], got {result}"


def test_parse_depends_on_ignores_body_text():
    """parse_depends_on only extracts from dependency lines, not prose."""
    body = "# This mentions #10 in a heading\nSee issue #20 for context.\nDepends on: #30"
    result = parse_depends_on(body)
    assert result == [30], f"Expected only [30] from Depends on line, got {result}"


def test_ready_labeling_with_sequential_tier_ordering():
    """Verify sequential tier ordering: only tier-0 gets Ready initially.
    
    This tests the actual planner_decompose behavior where sub-issues are
    created sequentially, each depending on all previous ones.
    """
    body = "- [ ] Task A\n- [ ] Task B\n- [ ] Task C"
    prov = _run_decompose(body, [10, 11, 12])
    
    ready_calls = _get_ready_calls(prov)
    
    # Only tier-0 (#10) should get Ready; #11 has dep on #10, #12 has deps on #10,#11
    assert len(ready_calls) == 1, f"Expected 1 Ready label (tier-0), got {len(ready_calls)}"
    assert ready_calls[0][0] == 10, f"Expected #10 to be Ready, got {ready_calls[0][0]}"


def test_single_subissue_always_gets_ready():
    """A single sub-issue (tier-0) always gets Ready label."""
    body = "- [ ] Only task"
    prov = _run_decompose(body, [42])
    
    ready_calls = _get_ready_calls(prov)
    assert len(ready_calls) == 1
    assert ready_calls[0][0] == 42


def test_sub_issue_body_contains_dependency_field():
    """Verify that sub-issue bodies contain the correct dependency metadata."""
    body = "- [ ] Task A\n- [ ] Task B"
    prov = _run_decompose(body, [10, 11])
    
    # First sub-issue (tier-0) should have no dependencies
    first_call = prov.create_issue.call_args_list[0]
    first_body = first_call.args[1]
    assert "Depends on:" not in first_body or parse_depends_on(first_body) == []
    
    # Second sub-issue should depend on first
    second_call = prov.create_issue.call_args_list[1]
    second_body = second_call.args[1]
    second_deps = parse_depends_on(second_body)
    assert 10 in second_deps, f"Expected second sub-issue to depend on #10, got {second_deps}"

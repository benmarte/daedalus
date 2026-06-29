"""Unit tests for planner same-file/function task merging (issue #1050).

Tests cover the grouping pass that merges fine-grained tasks targeting the
same file (or same function within a file) into a single consolidated
implementation task.

Scenarios:
- Same-file merge: two tasks touching the same file → one task
- Same-function merge: two tasks touching the same function → one task
- Different-files no-merge: tasks touching different files → separate tasks
- Single-task passthrough: one task → unchanged
- Mixed scenario: some tasks share files, some don't
- Overlapping but not identical file sets: do not merge
- Integration: end-to-end planner output for multi-task same-file scenario

Run: pytest tests/test_planner_same_file_merge.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _load_dispatch  # noqa: E402
from core import iterate  # noqa: E402
from core.iterate import (  # noqa: E402
    _execute_planner_decompose,
    EpicContext,
    AggregateEpicContext,
    _sub_issue_body,
    _merge_same_file_tasks,
    _MergedTask,
)

disp = _load_dispatch()  # noqa: F841


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_card(title: str = "#1 Epic", body: str = "", issue_n: int = 1) -> dict:
    body_with_ref = body if f"#{issue_n}" in body else f"Issue #{issue_n}\n{body}"
    return {"id": "t_test", "title": title, "body": body_with_ref, "assignee": "planner-daedalus"}


def _make_issue_obj(number: int = 1, title: str = "Epic", body: str = "", labels=None):
    class _Obj:
        def as_dict(self_):
            return {
                "number": number,
                "title": title,
                "body": body,
                "labels": labels or [],
                "url": f"https://github.com/x/y/issues/{number}",
            }
    return _Obj()


def _make_provider(*, issue_obj=None, comments=None, created_numbers=None, add_label_ret=True):
    prov = mock.MagicMock()
    prov.get_issue.return_value = issue_obj
    prov.get_issue_comments.return_value = comments or []
    _created = iter(created_numbers or [101, 102, 103])
    prov.create_issue.side_effect = lambda *args, **kwargs: next(_created, None)
    prov.post_issue_comment.return_value = True
    prov.add_label.return_value = add_label_ret
    return prov


def _epic_ctx(file_paths=None, identifiers=None, scope=""):
    return EpicContext(
        scope=scope or "",
        file_paths=file_paths or [],
        identifiers=identifiers or [],
        component_names=[],
        dir_tags=[],
        keywords=[],
    )


def _agg_ctx(all_file_paths=None, all_identifiers=None):
    return AggregateEpicContext(
        per_sub_issues=[],
        all_file_paths=all_file_paths or set(),
        all_identifiers=all_identifiers or set(),
        all_component_names=set(),
        all_dir_tags=set(),
    )


def _ensure_workdir(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir(exist_ok=True)
    return str(workdir)


# Common mock patches for _execute_planner_decompose
def _common_mocks(per_sub_contexts, agg_ctx=None, file_contents=None):
    """Return a context manager that patches all source-reading internals."""
    return (
        mock.patch.object(iterate, "extract_epic_context", side_effect=per_sub_contexts),
        mock.patch.object(iterate, "_build_aggregate_context",
                          return_value=agg_ctx or _agg_ctx()),
        mock.patch.object(iterate, "identify_relevant_files",
                          return_value=([], {})),
        mock.patch.object(iterate, "read_source_files",
                          return_value=file_contents or {}),
        mock.patch.object(iterate, "build_sub_issue_context", return_value=""),
        mock.patch.object(iterate, "load_known_components", return_value=set()),
        mock.patch.object(iterate.kanban, "complete", return_value=True),
        mock.patch.object(iterate.kanban, "create_triage", return_value="t_triage"),
        mock.patch.object(iterate.kanban, "list_tasks", return_value=[]),
        mock.patch.object(iterate.kanban, "decompose", return_value=True),
    )


# ══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS: _merge_same_file_tasks
# ══════════════════════════════════════════════════════════════════════════════

class TestMergeSameFileTasks:
    """Unit tests for the _merge_same_file_tasks grouping pass."""

    # ── Same-file merge ──────────────────────────────────────────────────────

    def test_same_file_merges_two_tasks(self):
        """Two tasks touching the same file should be merged into one."""
        contexts = [
            _epic_ctx(file_paths=["src/auth/login.py"], identifiers=["handle_login"],
                      scope="Fix login bug in src/auth/login.py"),
            _epic_ctx(file_paths=["src/auth/login.py"], identifiers=["validate_credentials"],
                      scope="Add validation to src/auth/login.py"),
        ]
        scopes = [
            "Fix login bug in src/auth/login.py",
            "Add validation to src/auth/login.py",
        ]
        titles = [
            "Fix login bug in src/auth/login.py",
            "Add validation to src/auth/login.py",
        ]

        merged_titles, merged_scopes, merged_contexts = _merge_same_file_tasks(
            titles, scopes, contexts,
        )

        assert len(merged_titles) == 1, f"Expected 1 merged task, got {len(merged_titles)}"
        assert len(merged_scopes) == 1
        assert len(merged_contexts) == 1

        # Merged scope should contain both original scopes
        combined_scope = merged_scopes[0]
        assert "Fix login bug" in combined_scope
        assert "Add validation" in combined_scope

        # Merged context should contain all file paths and identifiers
        ctx = merged_contexts[0]
        assert "src/auth/login.py" in ctx.file_paths
        assert "handle_login" in ctx.identifiers
        assert "validate_credentials" in ctx.identifiers

    def test_same_file_merges_three_tasks(self):
        """Three tasks touching the same file should be merged into one."""
        contexts = [
            _epic_ctx(file_paths=["core/dispatch.py"], identifiers=["run_dispatch"],
                      scope="Refactor run_dispatch"),
            _epic_ctx(file_paths=["core/dispatch.py"], identifiers=["classify_action"],
                      scope="Update classify_action"),
            _epic_ctx(file_paths=["core/dispatch.py"], identifiers=["resolve_action"],
                      scope="Fix resolve_action"),
        ]
        scopes = ["Refactor run_dispatch", "Update classify_action", "Fix resolve_action"]
        titles = scopes[:]

        merged_titles, merged_scopes, merged_contexts = _merge_same_file_tasks(
            titles, scopes, contexts,
        )

        assert len(merged_titles) == 1
        assert "run_dispatch" in merged_contexts[0].identifiers
        assert "classify_action" in merged_contexts[0].identifiers
        assert "resolve_action" in merged_contexts[0].identifiers
        assert "core/dispatch.py" in merged_contexts[0].file_paths

    # ── Same-function merge ───────────────────────────────────────────────────

    def test_same_function_different_scope_merges(self):
        """Two tasks touching the same function should merge into one."""
        contexts = [
            _epic_ctx(file_paths=["src/api/handler.py"], identifiers=["process_request"],
                      scope="Add error handling to process_request in src/api/handler.py"),
            _epic_ctx(file_paths=["src/api/handler.py"], identifiers=["process_request"],
                      scope="Add logging to process_request in src/api/handler.py"),
        ]
        scopes = [c.scope for c in contexts]
        titles = scopes[:]

        merged_titles, merged_scopes, merged_contexts = _merge_same_file_tasks(
            titles, scopes, contexts,
        )

        assert len(merged_titles) == 1
        # Both scopes preserved in merged body
        assert "error handling" in merged_scopes[0].lower()
        assert "logging" in merged_scopes[0].lower()
        # Function identifier should appear once (deduped)
        assert merged_contexts[0].identifiers.count("process_request") == 1

    # ── Different-files no-merge ──────────────────────────────────────────────

    def test_different_files_no_merge(self):
        """Tasks touching different files should remain separate."""
        contexts = [
            _epic_ctx(file_paths=["src/auth/login.py"], scope="Fix login"),
            _epic_ctx(file_paths=["src/api/users.py"], scope="Update users API"),
            _epic_ctx(file_paths=["core/utils.py"], scope="Refactor utils"),
        ]
        scopes = [c.scope for c in contexts]
        titles = scopes[:]

        merged_titles, merged_scopes, merged_contexts = _merge_same_file_tasks(
            titles, scopes, contexts,
        )

        assert len(merged_titles) == 3, f"Expected 3 separate tasks, got {len(merged_titles)}"
        assert merged_contexts[0].file_paths == ["src/auth/login.py"]
        assert merged_contexts[1].file_paths == ["src/api/users.py"]
        assert merged_contexts[2].file_paths == ["core/utils.py"]

    # ── Single-task passthrough ───────────────────────────────────────────────

    def test_single_task_passthrough(self):
        """A single task should pass through unchanged."""
        contexts = [
            _epic_ctx(file_paths=["src/main.py"], identifiers=["main"], scope="Fix main"),
        ]
        scopes = ["Fix main"]
        titles = ["Fix main"]

        merged_titles, merged_scopes, merged_contexts = _merge_same_file_tasks(
            titles, scopes, contexts,
        )

        assert len(merged_titles) == 1
        assert merged_titles[0] == "Fix main"
        assert merged_scopes[0] == "Fix main"
        assert merged_contexts[0].file_paths == ["src/main.py"]

    def test_empty_list_passthrough(self):
        """An empty list should return empty lists."""
        merged_titles, merged_scopes, merged_contexts = _merge_same_file_tasks(
            [], [], [],
        )
        assert merged_titles == []
        assert merged_scopes == []
        assert merged_contexts == []

    # ── Mixed scenarios ───────────────────────────────────────────────────────

    def test_mixed_some_merge_some_dont(self):
        """Some tasks share a file, others don't — only same-file ones merge."""
        contexts = [
            _epic_ctx(file_paths=["src/auth/login.py"], identifiers=["login"], scope="Fix login"),
            _epic_ctx(file_paths=["src/auth/login.py"], identifiers=["logout"], scope="Fix logout"),
            _epic_ctx(file_paths=["src/api/users.py"], identifiers=["get_users"], scope="Update API"),
        ]
        scopes = [c.scope for c in contexts]
        titles = scopes[:]

        merged_titles, merged_scopes, merged_contexts = _merge_same_file_tasks(
            titles, scopes, contexts,
        )

        # Two tasks: merged login+logout, separate API
        assert len(merged_titles) == 2
        # First merged task should have both identifiers
        assert "login" in merged_contexts[0].identifiers
        assert "logout" in merged_contexts[0].identifiers
        assert "src/auth/login.py" in merged_contexts[0].file_paths
        # Second task should be the API one
        assert "get_users" in merged_contexts[1].identifiers
        assert "src/api/users.py" in merged_contexts[1].file_paths

    def test_mixed_two_groups_merge(self):
        """Two separate pairs of same-file tasks — both pairs merge."""
        contexts = [
            _epic_ctx(file_paths=["a.py"], scope="Task A1 in a.py"),
            _epic_ctx(file_paths=["a.py"], scope="Task A2 in a.py"),
            _epic_ctx(file_paths=["b.py"], scope="Task B1 in b.py"),
            _epic_ctx(file_paths=["b.py"], scope="Task B2 in b.py"),
        ]
        scopes = [c.scope for c in contexts]
        titles = scopes[:]

        merged_titles, merged_scopes, merged_contexts = _merge_same_file_tasks(
            titles, scopes, contexts,
        )

        assert len(merged_titles) == 2
        assert "a.py" in merged_contexts[0].file_paths
        assert "b.py" in merged_contexts[1].file_paths

    # ── Overlapping but not identical file sets: do not merge ─────────────────

    def test_overlapping_file_sets_no_merge(self):
        """Tasks with overlapping but not identical file sets should NOT merge.

        Task A: [a.py, b.py]
        Task B: [b.py, c.py]
        These share b.py but also have unique files — they are not 'same file'
        in the strict sense, so they should NOT be merged.
        """
        contexts = [
            _epic_ctx(file_paths=["a.py", "b.py"], scope="Refactor a.py and b.py"),
            _epic_ctx(file_paths=["b.py", "c.py"], scope="Update b.py and c.py"),
        ]
        scopes = [c.scope for c in contexts]
        titles = scopes[:]

        merged_titles, merged_scopes, merged_contexts = _merge_same_file_tasks(
            titles, scopes, contexts,
        )

        # Should NOT merge because file sets are not identical
        # (overlapping but not the same set)
        assert len(merged_titles) == 2, (
            "Tasks with overlapping but non-identical file sets should not merge"
        )

    def test_identical_multi_file_set_merges(self):
        """Tasks with identical multi-file sets should merge."""
        contexts = [
            _epic_ctx(file_paths=["a.py", "b.py"], identifiers=["func1"], scope="Task 1"),
            _epic_ctx(file_paths=["a.py", "b.py"], identifiers=["func2"], scope="Task 2"),
        ]
        scopes = [c.scope for c in contexts]
        titles = scopes[:]

        merged_titles, merged_scopes, merged_contexts = _merge_same_file_tasks(
            titles, scopes, contexts,
        )

        assert len(merged_titles) == 1
        assert set(merged_contexts[0].file_paths) == {"a.py", "b.py"}
        assert "func1" in merged_contexts[0].identifiers
        assert "func2" in merged_contexts[0].identifiers

    # ── Same file, different functions: merge ─────────────────────────────────

    def test_same_file_different_functions_merges(self):
        """Tasks touching the same file but different functions should merge.

        The trigger is 'same file', not 'same function'. Two tasks editing
        different functions in the same file create a conflict and should
        be consolidated.
        """
        contexts = [
            _epic_ctx(file_paths=["src/service.py"], identifiers=["func_a"], scope="Implement func_a"),
            _epic_ctx(file_paths=["src/service.py"], identifiers=["func_b"], scope="Implement func_b"),
        ]
        scopes = [c.scope for c in contexts]
        titles = scopes[:]

        merged_titles, merged_scopes, merged_contexts = _merge_same_file_tasks(
            titles, scopes, contexts,
        )

        assert len(merged_titles) == 1
        assert "func_a" in merged_contexts[0].identifiers
        assert "func_b" in merged_contexts[0].identifiers

    # ── No file paths: no merge ───────────────────────────────────────────────

    def test_no_file_paths_no_merge(self):
        """Tasks with no file paths should not be merged (nothing to group by)."""
        contexts = [
            _epic_ctx(file_paths=[], scope="Generic task A"),
            _epic_ctx(file_paths=[], scope="Generic task B"),
        ]
        scopes = [c.scope for c in contexts]
        titles = scopes[:]

        merged_titles, merged_scopes, merged_contexts = _merge_same_file_tasks(
            titles, scopes, contexts,
        )

        assert len(merged_titles) == 2

    # ── MergedTask structure ───────────────────────────────────────────────────

    def test_merged_title_combines_originals(self):
        """The merged task title should combine the original task titles."""
        contexts = [
            _epic_ctx(file_paths=["core.py"], scope="Fix bug in core.py"),
            _epic_ctx(file_paths=["core.py"], scope="Add feature to core.py"),
        ]
        scopes = [c.scope for c in contexts]
        titles = ["Fix bug in core.py", "Add feature to core.py"]

        merged_titles, _, _ = _merge_same_file_tasks(titles, scopes, contexts)

        # Title should combine both original titles
        assert len(merged_titles) == 1
        merged_title = merged_titles[0]
        assert "Fix bug" in merged_title
        assert "Add feature" in merged_title

    def test_merged_scope_preserves_all_criteria(self):
        """The merged scope should preserve all original scope texts."""
        contexts = [
            _epic_ctx(file_paths=["mod.py"], scope="Implement function X"),
            _epic_ctx(file_paths=["mod.py"], scope="Add tests for X"),
            _epic_ctx(file_paths=["mod.py"], scope="Document X"),
        ]
        scopes = [c.scope for c in contexts]
        titles = scopes[:]

        _, merged_scopes, _ = _merge_same_file_tasks(titles, scopes, contexts)

        assert len(merged_scopes) == 1
        combined = merged_scopes[0]
        assert "Implement function X" in combined
        assert "Add tests for X" in combined
        assert "Document X" in combined

    def test_merged_context_deduplicates_file_paths(self):
        """Merged context should deduplicate file paths."""
        contexts = [
            _epic_ctx(file_paths=["mod.py", "util.py"], scope="Task 1"),
            _epic_ctx(file_paths=["mod.py", "util.py"], scope="Task 2"),
        ]
        scopes = [c.scope for c in contexts]
        titles = scopes[:]

        _, _, merged_contexts = _merge_same_file_tasks(titles, scopes, contexts)

        assert len(merged_contexts) == 1
        # Each file path should appear only once
        assert merged_contexts[0].file_paths.count("mod.py") == 1
        assert merged_contexts[0].file_paths.count("util.py") == 1

    def test_merged_context_deduplicates_identifiers(self):
        """Merged context should deduplicate identifiers."""
        contexts = [
            _epic_ctx(file_paths=["mod.py"], identifiers=["func_a", "func_b"], scope="Task 1"),
            _epic_ctx(file_paths=["mod.py"], identifiers=["func_b", "func_c"], scope="Task 2"),
        ]
        scopes = [c.scope for c in contexts]
        titles = scopes[:]

        _, _, merged_contexts = _merge_same_file_tasks(titles, scopes, contexts)

        ctx = merged_contexts[0]
        assert ctx.identifiers.count("func_a") == 1
        assert ctx.identifiers.count("func_b") == 1
        assert ctx.identifiers.count("func_c") == 1


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS: _execute_planner_decompose with same-file merge
# ══════════════════════════════════════════════════════════════════════════════

class TestPlannerDecomposeSameFileMerge:
    """Integration tests verifying end-to-end planner output with same-file merge."""

    def test_integration_same_file_creates_one_sub_issue(self, tmp_path):
        """When two checklist items reference the same file, only one sub-issue is created."""
        workdir = _ensure_workdir(tmp_path)
        parent_body = (
            "- [ ] Fix login bug in src/auth/login.py\n"
            "- [ ] Add validation to src/auth/login.py\n"
        )
        parent_issue = _make_issue_obj(1, "Same-file Epic", parent_body)
        prov = _make_provider(issue_obj=parent_issue, created_numbers=[10, 11])

        per_sub_contexts = [
            _epic_ctx(file_paths=["src/auth/login.py"], identifiers=["handle_login"],
                      scope="Fix login bug in src/auth/login.py"),
            _epic_ctx(file_paths=["src/auth/login.py"], identifiers=["validate_credentials"],
                      scope="Add validation to src/auth/login.py"),
        ]

        m = _common_mocks(per_sub_contexts,
                          agg_ctx=_agg_ctx(all_file_paths={"src/auth/login.py"}))
        with mock.patch.object(iterate, "extract_epic_context", side_effect=per_sub_contexts), \
             mock.patch.object(iterate, "_build_aggregate_context",
                               return_value=_agg_ctx(all_file_paths={"src/auth/login.py"})), \
             mock.patch.object(iterate, "identify_relevant_files",
                               return_value=(["src/auth/login.py"], {})), \
             mock.patch.object(iterate, "read_source_files",
                               return_value={"src/auth/login.py": "content"}), \
             mock.patch.object(iterate, "load_known_components", return_value=set()), \
             mock.patch.object(iterate.kanban, "complete", return_value=True), \
             mock.patch.object(iterate.kanban, "create_triage", return_value="t_t"), \
             mock.patch.object(iterate.kanban, "list_tasks", return_value=[]), \
             mock.patch.object(iterate.kanban, "decompose", return_value=True):
            ok = _execute_planner_decompose(
                "slug", _make_card(body=parent_body, issue_n=1), "o/r", "PLANNING COMPLETE",
                provider=prov, workdir=workdir,
            )

        assert ok is True
        # Should create only ONE sub-issue (merged from two checklist items)
        assert prov.create_issue.call_count == 1, (
            f"Expected 1 sub-issue (merged), got {prov.create_issue.call_count}"
        )

        # Verify the merged sub-issue body contains both scopes
        created_body = prov.create_issue.call_args[0][1]
        assert "Fix login bug" in created_body
        assert "Add validation" in created_body
        # File path should appear
        assert "src/auth/login.py" in created_body

    def test_integration_different_files_creates_separate_sub_issues(self, tmp_path):
        """When checklist items reference different files, separate sub-issues are created."""
        workdir = _ensure_workdir(tmp_path)
        parent_body = (
            "- [ ] Fix login in src/auth/login.py\n"
            "- [ ] Update API in src/api/users.py\n"
        )
        parent_issue = _make_issue_obj(2, "Different-file Epic", parent_body)
        prov = _make_provider(issue_obj=parent_issue, created_numbers=[20, 21])

        per_sub_contexts = [
            _epic_ctx(file_paths=["src/auth/login.py"], identifiers=["handle_login"],
                      scope="Fix login in src/auth/login.py"),
            _epic_ctx(file_paths=["src/api/users.py"], identifiers=["get_user"],
                      scope="Update API in src/api/users.py"),
        ]

        with mock.patch.object(iterate, "extract_epic_context", side_effect=per_sub_contexts), \
             mock.patch.object(iterate, "_build_aggregate_context",
                               return_value=_agg_ctx(
                                   all_file_paths={"src/auth/login.py", "src/api/users.py"})), \
             mock.patch.object(iterate, "identify_relevant_files",
                               return_value=(["src/auth/login.py", "src/api/users.py"], {})), \
             mock.patch.object(iterate, "read_source_files", return_value={}), \
             mock.patch.object(iterate, "load_known_components", return_value=set()), \
             mock.patch.object(iterate.kanban, "complete", return_value=True), \
             mock.patch.object(iterate.kanban, "create_triage", return_value="t_t"), \
             mock.patch.object(iterate.kanban, "list_tasks", return_value=[]), \
             mock.patch.object(iterate.kanban, "decompose", return_value=True):
            ok = _execute_planner_decompose(
                "slug", _make_card(body=parent_body, issue_n=2), "o/r", "PLANNING COMPLETE",
                provider=prov, workdir=workdir,
            )

        assert ok is True
        assert prov.create_issue.call_count == 2, (
            f"Expected 2 sub-issues (different files), got {prov.create_issue.call_count}"
        )

        # Verify each sub-issue references its own file
        bodies = [call.args[1] for call in prov.create_issue.call_args_list]
        assert "src/auth/login.py" in bodies[0]
        assert "src/api/users.py" in bodies[1]

    def test_integration_mixed_scenario(self, tmp_path):
        """Mixed: two items share a file, one doesn't — should create 2 sub-issues."""
        workdir = _ensure_workdir(tmp_path)
        parent_body = (
            "- [ ] Fix login in src/auth/login.py\n"
            "- [ ] Add logout to src/auth/login.py\n"
            "- [ ] Update users API in src/api/users.py\n"
        )
        parent_issue = _make_issue_obj(3, "Mixed Epic", parent_body)
        prov = _make_provider(issue_obj=parent_issue, created_numbers=[30, 31])

        per_sub_contexts = [
            _epic_ctx(file_paths=["src/auth/login.py"], identifiers=["handle_login"],
                      scope="Fix login in src/auth/login.py"),
            _epic_ctx(file_paths=["src/auth/login.py"], identifiers=["handle_logout"],
                      scope="Add logout to src/auth/login.py"),
            _epic_ctx(file_paths=["src/api/users.py"], identifiers=["get_users"],
                      scope="Update users API in src/api/users.py"),
        ]

        with mock.patch.object(iterate, "extract_epic_context", side_effect=per_sub_contexts), \
             mock.patch.object(iterate, "_build_aggregate_context",
                               return_value=_agg_ctx(
                                   all_file_paths={"src/auth/login.py", "src/api/users.py"})), \
             mock.patch.object(iterate, "identify_relevant_files",
                               return_value=(["src/auth/login.py", "src/api/users.py"], {})), \
             mock.patch.object(iterate, "read_source_files", return_value={}), \
             mock.patch.object(iterate, "load_known_components", return_value=set()), \
             mock.patch.object(iterate.kanban, "complete", return_value=True), \
             mock.patch.object(iterate.kanban, "create_triage", return_value="t_t"), \
             mock.patch.object(iterate.kanban, "list_tasks", return_value=[]), \
             mock.patch.object(iterate.kanban, "decompose", return_value=True):
            ok = _execute_planner_decompose(
                "slug", _make_card(body=parent_body, issue_n=3), "o/r", "PLANNING COMPLETE",
                provider=prov, workdir=workdir,
            )

        assert ok is True
        # 2 sub-issues: merged login+logout, separate API
        assert prov.create_issue.call_count == 2, (
            f"Expected 2 sub-issues (1 merged + 1 separate), got {prov.create_issue.call_count}"
        )

        # First sub-issue should be the merged one (login + logout)
        first_body = prov.create_issue.call_args_list[0].args[1]
        assert "src/auth/login.py" in first_body
        assert "handle_login" in first_body
        assert "handle_logout" in first_body

        # Second sub-issue should be the API one
        second_body = prov.create_issue.call_args_list[1].args[1]
        assert "src/api/users.py" in second_body

    def test_integration_no_file_context_no_merge(self, tmp_path):
        """When no file paths are extracted, tasks remain separate (no merge)."""
        workdir = _ensure_workdir(tmp_path)
        parent_body = (
            "- [ ] Generic task A\n"
            "- [ ] Generic task B\n"
        )
        parent_issue = _make_issue_obj(4, "Generic Epic", parent_body)
        prov = _make_provider(issue_obj=parent_issue, created_numbers=[40, 41])

        per_sub_contexts = [
            _epic_ctx(file_paths=[], scope="Generic task A"),
            _epic_ctx(file_paths=[], scope="Generic task B"),
        ]

        with mock.patch.object(iterate, "extract_epic_context", side_effect=per_sub_contexts), \
             mock.patch.object(iterate, "_build_aggregate_context",
                               return_value=_agg_ctx()), \
             mock.patch.object(iterate, "identify_relevant_files", return_value=([], {})), \
             mock.patch.object(iterate, "read_source_files", return_value={}), \
             mock.patch.object(iterate, "load_known_components", return_value=set()), \
             mock.patch.object(iterate.kanban, "complete", return_value=True), \
             mock.patch.object(iterate.kanban, "create_triage", return_value="t_t"), \
             mock.patch.object(iterate.kanban, "list_tasks", return_value=[]), \
             mock.patch.object(iterate.kanban, "decompose", return_value=True):
            ok = _execute_planner_decompose(
                "slug", _make_card(body=parent_body, issue_n=4), "o/r", "PLANNING COMPLETE",
                provider=prov, workdir=workdir,
            )

        assert ok is True
        # No file paths → no merge → 2 separate sub-issues
        assert prov.create_issue.call_count == 2
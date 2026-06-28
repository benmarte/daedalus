"""Unit tests for planner sub-issue creation with file-specific acceptance criteria.

Tests cover the code path where the planner has access to source files and creates
sub-issues with file-specific context. The source-availability layer is mocked to
return valid source content. Verifies:
1. Sub-issues are created for each relevant file
2. Each sub-issue's acceptance criteria reference specific file paths
3. No sub-issues are created for irrelevant or empty files
4. The planner's output structure matches the expected schema

Part of epic #152: Phase 4 — Codebase analysis integration.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _load_dispatch  # noqa: E402
from core import iterate  # noqa: E402
from core.iterate import (  # noqa: E402
    _execute_planner_decompose,
    EpicContext,
    AggregateEpicContext,
    _sub_issue_body,
)

disp = _load_dispatch()  # noqa: F841


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_card(title: str = "#1 Epic with files", body: str = "", issue_n: int = 1) -> dict:
    """Create a planner card dict."""
    body_with_ref = body if f"#{issue_n}" in body else f"Issue #{issue_n}\n{body}"
    return {"id": "t_test", "title": title, "body": body_with_ref, "assignee": "planner-daedalus"}


def _ensure_workdir_exists(tmp_path):
    """Helper to ensure workdir path exists for the tests."""
    workdir = tmp_path / "workdir"
    workdir.mkdir(exist_ok=True)
    return str(workdir)


def _make_issue_obj(number: int = 1, title: str = "Epic", body: str = "", labels=None):
    """Create a fake issue object."""
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
    """Create a mock VCS provider."""
    prov = mock.MagicMock()
    prov.get_issue.return_value = issue_obj
    prov.get_issue_comments.return_value = comments or []
    _created = iter(created_numbers or [101, 102, 103])
    prov.create_issue.side_effect = lambda *args, **kwargs: next(_created, None)
    prov.post_issue_comment.return_value = True
    prov.add_label.return_value = add_label_ret
    return prov


def _make_epic_context(file_paths=None, identifiers=None, scope=""):
    """Create an EpicContext with file paths."""
    return EpicContext(
        scope=scope or "",
        file_paths=file_paths or [],
        identifiers=identifiers or [],
        component_names=[],
        dir_tags=[],
        keywords=[],
    )


def _make_aggregate_context(all_file_paths=None, all_identifiers=None):
    """Create an AggregateEpicContext."""
    return AggregateEpicContext(
        per_sub_issues=[],
        all_file_paths=all_file_paths or set(),
        all_identifiers=all_identifiers or set(),
        all_component_names=set(),
        all_dir_tags=set(),
    )


# ── Test 1: Sub-issues created for each relevant file ────────────────────────

def test_subissues_created_for_each_relevant_file(tmp_path):
    """Each relevant file should result in a sub-issue with file-specific context."""
    workdir = _ensure_workdir_exists(tmp_path)
    # Epic with two checklist items referencing different files
    parent_body = (
        "- [ ] Fix login bug in src/auth/login.py\n"
        "- [ ] Update API endpoint in src/api/users.py\n"
    )
    parent_issue = _make_issue_obj(1, "File-specific Epic", parent_body)
    prov = _make_provider(issue_obj=parent_issue, created_numbers=[10, 11])

    # Mock: extract_epic_context returns different file paths per checklist item
    per_sub_contexts = [
        _make_epic_context(
            file_paths=["src/auth/login.py"],
            identifiers=["handle_login"],
            scope="Fix login bug in src/auth/login.py",
        ),
        _make_epic_context(
            file_paths=["src/api/users.py"],
            identifiers=["get_user"],
            scope="Update API endpoint in src/api/users.py",
        ),
    ]

    # Mock source reading
    file_contents = {
        "src/auth/login.py": "def handle_login():\n    pass\n",
        "src/api/users.py": "def get_user():\n    pass\n",
    }
    file_metadata = {}

    with (
        mock.patch.object(iterate, "extract_epic_context", side_effect=per_sub_contexts),
        mock.patch.object(iterate, "_build_aggregate_context", return_value=_make_aggregate_context(
            all_file_paths={"src/auth/login.py", "src/api/users.py"},
        )),
        mock.patch.object(iterate, "identify_relevant_files", return_value=(
            ["src/auth/login.py", "src/api/users.py"], file_metadata
        )),
        mock.patch.object(iterate, "read_source_files", return_value=file_contents),
        mock.patch.object(iterate, "build_sub_issue_context", return_value="## Source Context\n..."),
        mock.patch.object(iterate, "filter_context_for_sub", return_value={"src/auth/login.py": "content1"}),
        mock.patch.object(iterate, "build_enhanced_scope", return_value="Enhanced scope"),
        mock.patch.object(iterate, "load_known_components", return_value=set()),
        mock.patch.object(iterate.kanban, "complete", return_value=True),
        mock.patch.object(iterate.kanban, "create_triage", return_value="t_triage"),
        mock.patch.object(iterate.kanban, "list_tasks", return_value=[]),
        mock.patch.object(iterate.kanban, "decompose", return_value=True),
    ):
        ok = _execute_planner_decompose(
            "slug", _make_card(body=parent_body, issue_n=1), "o/r", "PLANNING COMPLETE",
            provider=prov, workdir=workdir,
        )

    assert ok is True
    # Two sub-issues created (one per checklist item, each referencing a different file)
    assert prov.create_issue.call_count == 2

    # Verify each sub-issue body was called with file-specific paths
    create_calls = prov.create_issue.call_args_list
    # First call: should reference src/auth/login.py
    first_call = create_calls[0]
    first_body = first_call.args[1]
    assert "src/auth/login.py" in first_body, f"First sub-issue missing auth file: {first_body}"

    # Second call: should reference src/api/users.py
    second_call = create_calls[1]
    second_body = second_call.args[1]
    assert "src/api/users.py" in second_body, f"Second sub-issue missing api file: {second_body}"


# ── Test 2: Sub-issue acceptance criteria reference file paths ───────────────

def test_subissue_acceptance_criteria_reference_file_paths(tmp_path):
    """Each sub-issue's body should reference the specific file path in the Affected Files section."""
    workdir = _ensure_workdir_exists(tmp_path)
    parent_body = "- [ ] Implement feature in src/core/feature.py\n"
    parent_issue = _make_issue_obj(42, "Feature Epic", parent_body)
    prov = _make_provider(issue_obj=parent_issue, created_numbers=[100])

    per_sub_context = [
        _make_epic_context(
            file_paths=["src/core/feature.py"],
            identifiers=["implement_feature"],
            scope="Implement feature in src/core/feature.py",
        ),
    ]

    file_contents = {
        "src/core/feature.py": "def implement_feature():\n    pass\n",
    }

    with (
        mock.patch.object(iterate, "extract_epic_context", side_effect=per_sub_context),
        mock.patch.object(iterate, "_build_aggregate_context", return_value=_make_aggregate_context(
            all_file_paths={"src/core/feature.py"},
        )),
        mock.patch.object(iterate, "identify_relevant_files", return_value=(
            ["src/core/feature.py"], {}
        )),
        mock.patch.object(iterate, "read_source_files", return_value=file_contents),
        mock.patch.object(iterate, "build_sub_issue_context", return_value="## Context"),
        mock.patch.object(iterate, "filter_context_for_sub", return_value=file_contents),
        mock.patch.object(iterate, "build_enhanced_scope", return_value="Enhanced"),
        mock.patch.object(iterate, "load_known_components", return_value=set()),
        mock.patch.object(iterate.kanban, "complete", return_value=True),
        mock.patch.object(iterate.kanban, "create_triage", return_value="t_triage"),
        mock.patch.object(iterate.kanban, "list_tasks", return_value=[]),
        mock.patch.object(iterate.kanban, "decompose", return_value=True),
    ):
        ok = _execute_planner_decompose(
            "slug", _make_card(body=parent_body, issue_n=42), "o/r", "PLANNING COMPLETE",
            provider=prov, workdir=workdir,
        )

    assert ok is True
    assert prov.create_issue.call_count == 1

    # Inspect the sub-issue body passed to create_issue
    created_body = prov.create_issue.call_args[0][1]

    # Standard template sections must be present
    assert "## Scope" in created_body
    assert "## Acceptance Criteria" in created_body
    assert "## Notes" in created_body

    # Affected files section must reference the specific file
    assert "### Affected files & symbols" in created_body
    assert "`src/core/feature.py`" in created_body
    assert "**Files:**" in created_body

    # Identifiers should also be listed if provided
    assert "`implement_feature`" in created_body


# ── Test 3: No sub-issues for irrelevant/empty files ─────────────────────────

def test_no_subissues_for_irrelevant_empty_files():
    """Empty or irrelevant file paths should not cause sub-issue creation failure."""
    parent_body = "- [ ] Generic task\n"
    parent_issue = _make_issue_obj(99, "Generic Epic", parent_body)
    prov = _make_provider(issue_obj=parent_issue, created_numbers=[200])

    # Mock: extract_epic_context returns empty file_paths (no relevant files found)
    per_sub_context = [
        _make_epic_context(
            file_paths=[],  # No files found
            identifiers=[],
            scope="Generic task",
        ),
    ]

    with (
        mock.patch.object(iterate, "extract_epic_context", side_effect=per_sub_context),
        mock.patch.object(iterate, "_build_aggregate_context", return_value=_make_aggregate_context(
            all_file_paths=set(),  # No files at all
        )),
        mock.patch.object(iterate, "identify_relevant_files", return_value=([], {})),
        mock.patch.object(iterate, "read_source_files", return_value={}),
        mock.patch.object(iterate, "build_sub_issue_context", return_value=""),
        mock.patch.object(iterate, "load_known_components", return_value=set()),
        mock.patch.object(iterate.kanban, "complete", return_value=True),
        mock.patch.object(iterate.kanban, "create_triage", return_value="t_triage"),
        mock.patch.object(iterate.kanban, "list_tasks", return_value=[]),
        mock.patch.object(iterate.kanban, "decompose", return_value=True),
    ):
        ok = _execute_planner_decompose(
            "slug", _make_card(body=parent_body, issue_n=99), "o/r", "PLANNING COMPLETE",
            provider=prov, workdir="/tmp/workdir",
        )

    assert ok is True
    # Sub-issue still created (fallback to template-only), but without file context
    assert prov.create_issue.call_count == 1

    created_body = prov.create_issue.call_args[0][1]
    # Should still have standard template
    assert "## Scope" in created_body
    assert "## Acceptance Criteria" in created_body
    # But no file references since none were relevant
    assert "### Affected files & symbols" not in created_body


# ── Test 4: Output structure matches expected schema ─────────────────────────

def test_output_structure_matches_schema(tmp_path):
    """Sub-issue body must follow the standard schema with all required sections."""
    workdir = _ensure_workdir_exists(tmp_path)
    parent_body = "- [ ] Task with file reference: src/module.py\n"
    parent_issue = _make_issue_obj(7, "Schema Test Epic", parent_body)
    prov = _make_provider(issue_obj=parent_issue, created_numbers=[50])

    per_sub_context = [
        _make_epic_context(
            file_paths=["src/module.py"],
            identifiers=["target_function"],
            scope="Task with file reference",
        ),
    ]

    with (
        mock.patch.object(iterate, "extract_epic_context", side_effect=per_sub_context),
        mock.patch.object(iterate, "_build_aggregate_context", return_value=_make_aggregate_context(
            all_file_paths={"src/module.py"},
        )),
        mock.patch.object(iterate, "identify_relevant_files", return_value=(
            ["src/module.py"], {}
        )),
        mock.patch.object(iterate, "read_source_files", return_value={"src/module.py": "content"}),
        mock.patch.object(iterate, "build_sub_issue_context", return_value="ctx"),
        mock.patch.object(iterate, "filter_context_for_sub", return_value={"src/module.py": "c"}),
        mock.patch.object(iterate, "build_enhanced_scope", return_value="enhanced"),
        mock.patch.object(iterate, "load_known_components", return_value=set()),
        mock.patch.object(iterate.kanban, "complete", return_value=True),
        mock.patch.object(iterate.kanban, "create_triage", return_value="t_t"),
        mock.patch.object(iterate.kanban, "list_tasks", return_value=[]),
        mock.patch.object(iterate.kanban, "decompose", return_value=True),
    ):
        ok = _execute_planner_decompose(
            "slug", _make_card(body=parent_body, issue_n=7), "o/r", "PLANNING COMPLETE",
            provider=prov, workdir=workdir,
        )

    assert ok is True
    created_body = prov.create_issue.call_args[0][1]
    created_title = prov.create_issue.call_args[0][0]
    created_labels = prov.create_issue.call_args[1].get("labels", [])

    # Schema verification:
    # 1. Must start with parent backlink
    assert created_body.startswith("Part of epic #7"), f"Body should start with parent backlink: {created_body[:50]}"
    assert "Schema Test Epic" in created_body, "Parent title must be in body"

    # 2. Must have depends_on line
    assert "depends_on:" in created_body, "Missing depends_on line"

    # 3. Must have Scope section
    assert "## Scope" in created_body, "Missing Scope section"

    # 4. Must have Affected files section (when file_paths provided)
    assert "### Affected files & symbols" in created_body, "Missing Affected files section"
    assert "**Files:**" in created_body, "Missing Files header"
    assert "`src/module.py`" in created_body, "Missing file path reference"

    # 5. Must have Acceptance Criteria section
    assert "## Acceptance Criteria" in created_body, "Missing Acceptance Criteria section"
    assert "- [ ] Implementation complete per scope" in created_body, "Missing standard AC item"
    assert "- [ ] Tests pass (unit + integration where applicable)" in created_body
    assert "- [ ] PR opened and passing CI" in created_body

    # 6. Must have Notes section
    assert "## Notes" in created_body, "Missing Notes section"
    assert "Auto-generated by Daedalus" in created_body, "Missing auto-gen note"

    # 7. Labels must include subtask and inherit parent labels
    assert "subtask" in created_labels, "Missing subtask label"


# ── Test 5: Source reading failure graceful degradation ──────────────────────

def test_source_reading_failure_graceful_degradation():
    """When source reading fails, sub-issues are still created with template-only body."""
    parent_body = "- [ ] Task A\n- [ ] Task B\n"
    parent_issue = _make_issue_obj(55, "Recovery Epic", parent_body)
    prov = _make_provider(issue_obj=parent_issue, created_numbers=[300, 301])

    # Mock source reading to fail
    with (
        mock.patch.object(iterate, "extract_epic_context", return_value=_make_epic_context()),
        mock.patch.object(iterate, "_build_aggregate_context", return_value=_make_aggregate_context()),
        mock.patch.object(iterate, "identify_relevant_files", side_effect=RuntimeError("Disk error")),
        mock.patch.object(iterate, "load_known_components", return_value=set()),
        mock.patch.object(iterate.kanban, "complete", return_value=True),
        mock.patch.object(iterate.kanban, "create_triage", return_value="t_t"),
        mock.patch.object(iterate.kanban, "list_tasks", return_value=[]),
        mock.patch.object(iterate.kanban, "decompose", return_value=True),
    ):
        ok = _execute_planner_decompose(
            "slug", _make_card(body=parent_body, issue_n=55), "o/r", "PLANNING COMPLETE",
            provider=prov, workdir="/tmp/workdir",
        )

    assert ok is True
    # Both sub-issues created despite source reading failure
    assert prov.create_issue.call_count == 2

    # Each should still have standard template structure
    for call in prov.create_issue.call_args_list:
        body = call.args[1]
        assert "## Scope" in body
        assert "## Acceptance Criteria" in body
        assert "Auto-generated by Daedalus" in body


# ── Test 6: Empty workdir falls back to Phase 3 behavior ─────────────────────

def test_empty_workdir_fallback_to_phase_3():
    """When workdir is empty, no source reading occurs; template-only sub-issues."""
    parent_body = "- [ ] Simple task\n"
    parent_issue = _make_issue_obj(88, "No Workdir Epic", parent_body)
    prov = _make_provider(issue_obj=parent_issue, created_numbers=[400])

    # No source-reading functions should be called
    with (
        mock.patch.object(iterate.kanban, "complete", return_value=True),
        mock.patch.object(iterate.kanban, "create_triage", return_value="t_t"),
        mock.patch.object(iterate.kanban, "list_tasks", return_value=[]),
        mock.patch.object(iterate.kanban, "decompose", return_value=True),
    ):
        ok = _execute_planner_decompose(
            "slug", _make_card(body=parent_body, issue_n=88), "o/r", "PLANNING COMPLETE",
            provider=prov, workdir="",  # Empty workdir
        )

    assert ok is True
    assert prov.create_issue.call_count == 1

    created_body = prov.create_issue.call_args[0][1]
    # Standard template without file-specific context
    assert "## Scope" in created_body
    assert "## Acceptance Criteria" in created_body
    assert "### Affected files & symbols" not in created_body  # No files since workdir empty


# ── Test 7: _sub_issue_body directly produces correct schema ─────────────────

def test_sub_issue_body_direct_schema_validation():
    """Direct test of _sub_issue_body with file_paths and identifiers."""
    body = _sub_issue_body(
        parent_n=123,
        parent_title="Test Epic",
        scope="Implement feature X",
        depends_on=[120, 121],
        file_paths=["src/core/module_a.py", "src/utils/helper.py"],
        identifiers=["process_data", "validate_input"],
    )

    # Schema checks
    assert body.startswith("Part of epic #123: Test Epic")
    assert "depends_on: #120, #121" in body
    assert "## Scope" in body
    assert "Implement feature X" in body
    assert "### Affected files & symbols" in body
    assert "**Files:**" in body
    assert "`src/core/module_a.py`" in body
    assert "`src/utils/helper.py`" in body
    assert "**Symbols:**" in body
    assert "`process_data`" in body
    assert "`validate_input`" in body
    assert "## Acceptance Criteria" in body
    assert "- [ ] Implementation complete per scope" in body
    assert "- [ ] Tests pass (unit + integration where applicable)" in body
    assert "- [ ] PR opened and passing CI" in body
    assert "## Notes" in body
    assert "Auto-generated by Daedalus Phase 3 epic decomposition." in body


# ── Test 8: Multiple files per sub-issue ─────────────────────────────────────

def test_multiple_files_per_subissue(tmp_path):
    """A single sub-issue can reference multiple files."""
    workdir = _ensure_workdir_exists(tmp_path)
    parent_body = "- [ ] Refactor auth module across login.py and logout.py\n"
    parent_issue = _make_issue_obj(77, "Refactor Epic", parent_body)
    prov = _make_provider(issue_obj=parent_issue, created_numbers=[500])

    per_sub_context = [
        _make_epic_context(
            file_paths=["src/auth/login.py", "src/auth/logout.py"],
            identifiers=["login_handler", "logout_handler"],
            scope="Refactor auth module",
        ),
    ]

    file_contents = {
        "src/auth/login.py": "def login_handler(): pass\n",
        "src/auth/logout.py": "def logout_handler(): pass\n",
    }

    with (
        mock.patch.object(iterate, "extract_epic_context", side_effect=per_sub_context),
        mock.patch.object(iterate, "_build_aggregate_context", return_value=_make_aggregate_context(
            all_file_paths={"src/auth/login.py", "src/auth/logout.py"},
        )),
        mock.patch.object(iterate, "identify_relevant_files", return_value=(
            ["src/auth/login.py", "src/auth/logout.py"], {}
        )),
        mock.patch.object(iterate, "read_source_files", return_value=file_contents),
        mock.patch.object(iterate, "build_sub_issue_context", return_value="ctx"),
        mock.patch.object(iterate, "filter_context_for_sub", return_value=file_contents),
        mock.patch.object(iterate, "build_enhanced_scope", return_value="enhanced"),
        mock.patch.object(iterate, "load_known_components", return_value=set()),
        mock.patch.object(iterate.kanban, "complete", return_value=True),
        mock.patch.object(iterate.kanban, "create_triage", return_value="t_t"),
        mock.patch.object(iterate.kanban, "list_tasks", return_value=[]),
        mock.patch.object(iterate.kanban, "decompose", return_value=True),
    ):
        ok = _execute_planner_decompose(
            "slug", _make_card(body=parent_body, issue_n=77), "o/r", "PLANNING COMPLETE",
            provider=prov, workdir=workdir,
        )

    assert ok is True
    assert prov.create_issue.call_count == 1

    created_body = prov.create_issue.call_args[0][1]
    # Both files referenced in the same sub-issue
    assert "`src/auth/login.py`" in created_body
    assert "`src/auth/logout.py`" in created_body
    # Both identifiers referenced
    assert "`login_handler`" in created_body
    assert "`logout_handler`" in created_body


# ── Test 9: Fallback count incremented on source reading failure ─────────────

def test_fallback_count_incremented_on_failure():
    """_source_reading_fallback_count should increment when source reading fails."""
    initial_count = getattr(iterate, "_source_reading_fallback_count", 0)

    parent_body = "- [ ] Task\n"
    parent_issue = _make_issue_obj(90, "Fallback Epic", parent_body)
    prov = _make_provider(issue_obj=parent_issue, created_numbers=[600])

    with (
        mock.patch.object(iterate, "extract_epic_context", return_value=_make_epic_context()),
        mock.patch.object(iterate, "_build_aggregate_context", return_value=_make_aggregate_context()),
        mock.patch.object(iterate, "identify_relevant_files", side_effect=OSError("Permission denied")),
        mock.patch.object(iterate, "load_known_components", return_value=set()),
        mock.patch.object(iterate.kanban, "complete", return_value=True),
        mock.patch.object(iterate.kanban, "create_triage", return_value="t_t"),
        mock.patch.object(iterate.kanban, "list_tasks", return_value=[]),
        mock.patch.object(iterate.kanban, "decompose", return_value=True),
    ):
        _execute_planner_decompose(
            "slug", _make_card(body=parent_body, issue_n=90), "o/r", "PLANNING COMPLETE",
            provider=prov, workdir="/tmp/workdir",
        )

    final_count = getattr(iterate, "_source_reading_fallback_count", 0)
    assert final_count > initial_count, "Fallback counter should have incremented"

import pytest
from core.iterate import _render_affected_files_section, _sub_issue_body


class TestRenderAffectedFilesSection:
    """Unit tests for _render_affected_files_section formatting logic"""

    def test_empty_inputs_return_empty_string(self):
        """No files and no identifiers should return empty string"""
        result = _render_affected_files_section(None, None)
        assert result == ""

    def test_empty_lists_return_empty_string(self):
        """Empty lists should return empty string"""
        result = _render_affected_files_section([], [])
        assert result == ""

    def test_single_file_renders_correctly(self):
        """Single file path should render as one bullet"""
        result = _render_affected_files_section(["src/core/iterate.py"], None)
        assert "### Affected files & symbols" in result
        assert "**Files:**" in result
        assert "- `src/core/iterate.py`" in result

    def test_multiple_files_sorted_alphabetically(self):
        """Multiple files should be sorted alphabetically"""
        files = ["zebra.py", "alpha.py", "middle.py"]
        result = _render_affected_files_section(files, None)
        lines = result.split("\n")
        # Find the positions of each file
        positions = {}
        for i, line in enumerate(lines):
            for f in files:
                if f"`{f}`" in line:
                    positions[f] = i

        # Verify alphabetical order
        assert positions["alpha.py"] < positions["middle.py"]
        assert positions["middle.py"] < positions["zebra.py"]

    def test_nested_module_paths_rendered_correctly(self):
        """Nested paths should render with full path"""
        files = ["src/core/submodule/deep/nested.py", "lib/utils.py"]
        result = _render_affected_files_section(files, None)
        assert "`src/core/submodule/deep/nested.py`" in result
        assert "`lib/utils.py`" in result

    def test_single_identifier_renders_correctly(self):
        """Single identifier should render as one bullet"""
        result = _render_affected_files_section(None, ["process_epic"])
        assert "### Affected files & symbols" in result
        assert "**Symbols:**" in result
        assert "`process_epic`" in result

    def test_multiple_identifiers_sorted_alphabetically(self):
        """Multiple identifiers should be sorted alphabetically"""
        identifiers = ["zebra_func", "alpha_func", "middle_func"]
        result = _render_affected_files_section(None, identifiers)
        lines = result.split("\n")
        positions = {}
        for i, line in enumerate(lines):
            for ident in identifiers:
                if f"`{ident}`" in line:
                    positions[ident] = i

        assert positions["alpha_func"] < positions["middle_func"]
        assert positions["middle_func"] < positions["zebra_func"]

    def test_combined_files_and_identifiers(self):
        """Both files and identifiers should render together"""
        files = ["src/core.py"]
        identifiers = ["process_epic", "helper_func"]
        result = _render_affected_files_section(files, identifiers)

        assert "### Affected files & symbols" in result
        assert "**Files:**" in result
        assert "**Symbols:**" in result
        assert "`src/core.py`" in result
        assert "`process_epic`" in result
        assert "`helper_func`" in result

    def test_overflow_at_50_files(self):
        """More than 50 files should be capped with overflow note"""
        files = [f"file{i}.py" for i in range(60)]
        result = _render_affected_files_section(files, None)

        # Should have overflow note
        assert "and" in result.lower()
        assert "additional" in result.lower() or "more" in result.lower()

    def test_overflow_at_50_identifiers(self):
        """More than 50 identifiers should be capped with overflow note"""
        identifiers = [f"func{i}" for i in range(60)]
        result = _render_affected_files_section(None, identifiers)

        assert "and" in result.lower()
        assert "additional" in result.lower() or "more" in result.lower()


class TestSubIssueBodyWithFileReferences:
    """Integration tests for _sub_issue_body with file references"""

    def test_no_files_identifiers_backward_compatible(self):
        """When no files or identifiers provided, body should be backward compatible"""
        body = _sub_issue_body(
            123, "Test Epic", "Test scope description", [120, 121]
        )

        assert "Part of epic #123: Test Epic" in body
        assert "depends_on: #120, #121" in body
        assert "## Scope" in body
        assert "Test scope description" in body
        assert "Affected files" not in body
        assert "Acceptance Criteria" in body

    def test_with_files_only(self):
        """When only files provided, should render Files section"""
        body = _sub_issue_body(
            123,
            "Test Epic",
            "Test scope",
            [],
            file_paths=["src/core/iterate.py"],
            identifiers=None,
        )

        assert "### Affected files & symbols" in body
        assert "**Files:**" in body
        assert "`src/core/iterate.py`" in body
        assert "**Symbols:**" not in body

    def test_with_identifiers_only(self):
        """When only identifiers provided, should render Symbols section"""
        body = _sub_issue_body(
            123,
            "Test Epic",
            "Test scope",
            [],
            file_paths=None,
            identifiers=["process_epic", "build_context"],
        )

        assert "### Affected files & symbols" in body
        assert "**Symbols:**" in body
        assert "`process_epic`" in body
        assert "`build_context`" in body
        assert "**Files:**" not in body

    def test_with_both_files_and_identifiers(self):
        """When both files and identifiers provided, should render both sections"""
        body = _sub_issue_body(
            123,
            "Test Epic",
            "Test scope",
            [],
            file_paths=["src/core/iterate.py", "lib/utils.py"],
            identifiers=["process_epic"],
        )

        assert "### Affected files & symbols" in body
        assert "**Files:**" in body
        assert "`src/core/iterate.py`" in body
        assert "`lib/utils.py`" in body
        assert "**Symbols:**" in body
        assert "`process_epic`" in body

    def test_file_references_appear_before_acceptance_criteria(self):
        """Affected files section should appear between Scope and Acceptance Criteria"""
        body = _sub_issue_body(
            123,
            "Test Epic",
            "Test scope",
            [],
            file_paths=["src/core.py"],
        )

        scope_pos = body.find("## Scope")
        affected_pos = body.find("### Affected files & symbols")
        acceptance_pos = body.find("## Acceptance Criteria")

        assert scope_pos > 0
        assert affected_pos > scope_pos
        assert acceptance_pos > affected_pos

    def test_end_to_end_with_real_analysis_output(self):
        """Integration test: simulate real codebase analysis output"""
        # Simulate what EpicContext analysis would produce
        file_paths = [
            "src/core/iterate.py",
            "src/providers/github.py",
            "src/dispatcher.py",
        ]
        identifiers = [
            "process_epic",
            "build_context",
            "extract_issue_number",
        ]

        body = _sub_issue_body(
            152,
            "Phase 4: Codebase analysis integration",
            "Implement file/module references in sub-issue generation",
            [148, 149, 150],
            file_paths=file_paths,
            identifiers=identifiers,
        )

        # Verify structure
        assert "Part of epic #152: Phase 4: Codebase analysis integration" in body
        assert "depends_on: #148, #149, #150" in body
        assert "### Affected files & symbols" in body

        # Verify all files present
        for f in file_paths:
            assert f"`{f}`" in body

        # Verify all identifiers present
        for ident in identifiers:
            assert f"`{ident}`" in body

        assert "## Acceptance Criteria" in body
        assert "Auto-generated by Daedalus Phase 3 epic decomposition." in body

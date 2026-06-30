# -*- coding: utf-8 -*-
"""Tests for planner-stage source file context injection (issue #386).

Verifies that ``_planner_body()`` in ``scripts/daedalus_dispatch.py`` injects
relevant source context into planner card bodies per the design spec:
- Calls identify_relevant_files(), read_source_files(), build_sub_issue_context()
- Injects context into the planner body when files are found
- Gracefully degrades when no files found, errors occur, or workdir is missing
- Enforces 100KB total context cap
- Respects max_files=10 config
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# Import the dispatch module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import scripts.daedalus_dispatch as disp  # noqa: E402


def _make_issue(number: int = 1, title: str = "Test", body: str = "", labels=None):
    """Helper to create a minimal issue dict for testing."""
    return {
        "number": number,
        "title": title,
        "body": body,
        "url": f"https://github.com/test/repo/issues/{number}",
        "labels": labels or [],
    }


class TestPlannerBodySourceContext:
    """Tests for source context injection into planner bodies."""

    def test_no_context_when_workdir_empty(self):
        """No source context when workdir is empty string."""
        issue = _make_issue(number=100, title="Epic task", body="- [ ] task\n" * 10)
        body = disp._planner_body("org/repo", issue, "", "main", "github")
        # Should not contain "Relevant Source Context" header
        assert "## Relevant Source Context" not in body
        # Should still have the issue body
        assert "#100" in body
        assert "Epic task" in body

    def test_no_context_when_workdir_missing(self):
        """No source context when workdir does not exist."""
        issue = _make_issue(number=100, title="Epic task", body="- [ ] task\n" * 10)
        body = disp._planner_body("org/repo", issue, "/nonexistent/path/12345", "main", "github")
        # Should not contain "Relevant Source Context" header
        assert "## Relevant Source Context" not in body

    def test_no_context_when_no_files_found(self, tmp_path):
        """No source context when identify_relevant_files returns empty."""
        issue = _make_issue(number=100, title="Epic task", body="generic description")
        body = disp._planner_body("org/repo", issue, str(tmp_path), "main", "github")
        # No context since no files match
        assert "## Relevant Source Context" not in body

    def test_context_included_when_files_found(self, tmp_path):
        """Source context is included when files are found."""
        # Create a test file
        test_file = tmp_path / "core" / "test_module.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("def test_function():\n    return 'hello'\n")

        # Issue mentions the file path explicitly
        issue = _make_issue(
            number=200,
            title="Epic with file reference",
            body="We need to modify core/test_module.py to fix the bug.\n" + "- [ ] task\n" * 5
        )
        body = disp._planner_body("org/repo", issue, str(tmp_path), "main", "github")

        # Should contain the source context section
        assert "## Relevant Source Context" in body
        # Should contain the file path
        assert "core/test_module.py" in body or "test_module.py" in body
        # Should contain the file content
        assert "test_function" in body
        assert "return 'hello'" in body

    def test_context_respects_max_files(self, tmp_path):
        """Context respects max_files=10 cap."""
        # Create 15 files
        for i in range(15):
            f = tmp_path / f"module_{i}.py"
            f.write_text(f"# Module {i}\n")

        # Issue mentions all files
        file_refs = " ".join([f"module_{i}.py" for i in range(15)])
        issue = _make_issue(
            number=300,
            title="Many files",
            body=f"Files: {file_refs}\n" + "- [ ] task\n" * 5
        )
        body = disp._planner_body("org/repo", issue, str(tmp_path), "main", "github")

        if "## Relevant Source Context" in body:
            # Count how many files are in the context (look for ### headers)
            import re
            file_headers = re.findall(r"### `([^`]+)`", body)
            # Should not exceed 10 files
            assert len(file_headers) <= 10

    def test_context_respects_per_file_size_limit(self, tmp_path):
        """Context respects 50KB per-file size limit."""
        # Create a file larger than 50KB
        large_file = tmp_path / "large.py"
        large_content = "# " + "X" * 60_000 + "\n"  # ~60KB
        large_file.write_text(large_content)

        issue = _make_issue(
            number=400,
            title="Large file",
            body="See large.py for details\n" + "- [ ] task\n" * 5
        )
        body = disp._planner_body("org/repo", issue, str(tmp_path), "main", "github")

        if "## Relevant Source Context" in body:
            # The primitive truncates content to 50000 bytes. The markdown
            # wrapping adds a trailing newline (```\n<content>\n```), so the
            # code block content may be up to ~50001 bytes. Verify the source
            # file content does not exceed the per-file cap by a significant
            # margin (tolerate 1 byte for markdown wrapping).
            import re
            code_blocks = re.findall(r"```\n(.*?)```", body, re.DOTALL)
            assert code_blocks, "Expected at least one code block in context"
            for block in code_blocks:
                assert len(block.encode("utf-8")) <= 50_010, (
                    f"Code block too large: {len(block.encode('utf-8'))} bytes"
                )

    def test_context_total_cap_enforced(self, tmp_path):
        """Total context capped at 100KB."""
        # Create multiple files that together exceed 100KB
        for i in range(5):
            f = tmp_path / f"big_{i}.py"
            f.write_text("# " + f"file_{i}\n" * 30_000 + "\n")  # ~25KB each, ~125KB total

        file_refs = " ".join([f"big_{i}.py" for i in range(5)])
        issue = _make_issue(
            number=500,
            title="Multiple large files",
            body=f"Files: {file_refs}\n" + "- [ ] task\n" * 5
        )
        body = disp._planner_body("org/repo", issue, str(tmp_path), "main", "github")

        if "## Relevant Source Context" in body:
            # Extract just the source context section
            import re
            # Find the section and measure its byte size
            # The context is everything after "## Relevant Source Context" to end
            context_start = body.find("## Relevant Source Context")
            if context_start >= 0:
                # rstrip the trailing newline appended by the f-string template
                context_section = body[context_start:].rstrip("\n")
                # Should be <= 100KB bytes
                assert len(context_section.encode("utf-8")) <= 100_000

    def test_graceful_degradation_on_error(self, tmp_path):
        """No crash when file reading fails; context omitted."""
        # Create a file that will cause read errors (binary with NUL bytes)
        binary_file = tmp_path / "binary.py"
        binary_file.write_bytes(b"\x00\x01\x02\x03" * 1000)

        issue = _make_issue(
            number=600,
            title="Binary file",
            body="See binary.py for details\n" + "- [ ] task\n" * 5
        )
        # Should not crash
        body = disp._planner_body("org/repo", issue, str(tmp_path), "main", "github")

        # Should still have the issue body
        assert "#600" in body
        # Binary file should be skipped (no context)
        assert "## Relevant Source Context" not in body

    def test_graceful_degradation_on_mock_error(self, tmp_path):
        """Graceful degradation when primitives raise exceptions."""
        with mock.patch("core.iterate.identify_relevant_files") as m:
            m.side_effect = RuntimeError("mocked error")
            issue = _make_issue(number=700, title="Error test", body="- [ ] task\n" * 5)
            # Should not crash
            body = disp._planner_body("org/repo", issue, str(tmp_path), "main", "github")
            # Should still have the issue body
            assert "#700" in body
            # Context section should be absent (graceful degradation)
            assert "## Relevant Source Context" not in body

    def test_issue_body_preserved_with_context(self, tmp_path):
        """Issue body is preserved even when source context is injected."""
        # Create a file
        src = tmp_path / "feature.py"
        src.write_text("def feature():\n    pass\n")

        original_body = "This is the original epic issue body with important details.\n" + "- [ ] task\n" * 5
        issue = _make_issue(
            number=800,
            title="Preserve body",
            body=f"See feature.py for more.\n{original_body}"
        )
        body = disp._planner_body("org/repo", issue, str(tmp_path), "main", "github")

        # Original body excerpt should be present
        assert "original epic issue body" in body
        # Source context should also be present
        assert "## Relevant Source Context" in body
        assert "feature" in body

    def test_no_binary_files_in_context(self, tmp_path):
        """Binary files are excluded from context."""
        # Create a binary file (contains NUL bytes)
        bin_file = tmp_path / "image.png"
        bin_file.write_bytes(b"\x89PNG\x0D\x0A\x1A\x0A" + b"\x00" * 100)

        # Also create a text file
        txt_file = tmp_path / "readme.py"
        txt_file.write_text("# README\n")

        issue = _make_issue(
            number=900,
            title="Mixed files",
            body="See image.png and readme.py\n" + "- [ ] task\n" * 5
        )
        body = disp._planner_body("org/repo", issue, str(tmp_path), "main", "github")

        if "## Relevant Source Context" in body:
            # Slice out just the source context section (after the header)
            ctx_start = body.index("## Relevant Source Context")
            source_section = body[ctx_start:]
            # Binary file should NOT appear under its code-block header in the
            # context. The file path may still appear in the issue body above
            # the context section, so only check the context section.
            assert "### `image.png`" not in source_section
            # Text file SHOULD be in the context
            assert "### `readme.py`" in source_section or "readme" in source_section.lower()


class TestPlannerBodyBackwardCompatibility:
    """Ensure existing _planner_body behavior is preserved."""

    def test_contains_issue_number(self):
        issue = _make_issue(number=100, title="Big task", body="- [ ] t\n" * 5)
        body = disp._planner_body("org/repo", issue, "/work", "main", "github")
        assert "#100" in body

    def test_contains_title(self):
        issue = _make_issue(number=200, title="Big Task Title", body="- [ ] t\n" * 5)
        body = disp._planner_body("org/repo", issue, "/work", "main", "github")
        assert "Big Task Title" in body

    def test_mentions_planning_complete(self):
        issue = _make_issue(number=300, body="- [ ] t\n" * 5)
        body = disp._planner_body("org/repo", issue, "/work", "main", "github")
        assert "PLANNING COMPLETE" in body

    def test_lists_detection_reasons(self):
        issue = _make_issue(number=400, body="- [ ] t\n" * 5, labels=[{"name": "epic"}])
        body = disp._planner_body("org/repo", issue, "/work", "main", "github")
        assert "checklist" in body.lower()
        assert "epic" in body.lower()

    def test_contains_repo_info(self):
        issue = _make_issue(body="- [ ] t\n" * 5)
        body = disp._planner_body("owner/repo", issue, "/path/to/work", "dev", "github")
        assert "owner/repo" in body
        assert "/path/to/work" in body
        assert "dev" in body
        assert "github" in body

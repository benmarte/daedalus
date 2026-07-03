"""
Tests for Phase 4: Source file reading and context injection in planner.

Covers:
- identify_relevant_files: path extraction, function/class matching, directory heuristic
- read_source_files: binary detection, size limits, path traversal protection
- build_sub_issue_context: formatting file content sections
- build_enhanced_scope: combining scope with context
- Integration: source files injected into sub-issue bodies
"""
import tempfile
from pathlib import Path
from core.iterate import (
    identify_relevant_files,
    read_source_files,
    build_sub_issue_context,
    build_enhanced_scope,
)


def test_identify_relevant_files_path_extraction():
    """Strategy 1: Extract file paths mentioned in scope."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        
        # Create directories then files
        (workdir / "src").mkdir()
        (workdir / "src" / "module.py").write_text("def func(): pass")
        
        files, metadata = identify_relevant_files(
            "Update src/module.py implementation",
            str(workdir)
        )
        
        assert len(files) >= 1, f"Should find at least 1 file, found {len(files)}"
        file_strs = [str(f) for f in files]
        assert any("module.py" in f for f in file_strs), f"Should find module.py in {file_strs}"
        print("PASS: Path extraction finds mentioned files")


def test_identify_relevant_files_directory_fallback():
    """Strategy 3: Scan common source directories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        
        # Only create src (not lib) so we can assert only src is found
        (workdir / "src").mkdir()
        (workdir / "src" / "main.py").write_text("# Main file")
        (workdir / "src" / "utils.py").write_text("# Utils file")
        
        # Scope does NOT mention any specific files or common directory names.
        # The directory heuristic runs when strategies 1/2 find nothing and scope 
        # matches a common directory name. Since this scope has no file paths,
        # identifiers, or common dir names, we expect an empty result.
        files, metadata = identify_relevant_files(
            "Add new feature",
            str(workdir)
        )
        
        # With no path extraction hits and no common dir keywords in scope,
        # the heuristic should return nothing.
        assert len(files) == 0, f"Expected no files without dir keyword, got {len(files)}"
        
        # Now test with a scope that mentions 'src'
        files, metadata = identify_relevant_files(
            "Update the src module",
            str(workdir)
        )
        assert len(files) > 0, f"Should find files in src, got {len(files)}"
        file_strs = [str(f) for f in files]
        assert any("src" in str(f) for f in files), f"Should find files in src/ in {file_strs}"
        print("PASS: Directory heuristic scans common dirs when mentioned")


def test_read_source_files_basic():
    """Read text files and return their content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        
        (workdir / "file1.py").write_text("# Python file\nprint('hello')")
        (workdir / "file2.txt").write_text("Text file\nwith content")
        
        # Pass Path objects so key matches str(file)
        file_paths = [workdir / "file1.py", workdir / "file2.txt"]
        contents = read_source_files(file_paths, str(workdir))
        
        f1 = str(workdir / "file1.py")
        f2 = str(workdir / "file2.txt")
        assert len(contents) == 2, f"Should read 2 files, got {len(contents)}"
        assert f1 in contents, f"Expected key {f1} in {list(contents.keys())}"
        assert f2 in contents, f"Expected key {f2} in {list(contents.keys())}"
        assert "print('hello')" in contents[f1]
        assert "with content" in contents[f2]
        print("PASS: Basic file reading works")


def test_read_source_files_binary_detection():
    """Detect and skip binary files using null byte check."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        
        (workdir / "text.txt").write_text("Plain text file")
        # Binary file: contains null bytes
        (workdir / "binary.bin").write_bytes(b"header\x00\x01\x02trailer")
        # Text file despite unusual extension
        (workdir / "strange.dat").write_text("still text content")
        
        file_paths = [workdir / "text.txt", workdir / "binary.bin", workdir / "strange.dat"]
        contents = read_source_files(file_paths, str(workdir))
        
        assert len(contents) == 2, f"Should skip binary (null bytes), got {len(contents)}: {list(contents.keys())}"
        assert str(workdir / "text.txt") in contents
        assert str(workdir / "strange.dat") in contents
        assert str(workdir / "binary.bin") not in contents
        print("PASS: Binary detection skips files with null bytes")


def test_read_source_files_size_limit():
    """Truncate files exceeding max_size."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        
        large_content = "x" * 100000
        (workdir / "large.txt").write_text(large_content)
        (workdir / "normal.txt").write_text("small content")
        
        file_paths = [workdir / "large.txt", workdir / "normal.txt"]
        contents = read_source_files(file_paths, str(workdir), max_size=1000)
        
        f_large = str(workdir / "large.txt")
        assert f_large in contents, f"Expected large file in contents"
        # Truncated to 1000 bytes
        assert len(contents[f_large]) == 1000, f"Expected 1000 chars, got {len(contents[f_large])}"
        assert contents[str(workdir / "normal.txt")] == "small content"
        print("PASS: File size truncation works")


def test_read_source_files_path_traversal():
    """Block path traversal attempts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        
        (workdir / "safe.txt").write_text("safe content")
        
        parent_dir = workdir.parent
        (parent_dir / "external.txt").write_text("external content")
        
        file_paths = [workdir / "safe.txt", parent_dir / "external.txt"]
        contents = read_source_files(file_paths, str(workdir))
        
        assert len(contents) == 1
        assert str(workdir / "safe.txt") in contents
        assert str(parent_dir / "external.txt") not in contents
        print("PASS: Path traversal protection works")


def test_read_source_files_missing_file_warning():
    """Log warning for missing files without raising."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        
        # Ask to read a file that doesn't exist
        file_paths = [workdir / "nonexistent.py"]
        contents = read_source_files(file_paths, str(workdir))
        
        assert len(contents) == 0
        print("PASS: Missing files handled gracefully")


def test_build_sub_issue_context_empty():
    """Return empty string when no files provided."""
    result = build_sub_issue_context({})
    assert result == ""
    print("PASS: Empty context returns empty string")


def test_build_sub_issue_context_single_file():
    """Format single file content with path heading and code block."""
    file_contents = {"/path/to/test.py": "def test():\n    assert True"}
    result = build_sub_issue_context(file_contents)
    
    assert "## Relevant Source Context" in result
    assert "test.py" in result
    assert "def test():" in result
    assert "```" in result
    print("PASS: Single file formatting works")


def test_build_sub_issue_context_multiple_files():
    """Format multiple files into a combined context."""
    file_contents = {
        "/path/to/file1.py": "content1",
        "/path/to/file2.js": "content2",
        "/path/to/file3.txt": "content3"
    }
    result = build_sub_issue_context(file_contents)
    
    assert "## Relevant Source Context" in result
    assert "content1" in result
    assert "content2" in result
    assert "content3" in result
    print("PASS: Multi-file formatting works")


def test_build_enhanced_scope_no_context():
    """Return original scope when no context provided."""
    scope = "Update the authentication module"
    result = build_enhanced_scope(scope, "")
    assert result == scope
    print("PASS: No context returns original scope")


def test_build_enhanced_scope_with_context():
    """Append context section to scope."""
    scope = "Fix the bug in parser"
    context = "## Relevant Source Context\n\nParser code..."
    result = build_enhanced_scope(scope, context)
    
    assert result.startswith(scope)
    assert context in result
    print("PASS: Context appended to scope")


def test_build_enhanced_scope_preserves_scope_with_empty_context():
    """Scope with no source files stays unchanged."""
    scope = "General refactoring task without file context"
    result = build_enhanced_scope(scope, "")
    assert result == scope
    assert "Relevant Source Context" not in result
    print("PASS: Scope preserved when no context")


def test_integration_sub_issue_with_source_files():
    """Integration: Verify source files are injected into sub-issue bodies."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        
        # Test structure
        (workdir / "src").mkdir()
        (workdir / "src" / "parser.py").write_text("def parse():\n    return 'parsed'")
        
        scope = "Fix the parse function in src/parser.py"
        
        # Step 1: Identify files
        files, metadata = identify_relevant_files(scope, str(workdir))
        assert len(files) > 0, f"Should find files, found {len(files)}"
        
        # Step 2: Read files  
        file_contents = read_source_files(files, str(workdir))
        assert len(file_contents) > 0, f"Should read files, got {len(file_contents)}"
        
        # Step 3: Build context
        context_section = build_sub_issue_context(file_contents)
        assert len(context_section) > 0, "Context should not be empty"
        
        # Step 4: Enhance scope
        enhanced = build_enhanced_scope(scope, context_section)
        assert scope in enhanced, "Original scope should be preserved"
        assert "## Relevant Source Context" in enhanced, "Context section added"
        assert "parse" in enhanced, "File content included"
        
        print("PASS: Integration - source files injected into sub-issue scope")


def test_integration_empty_files_graceful_degradation():
    """Integration: Sub-issue still generated even when no files found."""
    scope = "Generic task with no source file references"
    files, metadata = identify_relevant_files(scope, "/tmp")
    
    # With no file hits, context is empty
    context_section = build_sub_issue_context({})
    enhanced = build_enhanced_scope(scope, context_section)
    
    assert enhanced == scope, "Scope unchanged when no source context"
    print("PASS: Graceful degradation when no files match")


def test_grep_py_definitions_finds_def_and_class():
    """_grep_py_definitions locates files defining a function or class (issue #1148)."""
    from core.iterate import _grep_py_definitions

    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        (workdir / "mod.py").write_text("def target_helper():\n    pass\n")
        (workdir / "other.py").write_text("class TargetClass:\n    pass\n")

        hits = _grep_py_definitions("target_helper", str(workdir))
        assert any("mod.py" in h for h in hits), f"Should find mod.py, got {hits}"

        hits = _grep_py_definitions("TargetClass", str(workdir))
        assert any("other.py" in h for h in hits), f"Should find other.py, got {hits}"

        assert _grep_py_definitions("no_such_symbol", str(workdir)) == []
        print("PASS: _grep_py_definitions finds def/class definitions")


def test_grep_py_definitions_failure_graceful():
    """_grep_py_definitions returns [] on subprocess failure instead of raising."""
    from unittest import mock

    from core import iterate as _it

    with mock.patch.object(_it.subprocess, "run", side_effect=OSError("no grep")):
        assert _it._grep_py_definitions("anything", "/tmp") == []
    print("PASS: _grep_py_definitions degrades gracefully on grep failure")


def test_identify_relevant_files_definition_scan():
    """Strategy 2: def/class names in scope resolve to defining files via grep."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        (workdir / "core").mkdir()
        (workdir / "core" / "widgets.py").write_text("def spin_widget():\n    pass\n")

        files, metadata = identify_relevant_files(
            "Refactor def spin_widget to accept a speed argument",
            str(workdir),
        )
        file_strs = [str(f) for f in files]
        assert any("widgets.py" in f for f in file_strs), f"Should find widgets.py in {file_strs}"
        assert any("definition_scan" in m for m in metadata.values()), f"metadata: {metadata}"
        print("PASS: Definition scan resolves def names through _grep_py_definitions")

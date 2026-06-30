"""Tests for epic-context-informed source reading (issue #387).

Covers:
- extract_epic_context: path, identifier, component, dir-tag, keyword extraction
- AggregateEpicContext: union of per-sub-issue contexts
- filter_context_for_sub: narrows file_contents per sub-issue context
- identify_relevant_files with epic_context param (priority-boost)
- Integration: per-sub-issue scoped context injection
"""
import tempfile
from pathlib import Path

from core.iterate import (
    EpicContext,
    AggregateEpicContext,
    extract_epic_context,
    filter_context_for_sub,
    identify_relevant_files,
)


# ── extract_epic_context ─────────────────────────────────────────────────────


def test_extract_epic_context_extracts_file_paths():
    """Path extraction identifies explicit file paths."""
    ctx = extract_epic_context("Modify core/iterate.py and scripts/daedalus_dispatch.py")
    assert "core/iterate.py" in ctx.file_paths
    assert "scripts/daedalus_dispatch.py" in ctx.file_paths


def test_extract_epic_context_extracts_identifiers():
    """Identifier extraction captures def/class names."""
    ctx = extract_epic_context("Refactor def identify_relevant_files and class DependencySnapshot")
    assert "identify_relevant_files" in ctx.identifiers
    assert "DependencySnapshot" in ctx.identifiers


def test_extract_epic_context_extracts_component_names():
    """Component names are extracted when provided in known_components."""
    known = {"planner", "dispatcher", "reviewer"}
    ctx = extract_epic_context("Update the planner to notify the dispatcher", known_components=known)
    assert "planner" in ctx.component_names
    assert "dispatcher" in ctx.component_names


def test_extract_epic_context_extracts_dir_tags():
    """Directory tokens are extracted from common dir mentions."""
    ctx = extract_epic_context("Refactor core module and tests directory")
    assert "core" in ctx.dir_tags
    assert "tests" in ctx.dir_tags


def test_extract_epic_context_extracts_keywords():
    """Significant tokens are extracted as keywords (deduped, lowercased)."""
    ctx = extract_epic_context("Optimize authentication token rotation mechanism")
    # Significant words > 3 chars, after stop-word removal
    assert "authentication" in ctx.keywords
    assert "rotation" in ctx.keywords
    assert "mechanism" in ctx.keywords


def test_extract_epic_context_deduplicates():
    """Duplicate mentions are deduplicated."""
    ctx = extract_epic_context("core/iterate.py and core/iterate.py again")
    assert ctx.file_paths.count("core/iterate.py") == 1


def test_extract_epic_context_empty_input():
    """Empty text returns empty context."""
    ctx = extract_epic_context("")
    assert ctx.file_paths == []
    assert ctx.identifiers == []
    assert ctx.component_names == []
    assert ctx.dir_tags == []
    assert ctx.keywords == []


# ── AggregateEpicContext ─────────────────────────────────────────────────────


def test_aggregate_epic_context_union():
    """AggregateEpicContext unions per-sub-issue contexts."""
    c1 = EpicContext(
        scope="sub1",
        file_paths=["core/iterate.py"],
        identifiers=["identify_relevant_files"],
        component_names=["planner"],
        dir_tags=["core"],
        keywords=["iterate"],
    )
    c2 = EpicContext(
        scope="sub2",
        file_paths=["scripts/dispatch.py"],
        identifiers=["run_iterate"],
        component_names=["dispatcher"],
        dir_tags=["scripts"],
        keywords=["dispatch"],
    )
    agg = AggregateEpicContext(
        per_sub_issues=[c1, c2],
        all_file_paths={"core/iterate.py", "scripts/dispatch.py"},
        all_identifiers={"identify_relevant_files", "run_iterate"},
        all_component_names={"planner", "dispatcher"},
        all_dir_tags={"core", "scripts"},
    )
    assert "core/iterate.py" in agg.all_file_paths
    assert "scripts/dispatch.py" in agg.all_file_paths
    assert "identify_relevant_files" in agg.all_identifiers
    assert "run_iterate" in agg.all_identifiers
    assert "planner" in agg.all_component_names
    assert "dispatcher" in agg.all_component_names
    assert "core" in agg.all_dir_tags
    assert "scripts" in agg.all_dir_tags


# ── filter_context_for_sub ───────────────────────────────────────────────────


def test_filter_context_for_sub_matching():
    """Files matching sub-context signals are kept."""
    file_contents = {
        "core/iterate.py": "def identify_relevant_files(): ...",
        "scripts/dispatch.py": "def run_iterate(): ...",
        "core/providers/github.py": "class GitHubProvider: ...",
    }
    file_metadata = {
        "core/iterate.py": "path_extraction",
        "scripts/dispatch.py": "path_extraction",
        "core/providers/github.py": "path_extraction",
    }
    sub_ctx = EpicContext(
        scope="Update core/iterate.py and identify_relevant_files",
        file_paths=["core/iterate.py"],
        identifiers=["identify_relevant_files"],
        component_names=[],
        dir_tags=[],
        keywords=["iterate"],
    )
    filtered = filter_context_for_sub(file_contents, sub_ctx, file_metadata)
    assert "core/iterate.py" in filtered
    # scripts/dispatch.py and github.py don't match signals → excluded
    assert "scripts/dispatch.py" not in filtered
    assert "core/providers/github.py" not in filtered


def test_filter_context_for_sub_returns_all_when_no_match():
    """Graceful degradation: when no files match, all are returned."""
    file_contents = {
        "core/iterate.py": "def foo(): ...",
        "scripts/dispatch.py": "def bar(): ...",
    }
    file_metadata = {
        "core/iterate.py": "path_extraction",
        "scripts/dispatch.py": "path_extraction",
    }
    # Sub-context with no matching signals
    sub_ctx = EpicContext(
        scope="Do something generic",
        file_paths=[],
        identifiers=[],
        component_names=[],
        dir_tags=[],
        keywords=[],
    )
    filtered = filter_context_for_sub(file_contents, sub_ctx, file_metadata)
    # No match → graceful degradation → return all
    assert filtered == file_contents


def test_filter_context_for_sub_dir_tag_match():
    """Files in directories matching dir_tags are kept."""
    file_contents = {
        "core/iterate.py": "...",
        "scripts/dispatch.py": "...",
    }
    file_metadata = {
        "core/iterate.py": "path_extraction",
        "scripts/dispatch.py": "path_extraction",
    }
    sub_ctx = EpicContext(
        scope="Update scripts directory",
        file_paths=[],
        identifiers=[],
        component_names=[],
        dir_tags=["scripts"],
        keywords=[],
    )
    filtered = filter_context_for_sub(file_contents, sub_ctx, file_metadata)
    assert "scripts/dispatch.py" in filtered
    assert "core/iterate.py" not in filtered


# ── identify_relevant_files with epic_context ────────────────────────────────


def test_identify_relevant_files_with_epic_context():
    """epic_context boosts priority for mentioned file paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        (workdir / "core").mkdir()
        (workdir / "core" / "iterate.py").write_text("def identify_relevant_files(): pass")
        (workdir / "scripts").mkdir()
        (workdir / "scripts" / "dispatch.py").write_text("def run_iterate(): pass")

        # Scope text mentions dispatch, but epic_context mentions iterate.py prominently
        scope = "Refactor dispatch logic"
        agg = AggregateEpicContext(
            per_sub_issues=[],
            all_file_paths={"core/iterate.py"},
            all_identifiers=set(),
            all_component_names=set(),
            all_dir_tags=set(),
        )
        files, meta = identify_relevant_files(
            scope, str(workdir), epic_context=agg
        )
        file_strs = [str(f) for f in files]
        # The file from epic_context should be found (priority boost)
        assert any("iterate.py" in f for f in file_strs), f"Expected iterate.py in {file_strs}"


# ── Integration ──────────────────────────────────────────────────────────────


def test_integration_per_sub_issue_scoped_context():
    """Per-sub-issue filtering: each sub-issue gets only relevant context."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        (workdir / "core").mkdir()
        (workdir / "core" / "iterate.py").write_text("def identify_relevant_files(): pass")
        (workdir / "scripts").mkdir()
        (workdir / "scripts" / "dispatch.py").write_text("def run_iterate(): pass")

        # Create contexts for two sub-issues
        c1 = extract_epic_context("Update core/iterate.py and identify_relevant_files")
        c2 = extract_epic_context("Modify scripts/dispatch.py")

        # Full file contents from both files
        file_contents = {
            "core/iterate.py": "def identify_relevant_files(): pass",
            "scripts/dispatch.py": "def run_iterate(): pass",
        }
        file_metadata = {
            "core/iterate.py": "path_extraction",
            "scripts/dispatch.py": "path_extraction",
        }

        # Filter for sub-issue 1 — should get only iterate.py
        filtered1 = filter_context_for_sub(file_contents, c1, file_metadata)
        assert "core/iterate.py" in filtered1
        assert "scripts/dispatch.py" not in filtered1

        # Filter for sub-issue 2 — should get only dispatch.py
        filtered2 = filter_context_for_sub(file_contents, c2, file_metadata)
        assert "scripts/dispatch.py" in filtered2
        assert "core/iterate.py" not in filtered2

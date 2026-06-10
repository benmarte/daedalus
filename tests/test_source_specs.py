"""Tests for core.source_specs — spec-file trigger source."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

# Make the package root importable (config/, core/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import source_specs  # noqa: E402
from core import kanban  # noqa: E402

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}")


# ── list_spec_files ─────────────────────────────────────────────────────────

def test_list_spec_files_finds_md_files():
    """list_spec_files returns all *.md files in .hermes/pending/, sorted."""
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    pending = tmp / ".hermes" / "pending"
    pending.mkdir(parents=True)
    (pending / "foo.md").write_text("# Foo spec\nDo the thing.")
    (pending / "bar.md").write_text("# Bar spec")
    (pending / "not-a-spec.txt").write_text("nope")
    (pending / "baz.yml").write_text("nope")

    result = source_specs.list_spec_files(str(tmp))

    check("list_spec_files returns 2 files", len(result) == 2)
    check("returns sorted: bar.md first", result[0].name == "bar.md")
    check("returns sorted: foo.md second", result[1].name == "foo.md")
    check("ignores non-md files (txt, yml)", all(f.suffix == ".md" for f in result))

    import shutil
    shutil.rmtree(tmp)


def test_list_spec_files_missing_directory():
    """list_spec_files returns empty list when .hermes/pending/ does not exist."""
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    # No .hermes/ directory at all
    result = source_specs.list_spec_files(str(tmp))
    check("missing directory returns []", result == [])

    import shutil
    shutil.rmtree(tmp)


def test_list_spec_files_empty_directory():
    """list_spec_files returns empty list when directory exists but has no *.md."""
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    pending = tmp / ".hermes" / "pending"
    pending.mkdir(parents=True)
    result = source_specs.list_spec_files(str(tmp))
    check("empty directory returns []", result == [])

    import shutil
    shutil.rmtree(tmp)


# ── spec_to_triage ──────────────────────────────────────────────────────────

def test_spec_to_triage_creates_card():
    """spec_to_triage reads spec file, creates triage card with correct args."""
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp())
    try:
        pending = tmp / ".hermes" / "pending"
        pending.mkdir(parents=True)
        spec_file = pending / "feature-x.md"
        spec_content = "# Feature X\nImplement the thing."
        spec_file.write_text(spec_content)

        # Track what create_triage was called with
        calls = []

        def fake_create_triage(slug, issue_number, title, body, *, idempotency_key, workspace):
            calls.append({
                "slug": slug,
                "issue_number": issue_number,
                "title": title,
                "body": body,
                "idempotency_key": idempotency_key,
                "workspace": workspace,
            })
            return "t_test123"

        with mock.patch.object(kanban, "create_triage", fake_create_triage):
            tid = source_specs.spec_to_triage(
                slug="test-board",
                repo_path=str(tmp),
                spec_file=spec_file,
                base_branch="main",
            )

        check("returns the task id", tid == "t_test123")
        check("called create_triage once", len(calls) == 1)

        c = calls[0]
        check("slug is correct", c["slug"] == "test-board")
        check("issue_number is None (not a GitHub issue)", c["issue_number"] is None)
        check("title is filename stem", c["title"] == "feature-x")
        check("body contains lifecycle instruction",
              "PR into main" in c["body"])
        check("body contains spec content",
              spec_content in c["body"])
        check("workspace is dir:<repo_path>",
              c["workspace"] == f"dir:{tmp}")
        check("idempotency key starts with spec-",
              c["idempotency_key"].startswith("spec-"))
    finally:
        shutil.rmtree(tmp)


def test_spec_to_triage_idempotent():
    """Re-running spec_to_triage with the same file produces the same idempotency key."""
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp())
    try:
        pending = tmp / ".hermes" / "pending"
        pending.mkdir(parents=True)
        spec_file = pending / "feature-x.md"
        spec_file.write_text("# Feature X\nDo it.")

        keys = []

        def fake_create_triage(slug, issue_number, title, body, *, idempotency_key, workspace):
            keys.append(idempotency_key)
            return None  # simulate "already exists"

        with mock.patch.object(kanban, "create_triage", fake_create_triage):
            source_specs.spec_to_triage("board", str(tmp), spec_file)
            source_specs.spec_to_triage("board", str(tmp), spec_file)

        check("create_triage called twice (idempotency checked upstream)",
              len(keys) == 2)
        check("both calls use the same idempotency key",
              keys[0] == keys[1])
    finally:
        shutil.rmtree(tmp)


def test_spec_to_triage_missing_file_returns_none():
    """spec_to_triage returns None and logs a warning for a missing file."""
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp())
    try:
        spec_file = tmp / ".hermes" / "pending" / "nonexistent.md"
        tid = source_specs.spec_to_triage(
            slug="board", repo_path=str(tmp), spec_file=spec_file,
        )
        check("missing file returns None", tid is None)
    finally:
        shutil.rmtree(tmp)


def test_spec_to_triage_empty_file_skipped():
    """spec_to_triage returns None for an empty spec file."""
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp())
    try:
        pending = tmp / ".hermes" / "pending"
        pending.mkdir(parents=True)
        spec_file = pending / "empty.md"
        spec_file.write_text("   \n  ")  # whitespace-only -> strip() yields ""

        tid = source_specs.spec_to_triage(
            slug="board", repo_path=str(tmp), spec_file=spec_file,
        )
        check("empty file returns None", tid is None)
    finally:
        shutil.rmtree(tmp)


def test_spec_to_triage_explicit_workspace():
    """spec_to_triage respects an explicit workspace argument."""
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp())
    try:
        pending = tmp / ".hermes" / "pending"
        pending.mkdir(parents=True)
        spec_file = pending / "x.md"
        spec_file.write_text("# X\nYep.")

        calls = []

        def fake_create_triage(slug, issue_number, title, body, *, idempotency_key, workspace):
            calls.append(workspace)
            return "t_x"

        with mock.patch.object(kanban, "create_triage", fake_create_triage):
            source_specs.spec_to_triage(
                "board", str(tmp), spec_file,
                workspace="worktree:/custom/checkout",
            )
        check("explicit workspace passed through",
              calls[0] == "worktree:/custom/checkout")
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    print("Source Specs tests")
    print("-" * 60)
    for fn in (
        test_list_spec_files_finds_md_files,
        test_list_spec_files_missing_directory,
        test_list_spec_files_empty_directory,
        test_spec_to_triage_creates_card,
        test_spec_to_triage_idempotent,
        test_spec_to_triage_missing_file_returns_none,
        test_spec_to_triage_empty_file_skipped,
        test_spec_to_triage_explicit_workspace,
    ):
        fn()
    print("-" * 60)
    print(f"Results: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)

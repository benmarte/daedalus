"""Integration tests for planner sub-issue creation end-to-end.

These tests exercise the planner's sub-issue creation flow with minimal mocking:
- Scenario A: Source files are available on disk — the planner reads them and creates
  sub-issues with file-specific acceptance criteria.
- Scenario B: Source files are unavailable (workdir missing/empty) — the planner gracefully
  falls back to generic criteria without errors.

The source-reading layer (identify_relevant_files, read_source_files, etc.) runs against
REAL files on disk, not mocks. Only the kanban and VCS provider are faked via conftest fixtures.

Asserts:
- Correct number of sub-issues created
- Correct issue structure (title, body schema, labels)
- Correct acceptance criteria content (file-specific vs generic)
- No errors raised during fallback
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.iterate import _execute_planner_decompose  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_card(issue_n: int = 1, body: str = "") -> dict:
    """Create a planner card dict."""
    body_with_ref = body if f"#{issue_n}" in body else f"Issue #{issue_n}\n{body}"
    return {
        "id": "t_integration",
        "title": f"#{issue_n} Integration Epic",
        "body": body_with_ref,
        "assignee": "planner-daedalus",
    }


class _FakeIssue:
    """Minimal issue object returned by provider.get_issue()."""

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


class _FakeProvider:
    """Mocked VCS provider that records calls."""

    def __init__(self, parent_issue: _FakeIssue, created_numbers=None):
        self.parent_issue = parent_issue
        self.created_numbers = created_numbers or [100, 101, 102]
        self._create_calls = []
        self._labels_added = []
        self._comments_posted = []
        self._next_idx = 0

    def get_issue(self, number: int):
        if number == self.parent_issue.number:
            return self.parent_issue
        # Sub-issues created during decomposition — return stubs for triage loop
        if number in self.created_numbers:
            return _FakeIssue(
                number=number,
                title=f"sub-issue #{number}",
                body=f"body of sub-issue #{number}",
            )
        return None

    def get_issue_comments(self, number: int):
        return []

    def create_issue(self, title: str, body: str, labels=None):
        self._create_calls.append((title, body, labels or []))
        if self._next_idx < len(self.created_numbers):
            n = self.created_numbers[self._next_idx]
            self._next_idx += 1
            return n
        return None

    def add_label(self, number: int, label: str):
        self._labels_added.append((number, label))

    def post_issue_comment(self, number: int, text: str):
        self._comments_posted.append((number, text))


@pytest.fixture()
def workdir_with_source_files(tmp_path):
    """Create a workdir with actual source files for integration testing."""
    # Create src directory structure
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    # Create auth module
    auth_dir = src_dir / "auth"
    auth_dir.mkdir()
    (auth_dir / "login.py").write_text(
        "def handle_login(username, password):\n"
        "    # Authenticate user\n"
        "    return True\n"
    )
    (auth_dir / "logout.py").write_text(
        "def handle_logout(session_id):\n"
        "    # End user session\n"
        "    return True\n"
    )

    # Create API module
    api_dir = src_dir / "api"
    api_dir.mkdir()
    (api_dir / "users.py").write_text(
        "def get_user(user_id):\n"
        "    # Fetch user from database\n"
        "    return {'id': user_id}\n"
    )

    # Create core module
    core_dir = src_dir / "core"
    core_dir.mkdir()
    (core_dir / "feature.py").write_text(
        "def implement_feature():\n"
        "    # New feature implementation\n"
        "    return 'feature'\n"
    )

    return str(tmp_path)


def _make_kanban_stub():
    """Create a stub kanban module for integration tests."""
    kanban = mock.MagicMock()
    kanban.complete.return_value = True
    kanban.create_triage.return_value = "t_triage_stub"
    kanban.decompose.return_value = True
    kanban.list_tasks.return_value = []
    return kanban


# ── Scenario A: Source available ─────────────────────────────────────────────


def test_integration_source_available_creates_file_specific_subissues(
    workdir_with_source_files,
):
    """Scenario A: Real source files exist — sub-issues contain file-specific context."""
    # Parent issue with checklist referencing specific files
    parent_body = (
        "- [ ] Fix login bug in src/auth/login.py\n"
        "- [ ] Update API endpoint in src/api/users.py\n"
    )
    parent_issue = _FakeIssue(
        number=42,
        title="File-specific Epic",
        body=parent_body,
        labels=["epic"],
    )
    provider = _FakeProvider(parent_issue, created_numbers=[200, 201])

    # Stub kanban so triage/decompose/complete don't hit real DB
    with mock.patch("core.iterate.kanban", _make_kanban_stub()):
        ok = _execute_planner_decompose(
            "test",
            _make_card(issue_n=42, body=parent_body),
            "test/repo",
            "PLANNING COMPLETE",
            workdir=workdir_with_source_files,
            provider=provider,
        )

    assert ok is True, "planner should succeed"

    # Two sub-issues created (one per checklist item)
    assert len(provider._create_calls) == 2, (
        f"Expected 2 sub-issues, got {len(provider._create_calls)}"
    )

    # Verify first sub-issue (login.py) has file-specific content
    first_title, first_body, first_labels = provider._create_calls[0]
    assert "src/auth/login.py" in first_body, (
        f"First sub-issue should reference src/auth/login.py: {first_body[:200]}"
    )
    assert "## Relevant Source Context" in first_body, (
        "First sub-issue should contain source context section"
    )
    # The actual file content should be embedded
    assert "def handle_login" in first_body, (
        "First sub-issue should contain the actual source code from login.py"
    )

    # Verify second sub-issue (users.py) has file-specific content
    second_title, second_body, second_labels = provider._create_calls[1]
    assert "src/api/users.py" in second_body, (
        f"Second sub-issue should reference src/api/users.py: {second_body[:200]}"
    )
    assert "## Relevant Source Context" in second_body, (
        "Second sub-issue should contain source context section"
    )
    assert "def get_user" in second_body, (
        "Second sub-issue should contain the actual source code from users.py"
    )

    # Verify labels
    assert "subtask" in first_labels, "First sub-issue should have 'subtask' label"
    assert "subtask" in second_labels, "Second sub-issue should have 'subtask' label"

    # Verify idempotency marker posted
    marker_posts = [
        (n, t) for n, t in provider._comments_posted
        if n == 42 and "daedalus:decomposed" in t
    ]
    assert marker_posts, "Idempotency marker should be posted on parent issue"


def test_integration_source_available_acceptance_criteria_reference_files(
    workdir_with_source_files,
):
    """Scenario A: Sub-issue acceptance criteria must reference specific file paths."""
    parent_body = "- [ ] Implement feature in src/core/feature.py\n"
    parent_issue = _FakeIssue(
        number=55,
        title="Feature Epic",
        body=parent_body,
        labels=["epic"],
    )
    provider = _FakeProvider(parent_issue, created_numbers=[300])

    with mock.patch("core.iterate.kanban", _make_kanban_stub()):
        ok = _execute_planner_decompose(
            "test",
            _make_card(issue_n=55, body=parent_body),
            "test/repo",
            "PLANNING COMPLETE",
            workdir=workdir_with_source_files,
            provider=provider,
        )

    assert ok is True
    assert len(provider._create_calls) == 1

    created_body = provider._create_calls[0][1]

    # Standard template sections must be present
    assert "## Scope" in created_body
    assert "## Acceptance Criteria" in created_body
    assert "## Notes" in created_body

    # Affected files section must reference the specific file
    assert "### Affected files & symbols" in created_body, (
        "Sub-issue should have 'Affected files & symbols' section when source available"
    )
    assert "`src/core/feature.py`" in created_body, (
        "Sub-issue should reference the specific file path"
    )
    assert "**Files:**" in created_body

    # Standard acceptance criteria items
    assert "- [ ] Implementation complete per scope" in created_body
    assert "- [ ] Tests pass (unit + integration where applicable)" in created_body
    assert "- [ ] PR opened and passing CI" in created_body


def test_integration_source_available_multiple_files_per_subissue(
    workdir_with_source_files,
):
    """Scenario A: A single sub-issue can reference multiple files."""
    # Use full paths in the checklist so extract_epic_context extracts them correctly
    parent_body = "- [ ] Refactor auth module across src/auth/login.py and src/auth/logout.py\n"
    parent_issue = _FakeIssue(
        number=77,
        title="Refactor Epic",
        body=parent_body,
        labels=["epic"],
    )
    provider = _FakeProvider(parent_issue, created_numbers=[400])

    with mock.patch("core.iterate.kanban", _make_kanban_stub()):
        ok = _execute_planner_decompose(
            "test",
            _make_card(issue_n=77, body=parent_body),
            "test/repo",
            "PLANNING COMPLETE",
            workdir=workdir_with_source_files,
            provider=provider,
        )

    assert ok is True
    assert len(provider._create_calls) == 1

    created_body = provider._create_calls[0][1]

    # Both full paths should be referenced in the Affected files section
    # (extract_epic_context extracts file paths from the scope text as written)
    assert "`src/auth/login.py`" in created_body, (
        f"Sub-issue should reference src/auth/login.py: {created_body}"
    )
    assert "`src/auth/logout.py`" in created_body, (
        f"Sub-issue should reference src/auth/logout.py: {created_body}"
    )


# ── Scenario B: Source unavailable ───────────────────────────────────────────


def test_integration_source_unavailable_fallback_to_generic_criteria(tmp_path):
    """Scenario B: Workdir empty/missing — sub-issues created with generic criteria."""
    parent_body = (
        "- [ ] step one\n"
        "- [ ] step two\n"
        "- [ ] step three\n"
    )
    parent_issue = _FakeIssue(
        number=99,
        title="No Source Epic",
        body=parent_body,
        labels=["epic"],
    )
    provider = _FakeProvider(parent_issue, created_numbers=[500, 501, 502])

    # Workdir exists but is empty — no source files to read
    empty_workdir = str(tmp_path / "empty")
    Path(empty_workdir).mkdir(parents=True, exist_ok=True)

    with mock.patch("core.iterate.kanban", _make_kanban_stub()):
        ok = _execute_planner_decompose(
            "test",
            _make_card(issue_n=99, body=parent_body),
            "test/repo",
            "PLANNING COMPLETE",
            workdir=empty_workdir,
            provider=provider,
        )

    assert ok is True, "planner should succeed even when source unavailable"

    # All three sub-issues created
    assert len(provider._create_calls) == 3, (
        f"Expected 3 sub-issues, got {len(provider._create_calls)}"
    )

    # Verify each sub-issue has generic structure (no file-specific context)
    for idx, (title, body, labels) in enumerate(provider._create_calls):
        assert body, f"Sub-issue {idx+1} body must not be empty"

        # Standard template sections must be present
        assert "## Scope" in body, f"Sub-issue {idx+1} missing Scope section"
        assert "## Acceptance Criteria" in body, f"Sub-issue {idx+1} missing Acceptance Criteria"
        assert "## Notes" in body, f"Sub-issue {idx+1} missing Notes section"

        # Should NOT contain file-specific context when source unavailable
        assert "## Relevant Source Context" not in body, (
            f"Sub-issue {idx+1} should not have source context when files unavailable"
        )
        assert "### Affected files & symbols" not in body, (
            f"Sub-issue {idx+1} should not have affected files section when unavailable"
        )

        # Standard acceptance criteria items
        assert "- [ ] Implementation complete per scope" in body
        assert "- [ ] Tests pass (unit + integration where applicable)" in body
        assert "- [ ] PR opened and passing CI" in body

        # Labels
        assert "subtask" in labels


def test_integration_source_unavailable_workdir_missing(tmp_path):
    """Scenario B: Workdir does not exist — graceful fallback."""
    parent_body = "- [ ] task one\n"
    parent_issue = _FakeIssue(
        number=88,
        title="Missing Workdir Epic",
        body=parent_body,
        labels=["epic"],
    )
    provider = _FakeProvider(parent_issue, created_numbers=[600])

    # Workdir points to non-existent path
    missing_workdir = str(tmp_path / "does" / "not" / "exist")

    with mock.patch("core.iterate.kanban", _make_kanban_stub()):
        ok = _execute_planner_decompose(
            "test",
            _make_card(issue_n=88, body=parent_body),
            "test/repo",
            "PLANNING COMPLETE",
            workdir=missing_workdir,
            provider=provider,
        )

    assert ok is True, "planner should succeed even when workdir missing"
    assert len(provider._create_calls) == 1

    created_body = provider._create_calls[0][1]

    # Standard template without file-specific context
    assert "## Scope" in created_body
    assert "## Acceptance Criteria" in created_body
    assert "## Relevant Source Context" not in created_body
    assert "### Affected files & symbols" not in created_body


def test_integration_source_unavailable_empty_workdir_string():
    """Scenario B: workdir='' is a valid fallback path."""
    parent_body = "- [ ] one\n- [ ] two\n"
    parent_issue = _FakeIssue(
        number=77,
        title="Empty Workdir Epic",
        body=parent_body,
        labels=["epic"],
    )
    provider = _FakeProvider(parent_issue, created_numbers=[700, 701])

    with mock.patch("core.iterate.kanban", _make_kanban_stub()):
        ok = _execute_planner_decompose(
            "test",
            _make_card(issue_n=77, body=parent_body),
            "test/repo",
            "PLANNING COMPLETE",
            workdir="",  # Empty workdir
            provider=provider,
        )

    assert ok is True
    assert len(provider._create_calls) == 2

    for title, body, labels in provider._create_calls:
        assert "## Scope" in body
        assert "## Acceptance Criteria" in body
        assert "## Relevant Source Context" not in body


# ── Assertion helpers ─────────────────────────────────────────────────────────


def test_integration_correct_number_of_subissues_from_checklist(tmp_path):
    """Verify correct number of sub-issues created from checklist items."""
    # Parent with 5 checklist items
    parent_body = "\n".join(f"- [ ] task {i}\n" for i in range(1, 6))
    parent_issue = _FakeIssue(
        number=11,
        title="Five Tasks Epic",
        body=parent_body,
        labels=["epic"],
    )
    created_nums = list(range(800, 805))
    provider = _FakeProvider(parent_issue, created_numbers=created_nums)

    empty_workdir = str(tmp_path / "empty")
    Path(empty_workdir).mkdir(parents=True, exist_ok=True)

    with mock.patch("core.iterate.kanban", _make_kanban_stub()):
        ok = _execute_planner_decompose(
            "test",
            _make_card(issue_n=11, body=parent_body),
            "test/repo",
            "PLANNING COMPLETE",
            workdir=empty_workdir,
            provider=provider,
        )

    assert ok is True
    assert len(provider._create_calls) == 5, (
        f"Expected 5 sub-issues (one per checklist item), got {len(provider._create_calls)}"
    )


def test_integration_default_titles_when_no_checklist(tmp_path):
    """When no checklist present, default sub-issue titles are used."""
    parent_body = "Big ambiguous epic with no checklist at all."
    parent_issue = _FakeIssue(
        number=22,
        title="Ambiguous Epic",
        body=parent_body,
        labels=["epic"],
    )
    created_nums = list(range(900, 903))
    provider = _FakeProvider(parent_issue, created_numbers=created_nums)

    empty_workdir = str(tmp_path / "empty")
    Path(empty_workdir).mkdir(parents=True, exist_ok=True)

    with mock.patch("core.iterate.kanban", _make_kanban_stub()):
        ok = _execute_planner_decompose(
            "test",
            _make_card(issue_n=22, body=parent_body),
            "test/repo",
            "PLANNING COMPLETE",
            workdir=empty_workdir,
            provider=provider,
        )

    assert ok is True
    # Should create default 3 sub-issues (spec, implement, verify)
    assert len(provider._create_calls) == 3, (
        f"Expected 3 default sub-issues, got {len(provider._create_calls)}"
    )

    titles = [t for t, _, _ in provider._create_calls]
    joined = " | ".join(titles).lower()
    # Default titles include 'spec', 'implement', 'verify'
    assert "spec" in joined or "implement" in joined or "verify" in joined, (
        f"Default titles should include spec/implement/verify: {titles}"
    )


def test_integration_idempotency_marker_posted(tmp_path):
    """Even in fallback, the idempotency marker is posted on the parent."""
    parent_body = "- [ ] one\n- [ ] two\n"
    parent_issue = _FakeIssue(
        number=33,
        title="Marker Test Epic",
        body=parent_body,
        labels=["epic"],
    )
    created_nums = [1000, 1001]
    provider = _FakeProvider(parent_issue, created_numbers=created_nums)

    empty_workdir = str(tmp_path / "empty")
    Path(empty_workdir).mkdir(parents=True, exist_ok=True)

    with mock.patch("core.iterate.kanban", _make_kanban_stub()):
        ok = _execute_planner_decompose(
            "test",
            _make_card(issue_n=33, body=parent_body),
            "test/repo",
            "PLANNING COMPLETE",
            workdir=empty_workdir,
            provider=provider,
        )

    assert ok is True

    # Idempotency marker posted on parent
    marker_posts = [
        (n, t) for n, t in provider._comments_posted
        if n == 33 and "daedalus:decomposed" in t
    ]
    assert marker_posts, (
        f"Expected idempotency marker on parent; got comments: {provider._comments_posted}"
    )


def test_integration_epic_label_applied_to_parent(tmp_path):
    """Epic label is applied to parent issue regardless of source availability."""
    parent_body = "- [ ] x\n"
    parent_issue = _FakeIssue(
        number=44,
        title="Epic Label Test",
        body=parent_body,
        labels=[],
    )
    provider = _FakeProvider(parent_issue, created_numbers=[1100])

    empty_workdir = str(tmp_path / "empty")
    Path(empty_workdir).mkdir(parents=True, exist_ok=True)

    with mock.patch("core.iterate.kanban", _make_kanban_stub()):
        ok = _execute_planner_decompose(
            "test",
            _make_card(issue_n=44, body=parent_body),
            "test/repo",
            "PLANNING COMPLETE",
            workdir=empty_workdir,
            provider=provider,
        )

    assert ok is True
    assert (44, "epic") in provider._labels_added, (
        f"Epic label should be applied to parent; got labels: {provider._labels_added}"
    )

"""Tests for core/webhook_normalizer.py — VCS-agnostic webhook payload normalization.

Exercises all four provider parsers (GitHub, GitLab, Azure DevOps, Hermes Kanban)
plus edge cases: malformed payloads, unknown providers, PRs disguised as issues,
and status_map customization.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on sys.path BEFORE importing core.webhook_normalizer
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.webhook_normalizer import normalize, ReadyEvent
import pytest

# ── GitHub provider ──────────────────────────────────────────────────────────


def test_github_ready_event():
    """GitHub Projects v2 edited event with Status change to Ready returns ReadyEvent."""
    payload = {
        "action": "edited",
        "projects_v2_item": {
            "node_id": "PVTI_abc123",
            "__typename": "Issue",
            "number": 42,
            "project_node_id": "PVT_abc",
        },
        "changes": {
            "field_value": {
                "field_name": "Status",
            }
        },
        "repository": {
            "full_name": "owner/repo",
        },
    }
    result = normalize("github", payload)
    assert result is not None
    assert isinstance(result, ReadyEvent)
    assert result.provider == "github"
    assert result.repo == "owner/repo"
    assert result.issue_number == 42
    assert result.board_slug == "PVT_abc"


def test_github_ignores_pull_requests():
    """GitHub payload with __typename=PullRequest returns None."""
    payload = {
        "action": "edited",
        "projects_v2_item": {
            "node_id": "PR_abc123",
            "__typename": "PullRequest",
            "number": 42,
        },
        "changes": {"field_value": {"field_name": "Status"}},
    }
    result = normalize("github", payload)
    assert result is None


def test_github_ignores_non_edited_actions():
    """GitHub 'created' or 'deleted' actions return None."""
    payload = {
        "action": "created",
        "projects_v2_item": {"number": 42},
        "changes": {"field_value": {"field_name": "Status"}},
    }
    result = normalize("github", payload)
    assert result is None


def test_github_ignores_non_status_field_changes():
    """GitHub edits to fields other than 'Status' return None."""
    payload = {
        "action": "edited",
        "projects_v2_item": {"number": 42},
        "changes": {"field_value": {"field_name": "Priority"}},
    }
    result = normalize("github", payload)
    assert result is None


def test_github_custom_status_map():
    """GitHub normalizer respects custom status_map for 'ready'."""
    payload = {
        "action": "edited",
        "projects_v2_item": {
            "number": 99,
            "project_node_id": "PVT_xyz",
        },
        "changes": {"field_value": {"field_name": "Status"}},
        "repository": {"full_name": "org/project"},
    }
    # Custom status_map: 'ready' maps to 'Backlog' instead of 'Ready'
    # The normalizer should still return a ReadyEvent since we're checking
    # if the field changed to the configured ready status
    result = normalize("github", payload, status_map={"ready": "Backlog"})
    assert result is not None
    assert result.issue_number == 99


# ── GitLab provider ──────────────────────────────────────────────────────────


def test_gitlab_ready_event():
    """GitLab issue event with Ready label added returns ReadyEvent."""
    payload = {
        "object_kind": "issue",
        "object_attributes": {
            "iid": 123,
            "title": "Some issue",
        },
        "project": {
            "path_with_namespace": "namespace/project",
        },
        "changes": {
            "labels": {
                "current": [
                    {"title": "bug"},
                    {"title": "Ready"},
                ]
            }
        },
    }
    result = normalize("gitlab", payload)
    assert result is not None
    assert isinstance(result, ReadyEvent)
    assert result.provider == "gitlab"
    assert result.repo == "namespace/project"
    assert result.issue_number == 123
    assert result.board_slug == "namespace-project"


def test_gitlab_ignores_non_ready_labels():
    """GitLab issue event with other labels (no Ready) returns None."""
    payload = {
        "object_kind": "issue",
        "object_attributes": {"iid": 456},
        "project": {"path_with_namespace": "ns/proj"},
        "changes": {
            "labels": {"current": [{"title": "bug"}, {"title": "high-priority"}]}
        },
    }
    result = normalize("gitlab", payload)
    assert result is None


def test_gitlab_ignores_non_issue_events():
    """GitLab merge_request events return None."""
    payload = {
        "object_kind": "merge_request",
        "object_attributes": {"iid": 789},
        "project": {"path_with_namespace": "ns/proj"},
        "changes": {"labels": {"current": [{"title": "Ready"}]}},
    }
    result = normalize("gitlab", payload)
    assert result is None


def test_gitlab_custom_ready_label():
    """GitLab normalizer respects custom ready status name."""
    payload = {
        "object_kind": "issue",
        "object_attributes": {"iid": 55},
        "project": {"path_with_namespace": "org/repo"},
        "changes": {"labels": {"current": [{"title": "To Do"}]}},
    }
    result = normalize("gitlab", payload, status_map={"ready": "To Do"})
    assert result is not None
    assert result.issue_number == 55


# ── Azure DevOps provider ───────────────────────────────────────────────────


def test_azure_ready_event():
    """Azure DevOps workitem.updated with BoardColumn=Ready returns ReadyEvent."""
    payload = {
        "id": "evt-abc",
        "resource": {
            "workItemId": 1001,
            "fields": {
                "System.BoardColumn": {
                    "oldValue": "To Do",
                    "newValue": "Ready",
                }
            },
        },
        "resourceContainers": {
            "project": {"id": "proj-123"},
        },
    }
    result = normalize("azure", payload)
    assert result is not None
    assert isinstance(result, ReadyEvent)
    assert result.provider == "azure"
    assert result.issue_number == 1001
    assert result.board_slug == "proj-123"


def test_azure_ignores_non_ready_columns():
    """Azure DevOps with BoardColumn changed to 'In Progress' returns None."""
    payload = {
        "resource": {
            "workItemId": 1002,
            "fields": {
                "System.BoardColumn": {
                    "oldValue": "Ready",
                    "newValue": "In Progress",
                }
            },
        },
        "resourceContainers": {"project": {"id": "proj-456"}},
    }
    result = normalize("azure", payload)
    assert result is None


def test_azure_ignores_non_board_field_changes():
    """Azure DevOps changes to non-BoardColumn fields return None."""
    payload = {
        "resource": {
            "workItemId": 1003,
            "fields": {
                "System.Title": {
                    "oldValue": "Old Title",
                    "newValue": "New Title",
                }
            },
        },
        "resourceContainers": {"project": {"id": "proj-789"}},
    }
    result = normalize("azure", payload)
    assert result is None


# ── Hermes Kanban provider ───────────────────────────────────────────────────


def test_hermes_ready_event():
    """Hermes kanban status_changed with new_status=ready returns ReadyEvent."""
    payload = {
        "new_status": "ready",
        "task_id": "t_12345",
        "board_slug": "default",
    }
    result = normalize("hermes", payload)
    assert result is not None
    assert isinstance(result, ReadyEvent)
    assert result.provider == "hermes"
    assert result.issue_number == 12345
    assert result.board_slug == "default"


def test_hermes_ignores_non_ready_statuses():
    """Hermes kanban with new_status != 'ready' returns None."""
    payload = {
        "new_status": "in_progress",
        "task_id": "t_67890",
        "board_slug": "default",
    }
    result = normalize("hermes", payload)
    assert result is None


def test_hermes_task_id_without_underscore():
    """Hermes kanban with task_id that has no underscore parses as integer."""
    payload = {
        "new_status": "ready",
        "task_id": "99999",
        "board_slug": "proj-board",
    }
    result = normalize("hermes", payload)
    assert result is not None
    assert result.issue_number == 99999


# ── Edge cases and error handling ────────────────────────────────────────────


def test_empty_payload_raises():
    """Empty payload raises ValueError."""
    with pytest.raises(ValueError, match="Payload must be a non-empty dict"):
        normalize("github", {})


def test_non_dict_payload_raises():
    """Non-dict payload raises ValueError."""
    with pytest.raises(ValueError, match="Payload must be a non-empty dict"):
        normalize("github", "not a dict")


def test_unknown_provider_raises():
    """Unknown provider raises ValueError."""
    payload = {"some": "data"}
    with pytest.raises(ValueError, match="Unknown provider: bitbucket"):
        normalize("bitbucket", payload)


def test_github_missing_issue_number_returns_none():
    """GitHub payload without issue number returns None (can't dispatch)."""
    payload = {
        "action": "edited",
        "projects_v2_item": {
            "node_id": "PVTI_abc",
            "__typename": "Issue",
            "project_node_id": "PVT_xyz",
        },
        "changes": {"field_value": {"field_name": "Status"}},
        "repository": {"full_name": "owner/repo"},
    }
    result = normalize("github", payload)
    assert result is None


def test_github_organization_fallback_repo():
    """GitHub payload with organization but no repository returns org/unknown repo."""
    payload = {
        "action": "edited",
        "projects_v2_item": {
            "number": 77,
            "project_node_id": "PVT_abc",
        },
        "changes": {"field_value": {"field_name": "Status"}},
        "organization": {"login": "myorg"},
    }
    result = normalize("github", payload)
    assert result is not None
    assert result.repo == "myorg/unknown"


def test_gitlab_missing_iid_returns_none():
    """GitLab payload without iid returns None."""
    payload = {
        "object_kind": "issue",
        "object_attributes": {"title": "No IID"},
        "project": {"path_with_namespace": "ns/proj"},
        "changes": {"labels": {"current": [{"title": "Ready"}]}},
    }
    result = normalize("gitlab", payload)
    assert result is None


def test_azure_missing_workitemid_returns_none():
    """Azure payload without workItemId returns None."""
    payload = {
        "resource": {"fields": {"System.BoardColumn": {"newValue": "Ready"}}},
        "resourceContainers": {"project": {"id": "proj"}},
    }
    result = normalize("azure", payload)
    assert result is None


def test_hermes_missing_task_id_returns_none():
    """Hermes payload without task_id returns None."""
    payload = {"new_status": "ready", "board_slug": "default"}
    result = normalize("hermes", payload)
    assert result is None

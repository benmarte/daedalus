"""Tests for core/webhook_normalizer.py — VCS-agnostic webhook payload normalization.

Exercises all four provider parsers (GitHub, GitLab, Azure DevOps, Hermes Kanban)
plus edge cases: malformed payloads, unknown providers, PRs disguised as issues,
and status_map customization.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path BEFORE importing core.webhook_normalizer
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.webhook_normalizer import normalize, ReadyEvent, verify_signature  # noqa: E402

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
            "field_value": {
                "field_name": "Status",
                "name": "Ready",
            },
        },
        "changes": {
            "field_value": {
                "field_name": "Status",
                "from": "e123",  # option id of previous value
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
            "field_value": {
                "field_name": "Status",
                "name": "Ready",
            },
        },
        "changes": {"field_value": {"field_name": "Status", "from": "e100"}},
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


def test_github_ignores_non_ready_status_changes():
    """GitHub payload with Status changed to 'In Progress' (not 'Ready') returns None."""
    payload = {
        "action": "edited",
        "projects_v2_item": {
            "node_id": "PVTI_abc123",
            "__typename": "Issue",
            "number": 42,
            "field_value": {
                "field_name": "Status",
                "name": "In Progress",
            },
        },
        "changes": {"field_value": {"field_name": "Status", "from": "e100"}},
        "repository": {"full_name": "owner/repo"},
    }
    result = normalize("github", payload)
    assert result is None


def test_github_custom_status_map_positive():
    """GitHub normalizer returns ReadyEvent when new status matches custom status_map."""
    payload = {
        "action": "edited",
        "projects_v2_item": {
            "number": 99,
            "project_node_id": "PVT_xyz",
            "field_value": {
                "field_name": "Status",
                "name": "Backlog",  # matches custom map
            },
        },
        "changes": {"field_value": {"field_name": "Status", "from": "e200"}},
        "repository": {"full_name": "org/project"},
    }
    # Custom status_map: 'ready' maps to 'Backlog' instead of 'Ready'
    result = normalize("github", payload, status_map={"ready": "Backlog"})
    assert result is not None
    assert result.issue_number == 99


def test_github_custom_status_map_negative():
    """GitHub normalizer returns None when new status doesn't match custom status_map."""
    payload = {
        "action": "edited",
        "projects_v2_item": {
            "number": 100,
            "project_node_id": "PVT_xyz",
            "field_value": {
                "field_name": "Status",
                "name": "In Progress",  # doesn't match custom map 'Backlog'
            },
        },
        "changes": {"field_value": {"field_name": "Status", "from": "e300"}},
        "repository": {"full_name": "org/project"},
    }
    result = normalize("github", payload, status_map={"ready": "Backlog"})
    assert result is None


def test_github_field_values_array_fallback():
    """GitHub normalizer falls back to field_values array if field_value is absent."""
    payload = {
        "action": "edited",
        "projects_v2_item": {
            "number": 55,
            "project_node_id": "PVT_fv",
            "field_values": [
                {"field_name": "Priority", "name": "High"},
                {"field_name": "Status", "name": "Ready"},
            ],
        },
        "changes": {"field_value": {"field_name": "Status", "from": "e400"}},
        "repository": {"full_name": "owner/repo"},
    }
    result = normalize("github", payload)
    assert result is not None
    assert result.issue_number == 55


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
    # Default status_map maps canonical 'ready' → 'Ready' (capitalized).
    payload = {
        "new_status": "Ready",
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
    """Hermes kanban with new_status != 'Ready' returns None."""
    payload = {
        "new_status": "In Progress",
        "task_id": "t_67890",
        "board_slug": "default",
    }
    result = normalize("hermes", payload)
    assert result is None


def test_hermes_task_id_without_underscore():
    """Hermes kanban with task_id that has no underscore parses as integer."""
    payload = {
        "new_status": "Ready",
        "task_id": "99999",
        "board_slug": "proj-board",
    }
    result = normalize("hermes", payload)
    assert result is not None
    assert result.issue_number == 99999


def test_hermes_custom_ready_status():
    """Hermes normalizer respects custom ready_status from status_map."""
    payload = {
        "new_status": "todo",
        "task_id": "t_55555",
        "board_slug": "default",
    }
    # Custom status_map: 'ready' maps to 'todo' instead of 'Ready'
    result = normalize("hermes", payload, status_map={"ready": "todo"})
    assert result is not None
    assert result.issue_number == 55555
    assert result.provider == "hermes"


def test_hermes_custom_ready_status_negative():
    """Hermes normalizer returns None when new_status doesn't match custom ready_status."""
    payload = {
        "new_status": "Ready",
        "task_id": "t_66666",
        "board_slug": "default",
    }
    # Custom status_map: 'ready' maps to 'todo', but payload has 'Ready'
    result = normalize("hermes", payload, status_map={"ready": "todo"})
    assert result is None


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
            "field_value": {
                "field_name": "Status",
                "name": "Ready",
            },
        },
        "changes": {"field_value": {"field_name": "Status", "from": "e500"}},
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
            "field_value": {
                "field_name": "Status",
                "name": "Ready",
            },
        },
        "changes": {"field_value": {"field_name": "Status", "from": "e600"}},
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
    payload = {"new_status": "Ready", "board_slug": "default"}
    result = normalize("hermes", payload)
    assert result is None


def test_hermes_custom_status_map():
    """Hermes normalizer uses status_map instead of hard-coded 'ready'."""
    # Custom status_map: 'ready' maps to 'Triaged'
    payload = {
        "new_status": "Triaged",
        "task_id": "t_55555",
        "board_slug": "default",
    }
    result = normalize("hermes", payload, status_map={"ready": "Triaged"})
    assert result is not None
    assert isinstance(result, ReadyEvent)
    assert result.provider == "hermes"
    assert result.issue_number == 55555
    assert result.board_slug == "default"


def test_hermes_custom_status_map_negative():
    """Hermes normalizer returns None when new_status doesn't match custom map."""
    payload = {
        "new_status": "ready",  # default canonical name, not 'Triaged'
        "task_id": "t_66666",
        "board_slug": "default",
    }
    result = normalize("hermes", payload, status_map={"ready": "Triaged"})
    assert result is None


# ── HMAC signature verification ──────────────────────────────────────────────


import hmac
import hashlib
import os  # noqa: E402


def _hmac_sha256(payload_bytes: bytes, secret: str) -> str:
    """Helper: compute HMAC-SHA256 hex digest."""
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


# -- GitHub (X-Hub-Signature-256) --


def test_verify_github_valid_signature():
    """GitHub HMAC-SHA256 signature with valid header returns True."""
    secret = "test-secret"
    payload_bytes = b'{"action":"edited","projects_v2_item":{}}'
    digest = _hmac_sha256(payload_bytes, secret)
    headers = {"X-Hub-Signature-256": f"sha256={digest}"}
    assert verify_signature("github", payload_bytes, headers, secret) is True


def test_verify_github_invalid_signature():
    """GitHub HMAC-SHA256 signature with mismatched header returns False (no crash)."""
    secret = "test-secret"
    payload_bytes = b'{"action":"edited"}'
    headers = {"X-Hub-Signature-256": "sha256=" + "0" * 64}  # wrong digest
    assert verify_signature("github", payload_bytes, headers, secret) is False


def test_verify_github_missing_signature_header():
    """GitHub webhook without X-Hub-Signature-256 header returns False (logged)."""
    payload_bytes = b'{"test": true}'
    headers = {}  # no signature header
    assert verify_signature("github", payload_bytes, headers, "secret") is False


def test_verify_github_malformed_signature_header():
    """GitHub webhook with malformed X-Hub-Signature-256 (no sha256= prefix) returns False."""
    payload_bytes = b'{"test": true}'
    headers = {"X-Hub-Signature-256": "not-a-valid-sig"}  # no sha256= prefix
    assert verify_signature("github", payload_bytes, headers, "secret") is False


# -- GitLab (X-Gitlab-Token) --


def test_verify_gitlab_valid_token():
    """GitLab webhook with matching X-Gitlab-Token returns True."""
    secret = "gitlab-secret"
    payload_bytes = b'{"object_kind":"issue"}'
    headers = {"X-Gitlab-Token": secret}
    assert verify_signature("gitlab", payload_bytes, headers, secret) is True


def test_verify_gitlab_invalid_token():
    """GitLab webhook with mismatched X-Gitlab-Token returns False."""
    payload_bytes = b'{"object_kind":"issue"}'
    headers = {"X-Gitlab-Token": "wrong-token"}
    assert verify_signature("gitlab", payload_bytes, headers, "real-secret") is False


def test_verify_gitlab_missing_token():
    """GitLab webhook without X-Gitlab-Token header returns False."""
    payload_bytes = b'{"object_kind":"issue"}'
    headers = {}
    assert verify_signature("gitlab", payload_bytes, headers, "secret") is False


# -- Edge cases --


def test_verify_unknown_provider_returns_false():
    """Unknown provider returns False (logged, no crash)."""
    assert verify_signature("bitbucket", b'{}', {}, "secret") is False


def test_verify_empty_secret_returns_false():
    """Empty secret returns False for any provider (logged)."""
    assert verify_signature("github", b'{}', {"X-Hub-Signature-256": "sha256=abc"}, "") is False


def test_verify_none_headers_handled():
    """None headers dict doesn't crash — returns False."""
    assert verify_signature("github", b'{}', None, "secret") is False


def test_verify_case_insensitive_provider():
    """Provider name is case-insensitive for signature verification."""
    secret = "secret"
    payload_bytes = b'{"test": true}'
    digest = _hmac_sha256(payload_bytes, secret)
    # GitHub with uppercase should still work
    assert verify_signature("GitHub", payload_bytes, {"X-Hub-Signature-256": f"sha256={digest}"}, secret) is True

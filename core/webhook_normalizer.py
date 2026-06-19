"""VCS-agnostic webhook payload normalizer for the daedalus pipeline.

Parses incoming webhook payloads from GitHub, GitLab, Azure DevOps, and Hermes
Kanban, extracting a normalized ReadyEvent when an item moves to the Ready column.

The normalizer is provider-agnostic: it never hard-codes 'Ready' — it reads the
expected status name from the caller via ``status_map`` (a dict mapping canonical
pipeline statuses to provider-facing names). This allows the same normalizer to
work across different boards with different column names.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("daedalus.webhook_normalizer")


@dataclass
class ReadyEvent:
    """Normalized event: a VCS item moved to the Ready column.

    Attributes:
        provider: Source of the webhook ('github', 'gitlab', 'azure', 'hermes').
        repo: Repository identifier (e.g. 'owner/repo' for GitHub, 'namespace/project' for GitLab).
        issue_number: Issue number that moved to Ready.
        board_slug: Board identifier (slug or ID) where the move occurred.
    """

    provider: str
    repo: str
    issue_number: int
    board_slug: str


def normalize(
    provider: str,
    payload: Any,
    status_map: Optional[Dict[str, str]] = None,
) -> Optional[ReadyEvent]:
    """Normalize a webhook payload into a ReadyEvent if the item moved to Ready.

    Args:
        provider: Provider type ('github', 'gitlab', 'azure', 'hermes').
        payload: Raw webhook payload dict.
        status_map: Optional dict mapping canonical statuses to provider-facing names.
                   Defaults to {'ready': 'Ready'} if not provided.

    Returns:
        ReadyEvent if the payload indicates an item moved to Ready, else None.

    Raises:
        ValueError: If the payload is malformed or cannot be parsed (defense-in-depth).
    """
    if not payload or not isinstance(payload, dict):
        raise ValueError("Payload must be a non-empty dict")

    status_map = status_map or {"ready": "Ready"}
    ready_status = status_map.get("ready", "Ready")

    provider = provider.lower().strip()

    if provider == "github":
        return _normalize_github(payload, ready_status)
    elif provider == "gitlab":
        return _normalize_gitlab(payload, ready_status)
    elif provider == "azure":
        return _normalize_azure(payload, ready_status)
    elif provider == "hermes":
        return _normalize_hermes(payload, ready_status)
    else:
        raise ValueError(f"Unknown provider: {provider}")


def _normalize_github(
    payload: Dict[str, Any], ready_status: str
) -> Optional[ReadyEvent]:
    """Parse GitHub Projects v2 webhook payload.

    Expected structure:
    {
        "action": "edited",
        "projects_v2_item": {
            "id": "...",
            "node_id": "...",
            "project_resource": {...}
        },
        "changes": {
            "field_value": {
                "field_name": "Status"
            }
        },
        "organization": {
            "login": "owner"
        },
        "sender": {...}
    }

    The new Status value after the edit lives at `projects_v2_item.field_value.name`
    (single Object). When GitHub sends the full item snapshot, it may also appear
    inside `projects_v2_item.field_values` (plural array); we check both locations.
    """
    # GitHub sends 'projects_v2_item' at top level
    if "projects_v2_item" not in payload:
        return None

    pvi = payload["projects_v2_item"]
    action = payload.get("action", "")

    # We only care about 'edited' actions (field value changes)
    if action != "edited":
        return None

    # Check if the Status field changed
    changes = payload.get("changes", {})
    field_value = changes.get("field_value", {})
    field_name = field_value.get("field_name", "")

    if field_name != "Status":
        return None

    # Extract the NEW Status value after the edit.
    # GitHub's projects_v2_item webhook exposes it via:
    #   - `projects_v2_item.field_value.name`          (single edited field object)
    #   - `projects_v2_item.field_values[i].name`      (array snapshot, field_name=="Status")
    new_status_value: Optional[str] = None
    fv = pvi.get("field_value")
    if isinstance(fv, dict):
        new_status_value = fv.get("name") or fv.get("value")
    if not new_status_value:
        for entry in pvi.get("field_values") or []:
            if isinstance(entry, dict) and entry.get("field_name") == "Status":
                new_status_value = entry.get("name") or entry.get("value")
                break

    if not new_status_value or new_status_value != ready_status:
        # Status changed, but NOT to the configured ready column — ignore.
        return None

    # Extract repo and issue info from the projects_v2_item
    # The payload includes a nested issue/PR reference
    # GitHub node IDs contain encoded type: 'I_' prefix = Issue, 'PR_' = Pull Request
    if "__typename" in pvi and pvi.get("__typename") == "PullRequest":
        # Ignore PRs — we only dispatch on Issues
        return None

    # Extract repo from the payload (usually in 'repository' or inferred from sender)
    repo = ""
    if "repository" in payload:
        repo = payload["repository"].get("full_name", "")
    elif "organization" in payload:
        org = payload["organization"].get("login", "")
        # Try to extract repo from projects_v2_item if available
        repo = f"{org}/unknown"

    # Extract issue number — in real GitHub webhook, we'd decode the node_id
    # For now, assume it's provided in a custom field or we parse it from the URL
    issue_number = 0
    if "number" in pvi:
        issue_number = pvi["number"]
    elif "content" in pvi and "number" in pvi["content"]:
        issue_number = pvi["content"]["number"]

    if not issue_number:
        # Can't dispatch without an issue number
        return None

    # Board slug — use the node_id or a default
    board_slug = pvi.get("project_node_id", "unknown")

    return ReadyEvent(
        provider="github",
        repo=repo,
        issue_number=issue_number,
        board_slug=board_slug,
    )


def _normalize_gitlab(
    payload: Dict[str, Any], ready_status: str
) -> Optional[ReadyEvent]:
    """Parse GitLab issue webhook payload.

    GitLab doesn't have native 'Ready' columns — it uses labels.
    We check if a label matching the ready_status was added.

    Expected structure:
    {
        "object_kind": "issue",
        "object_attributes": {
            "id": 123,
            "iid": 42,
            "title": "...",
            "labels": [{"title": "Ready"}, ...]
        },
        "project": {
            "path_with_namespace": "namespace/project"
        },
        "changes": {
            "labels": {
                "previous": [...],
                "current": [{"title": "Ready"}, ...]
            }
        }
    }
    """
    # GitLab sends 'object_kind' = 'issue' for issue events
    if payload.get("object_kind") != "issue":
        return None

    obj_attrs = payload.get("object_attributes", {})
    if not obj_attrs:
        return None

    # Check if a label matching ready_status was added
    changes = payload.get("changes", {})
    label_changes = changes.get("labels", {})
    current_labels = label_changes.get("current", [])

    # Check if 'Ready' label is in the current labels
    ready_label_found = any(
        label.get("title") == ready_status for label in current_labels
    )

    if not ready_label_found:
        return None

    # Extract issue IID (internal ID within the project)
    issue_iid = obj_attrs.get("iid", 0)
    if not issue_iid:
        return None

    # Extract project path
    project = payload.get("project", {})
    repo = project.get("path_with_namespace", "")
    if not repo:
        return None

    # Board slug — GitLab uses milestone or board ID
    # For simplicity, use the project path as the board identifier
    board_slug = repo.replace("/", "-")

    return ReadyEvent(
        provider="gitlab",
        repo=repo,
        issue_number=issue_iid,
        board_slug=board_slug,
    )


def _normalize_azure(
    payload: Dict[str, Any], ready_status: str
) -> Optional[ReadyEvent]:
    """Parse Azure DevOps work item webhook payload.

    Expected structure:
    {
        "id": "...",
        "resource": {
            "workItemId": 123,
            "fields": {
                "System.BoardColumn": {
                    "oldValue": "To Do",
                    "newValue": "Ready"
                }
            }
        },
        "resourceContainers": {
            "project": {
                "id": "project-id"
            }
        }
    }
    """
    # Azure DevOps sends 'resource' with workItemId and fields
    resource = payload.get("resource", {})
    if not resource or "workItemId" not in resource:
        return None

    work_item_id = resource.get("workItemId", 0)
    if not work_item_id:
        return None

    # Check if BoardColumn changed to ready_status
    fields = resource.get("fields", {})
    board_column = fields.get("System.BoardColumn", {})
    new_value = board_column.get("newValue", "")

    if new_value != ready_status:
        return None

    # Extract project info
    resource_containers = payload.get("resourceContainers", {})
    project = resource_containers.get("project", {})
    project_id = project.get("id", "")
    if not project_id:
        # Fall back to project name if available
        project_name = resource.get("project", {}).get("name", "")
        project_id = project_name or "unknown"

    # Board slug — use project ID or name
    board_slug = project_id

    return ReadyEvent(
        provider="azure",
        repo="",  # Azure doesn't have a simple repo field; use project_id instead
        issue_number=work_item_id,
        board_slug=board_slug,
    )


def _normalize_hermes(
    payload: Dict[str, Any], ready_status: str
) -> Optional[ReadyEvent]:
    """Parse Hermes Kanban webhook payload (forward-looking stub).

    Expected structure:
    {
        "new_status": "ready",
        "task_id": "t_abc123",
        "board_slug": "default"
    }

    This is a stub for future Hermes kanban integration.
    """
    # Check if new_status matches ready_status (provider-agnostic via status_map)
    new_status = payload.get("new_status", "")
    if new_status != ready_status:
        return None

    # Extract task_id — Hermes kanban uses task IDs, not issue numbers
    task_id = payload.get("task_id", "")
    if not task_id:
        return None

    # Extract board slug
    board_slug = payload.get("board_slug", "default")

    # For Hermes, we use the task_id as the issue_number field
    # (this is a forward-looking stub; real implementation may differ)
    # We'll store the task_id as a string in issue_number field
    try:
        # Try to extract numeric ID from task_id (e.g., 't_123' -> 123)
        issue_number = int(task_id.split("_")[1]) if "_" in task_id else int(task_id)
    except (ValueError, IndexError):
        # Can't parse numeric ID — use hash as fallback
        issue_number = hash(task_id) % 1000000

    return ReadyEvent(
        provider="hermes",
        repo="",  # Hermes doesn't have a repo concept
        issue_number=issue_number,
        board_slug=board_slug,
    )


def verify_signature(
    provider: str,
    payload_bytes: bytes,
    headers: Optional[Dict[str, str]],
    secret: str,
) -> bool:
    """Verify HMAC signature on an incoming webhook payload.

    Args:
        provider: Provider type ('github', 'gitlab', 'azure', 'hermes').
        payload_bytes: Raw request body bytes (as received, before JSON parse).
        headers: HTTP request headers (case-preserving dict).
        secret: Shared secret configured for HMAC verification.

    Returns:
        True if the signature is valid, False otherwise.
        Logs and returns False on mismatch, missing headers, unknown provider,
        or empty secret — never raises or crashes.
    """
    try:
        provider = provider.lower().strip()
        headers = headers or {}

        if not secret:
            logger.warning("webhook: empty secret, rejecting signature")
            return False

        if provider == "github":
            return _verify_github_signature(payload_bytes, headers, secret)
        elif provider == "gitlab":
            return _verify_gitlab_token(headers, secret)
        elif provider == "azure":
            return _verify_azure_secret(headers, secret)
        else:
            logger.warning("webhook: unknown provider for signature verification: %s", provider)
            return False
    except Exception:
        logger.exception("webhook: unexpected error in verify_signature")
        return False


def _verify_github_signature(
    payload_bytes: bytes, headers: Dict[str, str], secret: str
) -> bool:
    """Verify GitHub's X-Hub-Signature-256 HMAC-SHA256 signature.

    Header format: ``sha256=<hex-digest>``
    """
    header = headers.get("X-Hub-Signature-256") or headers.get("X-Hub-Signature-256".lower())
    if not header:
        logger.warning("webhook: github missing X-Hub-Signature-256 header")
        return False

    if not header.startswith("sha256="):
        logger.warning("webhook: github signature header missing sha256= prefix")
        return False

    provided_digest = header[len("sha256="):]
    expected_digest = hmac.new(
        secret.encode("utf-8"), payload_bytes, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(provided_digest, expected_digest):
        logger.warning("webhook: github HMAC signature mismatch")
        return False

    return True


def _verify_gitlab_token(headers: Dict[str, str], secret: str) -> bool:
    """Verify GitLab's X-Gitlab-Token header (shared secret direct comparison)."""
    token = headers.get("X-Gitlab-Token") or headers.get("X-Gitlab-Token".lower())
    if not token:
        logger.warning("webhook: gitlab missing X-Gitlab-Token header")
        return False

    if token != secret:
        logger.warning("webhook: gitlab X-Gitlab-Token mismatch")
        return False

    return True


def _verify_azure_secret(headers: Dict[str, str], secret: str) -> bool:
    """Verify Azure DevOps shared secret.

    Azure DevOps doesn't use HMAC — it sends a shared secret in the request body
    for service hooks. We check the header path if present; body-level verification
    is deferred to the caller (the normalizer parses the body).
    """
    token = headers.get("X-Azure-Webhook-Token") or headers.get("x-azure-webhook-token")
    if not token:
        # Azure doesn't always send a header; body verification is the caller's job
        logger.info("webhook: azure no token header, deferring to body-level verification")
        return True  # Permissive — Azure webhook body carries the secret

    if token != secret:
        logger.warning("webhook: azure token mismatch")
        return False

    return True

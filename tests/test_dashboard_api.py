"""
Pytest for the Daedalus dashboard plugin API.

Projects come exclusively from the registry; each repo carries its own
.hermes/daedalus.yaml. Tests GET /projects, the per-project config API,
project creation, cron reconcile, and the meta endpoints — with mocked
kanban, providers, and registry.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
from pathlib import Path
from unittest import mock

import pytest
import yaml
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

# Ensure the daedalus package root is importable
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from dashboard.plugin_api import router


@pytest.fixture
def registry_repo(tmp_path):
    """Temp repo with .hermes/daedalus.yaml, registered via a mocked registry."""
    repo = tmp_path / "test-repo"
    (repo / ".hermes").mkdir(parents=True)
    cfg = {
        "name": "test-project",
        "repo": "org/test-repo",
        "workdir": str(repo),
        "vcs": {"target_branch": "dev"},
        "tracking": {"github_project_number": 1},
        "execution": {"worker_profile": "developer-daedalus"},
        "cron": {"schedule": "60m", "deliver": "slack:#engineering"},
        "sources": {"github": {"enabled": True}, "local_specs": {"enabled": False}},
    }
    (repo / ".hermes" / "daedalus.yaml").write_text(yaml.dump(cfg))
    with mock.patch("dashboard.plugin_api.registry") as mock_registry:
        mock_registry.list_projects.return_value = [str(repo)]
        yield repo


@pytest.fixture
def client(registry_repo, monkeypatch):
    """FastAPI TestClient with the full daedalus router mounted.

    Auth bypass is enabled so functional tests (not testing auth) can reach
    the endpoints. Auth tests explicitly control the env vars themselves.
    """
    monkeypatch.setenv("DAEDALUS_DASHBOARD_AUTH_DISABLED", "1")
    app = FastAPI()
    app.include_router(router, prefix="/api/plugins/daedalus")
    return TestClient(app)


# ── GET /projects tests ──────────────────────────────────────────────────────


def _make_kanban_tasks(statuses: list[str]) -> list[dict]:
    """Build mock kanban task dicts with the given statuses."""
    return [
        {"id": f"t_{i:08x}", "title": f"Task {i}", "status": s,
         "summary": f"summary for task {i}", "result": f"result for task {i}"}
        for i, s in enumerate(statuses)
    ]


def test_get_projects_returns_one_entry_per_registered_repo(client, registry_repo):
    """GET /projects returns one entry for each repo in the registry."""
    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = _make_kanban_tasks(["todo", "todo", "in_progress"])

        resp = client.get("/api/plugins/daedalus/projects")
        assert resp.status_code == 200, resp.text
        data = resp.json()

    assert len(data) == 1
    proj = data[0]
    assert proj["name"] == "test-project"
    assert proj["repo"] == "org/test-repo"
    assert proj["workdir"] == str(registry_repo)


def test_get_projects_has_all_required_fields(client):
    """GET /projects entries contain all required fields."""
    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = _make_kanban_tasks(["done", "todo"])

        resp = client.get("/api/plugins/daedalus/projects")
        assert resp.status_code == 200, resp.text
        data = resp.json()

    proj = data[0]
    required_fields = {
        "name", "repo", "workdir", "kanban_summary",
        "open_prs", "cron", "needs_attention",
        "tracking_mode", "sources",
    }
    for field in required_fields:
        assert field in proj, f"Missing field: {field}"


def test_get_projects_kanban_summary_counts_by_status(client):
    """GET /projects kanban_summary has correct counts by status."""
    tasks = _make_kanban_tasks(["todo", "todo", "in_progress", "done", "done", "done"])
    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = tasks

        resp = client.get("/api/plugins/daedalus/projects")
        assert resp.status_code == 200, resp.text
        data = resp.json()

    summary = data[0]["kanban_summary"]
    assert summary == {"todo": 2, "in_progress": 1, "done": 3}


def test_get_projects_kanban_summary_empty_on_empty(client):
    """kanban_summary is {} (not None) when list_tasks returns [] — board exists, 0 tasks."""
    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []

        resp = client.get("/api/plugins/daedalus/projects")
        assert resp.status_code == 200, resp.text
        data = resp.json()

    assert data[0]["kanban_summary"] == {}


def test_get_projects_needs_attention_blocked_and_gave_up(client):
    """needs_attention includes blocked and gave_up cards with ids and reasons."""
    blocked = [
        {"id": "t_block1", "title": "Blocked task", "status": "blocked",
         "summary": "review-required: needs eyes on SQL", "result": ""},
    ]
    gave_up = [
        {"id": "t_gave1", "title": "Gave up task", "status": "gave_up",
         "summary": "", "result": "CRASHED: OOM during build"},
    ]

    def mock_list_tasks(slug, status=""):
        # New implementation calls list_tasks(slug) once (no filter) and filters in Python.
        if not status:
            return blocked + gave_up
        if status == "blocked":
            return blocked
        elif status == "gave_up":
            return gave_up
        return []

    with mock.patch("dashboard.plugin_api.list_tasks", side_effect=mock_list_tasks):
        resp = client.get("/api/plugins/daedalus/projects")
        assert resp.status_code == 200, resp.text
        data = resp.json()

    attention = data[0]["needs_attention"]
    assert attention is not None
    assert len(attention) == 2
    ids = {a["task_id"] for a in attention}
    assert ids == {"t_block1", "t_gave1"}

    # Blocked entry has reason from summary
    blocked_entry = next(a for a in attention if a["task_id"] == "t_block1")
    assert "review-required" in blocked_entry.get("reason", "")


def test_get_projects_tracking_mode_github(client):
    """tracking_mode is 'github' when github_project_number is set."""
    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []

        resp = client.get("/api/plugins/daedalus/projects")
        assert resp.status_code == 200, resp.text
        data = resp.json()

    assert data[0]["tracking_mode"] == "github"


def test_get_projects_tracking_mode_kanban_without_board(client, registry_repo):
    """tracking_mode is 'kanban' when no board is configured."""
    cfg_path = registry_repo / ".hermes" / "daedalus.yaml"
    raw = yaml.safe_load(cfg_path.read_text())
    raw["tracking"] = {}
    cfg_path.write_text(yaml.dump(raw))

    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []

        resp = client.get("/api/plugins/daedalus/projects")
        assert resp.status_code == 200, resp.text
        data = resp.json()

    assert data[0]["tracking_mode"] == "kanban"


def test_get_projects_tracking_mode_per_provider(client, registry_repo):
    """tracking_mode reflects the provider when its board model is on."""
    cfg_path = registry_repo / ".hermes" / "daedalus.yaml"
    raw = yaml.safe_load(cfg_path.read_text())
    raw["vcs"]["provider"] = "gitlab"
    raw["tracking"] = {"label_board": True}
    cfg_path.write_text(yaml.dump(raw))

    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []
        resp = client.get("/api/plugins/daedalus/projects")
        data = resp.json()
    assert data[0]["tracking_mode"] == "gitlab"

    raw["vcs"] = {"provider": "azuredevops", "org": "a", "project": "p", "repo": "r"}
    raw["tracking"] = {}
    cfg_path.write_text(yaml.dump(raw))
    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []
        resp = client.get("/api/plugins/daedalus/projects")
        data = resp.json()
    assert data[0]["tracking_mode"] == "azuredevops"


def test_get_projects_cron_info(client):
    """cron field has schedule and delivery info."""
    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []

        resp = client.get("/api/plugins/daedalus/projects")
        assert resp.status_code == 200, resp.text
        data = resp.json()

    cron = data[0]["cron"]
    assert cron is not None
    assert cron["schedule"] == "60m"
    assert cron["deliver"] == "slack:#engineering"


def test_get_projects_sources_stripped(client):
    """sources has enabled flags, stripped of secrets."""
    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []

        resp = client.get("/api/plugins/daedalus/projects")
        assert resp.status_code == 200, resp.text
        data = resp.json()

    sources = data[0]["sources"]
    assert sources is not None
    assert sources["github"]["enabled"] is True
    assert sources["local_specs"]["enabled"] is False
    # No secret keys should leak
    assert "secret" not in json.dumps(sources)


def test_get_projects_degrade_gracefully_no_kanban(client):
    """When list_tasks is None (import failure), all fields are nulls not errors."""
    with mock.patch("dashboard.plugin_api.list_tasks", None):
        resp = client.get("/api/plugins/daedalus/projects")
        assert resp.status_code == 200, resp.text
        data = resp.json()

    proj = data[0]
    assert proj["kanban_summary"] is None
    assert proj["needs_attention"] is None


def test_get_projects_open_prs_mocked(client):
    """open_prs returns mocked PR data with ci_status field."""
    mock_pr_data = [
        {"number": 42, "title": "Fix auth", "headRefName": "fix/auth", "state": "open"},
        {"number": 43, "title": "Add rate limit", "headRefName": "feat/rate-limit", "state": "open"},
    ]

    fake_provider = mock.MagicMock()
    fake_provider.supports_ci_status = True
    fake_provider.list_prs.return_value = [
        mock.MagicMock(number=p["number"], title=p["title"],
                       head_branch=p["headRefName"]) for p in mock_pr_data
    ]
    # Batch CI-status lookup returns a dict keyed by PR number.
    fake_provider.get_prs_ci_status.return_value = {42: "green", 43: "red"}

    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []
        with mock.patch("dashboard.plugin_api.get_provider",
                        return_value=fake_provider):
            resp = client.get("/api/plugins/daedalus/projects")
            assert resp.status_code == 200, resp.text
            data = resp.json()

    prs = data[0]["open_prs"]
    assert prs is not None
    assert prs["count"] == 2
    assert len(prs["prs"]) == 2
    assert prs["prs"][0]["number"] == 42
    assert prs["prs"][0]["ci_status"] == "green"
    assert prs["prs"][1]["number"] == 43
    assert prs["prs"][1]["ci_status"] == "red"
    # ── batch verification (#1143) ────────────────────────────────────────
    # One batch call — not one per PR.
    fake_provider.get_prs_ci_status.assert_called_once_with([42, 43])
    # The sequential per-PR method must NOT be called on the success path.
    fake_provider.get_pr_ci_status.assert_not_called()


def test_get_projects_open_prs_batch_fallback_to_sequential(client):
    """When the batch CI call raises, _open_prs falls back to per-PR lookup."""
    mock_pr_data = [
        {"number": 10, "title": "PR A", "headRefName": "fix/a", "state": "open"},
        {"number": 11, "title": "PR B", "headRefName": "fix/b", "state": "open"},
    ]

    fake_provider = mock.MagicMock()
    fake_provider.supports_ci_status = True
    fake_provider.list_prs.return_value = [
        mock.MagicMock(number=p["number"], title=p["title"],
                       head_branch=p["headRefName"]) for p in mock_pr_data
    ]
    # Batch call blows up — sequential fallback should take over.
    fake_provider.get_prs_ci_status.side_effect = RuntimeError("graphql 502")
    fake_provider.get_pr_ci_status.side_effect = ["green", "red"]

    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []
        with mock.patch("dashboard.plugin_api.get_provider",
                        return_value=fake_provider):
            resp = client.get("/api/plugins/daedalus/projects")
            assert resp.status_code == 200, resp.text
            data = resp.json()

    prs = data[0]["open_prs"]
    assert prs is not None
    assert prs["count"] == 2
    assert prs["prs"][0]["number"] == 10
    assert prs["prs"][0]["ci_status"] == "green"
    assert prs["prs"][1]["number"] == 11
    assert prs["prs"][1]["ci_status"] == "red"
    # Verify the batch call was attempted exactly once.
    fake_provider.get_prs_ci_status.assert_called_once_with([10, 11])
    # And the sequential fallback was used for both PRs.
    assert fake_provider.get_pr_ci_status.call_count == 2


def test_get_projects_graceful_degradation_when_sources_return_nothing(client):
    """When kanban/provider sources return nothing, the endpoint still returns 200."""
    with mock.patch("dashboard.plugin_api.list_tasks", None):
        with mock.patch("dashboard.plugin_api.get_provider", None):
            resp = client.get("/api/plugins/daedalus/projects")
            assert resp.status_code == 200, resp.text
            data = resp.json()

    assert len(data) >= 1
    proj = data[0]
    assert proj["kanban_summary"] is None
    assert proj["open_prs"] is None
    assert proj["needs_attention"] is None
    # tracking mode still works (from config)
    assert proj["tracking_mode"] in ("github", "kanban")


def test_get_projects_empty_registry(registry_repo, monkeypatch):
    """An empty registry returns an empty list (no global config fallback)."""
    monkeypatch.setenv("DAEDALUS_DASHBOARD_AUTH_DISABLED", "1")
    app = FastAPI()
    app.include_router(router, prefix="/api/plugins/daedalus")
    c = TestClient(app)
    with mock.patch("dashboard.plugin_api.registry") as mock_registry:
        mock_registry.list_projects.return_value = []
        resp = c.get("/api/plugins/daedalus/projects")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_projects_registry_only_entries(client, registry_repo):
    """Registered repos without a config appear as lightweight entries."""
    with mock.patch("dashboard.plugin_api.registry") as mock_registry:
        mock_registry.list_projects.return_value = [
            str(registry_repo),
            "/repos/sampleproj",
        ]
        with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
            mock_list.return_value = []

            resp = client.get("/api/plugins/daedalus/projects")
            assert resp.status_code == 200, resp.text
            data = resp.json()

    # Two entries: one full (config), one lightweight (no config)
    assert len(data) == 2
    registry_entry = next(
        (p for p in data if p["repo"] == "/repos/sampleproj"),
        None,
    )
    assert registry_entry is not None
    assert registry_entry["name"] == "sampleproj"
    assert registry_entry["tracking_mode"] == "kanban"
    assert registry_entry["cron"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# Per-project config API tests — GET/POST /project/{name}/config
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def project_repo_dir():
    """Create a temp repo dir with .hermes/daedalus.yaml for per-project tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        hermes_dir = repo / ".hermes"
        hermes_dir.mkdir()
        cfg = {
            "name": "test-project",
            "repo": "org/test-project",
            "workdir": str(repo),
            "vcs": {"target_branch": "main"},
            "execution": {"worker_profile": "developer-daedalus"},
            "cron": {"schedule": "30m"},
        }
        (hermes_dir / "daedalus.yaml").write_text(yaml.dump(cfg))
        yield repo


@pytest.fixture
def project_client(project_repo_dir, monkeypatch):
    """Create a FastAPI TestClient with the project config router mounted.

    Auth bypass is enabled so functional tests can reach the endpoints.
    """
    monkeypatch.setenv("DAEDALUS_DASHBOARD_AUTH_DISABLED", "1")
    # Mount the project config router from plugin_api
    from dashboard.plugin_api import project_config_router

    app = FastAPI()
    app.include_router(project_config_router, prefix="/api/plugins/daedalus")
    return TestClient(app)


def test_get_project_config_returns_resolved_config(project_client, project_repo_dir):
    """GET /project/{name}/config returns stripped config for a known project."""
    project_name = "test-project"
    with mock.patch("dashboard.plugin_api.registry") as mock_registry:
        mock_registry.list_projects.return_value = [str(project_repo_dir)]

        resp = project_client.get(
            f"/api/plugins/daedalus/project/{project_name}/config"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

    # Should have resolved config fields
    assert data["workdir"] == str(project_repo_dir.resolve())
    assert data["vcs"]["target_branch"] == "main"
    assert data["execution"]["worker_profile"] == "developer-daedalus"


def test_get_project_config_strips_secrets(project_client, project_repo_dir):
    """GET /project/{name}/config never returns secret keys."""
    # Add a secret to the repo config
    hermes_dir = project_repo_dir / ".hermes"
    cfg = yaml.safe_load((hermes_dir / "daedalus.yaml").read_text())
    cfg["webhook"] = {"enabled": True, "secret": "super-secret-value"}
    (hermes_dir / "daedalus.yaml").write_text(yaml.dump(cfg))

    with mock.patch("dashboard.plugin_api.registry") as mock_registry:
        mock_registry.list_projects.return_value = [str(project_repo_dir)]

        resp = project_client.get(
            "/api/plugins/daedalus/project/test-project/config"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

    # Secret must not be present
    if "webhook" in data:
        assert "secret" not in data["webhook"]
    # But enabled should still be there
    if "webhook" in data:
        assert data["webhook"]["enabled"] is True


def test_get_project_config_unknown_project_returns_404(project_client):
    """GET /project/{name}/config returns 404 for an unknown project."""
    with mock.patch("dashboard.plugin_api.registry") as mock_registry:
        mock_registry.list_projects.return_value = []

        resp = project_client.get(
            "/api/plugins/daedalus/project/nonexistent/config"
        )
        assert resp.status_code == 404


def test_post_project_config_persists_editable_fields(project_client, project_repo_dir):
    """POST /project/{name}/config persists editable fields, not read-only ones."""
    # _reconcile_cron must be mocked: the payload carries a cron schedule, so the
    # endpoint would otherwise route through the real _cron_cli → _hermes_cli →
    # `hermes cron create test-project-daedalus`, firing a real cron job on every
    # test run (issue #61). This test only asserts field persistence to YAML, which
    # happens independently of cron reconciliation.
    with mock.patch("dashboard.plugin_api.registry") as mock_registry, \
         mock.patch("dashboard.plugin_api._reconcile_cron",
                    return_value={"cron": "updated"}):
        mock_registry.list_projects.return_value = [str(project_repo_dir)]

        payload = {
            "vcs": {"target_branch": "dev"},
            "cron": {"schedule": "15m"},
            "execution": {"worker_profile": "developer-daedalus"},
        }

        resp = project_client.post(
            "/api/plugins/daedalus/project/test-project/config",
            json=payload,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "saved"

    # Verify the file was updated
    hermes_dir = project_repo_dir / ".hermes"
    saved = yaml.safe_load((hermes_dir / "daedalus.yaml").read_text())
    assert saved["vcs"]["target_branch"] == "dev"
    assert saved["cron"]["schedule"] == "15m"


def test_post_project_config_rejects_repo_change(project_client, project_repo_dir):
    """POST /project/{name}/config rejects attempts to change repo — 422."""
    with mock.patch("dashboard.plugin_api.registry") as mock_registry:
        mock_registry.list_projects.return_value = [str(project_repo_dir)]

        payload = {
            "repo": "evil/hijacked",
            "vcs": {"target_branch": "main"},
        }

        resp = project_client.post(
            "/api/plugins/daedalus/project/test-project/config",
            json=payload,
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "repo" in str(detail).lower() or "read-only" in str(detail).lower()


def test_post_project_config_rejects_workdir_change(project_client, project_repo_dir):
    """POST /project/{name}/config rejects attempts to change workdir — 422."""
    with mock.patch("dashboard.plugin_api.registry") as mock_registry:
        mock_registry.list_projects.return_value = [str(project_repo_dir)]

        payload = {
            "workdir": "/tmp/evil-path",
            "vcs": {"target_branch": "main"},
        }

        resp = project_client.post(
            "/api/plugins/daedalus/project/test-project/config",
            json=payload,
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "workdir" in str(detail).lower() or "read-only" in str(detail).lower()


def test_post_project_config_rejects_name_change(project_client, project_repo_dir):
    """POST /project/{name}/config rejects attempts to change name — 422."""
    with mock.patch("dashboard.plugin_api.registry") as mock_registry:
        mock_registry.list_projects.return_value = [str(project_repo_dir)]

        payload = {
            "name": "evil-rename",
            "vcs": {"target_branch": "main"},
        }

        resp = project_client.post(
            "/api/plugins/daedalus/project/test-project/config",
            json=payload,
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "name" in str(detail).lower() or "read-only" in str(detail).lower()


def test_post_project_config_unknown_project_returns_404(project_client):
    """POST /project/{name}/config returns 404 for an unknown project."""
    with mock.patch("dashboard.plugin_api.registry") as mock_registry:
        mock_registry.list_projects.return_value = []

        payload = {"cron": {"schedule": "15m"}}

        resp = project_client.post(
            "/api/plugins/daedalus/project/nonexistent/config",
            json=payload,
        )
        assert resp.status_code == 404


def test_post_project_config_rejects_invalid_yaml_values(project_client, project_repo_dir):
    """POST /project/{name}/config rejects payloads with invalid field types — 422."""
    with mock.patch("dashboard.plugin_api.registry") as mock_registry:
        mock_registry.list_projects.return_value = [str(project_repo_dir)]

        payload = {"vcs": "not-a-dict"}

        resp = project_client.post(
            "/api/plugins/daedalus/project/test-project/config",
            json=payload,
        )
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# POST /project/create — dashboard add-project flow
# ═══════════════════════════════════════════════════════════════════════════════


class TestCreateProject:
    """POST /project/create scaffolds config + registry + board + cron."""

    def _post(self, client, payload):
        return client.post("/api/plugins/daedalus/project/create", json=payload)

    def _payload(self, workdir):
        return {"name": "new-proj", "repo": "org/new-proj", "workdir": str(workdir)}

    def test_create_success(self, project_client, tmp_path):
        with mock.patch("dashboard.plugin_api.registry") as mock_registry, \
             mock.patch("dashboard.plugin_api.ensure_board", return_value=True) as mock_board, \
             mock.patch("dashboard.plugin_api._reconcile_cron",
                        return_value={"cron": "created", "name": "new-proj-daedalus",
                                      "error": None}) as mock_cron:
            resp = self._post(project_client, {**self._payload(tmp_path),
                                               "cron": {"schedule": "45m"}})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "created"
        assert data["board"] == "org-new-proj"
        assert data["registered"] is True
        assert data["cron"]["cron"] == "created"

        # Config scaffolded on disk with identity + overrides applied
        cfg_path = tmp_path / ".hermes" / "daedalus.yaml"
        assert cfg_path.exists()
        cfg = yaml.safe_load(cfg_path.read_text())
        assert cfg["name"] == "new-proj"
        assert cfg["repo"] == "org/new-proj"
        assert cfg["cron"]["schedule"] == "45m"
        assert cfg["vcs"]["provider"] == "github"  # template default

        mock_registry.add_project.assert_called_once_with(str(tmp_path.resolve()))
        mock_board.assert_called_once_with("org-new-proj")
        mock_cron.assert_called_once()

    def test_create_adopts_existing_config(self, project_client, project_repo_dir):
        """When daedalus.yaml already exists, adopt it (200 + status=adopted) instead of 409."""
        with mock.patch("dashboard.plugin_api.registry") as mock_registry, \
             mock.patch("dashboard.plugin_api.ensure_board", return_value=True), \
             mock.patch("dashboard.plugin_api._reconcile_cron",
                        return_value={"cron": "ok", "name": "x-daedalus", "error": None}):
            resp = self._post(project_client, {"name": "x", "repo": "o/r",
                                               "workdir": str(project_repo_dir)})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "adopted"
        mock_registry.add_project.assert_called_once()

    def test_create_422_missing_fields(self, project_client, tmp_path):
        for missing in ("name", "repo", "workdir"):
            payload = self._payload(tmp_path)
            payload.pop(missing)
            resp = self._post(project_client, payload)
            assert resp.status_code == 422, missing
            assert missing in resp.json()["detail"]

    def test_create_422_relative_or_missing_workdir(self, project_client, tmp_path):
        resp = self._post(project_client, {"name": "x", "repo": "o/r",
                                           "workdir": "relative/path"})
        assert resp.status_code == 422
        resp2 = self._post(project_client, {"name": "x", "repo": "o/r",
                                            "workdir": str(tmp_path / "nope")})
        assert resp2.status_code == 422

    def test_create_validates_provider_config(self, project_client, tmp_path):
        """A gitlab project without project_path/id is rejected before any write."""
        resp = self._post(project_client, {
            "name": "x", "repo": "not-a-path", "workdir": str(tmp_path),
            "vcs": {"provider": "gitlab"},
        })
        assert resp.status_code == 422
        assert not (tmp_path / ".hermes" / "daedalus.yaml").exists()

    def test_create_validates_notifications(self, project_client, tmp_path):
        resp = self._post(project_client, {
            **self._payload(tmp_path),
            "cron": {"schedule": "30m",
                     "notifications": [{"platform": "Slack", "target": ""}]},
        })
        assert resp.status_code == 422
        assert not (tmp_path / ".hermes" / "daedalus.yaml").exists()

    def test_create_auto_detects_provider_and_repo(self, project_client, tmp_path):
        """No repo/provider in the request → both detected from the origin remote."""
        import subprocess
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                        "https://gitlab.corp.io/team/app.git"], check=True)
        with mock.patch("dashboard.plugin_api.registry"), \
             mock.patch("dashboard.plugin_api.ensure_board", return_value=True), \
             mock.patch("dashboard.plugin_api._reconcile_cron",
                        return_value={"cron": "created", "name": "x", "error": None}):
            resp = self._post(project_client,
                              {"name": "auto-proj", "workdir": str(tmp_path)})
        assert resp.status_code == 200, resp.text
        cfg = yaml.safe_load((tmp_path / ".hermes" / "daedalus.yaml").read_text())
        assert cfg["repo"] == "team/app"
        assert cfg["vcs"]["provider"] == "gitlab"
        assert cfg["vcs"]["base_url"] == "https://gitlab.corp.io"

    def test_create_explicit_provider_wins_over_detection(self, project_client, tmp_path):
        """A pinned vcs.provider in the request suppresses auto-detection."""
        import subprocess
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                        "https://gitlab.com/group/proj.git"], check=True)
        with mock.patch("dashboard.plugin_api.registry"), \
             mock.patch("dashboard.plugin_api.ensure_board", return_value=True), \
             mock.patch("dashboard.plugin_api._reconcile_cron",
                        return_value={"cron": "created", "name": "x", "error": None}):
            resp = self._post(project_client, {
                "name": "pinned", "repo": "org/pinned", "workdir": str(tmp_path),
                "vcs": {"provider": "github"},
            })
        assert resp.status_code == 200, resp.text
        cfg = yaml.safe_load((tmp_path / ".hermes" / "daedalus.yaml").read_text())
        assert cfg["vcs"]["provider"] == "github"
        assert cfg["repo"] == "org/pinned"

    def test_create_no_repo_and_no_remote_is_422(self, project_client, tmp_path):
        resp = self._post(project_client, {"name": "x", "workdir": str(tmp_path)})
        assert resp.status_code == 422
        assert "auto-detect" in resp.json()["detail"]

    def test_create_scaffolds_all_sources_enabled(self, project_client, tmp_path):
        """Template defaults: VCS issues + spec/plan drops + kanban triage all on."""
        with mock.patch("dashboard.plugin_api.registry"), \
             mock.patch("dashboard.plugin_api.ensure_board", return_value=True), \
             mock.patch("dashboard.plugin_api._reconcile_cron",
                        return_value={"cron": "created", "name": "x", "error": None}):
            resp = self._post(project_client, self._payload(tmp_path))
        assert resp.status_code == 200, resp.text
        cfg = yaml.safe_load((tmp_path / ".hermes" / "daedalus.yaml").read_text())
        assert cfg["sources"]["github_issues"]["enabled"] is True
        assert cfg["sources"]["local_specs"]["enabled"] is True
        assert cfg["sources"]["kanban_triage"]["enabled"] is True

    def test_create_gitlab_project(self, project_client, tmp_path):
        with mock.patch("dashboard.plugin_api.registry"), \
             mock.patch("dashboard.plugin_api.ensure_board", return_value=True), \
             mock.patch("dashboard.plugin_api._reconcile_cron",
                        return_value={"cron": "created", "name": "x", "error": None}):
            resp = self._post(project_client, {
                "name": "gl-proj", "repo": "group/gl-proj", "workdir": str(tmp_path),
                "vcs": {"provider": "gitlab"},
            })
        assert resp.status_code == 200, resp.text
        cfg = yaml.safe_load((tmp_path / ".hermes" / "daedalus.yaml").read_text())
        assert cfg["vcs"]["provider"] == "gitlab"


# ═══════════════════════════════════════════════════════════════════════════════
# _reconcile_cron unit tests — mocked subprocess
# ═══════════════════════════════════════════════════════════════════════════════

# Sample cron list output for mocking
_CRON_LIST_ONE_MATCH = """\
┌─────────────────────────────────────────────────────────────────────────┐
│                         Scheduled Jobs                                  │
└─────────────────────────────────────────────────────────────────────────┘

  99f7d116a95b [active]
    Name:      test-project-daedalus
    Schedule:  once in 15m
    Script:    daedalus-cron.sh

  ⚠  Gateway is not running — jobs won't fire automatically.
"""

_CRON_LIST_TWO_MATCHES = """\
┌─────────────────────────────────────────────────────────────────────────┐
│                         Scheduled Jobs                                  │
└─────────────────────────────────────────────────────────────────────────┘

  a1b2c3d4e5f6 [active]
    Name:      test-project-daedalus
    Schedule:  once in 15m
    Script:    daedalus-cron.sh

  99f7d116a95b [active]
    Name:      test-project-daedalus
    Schedule:  once in 30m
    Script:    daedalus-cron.sh

  ⚠  Gateway is not running — jobs won't fire automatically.
"""

_CRON_LIST_NO_MATCH = """\
┌─────────────────────────────────────────────────────────────────────────┐
│                         Scheduled Jobs                                  │
└─────────────────────────────────────────────────────────────────────────┘

  deadbeef1234 [active]
    Name:      other-project-daedalus
    Schedule:  once in 60m
    Script:    daedalus-cron.sh

  ⚠  Gateway is not running — jobs won't fire automatically.
"""


class TestReconcileCron:
    """Unit tests for _reconcile_cron with mocked _cron_cli.

    ``_cron_cli`` — not ``subprocess.run`` — is the seam: ``_hermes_cli`` is
    resolved to ``core.cli.hermes_cli`` at import time, which carries its own
    ``subprocess`` reference. Patching ``dashboard.plugin_api.subprocess.run``
    would be a no-op and let real ``hermes cron create`` commands fire on every
    test run (issue #61). ``_cron_cli(args)`` takes the cron subcommand args
    (no ``hermes cron`` prefix) and returns ``(returncode, output)``.
    """

    def _mock_run_ok(self, stdout=""):
        return (0, stdout)

    def _mock_run_fail(self, returncode=1, stderr="error"):
        return (returncode, stderr)

    def _make_dispatcher(self, list_stdout, remove_ok=True, create_ok=True,
                         edit_ok=True, create_stdout="created: job j1"):
        """Return a callable side_effect for the patched ``_cron_cli``.

        ``_cron_cli`` receives the cron subcommand args (no ``hermes cron``
        prefix) and returns ``(returncode, output)``:

        - ``["list", "--all"]`` → returns list_stdout
        - ``["edit", <hex_id>, ...]`` → returns ok or fail
        - ``["remove", <hex_id>]`` → returns ok or fail
        - ``["create", ...]`` → returns ok or fail
        """
        def _dispatch(args, **kwargs):
            cmd = args
            if cmd[0] == "list" and "--all" in cmd:
                return self._mock_run_ok(stdout=list_stdout)
            if cmd[0] == "edit":
                job_id = cmd[1]
                assert re.match(r"^[0-9a-fA-F]{6,}$", job_id), \
                    f"edit called with non-hex-id: {job_id}"
                if edit_ok:
                    return self._mock_run_ok(stdout="updated")
                return self._mock_run_fail(returncode=2, stderr="unknown command: edit")
            if cmd[0] == "remove":
                # Verify the second arg is a hex job ID (not a name)
                job_id = cmd[1]
                assert re.match(r"^[0-9a-fA-F]{6,}$", job_id), \
                    f"remove called with non-hex-id: {job_id}"
                if remove_ok:
                    return self._mock_run_ok()
                return self._mock_run_fail()
            if cmd[0] == "create":
                if create_ok:
                    return self._mock_run_ok(stdout=create_stdout)
                return self._mock_run_fail(returncode=2, stderr="hermes: schedule invalid")
            return self._mock_run_ok()
        return _dispatch

    # ── happy path: one match → edit in place, never remove+create ───────

    def test_updates_single_cron_in_place(self):
        """One existing job → `hermes cron edit <id>` updates it; no duplicate."""
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(_CRON_LIST_ONE_MATCH)
            result = _reconcile_cron("test-project", {"schedule": "60m"})

        assert result["cron"] == "updated"
        assert result["name"] == "test-project-daedalus"
        assert result["error"] is None

        # Verify calls: list, edit by hex id — no remove, no create.
        calls = mock_run.call_args_list
        assert len(calls) == 2

        list_args = calls[0][0][0]
        assert list_args == ["list", "--all"]

        edit_args = calls[1][0][0]
        assert edit_args[0] == "edit"
        assert edit_args[1] == "99f7d116a95b"  # hex id from list output
        assert "--schedule" in edit_args
        # Interval "60m" must be normalised to crontab so the cron repeats (#134).
        assert "0 * * * *" in edit_args
        assert "60m" not in edit_args

    def test_updates_cron_with_deliver(self):
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(_CRON_LIST_ONE_MATCH)
            result = _reconcile_cron(
                "test-project",
                {"schedule": "30m", "deliver": "slack:#engineering"},
            )

        assert result["cron"] == "updated"
        assert result["error"] is None

        edit_call = mock_run.call_args_list[1]
        args = edit_call[0][0]
        assert "--deliver" in args
        assert "slack:#engineering" in args

    def test_edit_unsupported_falls_back_to_remove_create(self):
        """Older hermes without `cron edit` → remove by id + create fresh."""
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(
                _CRON_LIST_ONE_MATCH, edit_ok=False
            )
            result = _reconcile_cron("test-project", {"schedule": "60m"})

        assert result["cron"] == "created"
        assert result["error"] is None

        calls = mock_run.call_args_list
        # list + failed edit + remove + create = 4 calls
        assert len(calls) == 4
        assert calls[1][0][0][0] == "edit"
        assert calls[2][0][0][0] == "remove"
        assert calls[2][0][0][1] == "99f7d116a95b"
        create_args = calls[3][0][0]
        assert create_args[0] == "create"
        assert "--name" in create_args
        assert "test-project-daedalus" in create_args
        assert "--script" in create_args
        assert "daedalus-cron.sh" in create_args
        assert "--no-agent" in create_args

    def test_notifications_suppress_cron_deliver(self):
        """cron.notifications[] → dispatcher self-delivers, cron gets no --deliver."""
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(_CRON_LIST_NO_MATCH)
            result = _reconcile_cron(
                "test-project",
                {"schedule": "30m", "deliver": "slack:#engineering",
                 "notifications": [{"platform": "Discord",
                                    "target": "discord:#daedalus"}]},
            )

        assert result["cron"] == "created"
        create_args = mock_run.call_args_list[1][0][0]
        assert "--deliver" not in create_args

    def test_workdir_passed_when_cfg_path_given(self):
        """Issue #137: _reconcile_cron passes --workdir <repo root> on create + edit
        so the dispatcher self-scopes to that project instead of sweeping all repos."""
        from pathlib import Path
        from dashboard.plugin_api import _reconcile_cron

        cfg_path = Path("/repos/myproj/.hermes/daedalus.yaml")
        expected = str(cfg_path.parent.parent.resolve())  # /repos/myproj

        # create path (no existing job). Use a crontab schedule so the function
        # never tries to write the schedule back to the (fake) cfg_path.
        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(_CRON_LIST_NO_MATCH)
            _reconcile_cron("myproj", {"schedule": "0 * * * *"}, cfg_path)
        create_args = mock_run.call_args_list[-1][0][0]
        assert create_args[0] == "create"
        assert "--workdir" in create_args
        assert expected in create_args

        # edit path (one existing "test-project-daedalus" job → edited in place).
        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(_CRON_LIST_ONE_MATCH)
            _reconcile_cron("test-project", {"schedule": "0 * * * *"}, cfg_path)
        edit_args = mock_run.call_args_list[1][0][0]
        assert edit_args[0] == "edit"
        assert "--workdir" in edit_args
        assert expected in edit_args

    # ── sweep duplicates: two same-name jobs → both removed by id ────────

    def test_sweeps_duplicate_cron_jobs(self):
        """Two jobs with same name → both removed by hex id, then one created."""
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(_CRON_LIST_TWO_MATCHES)
            result = _reconcile_cron("test-project", {"schedule": "60m"})

        assert result["cron"] == "created"
        assert result["error"] is None

        calls = mock_run.call_args_list
        # list + 2 removes + create = 4 calls
        assert len(calls) == 4

        # Both remove calls use hex ids
        remove1_args = calls[1][0][0]
        remove2_args = calls[2][0][0]
        assert remove1_args[1] == "a1b2c3d4e5f6"
        assert remove2_args[1] == "99f7d116a95b"

    # ── no match → no remove calls, just create ──────────────────────────

    def test_no_match_still_creates(self):
        """No matching job in list → no remove calls, still creates."""
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(_CRON_LIST_NO_MATCH)
            result = _reconcile_cron("test-project", {"schedule": "60m"})

        assert result["cron"] == "created"
        assert result["error"] is None

        calls = mock_run.call_args_list
        # list + create = 2 calls (no remove)
        assert len(calls) == 2
        assert calls[0][0][0] == ["list", "--all"]
        assert calls[1][0][0][0] == "create"

    # ── empty schedule → remove matches, no create ───────────────────────

    def test_removes_cron_when_schedule_empty(self):
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(_CRON_LIST_ONE_MATCH)
            result = _reconcile_cron("test-project", {"schedule": ""})

        assert result["cron"] == "removed"
        assert result["error"] is None
        # list + remove = 2 calls (no create)
        assert mock_run.call_count == 2

    def test_removes_cron_when_cron_cfg_none(self):
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(_CRON_LIST_ONE_MATCH)
            result = _reconcile_cron("test-project", {})

        assert result["cron"] == "removed"
        assert result["error"] is None
        assert mock_run.call_count == 2

    # ── error resilience ─────────────────────────────────────────────────

    def test_list_failure_is_non_fatal(self):
        """If 'hermes cron list' fails, we still attempt create."""
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(
                _CRON_LIST_ONE_MATCH, create_ok=True
            )
            # Override: first call (list) fails
            orig = mock_run.side_effect
            call_count = [0]
            def fail_list_then_dispatch(args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return self._mock_run_fail(returncode=1, stderr="list failed")
                return orig(args, **kwargs)
            mock_run.side_effect = fail_list_then_dispatch

            result = _reconcile_cron("test-project", {"schedule": "60m"})

        assert result["cron"] == "created"
        assert result["error"] is None

    def test_create_failure_captures_error(self):
        """A cron create failure is captured, not raised."""
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(
                _CRON_LIST_NO_MATCH, create_ok=False
            )
            result = _reconcile_cron("test-project", {"schedule": "bad-schedule"})

        assert result["error"] is not None
        assert "schedule invalid" in result["error"]

    def test_hermes_cli_not_found(self):
        from dashboard.plugin_api import _reconcile_cron

        # _hermes_cli (and thus _cron_cli) catches FileNotFoundError internally
        # and returns (-1, "hermes CLI not found") — it never raises. The
        # reconcile path must surface that string as the result error.
        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.return_value = (-1, "hermes CLI not found")
            result = _reconcile_cron("test-project", {"schedule": "60m"})

        assert result["error"] == "hermes CLI not found"

    def test_creates_cron_schedule_with_whitespace(self):
        """Schedule with leading/trailing whitespace is stripped, then converted."""
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(_CRON_LIST_ONE_MATCH)
            result = _reconcile_cron("test-project", {"schedule": "  60m  "})

        assert result["cron"] == "updated"
        edit_call = mock_run.call_args_list[1]
        args = edit_call[0][0]
        assert "0 * * * *" in args  # trimmed and normalised to crontab (#134)
        assert "  60m  " not in args
        assert "60m" not in args

    # ── schedule conversion: interval → crontab (issue #134) ─────────────

    def test_edit_path_converts_interval_to_crontab(self):
        """Edit path: interval schedule reaches hermes as crontab, never one-shot."""
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(_CRON_LIST_ONE_MATCH)
            _reconcile_cron("test-project", {"schedule": "every 2h"})

        edit_args = mock_run.call_args_list[1][0][0]
        assert edit_args[0] == "edit"
        assert "0 */2 * * *" in edit_args
        assert "every 2h" not in edit_args

    def test_create_path_converts_interval_to_crontab(self):
        """Create path: interval schedule reaches hermes as crontab, never one-shot."""
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(_CRON_LIST_NO_MATCH)
            result = _reconcile_cron("test-project", {"schedule": "30m"})

        assert result["cron"] == "created"
        create_args = mock_run.call_args_list[1][0][0]
        assert create_args[0] == "create"
        # `hermes cron create <schedule> ...` — schedule is the first positional.
        assert create_args[1] == "*/30 * * * *"
        assert "30m" not in create_args

    def test_crontab_schedule_passes_through_unchanged(self):
        """An already-crontab schedule is sent verbatim (no double conversion)."""
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(_CRON_LIST_ONE_MATCH)
            _reconcile_cron("test-project", {"schedule": "0 9 * * *"})

        edit_args = mock_run.call_args_list[1][0][0]
        assert "0 9 * * *" in edit_args

    def test_normalised_schedule_written_back_to_config(self):
        """When the schedule is normalised, the YAML is rewritten to crontab so
        it stays consistent with the live cron and never reverts on next save."""
        import tempfile
        from pathlib import Path as _Path
        from dashboard.plugin_api import _reconcile_cron

        with tempfile.TemporaryDirectory() as td:
            cfg_path = _Path(td) / "daedalus.yaml"
            cfg_path.write_text("cron:\n  schedule: 60m\n  deliver: slack:#eng\n")
            with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
                mock_run.side_effect = self._make_dispatcher(_CRON_LIST_ONE_MATCH)
                _reconcile_cron("test-project", {"schedule": "60m"}, cfg_path)

            text = cfg_path.read_text()
            assert '"0 * * * *"' in text
            assert "60m" not in text
            # Unrelated keys are preserved.
            assert "deliver: slack:#eng" in text

    def test_crontab_schedule_not_written_back(self):
        """A schedule already in crontab format is left untouched in the YAML."""
        import tempfile
        from pathlib import Path as _Path
        from dashboard.plugin_api import _reconcile_cron

        with tempfile.TemporaryDirectory() as td:
            cfg_path = _Path(td) / "daedalus.yaml"
            original = "cron:\n  schedule: 0 9 * * *\n"
            cfg_path.write_text(original)
            with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
                mock_run.side_effect = self._make_dispatcher(_CRON_LIST_ONE_MATCH)
                _reconcile_cron("test-project", {"schedule": "0 9 * * *"}, cfg_path)

            assert cfg_path.read_text() == original

    def test_repeated_saves_never_stack_jobs(self):
        """Regression: saving twice (schedule change) must never produce a
        second job — each save either edits in place or recreates exactly one."""
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
            mock_run.side_effect = self._make_dispatcher(_CRON_LIST_ONE_MATCH)
            r1 = _reconcile_cron("test-project", {"schedule": "30m"})
            r2 = _reconcile_cron("test-project", {"schedule": "45m"})

        assert r1["cron"] == "updated" and r2["cron"] == "updated"
        cmds = [c[0][0][0] for c in mock_run.call_args_list]
        assert cmds == ["list", "edit", "list", "edit"]
        assert "create" not in cmds  # no job ever stacked

    # ── _parse_cron_list_blocks unit tests ───────────────────────────────

    def test_parse_cron_list_blocks_one_job(self):
        from dashboard.plugin_api import _parse_cron_jobs

        blocks = _parse_cron_jobs(_CRON_LIST_ONE_MATCH)
        assert len(blocks) == 1
        assert blocks[0]["job_id"] == "99f7d116a95b"
        assert blocks[0]["name"] == "test-project-daedalus"

    def test_parse_cron_list_blocks_two_jobs(self):
        from dashboard.plugin_api import _parse_cron_jobs

        blocks = _parse_cron_jobs(_CRON_LIST_TWO_MATCHES)
        assert len(blocks) == 2
        assert blocks[0]["job_id"] == "a1b2c3d4e5f6"
        assert blocks[0]["name"] == "test-project-daedalus"
        assert blocks[1]["job_id"] == "99f7d116a95b"
        assert blocks[1]["name"] == "test-project-daedalus"

    def test_parse_cron_list_blocks_empty(self):
        from dashboard.plugin_api import _parse_cron_jobs

        blocks = _parse_cron_jobs("")
        assert blocks == []

    def test_parse_cron_list_blocks_skips_warning(self):
        from dashboard.plugin_api import _parse_cron_jobs

        # Warning-only output (no jobs)
        warning_only = "  ⚠  Gateway is not running — jobs won't fire automatically.\n"
        blocks = _parse_cron_jobs(warning_only)
        assert blocks == []


class TestPostProjectConfigCron:
    """Integration tests for post_project_config including cron reconciliation."""

    @pytest.fixture
    def cron_project_dir(self):
        """Create a temp repo dir with .hermes/daedalus.yaml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            hermes_dir = repo / ".hermes"
            hermes_dir.mkdir()
            cfg = {
                "name": "cron-project",
                "repo": "org/cron-project",
                "workdir": str(repo),
                "vcs": {"target_branch": "main"},
                "cron": {"schedule": "60m"},
            }
            (hermes_dir / "daedalus.yaml").write_text(yaml.dump(cfg))
            yield repo

    @pytest.fixture
    def cron_client(self, cron_project_dir):
        from dashboard.plugin_api import project_config_router

        app = FastAPI()
        app.include_router(project_config_router, prefix="/api/plugins/daedalus")
        return TestClient(app)

    def test_save_returns_cron_result(self, cron_client, cron_project_dir):
        """POST /project/{name}/config returns cron result in response."""
        with mock.patch("dashboard.plugin_api.registry") as mock_registry:
            mock_registry.list_projects.return_value = [str(cron_project_dir)]
            with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
                mock_run.return_value = (0, "created: job j1")

                payload = {
                    "cron": {"schedule": "15m", "deliver": "slack:tasks"},
                    "vcs": {"target_branch": "dev"},
                }
                resp = cron_client.post(
                    "/api/plugins/daedalus/project/cron-project/config",
                    json=payload,
                )
                assert resp.status_code == 200, resp.text
                data = resp.json()

        assert data["status"] == "saved"
        assert "cron" in data
        assert data["cron"]["name"] == "cron-project-daedalus"
        assert "error" in data["cron"]

    def test_save_clearing_schedule_removes_cron(self, cron_client, cron_project_dir):
        """Clearing the schedule in the payload removes the cron job."""
        with mock.patch("dashboard.plugin_api.registry") as mock_registry:
            mock_registry.list_projects.return_value = [str(cron_project_dir)]
            with mock.patch("dashboard.plugin_api._cron_cli") as mock_run:
                mock_run.return_value = (0, "")

                payload = {"cron": {"schedule": ""}}
                resp = cron_client.post(
                    "/api/plugins/daedalus/project/cron-project/config",
                    json=payload,
                )
                assert resp.status_code == 200, resp.text
                data = resp.json()

        assert data["status"] == "saved"
        assert data["cron"]["cron"] == "removed"


# ═══════════════════════════════════════════════════════════════════════════════
# Meta /notifications endpoint tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestParseSendListOutput:
    """Unit tests for _parse_send_list_output — no subprocess needed."""

    def test_parse_single_method_single_target(self):
        from dashboard.plugin_api import _parse_send_list_output

        output = "Slack:\n  slack:tasks"
        result = _parse_send_list_output(output)
        assert result == {"Slack": ["slack:tasks"]}

    def test_parse_single_method_multiple_targets(self):
        from dashboard.plugin_api import _parse_send_list_output

        output = "Slack:\n  slack:tasks\n  slack:#engineering\n  slack:#alerts"
        result = _parse_send_list_output(output)
        assert result == {"Slack": ["slack:tasks", "slack:#engineering", "slack:#alerts"]}

    def test_parse_multiple_methods(self):
        from dashboard.plugin_api import _parse_send_list_output

        output = (
            "Slack:\n"
            "  slack:tasks\n"
            "  slack:#engineering\n"
            "Discord:\n"
            "  discord:#general\n"
        )
        result = _parse_send_list_output(output)
        assert result == {
            "Slack": ["slack:tasks", "slack:#engineering"],
            "Discord": ["discord:#general"],
        }

    def test_parse_method_with_profile_annotation(self):
        """Method headers like 'Discord (Glados):' strip the profile annotation."""
        from dashboard.plugin_api import _parse_send_list_output

        output = "Discord (Glados):\n  discord:#general"
        result = _parse_send_list_output(output)
        assert "Discord" in result
        assert result["Discord"] == ["discord:#general"]

    def test_strips_trailing_annotations_from_targets(self):
        """Targets like 'slack:tasks (private)' strip the parenthesized suffix."""
        from dashboard.plugin_api import _parse_send_list_output

        output = "Slack:\n  slack:tasks (private)\n  slack:#engineering (channel)"
        result = _parse_send_list_output(output)
        assert result == {"Slack": ["slack:tasks", "slack:#engineering"]}

    def test_strips_annotations_with_mixed_whitespace(self):
        """Parenthesized suffixes with varying whitespace are stripped."""
        from dashboard.plugin_api import _parse_send_list_output

        output = "Telegram:\n  telegram:-1001234567890  (group)\n  telegram:+15551234567(dm)"
        result = _parse_send_list_output(output)
        assert result == {
            "Telegram": ["telegram:-1001234567890", "telegram:+15551234567"],
        }

    def test_parse_empty_output_returns_empty_dict(self):
        from dashboard.plugin_api import _parse_send_list_output

        assert _parse_send_list_output("") == {}
        assert _parse_send_list_output("\n\n") == {}

    def test_parse_no_method_header(self):
        """Output without method headers returns empty dict."""
        from dashboard.plugin_api import _parse_send_list_output

        output = "  slack:tasks\n  slack:#general"
        result = _parse_send_list_output(output)
        assert result == {}

    def test_parse_no_targets_under_method(self):
        """Method header without any targets still appears with empty list."""
        from dashboard.plugin_api import _parse_send_list_output

        output = "Slack:"
        result = _parse_send_list_output(output)
        assert result == {"Slack": []}

    def test_parse_typical_full_output(self):
        """Simulate a realistic full `hermes send --list` output."""
        from dashboard.plugin_api import _parse_send_list_output

        output = (
            "Slack:\n"
            "  slack:tasks (private)\n"
            "  slack:#engineering (channel)\n"
            "Discord (Glados):\n"
            "  discord:#general\n"
            "Telegram:\n"
            "  telegram:-1001234567890:17585 (topic)\n"
            "Signal:\n"
            "  signal:+155****4567"
        )
        result = _parse_send_list_output(output)
        assert result == {
            "Slack": ["slack:tasks", "slack:#engineering"],
            "Discord": ["discord:#general"],
            "Telegram": ["telegram:-1001234567890:17585"],
            "Signal": ["signal:+155****4567"],
        }


    def test_parse_typical_full_output_with_header(self):
        """Simulate a realistic `hermes send --list` output with intro header."""
        from dashboard.plugin_api import _parse_send_list_output

        output = (
            "Available messaging targets:\n"
            "\n"
            "Slack:\n"
            "  slack:tasks (private)\n"
            "  slack:#engineering (channel)\n"
            "\n"
            "Discord (Glados):\n"
            "  discord:#general\n"
            "\n"
            "Telegram:\n"
            "  telegram:-1001234567890:17585 (topic)\n"
            "\n"
            "Signal:\n"
            "  signal:+155****4567"
        )
        result = _parse_send_list_output(output)
        # No header key, clean method names (annotation stripped)
        assert "Available messaging targets" not in result
        assert result == {
            "Slack": ["slack:tasks", "slack:#engineering"],
            "Discord": ["discord:#general"],
            "Telegram": ["telegram:-1001234567890:17585"],
            "Signal": ["signal:+155****4567"],
        }


class TestNotificationMethods:
    """Integration tests for _list_notification_methods with mocked subprocess."""

    def test_returns_parsed_dict_on_success(self):
        from dashboard.plugin_api import _list_notification_methods

        json_out = json.dumps({"platforms": {
            "slack": [{"id": "tasks", "name": "tasks"}],
            "discord": [{"id": "general", "name": "general"}],
        }})

        def fake_cli(args, timeout=30):
            if args[:3] == ["send", "--list", "--json"]:
                return 0, json_out
            return -1, ""

        with mock.patch("dashboard.plugin_api._hermes_cli", side_effect=fake_cli):
            result = _list_notification_methods()

        assert result == {
            "Slack": [{"value": "slack:tasks", "label": "tasks"}],
            "Discord": [{"value": "discord:general", "label": "#general"}],
        }

    def test_returns_empty_dict_on_nonzero_returncode(self):
        from dashboard.plugin_api import _list_notification_methods

        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1, stdout="", stderr="hermes: command not found"
            )
            result = _list_notification_methods()
            assert result == {}

    def test_returns_empty_dict_on_filenotfound(self):
        from dashboard.plugin_api import _list_notification_methods

        with mock.patch(
            "dashboard.plugin_api.subprocess.run",
            side_effect=FileNotFoundError("hermes not found"),
        ):
            result = _list_notification_methods()
            assert result == {}

    def test_returns_empty_dict_on_timeout(self):
        from dashboard.plugin_api import _list_notification_methods

        with mock.patch(
            "dashboard.plugin_api.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="hermes", timeout=10),
        ):
            result = _list_notification_methods()
            assert result == {}

    def test_returns_empty_dict_on_oserror(self):
        from dashboard.plugin_api import _list_notification_methods

        with mock.patch(
            "dashboard.plugin_api.subprocess.run",
            side_effect=OSError("permission denied"),
        ):
            result = _list_notification_methods()
            assert result == {}


class TestMetaNotificationsEndpoint:
    """HTTP-level tests for GET /meta/notifications."""

    @pytest.fixture
    def meta_client(self):
        """Create a FastAPI TestClient with the meta router mounted."""
        from dashboard.plugin_api import meta_router

        app = FastAPI()
        app.include_router(meta_router, prefix="/api/plugins/daedalus")
        return TestClient(app)

    def test_get_notifications_success(self, meta_client):
        json_out = json.dumps({"platforms": {
            "slack": [{"id": "tasks", "name": "tasks"}],
            "discord": [{"id": "general", "name": "general"}],
        }})

        def fake_cli(args, timeout=30):
            if args[:3] == ["send", "--list", "--json"]:
                return 0, json_out
            return -1, ""

        with mock.patch("dashboard.plugin_api._hermes_cli", side_effect=fake_cli):
            resp = meta_client.get("/api/plugins/daedalus/meta/notifications")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data == {
            "Slack": [{"value": "slack:tasks", "label": "tasks"}],
            "Discord": [{"value": "discord:general", "label": "#general"}],
        }

    def test_get_notifications_empty_on_failure(self, meta_client):
        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1, stdout="", stderr="error"
            )
            resp = meta_client.get("/api/plugins/daedalus/meta/notifications")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data == {}

    def test_get_notifications_with_annotations_stripped(self, meta_client):
        json_out = json.dumps({"platforms": {
            "slack": [
                {"id": "tasks", "name": "tasks"},
                {"id": "engineering", "name": "engineering"},
            ],
        }})

        def fake_cli(args, timeout=30):
            if args[:3] == ["send", "--list", "--json"]:
                return 0, json_out
            return -1, ""

        with mock.patch("dashboard.plugin_api._hermes_cli", side_effect=fake_cli):
            resp = meta_client.get("/api/plugins/daedalus/meta/notifications")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data == {
            "Slack": [
                {"value": "slack:tasks", "label": "tasks"},
                {"value": "slack:engineering", "label": "engineering"},
            ],
        }


# ═══════════════════════════════════════════════════════════════════════════════
# GET /meta/version endpoint tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMetaVersionEndpoint:
    """HTTP-level tests for GET /meta/version (the released-version surface)."""

    @pytest.fixture
    def meta_client(self):
        """Create a FastAPI TestClient with the meta router mounted."""
        from dashboard.plugin_api import meta_router

        app = FastAPI()
        app.include_router(meta_router, prefix="/api/plugins/daedalus")
        return TestClient(app)

    def test_version_matches_plugin_yaml(self, meta_client):
        """The endpoint reports the version pinned in plugin.yaml (version-agnostic)."""
        import yaml as _yaml
        from dashboard import plugin_api

        plugin_yaml = Path(plugin_api.__file__).resolve().parent.parent / "plugin.yaml"
        with open(plugin_yaml) as f:
            expected = (_yaml.safe_load(f) or {}).get("version")

        resp = meta_client.get("/api/plugins/daedalus/meta/version")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"version": expected}

    def test_version_unknown_on_read_failure(self, meta_client):
        """A read error degrades gracefully to version 'unknown'."""
        with mock.patch("builtins.open", side_effect=OSError("boom")):
            resp = meta_client.get("/api/plugins/daedalus/meta/version")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"version": "unknown"}


# ═══════════════════════════════════════════════════════════════════════════════
# POST /meta/test-deliver endpoint tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMetaTestDeliverEndpoint:
    """HTTP-level tests for POST /meta/test-deliver."""

    @pytest.fixture
    def deliver_client(self):
        """Create a FastAPI TestClient with the meta router mounted."""
        from dashboard.plugin_api import meta_router

        app = FastAPI()
        app.include_router(meta_router, prefix="/api/plugins/daedalus")
        return TestClient(app)

    def test_success(self, deliver_client):
        """A successful send returns ok=true with no error."""
        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0, stdout="  sent to slack:#tasks\n", stderr=""
            )
            resp = deliver_client.post(
                "/api/plugins/daedalus/meta/test-deliver",
                json={"deliver": "slack:#tasks"},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["ok"] is True
            assert data["target"] == "slack:#tasks"
            assert data["error"] is None

    def test_failure_nonzero_exit(self, deliver_client):
        """A non-zero exit is captured as ok=false with error."""
        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1, stdout="", stderr="could not resolve target"
            )
            resp = deliver_client.post(
                "/api/plugins/daedalus/meta/test-deliver",
                json={"deliver": "bad-target"},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["ok"] is False
            assert data["target"] == "bad-target"
            assert "could not resolve" in data["error"]

    def test_empty_target(self, deliver_client):
        """Empty deliver returns 'no delivery target selected' without running send."""
        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            resp = deliver_client.post(
                "/api/plugins/daedalus/meta/test-deliver",
                json={"deliver": ""},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["ok"] is False
            assert data["error"] == "no delivery target selected"
            # subprocess.run must NOT have been called
            mock_run.assert_not_called()

    def test_missing_deliver_key(self, deliver_client):
        """Missing deliver key in body returns 'no delivery target selected'."""
        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            resp = deliver_client.post(
                "/api/plugins/daedalus/meta/test-deliver",
                json={},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["ok"] is False
            assert data["error"] == "no delivery target selected"
            mock_run.assert_not_called()

    def test_whitespace_only_target(self, deliver_client):
        """Whitespace-only deliver is treated as empty."""
        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            resp = deliver_client.post(
                "/api/plugins/daedalus/meta/test-deliver",
                json={"deliver": "   "},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["ok"] is False
            assert data["error"] == "no delivery target selected"
            mock_run.assert_not_called()

    def test_hermes_cli_not_found(self, deliver_client):
        """FileNotFoundError maps to 'hermes CLI not found'."""
        with mock.patch(
            "dashboard.plugin_api.subprocess.run",
            side_effect=FileNotFoundError("hermes not on PATH"),
        ):
            resp = deliver_client.post(
                "/api/plugins/daedalus/meta/test-deliver",
                json={"deliver": "slack:tasks"},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["ok"] is False
            assert data["error"] == "hermes CLI not found"

    def test_timeout(self, deliver_client):
        """TimeoutExpired maps to a timeout error."""
        with mock.patch(
            "dashboard.plugin_api.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["hermes"], timeout=10),
        ):
            resp = deliver_client.post(
                "/api/plugins/daedalus/meta/test-deliver",
                json={"deliver": "slack:tasks"},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["ok"] is False
            assert "timed out" in data["error"]

    def test_oserror(self, deliver_client):
        """OSError is captured."""
        with mock.patch(
            "dashboard.plugin_api.subprocess.run",
            side_effect=OSError("permission denied"),
        ):
            resp = deliver_client.post(
                "/api/plugins/daedalus/meta/test-deliver",
                json={"deliver": "slack:tasks"},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["ok"] is False
            assert "permission denied" in data["error"]

    def test_invalid_json_body(self, deliver_client):
        """Non-JSON body returns ok=false."""
        resp = deliver_client.post(
            "/api/plugins/daedalus/meta/test-deliver",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is False
        assert "invalid JSON" in data["error"]

    def test_body_not_a_dict(self, deliver_client):
        """Body that parses as non-dict returns ok=false."""
        resp = deliver_client.post(
            "/api/plugins/daedalus/meta/test-deliver",
            json=["not", "a", "dict"],
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is False
        assert "body must be a JSON object" in data["error"]

    def test_command_is_list_args_no_shell(self, deliver_client):
        """Verify the command uses list-args (no shell injection)."""
        from dashboard.plugin_api import _TEST_MESSAGE

        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0, stdout="  sent\n", stderr=""
            )
            deliver_client.post(
                "/api/plugins/daedalus/meta/test-deliver",
                json={"deliver": "slack:#general"},
            )
            # Check the first positional argument is a list (not a string)
            call_args = mock_run.call_args[0][0]
            assert isinstance(call_args, list), (
                f"Expected list-args, got {type(call_args)}"
            )
            assert call_args == [
                "hermes", "send", "-t", "slack:#general", _TEST_MESSAGE
            ]


# ═══════════════════════════════════════════════════════════════════════════════
# Mount-level integration test — validates that the single top-level router
# exposes all child routes when mounted with a prefix.
# ═══════════════════════════════════════════════════════════════════════════════


def test_router_mount_exposes_all_endpoints(registry_repo, project_repo_dir, monkeypatch):
    """Build a FastAPI app, mount the unified router, and assert every endpoint
    group (/projects, /project/{name}/config, /meta/notifications) is reachable.

    This is the regression test for the bug where some sub-routers were
    silently missing from the unified router.
    """
    from dashboard.plugin_api import router as unified_router

    monkeypatch.setenv("DAEDALUS_DASHBOARD_AUTH_DISABLED", "1")
    app = FastAPI()
    app.include_router(unified_router, prefix="/api/plugins/daedalus")
    client = TestClient(app)

    # ── /projects (GET) ──────────────────────────────────────────────────
    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []
        resp = client.get("/api/plugins/daedalus/projects")
        assert resp.status_code == 200, f"GET /projects: {resp.status_code} {resp.text}"
        projects = resp.json()
        assert isinstance(projects, list)
        assert len(projects) >= 1

    # ── /project/{name}/config (GET) ─────────────────────────────────────
    project_name = "test-project"
    with mock.patch("dashboard.plugin_api.registry") as mock_registry:
        mock_registry.list_projects.return_value = [str(project_repo_dir)]

        resp = client.get(
            f"/api/plugins/daedalus/project/{project_name}/config"
        )
        assert resp.status_code == 200, (
            f"GET /project/{project_name}/config: {resp.status_code} {resp.text}"
        )
        proj_data = resp.json()
        assert proj_data["name"] == project_name
        assert "workdir" in proj_data
        assert "vcs" in proj_data

    # ── /meta/notifications (GET) ────────────────────────────────────────
    json_out = json.dumps({"platforms": {"slack": [
        {"id": "tasks", "name": "tasks"},
        {"id": "engineering", "name": "engineering"},
    ]}})

    def fake_cli_notif(args, timeout=30):
        if args[:3] == ["send", "--list", "--json"]:
            return 0, json_out
        return -1, ""

    with mock.patch("dashboard.plugin_api._hermes_cli", side_effect=fake_cli_notif):
        resp = client.get("/api/plugins/daedalus/meta/notifications")
    assert resp.status_code == 200, (
        f"GET /meta/notifications: {resp.status_code} {resp.text}"
    )
    notif_data = resp.json()
    assert isinstance(notif_data, dict)
    assert "Slack" in notif_data
    assert notif_data["Slack"] == [
        {"value": "slack:tasks", "label": "tasks"},
        {"value": "slack:engineering", "label": "engineering"},
    ]


def test_all_sub_routers_mounted_and_resolve(registry_repo, project_repo_dir, monkeypatch):
    """Build a FastAPI app, mount only plugin_api.router, introspect the module
    for all APIRouter instances (config, projects, project_config, meta),
    and assert each one's routes resolve to non-404 responses.

    This is the introspective guard test: even if a router is included in
    the top-level router, some sub-routers could still be silently missing
    (e.g. added as a constant but never include_router'd). This test
    discovers all router instances by name and verifies their routes work.
    """
    import dashboard.plugin_api as papi

    monkeypatch.setenv("DAEDALUS_DASHBOARD_AUTH_DISABLED", "1")

    # ── Discover all APIRouter instances in the module ──────────────────
    sub_routers: dict[str, APIRouter] = {}
    expected = {"projects_router", "project_config_router", "meta_router"}
    for name in expected:
        obj = getattr(papi, name, None)
        assert isinstance(obj, APIRouter), (
            f"Expected {name} to be an APIRouter, got {type(obj)}"
        )
        sub_routers[name] = obj

    # ── Verify each sub-router has at least one registered route ────────
    for name, sr in sub_routers.items():
        assert len(sr.routes) > 0, (
            f"{name} has zero routes registered — are endpoints defined "
            f"before the top-level router is assembled?"
        )

    # ── Build app with the unified top-level router ─────────────────────
    # Build first so we can verify coverage via the OpenAPI schema, which is
    # version-agnostic (FastAPI's include_router flattens routes differently
    # at the APIRouter level depending on the Python/Starlette version, but
    # app.openapi() always reflects the fully-resolved path set).
    top_router = papi.router
    app = FastAPI()
    app.include_router(top_router, prefix="/api/plugins/daedalus")

    # ── Verify all sub-router routes appear in the assembled app ─────────
    PREFIX = "/api/plugins/daedalus"
    schema_paths = set(app.openapi()["paths"].keys())

    expected_paths: set[str] = set()
    for name, sr in sub_routers.items():
        for route in sr.routes:
            if hasattr(route, "path"):
                expected_paths.add(PREFIX + route.path)  # type: ignore[attr-defined]

    missing = expected_paths - schema_paths
    assert not missing, (
        f"Expected sub-router routes not found in assembled app: {sorted(missing)}"
    )
    client = TestClient(app)

    # ── /projects (GET) ──────────────────────────────────────────────────
    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []
        resp = client.get("/api/plugins/daedalus/projects")
        assert resp.status_code == 200, f"/projects: {resp.status_code}"

    # ── /project/{name}/config (GET) ─────────────────────────────────────
    with mock.patch("dashboard.plugin_api.registry") as mock_registry:
        mock_registry.list_projects.return_value = [str(project_repo_dir)]
        resp = client.get(
            "/api/plugins/daedalus/project/test-project/config"
        )
        assert resp.status_code == 200, (
            f"/project/test-project/config: {resp.status_code}"
        )

    # ── /meta/notifications (GET) ────────────────────────────────────────
    json_out2 = json.dumps({"platforms": {
        "slack": [{"id": "tasks", "name": "tasks"}],
        "discord": [{"id": "general", "name": "general"}],
    }})

    def fake_cli_notif2(args, timeout=30):
        if args[:3] == ["send", "--list", "--json"]:
            return 0, json_out2
        return -1, ""

    with mock.patch("dashboard.plugin_api._hermes_cli", side_effect=fake_cli_notif2):
        resp = client.get("/api/plugins/daedalus/meta/notifications")
    assert resp.status_code == 200, (
        f"/meta/notifications: {resp.status_code} {resp.text}"
    )
    data = resp.json()
    assert isinstance(data, dict)
    assert "Slack" in data
    assert "Discord" in data


# ═══════════════════════════════════════════════════════════════════════════════
# Fix A — Slack channel name resolution tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestParseSendListThreadedFormat:
    """Tests for _parse_send_list_output with the new threaded Slack format."""

    def test_dedup_threaded_slack_to_unique_channels(self):
        """33 thread rows for 6 unique channels → 6 unique slack:<id> entries."""
        from dashboard.plugin_api import _parse_send_list_output

        output = (
            "Slack:\n"
            "  slack:C0B3P2Q39LN / topic 1780366834.325169 (group)\n"
            "  slack:C0B3P2Q39LN / topic 1780398043.510689 (group)\n"
            "  slack:C0B3P2Q39LN / topic 1780398043.999999 (group)\n"
            "  slack:D0B4J9MMJ3A / topic 1778715907.175579 (dm)\n"
            "  slack:D0B4J9MMJ3A / topic 1778715907.999999 (dm)\n"
            "  slack:C0B5X8ZZZ99 / topic 1780000000.000001 (channel)\n"
        )
        result = _parse_send_list_output(output)
        assert "Slack" in result
        slack_targets = result["Slack"]
        assert len(slack_targets) == 3, (
            f"Expected 3 unique channels, got {len(slack_targets)}: {slack_targets}"
        )
        assert set(slack_targets) == {
            "slack:C0B3P2Q39LN",
            "slack:D0B4J9MMJ3A",
            "slack:C0B5X8ZZZ99",
        }

    def test_strips_topic_suffix_variations(self):
        """Various / topic <ts> formats are all stripped."""
        from dashboard.plugin_api import _parse_send_list_output

        output = (
            "Slack:\n"
            "  slack:C0B3P2Q39LN / topic 1780366834.325169 (group)\n"
            "  slack:C0B3P2Q39LN/topic 1780366834.325169(group)\n"
            "  slack:C0B3P2Q39LN / topic 1780366834.325169\n"
        )
        result = _parse_send_list_output(output)
        assert result["Slack"] == ["slack:C0B3P2Q39LN"]

    def test_non_slack_methods_unchanged(self):
        """Discord/Telegram targets are not affected by Slack dedup logic."""
        from dashboard.plugin_api import _parse_send_list_output

        output = (
            "Slack:\n"
            "  slack:C0B3P2Q39LN / topic 1780366834.325169 (group)\n"
            "  slack:C0B3P2Q39LN / topic 1780398043.510689 (group)\n"
            "Discord:\n"
            "  discord:#general\n"
            "  discord:#alerts\n"
        )
        result = _parse_send_list_output(output)
        assert result["Slack"] == ["slack:C0B3P2Q39LN"]
        assert result["Discord"] == ["discord:#general", "discord:#alerts"]




class TestListNotificationMethodsNewShape:
    """Tests for _list_notification_methods returning {value, label} shape."""

    def test_returns_value_label_pairs(self):
        """JSON path: each entry uses name as label, target as value."""
        from dashboard.plugin_api import _list_notification_methods

        json_out = json.dumps({"platforms": {
            "slack": [{"id": "C0B3P2Q39LN", "name": "tasks"}],
            "discord": [{"id": "general", "name": "general"}],
        }})

        def fake_cli(args, timeout=30):
            if args[:3] == ["send", "--list", "--json"]:
                return 0, json_out
            return -1, ""

        with mock.patch("dashboard.plugin_api._hermes_cli", side_effect=fake_cli):
            result = _list_notification_methods()

        assert "Slack" in result
        assert "Discord" in result
        slack_entries = result["Slack"]
        assert len(slack_entries) == 1
        assert slack_entries[0] == {"value": "slack:C0B3P2Q39LN", "label": "tasks"}
        discord_entries = result["Discord"]
        assert discord_entries[0] == {"value": "discord:general", "label": "#general"}

    def test_slack_fallback_labels_when_no_resolution(self):
        """Text-parser fallback: labels are raw target strings."""
        from dashboard.plugin_api import _list_notification_methods

        def fake_cli(args, timeout=30):
            if args[:3] == ["send", "--list", "--json"]:
                return -1, ""  # JSON path fails
            if args[:3] == ["send", "--list"]:
                return 0, "Slack:\n  slack:C0B3P2Q39LN"
            return -1, ""

        with mock.patch("dashboard.plugin_api._hermes_cli", side_effect=fake_cli):
            result = _list_notification_methods()

        assert result["Slack"][0] == {
            "value": "slack:C0B3P2Q39LN",
            "label": "slack:C0B3P2Q39LN",
        }


class TestMetaNotificationsNewShapeEndpoint:
    """HTTP-level tests for GET /meta/notifications with new {value, label} shape."""

    @pytest.fixture
    def meta_client(self):
        from dashboard.plugin_api import meta_router
        app = FastAPI()
        app.include_router(meta_router, prefix="/api/plugins/daedalus")
        return TestClient(app)

    def test_endpoint_returns_value_label_shape(self, meta_client):
        """GET /meta/notifications returns {value, label} entries from JSON path."""
        json_out = json.dumps({"platforms": {
            "slack": [{"id": "C0B3P2Q39LN", "name": "tasks"}],
        }})

        def fake_cli(args, timeout=30):
            if args[:3] == ["send", "--list", "--json"]:
                return 0, json_out
            return -1, ""

        with mock.patch("dashboard.plugin_api._hermes_cli", side_effect=fake_cli):
            resp = meta_client.get("/api/plugins/daedalus/meta/notifications")
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert isinstance(data, dict)
        slack_entries = data["Slack"]
        assert isinstance(slack_entries, list)
        assert len(slack_entries) == 1
        assert slack_entries[0]["value"] == "slack:C0B3P2Q39LN"
        assert slack_entries[0]["label"] == "tasks"


# ═══════════════════════════════════════════════════════════════════════════════
# Fix B — Config-edit 404: registry name from resolved daedalus.yaml
# ═══════════════════════════════════════════════════════════════════════════════


class TestRegistryNameResolution:
    """Tests for _build_registry_only_entry using resolved config name."""

    def test_registry_entry_uses_config_name_not_folder_basename(self):
        """When folder basename ≠ config name, the list entry uses config name."""
        from dashboard.plugin_api import _build_registry_only_entry

        # Folder basename is 'webshop' but the config names the project 'webshop.app'
        entry = _build_registry_only_entry(
            "/repos/acme/webshop",
            "webshop.app",  # resolved from daedalus.yaml
        )
        assert entry["name"] == "webshop.app"
        assert entry["repo"] == "/repos/acme/webshop"

    def test_get_projects_resolves_registry_name_from_config(self, client):
        """GET /projects uses the resolved config name, not the folder basename."""
        import tempfile

        # Create a temp repo dir with .hermes/daedalus.yaml where name ≠ folder basename
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            hermes_dir = repo / ".hermes"
            hermes_dir.mkdir()
            cfg = {
                "name": "webshop.app",
                "repo": "ACME-ORG/webshop.app",
                "workdir": str(repo),
            }
            (hermes_dir / "daedalus.yaml").write_text(yaml.dump(cfg))

            with mock.patch("dashboard.plugin_api.registry") as mock_registry:
                mock_registry.list_projects.return_value = [str(repo)]
                with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
                    mock_list.return_value = []

                    resp = client.get("/api/plugins/daedalus/projects")
                    assert resp.status_code == 200, resp.text
                    data = resp.json()

            entry = next(
                (p for p in data if p["name"] == "webshop.app"), None
            )
            assert entry is not None, (
                f"Entry not found in response. Projects: {[p['name'] for p in data]}"
            )
            assert entry["repo"] == "ACME-ORG/webshop.app"

    def test_resolve_project_path_matches_config_name(self):
        """_resolve_project_path finds project by config name, not folder name."""
        import tempfile
        from dashboard.plugin_api import _resolve_project_path

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            hermes_dir = repo / ".hermes"
            hermes_dir.mkdir()
            cfg = {
                "name": "webshop.app",
                "repo": "ACME-ORG/webshop.app",
                "workdir": str(repo),
            }
            (hermes_dir / "daedalus.yaml").write_text(yaml.dump(cfg))

            with mock.patch("dashboard.plugin_api.registry") as mock_registry:
                mock_registry.list_projects.return_value = [str(repo)]

                # Look up by config name — should find it
                resolved = _resolve_project_path("webshop.app")
                # macOS /var is a symlink to /private/var — compare real paths
                assert os.path.realpath(str(resolved)) == os.path.realpath(str(repo))

                # Look up by folder basename — should 404
                with pytest.raises(HTTPException) as exc_info:
                    _resolve_project_path(Path(tmpdir).name)
                assert exc_info.value.status_code == 404

    def test_fallback_to_folder_basename_when_no_config(self, client):
        """When .hermes/daedalus.yaml doesn't exist, fall back to folder basename."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            # No .hermes/daedalus.yaml — just a bare directory

            with mock.patch("dashboard.plugin_api.registry") as mock_registry:
                mock_registry.list_projects.return_value = [str(repo)]
                with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
                    mock_list.return_value = []

                    resp = client.get("/api/plugins/daedalus/projects")
                    assert resp.status_code == 200, resp.text
                    data = resp.json()

            registry_entry = next(
                (p for p in data if p["repo"] == str(repo)), None
            )
            assert registry_entry is not None
            # Falls back to folder basename
            assert registry_entry["name"] == Path(tmpdir).name

    def test_existing_happy_path_still_green(self, client):
        """When folder name == config name, everything still works."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            hermes_dir = repo / ".hermes"
            hermes_dir.mkdir()
            cfg = {
                "name": repo.name,  # same as folder basename
                "repo": "org/test-repo",
                "workdir": str(repo),
            }
            (hermes_dir / "daedalus.yaml").write_text(yaml.dump(cfg))

            with mock.patch("dashboard.plugin_api.registry") as mock_registry:
                mock_registry.list_projects.return_value = [str(repo)]
                with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
                    mock_list.return_value = []

                    resp = client.get("/api/plugins/daedalus/projects")
                    assert resp.status_code == 200, resp.text
                    data = resp.json()

            entry = next(
                (p for p in data if p["name"] == repo.name), None
            )
            assert entry is not None
            assert entry["repo"] == "org/test-repo"


# ═══════════════════════════════════════════════════════════════════════════════
# Authentication — require_dashboard_auth gates every daedalus plugin route (#1130)
# ═══════════════════════════════════════════════════════════════════════════════

_AUTH_TOKEN = "s3cr3t-dashboard-token"
_PROJECTS_URL = "/api/plugins/daedalus/projects"
_RUN_URL = "/api/plugins/daedalus/project/test-project/run"
_CONFIG_URL = "/api/plugins/daedalus/project/test-project/config"
_CREATE_URL = "/api/plugins/daedalus/project/create"
_DELETE_URL = "/api/plugins/daedalus/project/test-project"


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    """Ensure no stray gating secret or auth-disabled flag leaks between tests."""
    monkeypatch.delenv("DAEDALUS_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("DAEDALUS_DASHBOARD_AUTH_DISABLED", raising=False)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_get_route_401_without_token_when_configured(client, monkeypatch):
    """With a secret configured, an unauthenticated GET is rejected 401."""
    monkeypatch.setenv("DAEDALUS_DASHBOARD_TOKEN", _AUTH_TOKEN)
    resp = client.get(_PROJECTS_URL)
    assert resp.status_code == 401, resp.text


def test_get_route_401_with_wrong_token(client, monkeypatch):
    """A mismatched bearer token is rejected 401 (constant-time compare)."""
    monkeypatch.setenv("DAEDALUS_DASHBOARD_TOKEN", _AUTH_TOKEN)
    resp = client.get(_PROJECTS_URL, headers=_bearer("wrong-token"))
    assert resp.status_code == 401, resp.text


def test_get_route_success_with_bearer_token(client, monkeypatch):
    """A correct Authorization: Bearer token authenticates a GET."""
    monkeypatch.setenv("DAEDALUS_DASHBOARD_TOKEN", _AUTH_TOKEN)
    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []
        resp = client.get(_PROJECTS_URL, headers=_bearer(_AUTH_TOKEN))
    assert resp.status_code == 200, resp.text


def test_get_route_success_with_session_header(client, monkeypatch):
    """The Hermes X-Hermes-Session-Token header also authenticates."""
    monkeypatch.setenv("DAEDALUS_DASHBOARD_TOKEN", _AUTH_TOKEN)
    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []
        resp = client.get(_PROJECTS_URL,
                           headers={"X-Hermes-Session-Token": _AUTH_TOKEN})
    assert resp.status_code == 200, resp.text


def test_hermes_dashboard_session_token_env_is_honored(client, monkeypatch):
    """The token the Hermes host injects into the SPA validates unchanged."""
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", _AUTH_TOKEN)
    assert client.get(_PROJECTS_URL).status_code == 401
    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []
        ok = client.get(_PROJECTS_URL, headers=_bearer(_AUTH_TOKEN))
    assert ok.status_code == 200, ok.text


def test_run_mutation_401_without_token(client, monkeypatch):
    """The dispatch-trigger mutation is gated: 401 without a token."""
    monkeypatch.setenv("DAEDALUS_DASHBOARD_TOKEN", _AUTH_TOKEN)
    resp = client.post(_RUN_URL)
    assert resp.status_code == 401, resp.text


def test_run_mutation_passes_gate_with_token(client, monkeypatch):
    """A correct token lets the dispatch-trigger mutation through the gate."""
    monkeypatch.setenv("DAEDALUS_DASHBOARD_TOKEN", _AUTH_TOKEN)
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
    with mock.patch("dashboard.plugin_api.subprocess.run", return_value=fake):
        resp = client.post(_RUN_URL, headers=_bearer(_AUTH_TOKEN))
    assert resp.status_code != 401, resp.text


def test_config_write_mutation_401_without_token(client, monkeypatch):
    """The config-write mutation is gated: 401 without a token."""
    monkeypatch.setenv("DAEDALUS_DASHBOARD_TOKEN", _AUTH_TOKEN)
    resp = client.post(_CONFIG_URL, json={"cron": {"schedule": "15m"}})
    assert resp.status_code == 401, resp.text


def test_create_mutation_401_without_token(client, monkeypatch):
    """The project-create mutation is gated: 401 without a token."""
    monkeypatch.setenv("DAEDALUS_DASHBOARD_TOKEN", _AUTH_TOKEN)
    resp = client.post(_CREATE_URL, json={"name": "x", "workdir": "/tmp/x"})
    assert resp.status_code == 401, resp.text


def test_delete_mutation_401_without_token(client, monkeypatch):
    """The project-delete mutation is gated: 401 without a token."""
    monkeypatch.setenv("DAEDALUS_DASHBOARD_TOKEN", _AUTH_TOKEN)
    resp = client.delete(_DELETE_URL)
    assert resp.status_code == 401, resp.text


def test_no_token_configured_rejects_with_403(client, monkeypatch, caplog):
    """Fail-closed: no secret configured and no explicit opt-in → 403."""
    import logging as _logging

    import dashboard.plugin_api as plugin_api

    # The `client` fixture sets AUTH_DISABLED=1 for functional tests; remove it
    # so we test the true fail-closed default.
    monkeypatch.delenv("DAEDALUS_DASHBOARD_AUTH_DISABLED", raising=False)
    plugin_api._auth_warning_emitted = False
    with caplog.at_level(_logging.WARNING, logger="daedalus.dashboard.auth"):
        with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
            mock_list.return_value = []
            resp = client.get(_PROJECTS_URL)
    assert resp.status_code == 403, resp.text
    assert "auth not configured" in resp.json()["detail"]
    # No "auth disabled" warning should be logged in fail-closed mode.
    assert not any("auth" in r.message.lower() and "disabled" in r.message.lower()
                   for r in caplog.records)


def test_auth_disabled_opt_in_allows_request_and_warns(client, monkeypatch, caplog):
    """DAEDALUS_DASHBOARD_AUTH_DISABLED=1 → request allowed + loud warning."""
    import logging as _logging

    import dashboard.plugin_api as plugin_api

    monkeypatch.setenv("DAEDALUS_DASHBOARD_AUTH_DISABLED", "1")
    plugin_api._auth_warning_emitted = False
    with caplog.at_level(_logging.WARNING, logger="daedalus.dashboard.auth"):
        with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
            mock_list.return_value = []
            resp = client.get(_PROJECTS_URL)
    assert resp.status_code == 200, resp.text
    assert any("EXPLICITLY DISABLED" in r.message for r in caplog.records)

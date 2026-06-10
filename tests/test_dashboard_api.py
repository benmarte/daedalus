"""
Pytest for the Daedalus dashboard plugin API.

Round-trips GET → POST → GET /config against a temporary daedalus.yaml.
Tests validation edge cases (missing fields, invalid types, unknown profiles).
Tests GET /projects endpoint with mocked kanban, gh, and registry.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest
import yaml
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

# Ensure the daedalus package root is importable
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# Import the router (it will import ConfigLoader from config/)
from dashboard.plugin_api import router, DEFAULT_CONFIG_PATH, HERMES_PROFILES_DIR


@pytest.fixture
def temp_config_path():
    """Create a temporary daedalus.yaml and patch DEFAULT_CONFIG_PATH."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as f:
        f.write(
            yaml.dump(
                {
                    "defaults": {
                        "vcs": {"target_branch": "dev"},
                        "lifecycle": {"kanban": {"enabled": True}},
                        "cron": {"schedule": "60m", "deliver": "slack:#engineering"},
                    },
                    "projects": [
                        {
                            "name": "test-project",
                            "repo": "org/test-repo",
                            "workdir": "/tmp/test-workdir",
                            "tracking": {"github_project_number": 1},
                            "execution": {"worker_profile": "developer"},
                            "sources": {"github": {"enabled": True}, "local_specs": {"enabled": False}},
                        }
                    ],
                }
            )
        )
        tmp_path = f.name

    with mock.patch(
        "dashboard.plugin_api.DEFAULT_CONFIG_PATH", Path(tmp_path)
    ):
        yield tmp_path

    Path(tmp_path).unlink(missing_ok=True)


@pytest.fixture
def client(temp_config_path):
    """Create a FastAPI TestClient with the daedalus routers mounted."""
    app = FastAPI()
    app.include_router(router, prefix="/api/plugins/daedalus")
    return TestClient(app)


@pytest.fixture
def populated_profiles_dir():
    """Create a temporary profiles directory with a known profile name."""
    with tempfile.TemporaryDirectory() as tmpdir:
        profiles_dir = Path(tmpdir)
        # Create a known profile so the directory is non-empty
        (profiles_dir / "developer").mkdir()
        with mock.patch(
            "dashboard.plugin_api.HERMES_PROFILES_DIR", profiles_dir
        ):
            yield profiles_dir


# ── Round-trip tests ────────────────────────────────────────────────────────


def test_get_config_returns_resolved_config(client):
    """GET /config returns defaults, projects, and meta."""
    resp = client.get("/api/plugins/daedalus/config")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert "defaults" in data
    assert "projects" in data
    assert "meta" in data
    assert "profiles" in data["meta"]
    assert "slack_targets" in data["meta"]
    assert "path" in data["meta"]

    # Projects should be resolved (merged with defaults)
    projects = data["projects"]
    assert len(projects) == 1
    assert projects[0]["name"] == "test-project"
    assert projects[0]["repo"] == "org/test-repo"
    assert projects[0]["workdir"] == "/tmp/test-workdir"
    # Inherited from defaults
    assert projects[0]["vcs"]["target_branch"] == "dev"


def test_get_config_strips_secrets(client, temp_config_path):
    """GET /config never returns secret keys."""
    # Write a config with a secret field
    raw = yaml.safe_load(Path(temp_config_path).read_text())
    raw["defaults"]["webhook"] = {"enabled": True, "secret": "shhh-dont-leak"}
    Path(temp_config_path).write_text(yaml.dump(raw))

    resp = client.get("/api/plugins/daedalus/config")
    assert resp.status_code == 200
    data = resp.json()

    # The 'secret' key should be stripped from defaults
    defaults = data["defaults"]
    if "webhook" in defaults:
        assert "secret" not in defaults["webhook"]


def test_round_trip_get_post_get(client, temp_config_path):
    """GET → POST (modified) → GET: changes persist."""
    # 1. GET initial config
    resp1 = client.get("/api/plugins/daedalus/config")
    assert resp1.status_code == 200
    initial = resp1.json()

    # 2. Modify and POST back
    payload = {
        "defaults": initial["defaults"],
        "projects": initial["projects"],
    }
    # Add a second project
    payload["projects"].append(
        {
            "name": "second-project",
            "repo": "org/second",
            "workdir": "/tmp/second",
            "execution": {"worker_profile": "developer"},
        }
    )

    resp2 = client.post("/api/plugins/daedalus/config", json=payload)
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["status"] == "saved"

    # 3. GET again — should have two projects
    resp3 = client.get("/api/plugins/daedalus/config")
    assert resp3.status_code == 200
    updated = resp3.json()
    assert len(updated["projects"]) == 2
    names = {p["name"] for p in updated["projects"]}
    assert names == {"test-project", "second-project"}


# ── Validation tests ────────────────────────────────────────────────────────


def _valid_payload():
    """Return a minimal valid POST payload."""
    return {
        "defaults": {},
        "projects": [
            {
                "name": "valid",
                "repo": "org/valid",
                "workdir": "/tmp/valid",
                "execution": {"worker_profile": "developer"},
            }
        ],
    }


def test_post_rejects_missing_name(client):
    """POST rejects a project with no name."""
    payload = _valid_payload()
    payload["projects"][0]["name"] = ""
    resp = client.post("/api/plugins/daedalus/config", json=payload)
    assert resp.status_code == 422
    assert "name" in str(resp.json()["detail"]["errors"]).lower()


def test_post_rejects_missing_repo(client):
    """POST rejects a project with no repo."""
    payload = _valid_payload()
    payload["projects"][0]["repo"] = ""
    resp = client.post("/api/plugins/daedalus/config", json=payload)
    assert resp.status_code == 422
    assert "repo" in str(resp.json()["detail"]["errors"]).lower()


def test_post_rejects_missing_workdir(client):
    """POST rejects a project with no workdir."""
    payload = _valid_payload()
    payload["projects"][0]["workdir"] = ""
    resp = client.post("/api/plugins/daedalus/config", json=payload)
    assert resp.status_code == 422
    assert "workdir" in str(resp.json()["detail"]["errors"]).lower()


def test_post_rejects_non_numeric_github_project_number(client):
    """POST rejects a non-numeric github_project_number."""
    payload = _valid_payload()
    payload["projects"][0]["tracking"] = {"github_project_number": "not-a-number"}
    resp = client.post("/api/plugins/daedalus/config", json=payload)
    assert resp.status_code == 422
    assert "github_project_number" in str(resp.json()["detail"]["errors"]).lower()


def test_post_rejects_unknown_worker_profile(client, populated_profiles_dir):
    """POST rejects a worker_profile that doesn't exist (when profiles dir is populated)."""
    payload = _valid_payload()
    payload["projects"][0]["execution"] = {
        "worker_profile": "nonexistent-profile-xyz"
    }
    resp = client.post("/api/plugins/daedalus/config", json=payload)
    assert resp.status_code == 422
    errors_str = str(resp.json()["detail"]["errors"]).lower()
    assert "worker_profile" in errors_str
    assert "nonexistent-profile-xyz" in errors_str


def test_post_accepts_valid_config(client):
    """POST accepts a valid config and saves it."""
    payload = _valid_payload()
    resp = client.post("/api/plugins/daedalus/config", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "saved"


def test_post_accepts_optional_github_project_number(client):
    """POST accepts a project without github_project_number (kanban-only mode)."""
    payload = _valid_payload()
    # No tracking key at all
    resp = client.post("/api/plugins/daedalus/config", json=payload)
    assert resp.status_code == 200, resp.text


def test_post_accepts_numeric_github_project_number(client):
    """POST accepts a numeric github_project_number."""
    payload = _valid_payload()
    payload["projects"][0]["tracking"] = {"github_project_number": 42}
    resp = client.post("/api/plugins/daedalus/config", json=payload)
    assert resp.status_code == 200, resp.text


# ── GET /projects tests ──────────────────────────────────────────────────────


def _make_kanban_tasks(statuses: list[str]) -> list[dict]:
    """Build mock kanban task dicts with the given statuses."""
    return [
        {"id": f"t_{i:08x}", "title": f"Task {i}", "status": s,
         "summary": f"summary for task {i}", "result": f"result for task {i}"}
        for i, s in enumerate(statuses)
    ]


def test_get_projects_returns_one_entry_per_config_project(client):
    """GET /projects returns one entry for each project in the config."""
    with mock.patch("dashboard.plugin_api.registry") as mock_registry:
        mock_registry.list_projects.return_value = []
        with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
            mock_list.return_value = _make_kanban_tasks(["todo", "todo", "in_progress"])

            resp = client.get("/api/plugins/daedalus/projects")
            assert resp.status_code == 200, resp.text
            data = resp.json()

    assert len(data) == 1
    proj = data[0]
    assert proj["name"] == "test-project"
    assert proj["repo"] == "org/test-repo"
    assert proj["workdir"] == "/tmp/test-workdir"


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


def test_get_projects_kanban_summary_none_on_empty(client):
    """kanban_summary is None when list_tasks returns empty."""
    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []

        resp = client.get("/api/plugins/daedalus/projects")
        assert resp.status_code == 200, resp.text
        data = resp.json()

    assert data[0]["kanban_summary"] is None


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


def test_get_projects_tracking_mode_kanban_without_board(client, temp_config_path):
    """tracking_mode is 'kanban' when github_project_number is absent."""
    raw = yaml.safe_load(Path(temp_config_path).read_text())
    # Remove github_project_number
    raw["projects"][0]["tracking"] = {}
    Path(temp_config_path).write_text(yaml.dump(raw))

    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []

        resp = client.get("/api/plugins/daedalus/projects")
        assert resp.status_code == 200, resp.text
        data = resp.json()

    assert data[0]["tracking_mode"] == "kanban"


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
    """open_prs returns mocked PR data with ci_green field."""
    mock_pr_data = [
        {"number": 42, "title": "Fix auth", "headRefName": "fix/auth", "state": "open"},
        {"number": 43, "title": "Add rate limit", "headRefName": "feat/rate-limit", "state": "open"},
    ]

    with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
        mock_list.return_value = []
        with mock.patch("dashboard.plugin_api._gh_json") as mock_gh:
            mock_gh.return_value = mock_pr_data
            with mock.patch("dashboard.plugin_api.pr_ci_green") as mock_ci:
                mock_ci.side_effect = [True, False]

                resp = client.get("/api/plugins/daedalus/projects")
                assert resp.status_code == 200, resp.text
                data = resp.json()

    prs = data[0]["open_prs"]
    assert prs is not None
    assert prs["count"] == 2
    assert len(prs["prs"]) == 2
    assert prs["prs"][0]["number"] == 42
    assert prs["prs"][0]["ci_green"] is True
    assert prs["prs"][1]["number"] == 43
    assert prs["prs"][1]["ci_green"] is False


def test_get_projects_graceful_degration_when_sources_return_nothing(client):
    """When all data sources return nothing/None, the endpoint still returns 200."""
    with mock.patch("dashboard.plugin_api.list_tasks", None):
        with mock.patch("dashboard.plugin_api._gh_json", None):
            with mock.patch("dashboard.plugin_api.registry", None):
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


def test_get_projects_registry_only_entries(client, temp_config_path):
    """Registry-only repos appear as lightweight entries alongside config projects."""
    # Write a config with one project
    raw = yaml.safe_load(Path(temp_config_path).read_text())
    # Keep one project
    raw["projects"] = [raw["projects"][0]]
    Path(temp_config_path).write_text(yaml.dump(raw))

    with mock.patch("dashboard.plugin_api.registry") as mock_registry:
        mock_registry.list_projects.return_value = [
            "/Users/benmarte/Documents/github/terrasow",
        ]
        with mock.patch("dashboard.plugin_api.list_tasks") as mock_list:
            mock_list.return_value = []

            resp = client.get("/api/plugins/daedalus/projects")
            assert resp.status_code == 200, resp.text
            data = resp.json()

    # Should have 2 entries: one from config, one from registry
    assert len(data) == 2
    registry_entry = next(
        (p for p in data if p["repo"] == "/Users/benmarte/Documents/github/terrasow"),
        None,
    )
    assert registry_entry is not None
    assert registry_entry["name"] == "terrasow"
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
            "execution": {"worker_profile": "developer"},
            "cron": {"schedule": "30m"},
        }
        (hermes_dir / "daedalus.yaml").write_text(yaml.dump(cfg))
        yield repo


@pytest.fixture
def project_client(project_repo_dir):
    """Create a FastAPI TestClient with the project config router mounted."""
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
    assert data["execution"]["worker_profile"] == "developer"


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
    with mock.patch("dashboard.plugin_api.registry") as mock_registry:
        mock_registry.list_projects.return_value = [str(project_repo_dir)]

        payload = {
            "vcs": {"target_branch": "dev"},
            "cron": {"schedule": "15m"},
            "execution": {"worker_profile": "developer"},
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
# _reconcile_cron unit tests — mocked subprocess
# ═══════════════════════════════════════════════════════════════════════════════


class TestReconcileCron:
    """Unit tests for _reconcile_cron with mocked subprocess.run."""

    def _mock_run_ok(self, stdout="", stderr=""):
        return mock.Mock(returncode=0, stdout=stdout, stderr=stderr)

    def _mock_run_fail(self, returncode=1, stderr="error creating cron"):
        return mock.Mock(returncode=returncode, stdout="", stderr=stderr)

    def test_creates_cron_with_schedule_no_deliver(self):
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            mock_run.side_effect = [
                self._mock_run_ok(),                               # remove
                self._mock_run_ok(stdout="created: job j1"),       # create
            ]
            result = _reconcile_cron("test-project", {"schedule": "60m"})

        assert result["cron"] == "created"
        assert result["name"] == "test-project-daedalus"
        assert result["error"] is None

        # Verify the create call args (it's the second subprocess.run call)
        create_call = mock_run.call_args_list[1]
        args = create_call[0][0]
        assert args[0] == "hermes"
        assert args[1] == "cron"
        assert args[2] == "create"
        assert "60m" in args
        assert "--name" in args
        assert "test-project-daedalus" in args
        assert "--script" in args
        assert "daedalus-cron.sh" in args
        assert "--no-agent" in args
        assert "--deliver" not in args  # no deliver set

    def test_creates_cron_with_schedule_and_deliver(self):
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            mock_run.side_effect = [
                self._mock_run_ok(),
                self._mock_run_ok(stdout="created: job j1"),
            ]
            result = _reconcile_cron(
                "test-project",
                {"schedule": "30m", "deliver": "slack:#engineering"},
            )

        assert result["cron"] == "created"
        assert result["error"] is None

        create_call = mock_run.call_args_list[1]
        args = create_call[0][0]
        assert "--deliver" in args
        assert "slack:#engineering" in args

    def test_removes_cron_when_schedule_empty(self):
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            mock_run.side_effect = [
                self._mock_run_ok(),  # remove
                # No create call expected
            ]
            result = _reconcile_cron("test-project", {"schedule": ""})

        assert result["cron"] == "removed"
        assert result["error"] is None
        # Only one call — the remove
        assert mock_run.call_count == 1

    def test_removes_cron_when_cron_cfg_none(self):
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            mock_run.return_value = self._mock_run_ok()
            result = _reconcile_cron("test-project", {})

        assert result["cron"] == "removed"
        assert result["error"] is None
        assert mock_run.call_count == 1

    def test_remove_failure_is_non_fatal(self):
        """Even if 'hermes cron remove' fails, we still attempt create."""
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            mock_run.side_effect = [
                self._mock_run_fail(returncode=1, stderr="not found"),  # remove fails
                self._mock_run_ok(stdout="created: job j1"),            # create works
            ]
            result = _reconcile_cron("test-project", {"schedule": "60m"})

        assert result["cron"] == "created"
        assert result["error"] is None
        assert mock_run.call_count == 2

    def test_create_failure_captures_error(self):
        """A cron create failure is captured, not raised."""
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            mock_run.side_effect = [
                self._mock_run_ok(),
                self._mock_run_fail(returncode=2, stderr="hermes: schedule invalid"),
            ]
            result = _reconcile_cron("test-project", {"schedule": "bad-schedule"})

        assert result["error"] is not None
        assert "schedule invalid" in result["error"]
        # The config is still saved — error is just reported

    def test_hermes_cli_not_found(self):
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            mock_run.side_effect = [
                self._mock_run_ok(),  # remove call (first call uses a different mock_run)
            ]
            # Second call raises FileNotFoundError
            mock_run.side_effect = FileNotFoundError("hermes not on PATH")

            result = _reconcile_cron("test-project", {"schedule": "60m"})

        assert result["error"] == "hermes CLI not found"

    def test_creates_cron_schedule_with_whitespace(self):
        """Schedule with leading/trailing whitespace is stripped."""
        from dashboard.plugin_api import _reconcile_cron

        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            mock_run.side_effect = [
                self._mock_run_ok(),
                self._mock_run_ok(stdout="created: job j1"),
            ]
            result = _reconcile_cron("test-project", {"schedule": "  60m  "})

        assert result["cron"] == "created"
        create_call = mock_run.call_args_list[1]
        args = create_call[0][0]
        assert "60m" in args  # trimmed
        assert "  60m  " not in args


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
            with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
                mock_run.return_value = mock.Mock(
                    returncode=0, stdout="created: job j1", stderr=""
                )

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
            with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
                mock_run.return_value = mock.Mock(
                    returncode=0, stdout="", stderr=""
                )

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

        output = "Slack:\n  slack:tasks\nDiscord:\n  discord:#general"
        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0, stdout=output, stderr=""
            )
            result = _list_notification_methods()
            assert result == {"Slack": ["slack:tasks"], "Discord": ["discord:#general"]}

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
        output = "Slack:\n  slack:tasks\nDiscord:\n  discord:#general"
        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0, stdout=output, stderr=""
            )
            resp = meta_client.get("/api/plugins/daedalus/meta/notifications")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data == {"Slack": ["slack:tasks"], "Discord": ["discord:#general"]}

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
        output = "Slack:\n  slack:tasks (private)\n  slack:#engineering (channel)"
        with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0, stdout=output, stderr=""
            )
            resp = meta_client.get("/api/plugins/daedalus/meta/notifications")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data == {"Slack": ["slack:tasks", "slack:#engineering"]}


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


def test_router_mount_exposes_all_endpoints(temp_config_path, project_repo_dir):
    """Build a FastAPI app, mount the unified router, and assert every endpoint
    group (/config, /projects, /project/{name}/config, /meta/notifications)
    is reachable.

    This is the regression test for the bug where only the /config router was
    mounted and /projects + /project/{name}/config were silently missing.
    """
    from dashboard.plugin_api import router as unified_router

    app = FastAPI()
    app.include_router(unified_router, prefix="/api/plugins/daedalus")
    client = TestClient(app)

    # ── /config (GET) ────────────────────────────────────────────────────
    resp = client.get("/api/plugins/daedalus/config")
    assert resp.status_code == 200, f"GET /config: {resp.status_code} {resp.text}"
    data = resp.json()
    assert "defaults" in data
    assert "projects" in data
    assert "meta" in data

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
    mock_output = "Slack:\n  slack:tasks\n  slack:#engineering"
    with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=0, stdout=mock_output, stderr=""
        )
        resp = client.get("/api/plugins/daedalus/meta/notifications")
        assert resp.status_code == 200, (
            f"GET /meta/notifications: {resp.status_code} {resp.text}"
        )
        notif_data = resp.json()
        assert isinstance(notif_data, dict)
        assert "Slack" in notif_data
        assert notif_data["Slack"] == ["slack:tasks", "slack:#engineering"]


def test_all_sub_routers_mounted_and_resolve(temp_config_path, project_repo_dir):
    """Build a FastAPI app, mount only plugin_api.router, introspect the module
    for all APIRouter instances (config, projects, project_config, meta),
    and assert each one's routes resolve to non-404 responses.

    This is the introspective guard test: even if a router is included in
    the top-level router, some sub-routers could still be silently missing
    (e.g. added as a constant but never include_router'd). This test
    discovers all router instances by name and verifies their routes work.
    """
    import dashboard.plugin_api as papi

    # ── Discover all APIRouter instances in the module ──────────────────
    sub_routers: dict[str, APIRouter] = {}
    expected = {"config_router", "projects_router", "project_config_router", "meta_router"}
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

    # ── Verify top-level router copies all sub-router routes ────────────
    top_router = papi.router
    # Collect all path+method pairs from the top-level router
    top_paths: set[tuple[str, str]] = set()
    for route in top_router.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            for method in route.methods:  # type: ignore[attr-defined]
                top_paths.add((route.path, method))  # type: ignore[attr-defined]

    # Collect all path+method pairs expected from sub-routers
    expected_paths: set[tuple[str, str]] = set()
    for name, sr in sub_routers.items():
        for route in sr.routes:
            if hasattr(route, "methods") and hasattr(route, "path"):
                for method in route.methods:  # type: ignore[attr-defined]
                    expected_paths.add((route.path, method))  # type: ignore[attr-defined]

    missing = expected_paths - top_paths
    assert not missing, (
        f"Expected sub-router routes not found in top-level router: {sorted(missing)}"
    )

    # ── Build app with the unified top-level router ─────────────────────
    app = FastAPI()
    app.include_router(top_router, prefix="/api/plugins/daedalus")
    client = TestClient(app)

    # ── /config (GET) ────────────────────────────────────────────────────
    resp = client.get("/api/plugins/daedalus/config")
    assert resp.status_code == 200, f"/config: {resp.status_code}"

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
    mock_output = "Slack:\n  slack:tasks\nDiscord:\n  discord:#general"
    with mock.patch("dashboard.plugin_api.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=0, stdout=mock_output, stderr=""
        )
        resp = client.get("/api/plugins/daedalus/meta/notifications")
        assert resp.status_code == 200, (
            f"/meta/notifications: {resp.status_code} {resp.text}"
        )
        data = resp.json()
        assert isinstance(data, dict)
        assert "Slack" in data
        assert "Discord" in data

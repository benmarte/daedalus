"""Integration tests for config options flowing through the dispatcher.

Covers realistic end-to-end scenarios where config values resolve and materially
affect dispatcher behavior:
1. ConfigLoader resolves repo config from disk and resolvers accept it
2. Custom profile overrides are applied to created kanban tasks
3. Per-role agent overrides appear in task bodies
4. Label overrides route security before developer
5. coding_agent_max_wait sets the module-level wait ceiling
6. max_dispatch limit is resolvable from resolved config
7. Checklist threshold resolver works with config-driven values
8. Validator retry resolver works with config-driven values

Uses ConfigLoader against a tmp_path repo plus the in-memory FakeKanban/
FakeProvider from conftest. No network, no subprocess.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Stub portalocker before importing the dispatcher.
if "portalocker" not in sys.modules:
    _pl = types.ModuleType("portalocker")
    _pl.lock = lambda *a, **kw: None  # type: ignore[attr-defined]
    _pl.unlock = lambda *a, **kw: None  # type: ignore[attr-defined]
    _pl.LOCK_EX = 1  # type: ignore[attr-defined]
    sys.modules["portalocker"] = _pl

from conftest import FakeKanban, FakeProvider  # noqa: E402
from config import ConfigLoader  # noqa: E402


def _load_dispatch():
    p = _ROOT / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp_intg", str(p))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sd_module():
    """A single dispatcher module instance shared across all tests in this file."""
    return _load_dispatch()


@pytest.fixture
def disp(monkeypatch, sd_module):
    """Yield a freshly-patched dispatcher with safe module-level defaults."""
    monkeypatch.setattr(sd_module, "_CODING_AGENT_MAX_WAIT", 3600)
    return sd_module


def _write_repo(tmp_path: Path, yaml_body: str) -> Path:
    hermes = tmp_path / ".hermes"
    hermes.mkdir(exist_ok=True)
    (hermes / "daedalus.yaml").write_text(yaml_body)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. ConfigLoader resolves real YAML from disk and resolvers consume it
# ---------------------------------------------------------------------------
class TestConfigLoaderIntegration:
    def test_custom_execution_section_merges_into_resolved(self, tmp_path, sd_module):
        _write_repo(
            tmp_path,
            "name: proj\nrepo: o/r\n"
            "execution:\n"
            "  coding_agent: claude-code\n"
            "  max_dispatch: 7\n"
            "  max_validator_retries: 4\n"
            "  checklist_threshold: 10\n"
        )
        resolved = ConfigLoader().resolve_repo_config(str(tmp_path))
        ex = resolved["execution"]
        assert sd_module._resolve_coding_agent(ex) == "claude-code"
        assert sd_module._resolve_max_dispatch(ex) == 7
        assert sd_module._resolve_max_validator_retries(ex) == 4
        assert sd_module._resolve_checklist_threshold(ex) == 10

    def test_sources_toggle_disables_github_issues(self, tmp_path):
        _write_repo(
            tmp_path,
            "name: proj\nrepo: o/r\n"
            "sources:\n"
            "  github_issues:\n"
            "    enabled: false\n",
        )
        resolved = ConfigLoader().resolve_repo_config(str(tmp_path))
        assert resolved["sources"]["github_issues"]["enabled"] is False
        assert resolved["sources"]["local_specs"]["enabled"] is True

    def test_profile_overrides_in_resolved_config(self, tmp_path, sd_module):
        _write_repo(
            tmp_path,
            "name: proj\nrepo: o/r\n"
            "execution:\n"
            "  profiles:\n"
            "    developer: senior-dev\n"
            "    reviewer:\n"
            "      profile: strict-rev\n"
            "      skills: [deep-review]\n",
        )
        resolved = ConfigLoader().resolve_repo_config(str(tmp_path))
        ex = resolved["execution"]
        profiles = sd_module._resolve_profiles(ex)
        assert profiles["developer"] == "senior-dev"
        assert profiles["reviewer"] == "strict-rev"
        skills = sd_module._resolve_role_skills(ex)
        assert skills["reviewer"] == ["deep-review"]


# ---------------------------------------------------------------------------
# 2. max_dispatch resolver applied to resolved config
# ---------------------------------------------------------------------------
class TestMaxDispatchIntegration:
    def test_custom_max_dispatch_from_resolved_yaml(self, tmp_path, sd_module):
        _write_repo(tmp_path,
                    "name: proj\nrepo: o/r\nexecution:\n  max_dispatch: 3\n")
        resolved = ConfigLoader().resolve_repo_config(str(tmp_path))
        md = sd_module._resolve_max_dispatch(resolved.get("execution") or {})
        assert md == 3

    def test_default_when_unconfigured(self, tmp_path, sd_module):
        _write_repo(tmp_path, "name: proj\nrepo: o/r\n")
        resolved = ConfigLoader().resolve_repo_config(str(tmp_path))
        md = sd_module._resolve_max_dispatch(resolved.get("execution") or {})
        assert md == 5  # dispatcher default


# ---------------------------------------------------------------------------
# 3. coding_agent_max_wait sets module-level global and appears in delegation
# ---------------------------------------------------------------------------
class TestCodingAgentMaxWaitIntegration:
    def test_module_global_is_set_from_resolved_config(self, sd_module):
        execution = {"coding_agent_max_wait": 7200}
        sd_module._CODING_AGENT_MAX_WAIT = sd_module._resolve_coding_agent_max_wait(execution)
        assert sd_module._CODING_AGENT_MAX_WAIT == 7200

    def test_delegation_instructions_embed_custom_max_wait(self, disp):
        disp._CODING_AGENT_MAX_WAIT = 7200
        instructions = disp._build_delegation_instructions(
            "claude-code", "", role="developer", issue_number=42,
        )
        assert "7200" in instructions


# ---------------------------------------------------------------------------
# 4. Profile overrides drive created-task assignees via _check_completed_pm
# ---------------------------------------------------------------------------
class TestProfileOverrideIntegration:
    def test_completed_pm_uses_custom_developer_profile(self, monkeypatch, sd_module):
        fk = FakeKanban()
        monkeypatch.setattr(sd_module, "kanban", fk)

        fk.seed(
            assignee="project-manager-daedalus",
            title="#7 Feature",
            status="done",
            summary="spec: do the thing",
        )
        issues_map = {
            7: {"number": 7, "title": "Feature", "body": "", "labels": [],
                "url": "https://example.com/issues/7"},
        }
        custom = dict(sd_module._DEFAULT_PROFILES)
        custom["developer"] = "senior-dev"

        triggered = sd_module._check_completed_pm(
            "proj", "o/r", issues_map, 3, "/tmp", "", "dev", "github",
            profiles=custom,
            coding_agent="none", coding_agent_cmd="", dry_run=False,
        )
        assert 7 in triggered
        dev_card = fk.created_with_key("developer-7")
        assert dev_card is not None
        assert dev_card["assignee"] == "senior-dev"


# ---------------------------------------------------------------------------
# 5. Per-role agent overrides appear in the developer task body
# ---------------------------------------------------------------------------
class TestPerRoleAgentIntegration:
    def test_role_agent_override_appears_in_task_body(self, disp):
        execution = {
            "coding_agent": "hermes",
            "profiles": {"developer": {"agent": "claude-code"}},
        }
        role_agents = {
            role: disp._resolve_agent_for_role(execution, role)
            for role in disp._DEFAULT_PROFILES
        }
        assert role_agents["developer"] == "claude-code"
        assert role_agents["reviewer"] == "hermes"
        body = disp._dev_task_body(
            "o/r",
            {"number": 9, "title": "T", "body": "", "labels": [], "url": ""},
            3, "/tmp", "dev", "github",
            role_agents["developer"], "", profiles=disp._DEFAULT_PROFILES,
        )
        assert "AGENT DELEGATION" in body


# ---------------------------------------------------------------------------
# 6. Label overrides route security before developer
# ---------------------------------------------------------------------------
class TestLabelOverridesIntegration:
    def test_security_first_label_creates_security_card(self, monkeypatch, sd_module):
        fk = FakeKanban()
        monkeypatch.setattr(sd_module, "kanban", fk)
        fk.seed(
            assignee="project-manager-daedalus",
            title="#12 Crypto",
            status="done",
            summary="spec: crypto change",
        )
        issues_map = {
            12: {"number": 12, "title": "Crypto", "body": "",
                 "labels": [{"name": "security-critical"}], "url": ""},
        }
        label_overrides = {"security-critical": {"security_first": True}}
        triggered = sd_module._check_completed_pm(
            "proj", "o/r", issues_map, 3, "/tmp", "", "dev", "github",
            label_overrides=label_overrides,
            coding_agent="none", coding_agent_cmd="", dry_run=False,
        )
        assert 12 in triggered
        sec_card = fk.created_with_key("security-12")
        dev_card = fk.created_with_key("developer-12")
        assert sec_card is not None
        assert dev_card is not None


# ---------------------------------------------------------------------------
# 7. max_validator_retries + checklist_threshold resolvers honor config inputs
# ---------------------------------------------------------------------------
class TestCapResolvers:
    def test_validator_retry_cap_from_resolved_yaml(self, tmp_path, sd_module):
        _write_repo(
            tmp_path,
            "name: proj\nrepo: o/r\nexecution:\n  max_validator_retries: 4\n",
        )
        resolved = ConfigLoader().resolve_repo_config(str(tmp_path))
        ex = resolved.get("execution") or {}
        assert sd_module._resolve_max_validator_retries(ex) == 4

    def test_checklist_threshold_from_resolved_yaml(self, tmp_path, sd_module):
        _write_repo(
            tmp_path,
            "name: proj\nrepo: o/r\nexecution:\n  checklist_threshold: 10\n",
        )
        resolved = ConfigLoader().resolve_repo_config(str(tmp_path))
        ex = resolved.get("execution") or {}
        assert sd_module._resolve_checklist_threshold(ex) == 10

    def test_invalid_values_fall_back_to_defaults(self, sd_module):
        assert sd_module._resolve_checklist_threshold({"checklist_threshold": "many"}) == 5
        assert sd_module._resolve_max_validator_retries({"max_validator_retries": -1}) == 2

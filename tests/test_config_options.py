"""Comprehensive unit tests for all config options.

Covers:
- deep_merge utility (config/__init__.py)
- validate_vcs() validation (config/__init__.py)
- ConfigLoader.resolve_repo_config() (config/__init__.py)
- All resolver functions in scripts/daedalus_dispatch.py:
  _resolve_coding_agent, _resolve_coding_agent_cmd, _resolve_coding_agent_max_turns,
  _resolve_coding_agent_max_wait, _resolve_max_dispatch, _resolve_max_validator_retries,
  _resolve_max_pm_retries, _resolve_history_max_lines, _resolve_stall_minutes,
  _resolve_follow_up_scan_limit, _resolve_checklist_threshold, _resolve_github_issue_limit,
  _resolve_profiles, _resolve_role_skills, _resolve_agent_for_role
- Default values, valid custom values, invalid inputs, edge cases
"""
import sys
import types
import pytest

# ---------------------------------------------------------------------------
# Make project root importable
# ---------------------------------------------------------------------------
_ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import ConfigLoader, deep_merge, validate_vcs, VALID_VCS_PROVIDERS


# ---------------------------------------------------------------------------
# Stub out heavy/OS deps so scripts/daedalus_dispatch.py imports cleanly
# ---------------------------------------------------------------------------
if "portalocker" not in sys.modules:
    _pl = types.ModuleType("portalocker")
    _pl.lock = lambda *a, **kw: None
    _pl.unlock = lambda *a, **kw: None
    _pl.LOCK_EX = 1
    sys.modules["portalocker"] = _pl

if "yaml" not in sys.modules:
    try:
        import yaml as _real_yaml  # noqa: F401
    except ImportError:
        _yaml_stub = types.ModuleType("yaml")
        _yaml_stub.safe_load = lambda *a, **kw: {}
        _yaml_stub.dump = lambda *a, **kw: ""
        sys.modules["yaml"] = _yaml_stub

import scripts  # noqa: E402
# Avoid re-importing if already cached; use importlib.reload if needed
if hasattr(scripts, "daedalus_dispatch"):
    sd = scripts.daedalus_dispatch
else:
    import importlib
    sd = importlib.import_module("scripts.daedalus_dispatch")


# ===========================================================================
# deep_merge (config/__init__.py)
# ===========================================================================
class TestDeepMerge:
    def test_replaces_lists_wholesale(self):
        result = deep_merge({"a": [1, 2, 3]}, {"a": [4, 5]})
        assert result["a"] == [4, 5]

    def test_merges_nested_dicts(self):
        result = deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 99, "z": 3}})
        assert result["a"] == {"x": 1, "y": 99, "z": 3}

    def test_preserves_base_keys_not_overridden(self):
        result = deep_merge({"a": 1, "b": 2, "c": 3}, {"b": 20})
        assert result == {"a": 1, "b": 20, "c": 3}

    def test_does_not_mutate_base(self):
        base = {"a": {"x": 1}, "b": [1, 2]}
        deep_merge(base, {"a": {"y": 2}, "b": [3]})
        assert base == {"a": {"x": 1}, "b": [1, 2]}

    def test_does_not_mutate_override(self):
        override = {"a": {"x": 1}, "b": [1]}
        deep_merge({"a": {"y": 2}}, override)
        assert override == {"a": {"x": 1}, "b": [1]}

    def test_empty_override_returns_base_copy(self):
        base = {"a": 1, "b": {"c": 2}}
        result = deep_merge(base, {})
        assert result == base
        assert result is not base

    def test_empty_base_returns_override_copy(self):
        result = deep_merge({}, {"a": 1, "b": 2})
        assert result == {"a": 1, "b": 2}

    def test_deeply_nested_merge(self):
        base = {"l1": {"l2": {"l3": {"x": 1, "y": 2}}}}
        override = {"l1": {"l2": {"l3": {"y": 99}}}}
        result = deep_merge(base, override)
        assert result["l1"]["l2"]["l3"] == {"x": 1, "y": 99}

    def test_override_dict_replaces_non_dict_value(self):
        result = deep_merge({"a": "string"}, {"a": {"nested": True}})
        assert result["a"] == {"nested": True}

    def test_override_non_dict_replaces_dict_value(self):
        result = deep_merge({"a": {"nested": True}}, {"a": "string"})
        assert result["a"] == "string"

    def test_boolean_values(self):
        result = deep_merge({"flag": True}, {"flag": False})
        assert result["flag"] is False

    def test_null_values_in_override(self):
        result = deep_merge({"a": 1}, {"a": None})
        assert result["a"] is None


# ===========================================================================
# validate_vcs (config/__init__.py)
# ===========================================================================
class TestValidateVcs:
    def test_no_vcs_section_defaults_to_github(self):
        """A missing vcs section defaults to GitHub — no errors if repo is present."""
        errors = validate_vcs({"repo": "owner/repo"})
        assert errors == []

    def test_empty_vcs_section_defaults_to_github(self):
        errors = validate_vcs({"vcs": {}})
        # No repo key → github requires repo
        assert any("github" in e and "repo" in e for e in errors)

    def test_valid_github_provider(self):
        errors = validate_vcs({"vcs": {"provider": "github"}, "repo": "owner/repo"})
        assert errors == []

    def test_valid_gitlab_provider_with_project_path(self):
        errors = validate_vcs({"vcs": {"provider": "gitlab", "project_path": "group/project"}})
        assert errors == []

    def test_valid_gitlab_provider_with_project_id(self):
        errors = validate_vcs({"vcs": {"provider": "gitlab", "project_id": 12345}})
        assert errors == []

    def test_valid_azuredevops_provider_complete(self):
        errors = validate_vcs({
            "vcs": {"provider": "azuredevops", "org": "my-org", "project": "MyProj", "repo": "my-repo"}
        })
        assert errors == []

    def test_invalid_provider(self):
        errors = validate_vcs({"vcs": {"provider": "bitbucket"}})
        assert len(errors) == 1
        assert "bitbucket" in errors[0]
        assert "github" in errors[0]

    def test_github_missing_repo(self):
        errors = validate_vcs({"vcs": {"provider": "github"}})
        assert any("repo" in e for e in errors)

    def test_gitlab_missing_project_id_and_path(self):
        errors = validate_vcs({"vcs": {"provider": "gitlab"}})
        assert any("gitlab" in e for e in errors)

    def test_gitlab_path_without_slash_and_no_id(self):
        errors = validate_vcs({"vcs": {"provider": "gitlab", "project_path": "justproject"}})
        assert any("gitlab" in e for e in errors)

    def test_gitlab_path_with_slash_acceptable(self):
        errors = validate_vcs({"vcs": {"provider": "gitlab", "project_path": "group/project"}})
        assert errors == []

    def test_azuredevops_missing_org(self):
        errors = validate_vcs({
            "vcs": {"provider": "azuredevops", "project": "P", "repo": "R"}
        })
        assert any("org" in e for e in errors)

    def test_azuredevops_missing_project(self):
        errors = validate_vcs({
            "vcs": {"provider": "azuredevops", "org": "O", "repo": "R"}
        })
        assert any("project" in e for e in errors)

    def test_azuredevops_missing_repo(self):
        errors = validate_vcs({
            "vcs": {"provider": "azuredevops", "org": "O", "project": "P"}
        })
        assert any("repo" in e for e in errors)

    def test_azuredevops_all_missing(self):
        errors = validate_vcs({"vcs": {"provider": "azuredevops"}})
        assert len(errors) == 3

    def test_status_map_valid(self):
        errors = validate_vcs({
            "vcs": {
                "provider": "github",
                "status_map": {
                    "ready": "Ready",
                    "in_progress": "In Progress",
                    "in_review": "In Review",
                    "done": "Done",
                }
            },
            "repo": "o/r"
        })
        assert errors == []

    def test_status_map_unknown_key(self):
        errors = validate_vcs({
            "vcs": {"provider": "github", "status_map": {"unknown_status": "Foo"}},
            "repo": "o/r"
        })
        assert any("unknown_status" in e for e in errors)

    def test_status_map_empty_string_value(self):
        errors = validate_vcs({
            "vcs": {"provider": "github", "status_map": {"ready": ""}},
            "repo": "o/r"
        })
        assert any("ready" in e for e in errors)

    def test_status_map_non_string_value(self):
        errors = validate_vcs({
            "vcs": {"provider": "github", "status_map": {"ready": 123}},
            "repo": "o/r"
        })
        assert any("ready" in e for e in errors)

    def test_provider_case_insensitive(self):
        errors = validate_vcs({"vcs": {"provider": "GitHub"}, "repo": "o/r"})
        assert errors == []

    def test_provider_ado_alias(self):
        errors = validate_vcs({
            "vcs": {"provider": "ado", "org": "o", "project": "p", "repo": "r"}
        })
        assert errors == []

    def test_provider_azure_alias(self):
        errors = validate_vcs({
            "vcs": {"provider": "azure", "org": "o", "project": "p", "repo": "r"}
        })
        assert errors == []

    def test_valid_vcs_providers_constant(self):
        assert "github" in VALID_VCS_PROVIDERS
        assert "gitlab" in VALID_VCS_PROVIDERS
        assert "azuredevops" in VALID_VCS_PROVIDERS
        assert len(VALID_VCS_PROVIDERS) == 3


# ===========================================================================
# ConfigLoader.resolve_repo_config (config/__init__.py)
# ===========================================================================
class TestConfigLoader:
    def test_raises_when_config_missing(self, tmp_path):
        loader = ConfigLoader()
        with pytest.raises(FileNotFoundError, match="No daedalus config found"):
            loader.resolve_repo_config(str(tmp_path))

    def test_loads_and_merges_with_defaults(self, tmp_path):
        hermes_dir = tmp_path / ".hermes"
        hermes_dir.mkdir()
        cfg = hermes_dir / "daedalus.yaml"
        cfg.write_text(
            "name: test-project\n"
            "repo: org/test\n"
            "workdir: /tmp\n"
            "vcs:\n"
            "  provider: github\n"
            "  status_map:\n"
            "    ready: Backlog\n"
        )
        loader = ConfigLoader()
        result = loader.resolve_repo_config(str(tmp_path))

        assert result["name"] == "test-project"
        assert result["repo"] == "org/test"
        assert result["workdir"] == str(tmp_path.resolve())
        assert result["vcs"]["provider"] == "github"
        assert result["vcs"]["status_map"]["ready"] == "Backlog"

    def test_workdir_always_pinned_to_repo_path(self, tmp_path):
        hermes_dir = tmp_path / ".hermes"
        hermes_dir.mkdir()
        cfg = hermes_dir / "daedalus.yaml"
        cfg.write_text(
            "name: p\nrepo: o/r\nworkdir: /some/other/path\n"
        )
        result = ConfigLoader().resolve_repo_config(str(tmp_path))
        assert result["workdir"] == str(tmp_path.resolve())

    def test_sources_defaults_present(self, tmp_path):
        hermes_dir = tmp_path / ".hermes"
        hermes_dir.mkdir()
        cfg = hermes_dir / "daedalus.yaml"
        cfg.write_text("name: p\nrepo: o/r\n")
        result = ConfigLoader().resolve_repo_config(str(tmp_path))
        assert "sources" in result
        sources = result["sources"]
        assert sources.get("github_issues", {}).get("enabled") is True
        assert sources.get("local_specs", {}).get("enabled") is True
        assert sources.get("kanban_triage", {}).get("enabled") is True

    def test_sources_toggles_can_disable(self, tmp_path):
        hermes_dir = tmp_path / ".hermes"
        hermes_dir.mkdir()
        cfg = hermes_dir / "daedalus.yaml"
        cfg.write_text(
            "name: p\nrepo: o/r\n"
            "sources:\n"
            "  github_issues:\n"
            "    enabled: false\n"
        )
        result = ConfigLoader().resolve_repo_config(str(tmp_path))
        assert result["sources"]["github_issues"]["enabled"] is False
        # Others remain default
        assert result["sources"]["local_specs"]["enabled"] is True

    def test_execution_section_merges(self, tmp_path):
        hermes_dir = tmp_path / ".hermes"
        hermes_dir.mkdir()
        cfg = hermes_dir / "daedalus.yaml"
        cfg.write_text(
            "name: p\nrepo: o/r\n"
            "execution:\n"
            "  max_dispatch: 3\n"
            "  max_validator_retries: 5\n"
        )
        result = ConfigLoader().resolve_repo_config(str(tmp_path))
        assert result["execution"]["max_dispatch"] == 3
        assert result["execution"]["max_validator_retries"] == 5


# ===========================================================================
# Resolver functions (scripts/daedalus_dispatch.py)
# ===========================================================================
class TestResolveCodingAgent:
    def test_default_is_hermes(self):
        assert sd._resolve_coding_agent(None) == "hermes"

    def test_default_for_empty_dict(self):
        assert sd._resolve_coding_agent({}) == "hermes"

    def test_valid_hermes(self):
        assert sd._resolve_coding_agent({"coding_agent": "hermes"}) == "hermes"

    def test_valid_claude_code(self):
        assert sd._resolve_coding_agent({"coding_agent": "claude-code"}) == "claude-code"

    def test_valid_codex(self):
        assert sd._resolve_coding_agent({"coding_agent": "codex"}) == "codex"

    def test_valid_opencode(self):
        assert sd._resolve_coding_agent({"coding_agent": "opencode"}) == "opencode"

    def test_valid_none(self):
        assert sd._resolve_coding_agent({"coding_agent": "none"}) == "none"

    def test_case_insensitive(self):
        assert sd._resolve_coding_agent({"coding_agent": "Claude-Code"}) == "claude-code"

    def test_invalid_falls_back_to_hermes(self):
        assert sd._resolve_coding_agent({"coding_agent": "invalid"}) == "hermes"

    def test_non_string_type_falls_back(self):
        assert sd._resolve_coding_agent({"coding_agent": 123}) == "hermes"

    def test_whitespace_stripped(self):
        assert sd._resolve_coding_agent({"coding_agent": "  codex  "}) == "codex"


class TestResolveCodingAgentCmd:
    def test_returns_empty_when_missing(self):
        assert sd._resolve_coding_agent_cmd({}) == ""
        assert sd._resolve_coding_agent_cmd(None) == ""

    def test_returns_stripped_cmd(self):
        result = sd._resolve_coding_agent_cmd({"coding_agent_cmd": "  my-cmd --flag  "})
        assert result == "my-cmd --flag"

    def test_non_string_returns_empty(self):
        assert sd._resolve_coding_agent_cmd({"coding_agent_cmd": 123}) == ""

    def test_empty_string_returns_empty(self):
        assert sd._resolve_coding_agent_cmd({"coding_agent_cmd": ""}) == ""


class TestResolveCodingAgentMaxTurns:
    def test_default_when_missing(self):
        result = sd._resolve_coding_agent_max_turns({})
        assert result == sd._DEFAULT_CODING_AGENT_MAX_TURNS

    def test_custom_positive_value(self):
        assert sd._resolve_coding_agent_max_turns({"coding_agent_max_turns": 50}) == 50

    def test_zero_falls_back_to_default(self):
        assert sd._resolve_coding_agent_max_turns({"coding_agent_max_turns": 0}) == sd._DEFAULT_CODING_AGENT_MAX_TURNS

    def test_negative_falls_back_to_default(self):
        assert sd._resolve_coding_agent_max_turns({"coding_agent_max_turns": -5}) == sd._DEFAULT_CODING_AGENT_MAX_TURNS

    def test_non_numeric_falls_back(self):
        assert sd._resolve_coding_agent_max_turns({"coding_agent_max_turns": "abc"}) == sd._DEFAULT_CODING_AGENT_MAX_TURNS

    def test_none_falls_back(self):
        assert sd._resolve_coding_agent_max_turns({"coding_agent_max_turns": None}) == sd._DEFAULT_CODING_AGENT_MAX_TURNS

    def test_string_number_works(self):
        assert sd._resolve_coding_agent_max_turns({"coding_agent_max_turns": "25"}) == 25


class TestResolveCodingAgentMaxWait:
    def test_default_when_missing(self):
        result = sd._resolve_coding_agent_max_wait({})
        assert result == sd._DEFAULT_CODING_AGENT_MAX_WAIT

    def test_custom_positive_value(self):
        assert sd._resolve_coding_agent_max_wait({"coding_agent_max_wait": 600}) == 600

    def test_zero_falls_back(self):
        assert sd._resolve_coding_agent_max_wait({"coding_agent_max_wait": 0}) == sd._DEFAULT_CODING_AGENT_MAX_WAIT

    def test_negative_falls_back(self):
        assert sd._resolve_coding_agent_max_wait({"coding_agent_max_wait": -100}) == sd._DEFAULT_CODING_AGENT_MAX_WAIT

    def test_non_numeric_falls_back(self):
        assert sd._resolve_coding_agent_max_wait({"coding_agent_max_wait": "fast"}) == sd._DEFAULT_CODING_AGENT_MAX_WAIT

    def test_string_number_works(self):
        assert sd._resolve_coding_agent_max_wait({"coding_agent_max_wait": "1800"}) == 1800


class TestResolveMaxDispatch:
    def test_default_is_5(self):
        assert sd._resolve_max_dispatch({}) == 5

    def test_custom_value(self):
        assert sd._resolve_max_dispatch({"max_dispatch": 3}) == 3

    def test_zero_falls_back(self):
        assert sd._resolve_max_dispatch({"max_dispatch": 0}) == 5

    def test_negative_falls_back(self):
        assert sd._resolve_max_dispatch({"max_dispatch": -1}) == 5

    def test_non_numeric_falls_back(self):
        assert sd._resolve_max_dispatch({"max_dispatch": "many"}) == 5

    def test_custom_default_parameter(self):
        assert sd._resolve_max_dispatch({}, default=10) == 10

    def test_string_number_works(self):
        assert sd._resolve_max_dispatch({"max_dispatch": "7"}) == 7


class TestResolveMaxValidatorRetries:
    def test_default_is_2(self):
        assert sd._resolve_max_validator_retries({}) == 2

    def test_custom_value(self):
        assert sd._resolve_max_validator_retries({"max_validator_retries": 5}) == 5

    def test_zero_falls_back(self):
        assert sd._resolve_max_validator_retries({"max_validator_retries": 0}) == 2

    def test_negative_falls_back(self):
        assert sd._resolve_max_validator_retries({"max_validator_retries": -1}) == 2

    def test_non_numeric_falls_back(self):
        assert sd._resolve_max_validator_retries({"max_validator_retries": "lots"}) == 2

    def test_none_falls_back(self):
        assert sd._resolve_max_validator_retries({"max_validator_retries": None}) == 2

    def test_custom_default(self):
        assert sd._resolve_max_validator_retries({}, default=4) == 4


class TestResolveMaxPmRetries:
    def test_default_is_3(self):
        assert sd._resolve_max_pm_retries({}) == 3

    def test_custom_value(self):
        assert sd._resolve_max_pm_retries({"max_pm_retries": 10}) == 10

    def test_zero_falls_back(self):
        assert sd._resolve_max_pm_retries({"max_pm_retries": 0}) == 3

    def test_negative_falls_back(self):
        assert sd._resolve_max_pm_retries({"max_pm_retries": -2}) == 3

    def test_non_numeric_falls_back(self):
        assert sd._resolve_max_pm_retries({"max_pm_retries": "abc"}) == 3

    def test_none_falls_back(self):
        assert sd._resolve_max_pm_retries({"max_pm_retries": None}) == 3

    def test_custom_default(self):
        assert sd._resolve_max_pm_retries({}, default=6) == 6


class TestResolveHistoryMaxLines:
    def test_default_is_1000(self):
        assert sd._resolve_history_max_lines({}) == 1000

    def test_custom_value(self):
        assert sd._resolve_history_max_lines({"history_max_lines": 500}) == 500

    def test_zero_falls_back(self):
        assert sd._resolve_history_max_lines({"history_max_lines": 0}) == 1000

    def test_negative_falls_back(self):
        assert sd._resolve_history_max_lines({"history_max_lines": -100}) == 1000

    def test_non_numeric_falls_back(self):
        assert sd._resolve_history_max_lines({"history_max_lines": "huge"}) == 1000

    def test_none_falls_back(self):
        assert sd._resolve_history_max_lines({"history_max_lines": None}) == 1000

    def test_custom_default(self):
        assert sd._resolve_history_max_lines({}, default=2000) == 2000


class TestResolveStallMinutes:
    def test_default_is_30(self):
        assert sd._resolve_stall_minutes({}) == 30

    def test_seconds_converted_to_minutes(self):
        # 3600 seconds = 60 minutes
        assert sd._resolve_stall_minutes({"dispatch_stale_timeout_seconds": 3600}) == 60

    def test_minutes_fallback_key_treated_as_seconds(self):
        # Minutes-native key is actually interpreted as seconds (45 // 60 = 0, clamped to 1)
        result = sd._resolve_stall_minutes({"dispatch_stale_timeout_minutes": 45})
        assert result == 1  # 45 seconds // 60 = 0, max(1, 0) = 1

    def test_minutes_fallback_key_rounds_down(self):
        # 3599 seconds = 59 minutes (integer division)
        result = sd._resolve_stall_minutes({"dispatch_stale_timeout_minutes": 3599})
        assert result == 59

    def test_seconds_takes_precedence_over_minutes(self):
        result = sd._resolve_stall_minutes({
            "dispatch_stale_timeout_seconds": 1800,
            "dispatch_stale_timeout_minutes": 45
        })
        assert result == 30  # 1800 // 60

    def test_zero_falls_back(self):
        assert sd._resolve_stall_minutes({"dispatch_stale_timeout_seconds": 0}) == 30

    def test_negative_falls_back(self):
        assert sd._resolve_stall_minutes({"dispatch_stale_timeout_seconds": -100}) == 30

    def test_non_numeric_falls_back(self):
        assert sd._resolve_stall_minutes({"dispatch_stale_timeout_seconds": "fast"}) == 30

    def test_none_falls_back(self):
        assert sd._resolve_stall_minutes({"dispatch_stale_timeout_seconds": None}) == 30

    def test_one_second_rounds_to_one_minute(self):
        assert sd._resolve_stall_minutes({"dispatch_stale_timeout_seconds": 1}) == 1

    def test_custom_default(self):
        assert sd._resolve_stall_minutes({}, default=60) == 60


class TestResolveFollowUpScanLimit:
    def test_default_is_50(self):
        assert sd._resolve_follow_up_scan_limit({}) == 50

    def test_custom_value(self):
        assert sd._resolve_follow_up_scan_limit({"scan_pr_limit": 25}) == 25

    def test_zero_falls_back(self):
        assert sd._resolve_follow_up_scan_limit({"scan_pr_limit": 0}) == 50

    def test_negative_falls_back(self):
        assert sd._resolve_follow_up_scan_limit({"scan_pr_limit": -10}) == 50

    def test_non_numeric_falls_back(self):
        assert sd._resolve_follow_up_scan_limit({"scan_pr_limit": "all"}) == 50

    def test_none_falls_back(self):
        assert sd._resolve_follow_up_scan_limit({"scan_pr_limit": None}) == 50

    def test_custom_default(self):
        assert sd._resolve_follow_up_scan_limit({}, default=100) == 100


class TestResolveChecklistThreshold:
    def test_default_is_5(self):
        assert sd._resolve_checklist_threshold({}) == 5

    def test_custom_value(self):
        assert sd._resolve_checklist_threshold({"checklist_threshold": 10}) == 10

    def test_zero_falls_back(self):
        assert sd._resolve_checklist_threshold({"checklist_threshold": 0}) == 5

    def test_negative_falls_back(self):
        assert sd._resolve_checklist_threshold({"checklist_threshold": -3}) == 5

    def test_non_numeric_falls_back(self):
        assert sd._resolve_checklist_threshold({"checklist_threshold": "many"}) == 5

    def test_none_falls_back(self):
        assert sd._resolve_checklist_threshold({"checklist_threshold": None}) == 5

    def test_custom_default(self):
        assert sd._resolve_checklist_threshold({}, default=8) == 8


class TestResolveGitHubIssueLimit:
    def test_default_is_100(self):
        assert sd._resolve_github_issue_limit({}) == 100

    def test_custom_value(self):
        assert sd._resolve_github_issue_limit({"github_issue_limit": 50}) == 50

    def test_zero_falls_back(self):
        assert sd._resolve_github_issue_limit({"github_issue_limit": 0}) == 100

    def test_negative_falls_back(self):
        assert sd._resolve_github_issue_limit({"github_issue_limit": -1}) == 100

    def test_non_numeric_falls_back(self):
        assert sd._resolve_github_issue_limit({"github_issue_limit": "all"}) == 100

    def test_none_falls_back(self):
        assert sd._resolve_github_issue_limit({"github_issue_limit": None}) == 100

    def test_custom_default(self):
        assert sd._resolve_github_issue_limit({}, default=200) == 200


class TestResolveProfiles:
    def test_defaults_returned_when_no_execution(self):
        result = sd._resolve_profiles(None)
        assert result == sd._DEFAULT_PROFILES

    def test_defaults_returned_for_empty_dict(self):
        result = sd._resolve_profiles({})
        assert result == sd._DEFAULT_PROFILES

    def test_string_form_overrides_profile(self):
        result = sd._resolve_profiles({"profiles": {"developer": "my-dev"}})
        assert result["developer"] == "my-dev"
        # Other roles unchanged
        assert result["reviewer"] == sd._DEFAULT_PROFILES["reviewer"]

    def test_dict_form_overrides_profile(self):
        result = sd._resolve_profiles({
            "profiles": {"developer": {"profile": "my-dev", "skills": ["s1"]}}
        })
        assert result["developer"] == "my-dev"

    def test_dict_form_without_profile_key_uses_default(self):
        result = sd._resolve_profiles({
            "profiles": {"developer": {"skills": ["s1"]}}
        })
        # Empty profile string → default is kept
        assert result["developer"] == sd._DEFAULT_PROFILES["developer"]

    def test_unknown_role_keys_ignored(self):
        result = sd._resolve_profiles({"profiles": {"unknown_role": "foo"}})
        assert "unknown_role" not in result

    def test_empty_string_ignored(self):
        result = sd._resolve_profiles({"profiles": {"developer": ""}})
        assert result["developer"] == sd._DEFAULT_PROFILES["developer"]

    def test_whitespace_only_string_ignored(self):
        result = sd._resolve_profiles({"profiles": {"developer": "   "}})
        assert result["developer"] == sd._DEFAULT_PROFILES["developer"]

    def test_non_string_non_dict_ignored(self):
        result = sd._resolve_profiles({"profiles": {"developer": 123}})
        assert result["developer"] == sd._DEFAULT_PROFILES["developer"]


class TestResolveRoleSkills:
    def test_no_skills_when_execution_empty(self):
        assert sd._resolve_role_skills({}) == {}

    def test_no_skills_when_no_profiles(self):
        assert sd._resolve_role_skills({"profiles": {}}) == {}

    def test_string_form_contributes_no_skills(self):
        result = sd._resolve_role_skills({"profiles": {"developer": "my-dev"}})
        assert result == {}

    def test_dict_form_with_skills(self):
        result = sd._resolve_role_skills({
            "profiles": {"developer": {"profile": "dev", "skills": ["s1", "s2"]}}
        })
        assert result["developer"] == ["s1", "s2"]

    def test_dict_form_without_skills_key(self):
        result = sd._resolve_role_skills({
            "profiles": {"developer": {"profile": "dev"}}
        })
        assert result == {}

    def test_empty_skills_list_ignored(self):
        result = sd._resolve_role_skills({
            "profiles": {"developer": {"skills": []}}
        })
        assert result == {}

    def test_non_string_skills_filtered_out(self):
        result = sd._resolve_role_skills({
            "profiles": {"developer": {"skills": ["valid", 123, None, "also-valid"]}}
        })
        assert result["developer"] == ["valid", "also-valid"]

    def test_unknown_role_ignored(self):
        result = sd._resolve_role_skills({
            "profiles": {"nonexistent_role": {"skills": ["s1"]}}
        })
        assert result == {}


class TestResolveAgentForRole:
    def test_falls_back_to_coding_agent_default(self):
        result = sd._resolve_agent_for_role({"coding_agent": "claude-code"}, "developer")
        assert result == "claude-code"

    def test_per_role_agent_override(self):
        execution = {
            "profiles": {
                "developer": {"agent": "claude-code"}
            }
        }
        result = sd._resolve_agent_for_role(execution, "developer")
        assert result == "claude-code"

    def test_per_role_invalid_agent_falls_back_to_coding_agent(self):
        execution = {
            "coding_agent": "codex",
            "profiles": {
                "developer": {"agent": "invalid-agent"}
            }
        }
        result = sd._resolve_agent_for_role(execution, "developer")
        assert result == "codex"

    def test_role_without_profile_entry_uses_coding_agent(self):
        execution = {"coding_agent": "opencode"}
        result = sd._resolve_agent_for_role(execution, "qa")
        assert result == "opencode"


class TestApplyCodingAgentMaxTurns:
    def test_no_op_for_non_claude(self):
        cmd = "codex exec --full-auto"
        result = sd._apply_coding_agent_max_turns("codex", cmd, {})
        assert result == cmd

    def test_appends_max_turns_for_claude(self):
        cmd = "claude --dangerously-skip-permissions -p"
        result = sd._apply_coding_agent_max_turns("claude-code", cmd, {})
        assert "--max-turns" in result

    def test_no_op_when_already_has_max_turns(self):
        cmd = "claude -p --max-turns 50"
        result = sd._apply_coding_agent_max_turns("claude-code", cmd, {})
        assert result == cmd  # unchanged

    def test_uses_default_cmd_when_empty(self):
        # Empty cmd → falls back to the default claude-code command + appends --max-turns
        result = sd._apply_coding_agent_max_turns("claude-code", "", {})
        assert result.startswith("CLAUDE_CONFIG_DIR=$HOME/.claude claude")
        assert "--max-turns" in result

    def test_uses_configured_value(self):
        cmd = "claude -p"
        result = sd._apply_coding_agent_max_turns("claude-code", cmd, {"coding_agent_max_turns": 75})
        assert "--max-turns 75" in result


# ===========================================================================
# Edge cases / integration
# ===========================================================================
class TestEdgeCases:
    def test_deep_merge_preserves_none_values_in_base(self):
        result = deep_merge({"a": None}, {"b": 1})
        assert result == {"a": None, "b": 1}

    def test_deep_merge_with_complex_nested_structure(self):
        base = {
            "execution": {
                "max_dispatch": 5,
                "profiles": {"developer": "default-dev"},
                "forbidden_files": [".env", "*.pem"]
            }
        }
        override = {
            "execution": {
                "max_dispatch": 3,
                "profiles": {"developer": "my-dev", "reviewer": "my-rev"},
            }
        }
        result = deep_merge(base, override)
        assert result["execution"]["max_dispatch"] == 3
        assert result["execution"]["profiles"]["developer"] == "my-dev"
        assert result["execution"]["profiles"]["reviewer"] == "my-rev"
        # List was replaced (not merged)
        assert result["execution"]["forbidden_files"] == [".env", "*.pem"]

    def test_validate_vcs_with_whitespace_only_status(self):
        errors = validate_vcs({
            "vcs": {"provider": "github", "status_map": {"ready": "   "}},
            "repo": "o/r"
        })
        assert any("ready" in e for e in errors)

    def test_all_resolvers_handle_none_execution(self):
        """All resolver functions should handle None/empty execution gracefully."""
        assert sd._resolve_coding_agent_max_turns(None) == sd._DEFAULT_CODING_AGENT_MAX_TURNS
        assert sd._resolve_coding_agent_max_wait(None) == sd._DEFAULT_CODING_AGENT_MAX_WAIT
        assert sd._resolve_max_dispatch(None) == 5
        assert sd._resolve_max_validator_retries(None) == 2
        assert sd._resolve_max_pm_retries(None) == 3
        assert sd._resolve_history_max_lines(None) == 1000
        assert sd._resolve_checklist_threshold(None) == 5
        assert sd._resolve_github_issue_limit(None) == 100

    def test_resolver_large_positive_values(self):
        """Large but valid values should be accepted."""
        assert sd._resolve_max_dispatch({"max_dispatch": 999999}) == 999999
        assert sd._resolve_coding_agent_max_wait({"coding_agent_max_wait": 86400}) == 86400

    def test_default_profiles_constant(self):
        """_DEFAULT_PROFILES should contain all expected roles."""
        expected_roles = {"validator", "pm", "developer", "reviewer", "security", "documentation"}
        assert expected_roles.issubset(set(sd._DEFAULT_PROFILES.keys()))

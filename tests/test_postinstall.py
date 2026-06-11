"""
Tests for scripts/postinstall.py — prerequisite checks and provision invocation.

Verifies:
- Each prereq check fails loudly with actionable guidance when the thing is missing.
- When all prereqs pass, provision_roster.sh is invoked via subprocess.
- Isolated from real ~/.hermes (mock filesystem and subprocess calls).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

# Make the repo root importable so we can load postinstall functions.
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))


# ── load postinstall module ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def postinstall():
    """Import scripts/postinstall.py safely."""
    import importlib.util
    path = _repo_root / "scripts" / "postinstall.py"
    spec = importlib.util.spec_from_file_location("postinstall", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_hermes_home(tmp_path):
    """Patch _HERMES_HOME to a temp dir so tests never touch real ~/.hermes."""
    home = tmp_path / ".hermes"
    home.mkdir()
    # Create a default profile
    (home / "profiles" / "default").mkdir(parents=True)
    (home / "profiles" / "default" / "config.yaml").write_text("model: fake\n")
    # Create agent-skills plugin
    (home / "plugins" / "agent-skills" / "skills" / "using-agent-skills").mkdir(parents=True)
    return home


# ── prereq check tests ───────────────────────────────────────────────────────

class TestCheckDefaultProfile:
    """_check_default_profile returns clear messages."""

    def test_default_present(self, postinstall, tmp_path):
        home = tmp_path / ".hermes"
        (home / "profiles" / "default" / "config.yaml").parent.mkdir(parents=True, exist_ok=True)
        (home / "profiles" / "default" / "config.yaml").write_text("model: test\n")
        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            ok, msg = postinstall._check_default_profile()
        assert ok is True
        assert "OK:" in msg

    def test_default_missing(self, postinstall, tmp_path):
        home = tmp_path / ".hermes_empty"
        home.mkdir()
        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            ok, msg = postinstall._check_default_profile()
        assert ok is False
        assert "MISSING" in msg
        assert "hermes profile create default" in msg.lower() or "hermes setup" in msg.lower()

    def test_default_no_config_yaml(self, postinstall, tmp_path):
        home = tmp_path / ".hermes_noconfig"
        (home / "profiles" / "default").mkdir(parents=True)
        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            ok, msg = postinstall._check_default_profile()
        assert ok is False
        assert "MISSING" in msg

    def test_default_root_config_only(self, postinstall, tmp_path):
        """Root config.yaml (standard install default) passes — no profiles/default/."""
        home = tmp_path / ".hermes_root"
        home.mkdir()
        (home / "config.yaml").write_text("model: root-only\n")
        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            ok, msg = postinstall._check_default_profile()
        assert ok is True
        assert "OK:" in msg
        assert str(home / "config.yaml") in msg


class TestCheckAgentSkills:
    """_check_agent_skills returns clear messages."""

    def test_agent_skills_present(self, postinstall, tmp_path):
        home = tmp_path / ".hermes"
        (home / "plugins" / "agent-skills" / "skills" / "test-skill").mkdir(parents=True)
        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            ok, msg = postinstall._check_agent_skills()
        assert ok is True
        assert "OK:" in msg

    def test_agent_skills_missing(self, postinstall, tmp_path):
        home = tmp_path / ".hermes_no_skills"
        home.mkdir()
        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            ok, msg = postinstall._check_agent_skills()
        assert ok is False
        assert "MISSING" in msg
        assert "hermes plugins install" in msg.lower()
        assert "agent-skills" in msg.lower()

    def test_agent_skills_dir_not_exists(self, postinstall, tmp_path):
        home = tmp_path / ".hermes_noplugins"
        home.mkdir()
        (home / "plugins").mkdir()
        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            ok, msg = postinstall._check_agent_skills()
        assert ok is False
        assert "MISSING" in msg


class TestCheckVcsTokens:
    """_check_vcs_tokens is advisory — never blocks install."""

    def test_token_present(self, postinstall):
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}, clear=False):
            ok, msg = postinstall._check_vcs_tokens()
        assert ok is True
        assert "OK:" in msg and "GITHUB_TOKEN" in msg

    def test_no_tokens_is_advisory(self, postinstall):
        """No tokens must NOT block install — kanban-only setups need none."""
        with mock.patch.dict("os.environ", {}, clear=True):
            ok, msg = postinstall._check_vcs_tokens()
        assert ok is True
        assert "WARN" in msg
        assert "GITLAB_TOKEN" in msg and "AZURE_DEVOPS_PAT" in msg

    def test_multiple_tokens_listed(self, postinstall):
        with mock.patch.dict("os.environ",
                             {"GITLAB_TOKEN": "a", "AZURE_DEVOPS_PAT": "b"},
                             clear=True):
            ok, msg = postinstall._check_vcs_tokens()
        assert ok is True
        assert "GITLAB_TOKEN" in msg and "AZURE_DEVOPS_PAT" in msg


class TestRunProvision:
    """_run_provision invokes the shell script and reports results."""

    def test_provision_success(self, postinstall, tmp_path):
        script_dir = tmp_path / "scripts"
        script_dir.mkdir()
        (script_dir / "provision_roster.sh").write_text(
            "#!/bin/bash\necho '=== project-manager ==='\necho '=== developer ==='\necho 'roster done'\n"
        )
        (script_dir / "provision_roster.sh").chmod(0o755)

        ok, output = postinstall._run_provision(script_dir)
        assert ok is True
        assert "roster done" in output

    def test_provision_failure(self, postinstall, tmp_path):
        script_dir = tmp_path / "scripts"
        script_dir.mkdir()
        (script_dir / "provision_roster.sh").write_text(
            "#!/bin/bash\necho 'something broke' >&2\nexit 1\n"
        )
        (script_dir / "provision_roster.sh").chmod(0o755)

        ok, output = postinstall._run_provision(script_dir)
        assert ok is False
        assert "Provision failed" in output

    def test_provision_script_missing(self, postinstall, tmp_path):
        script_dir = tmp_path / "scripts"
        script_dir.mkdir()
        ok, output = postinstall._run_provision(script_dir)
        assert ok is False
        assert "MISSING" in output

    def test_extract_profiles(self, postinstall):
        output = """
=== project-manager ===
  skills: spec-driven-development ...
=== developer ===
  skills: context-engineering ...
=== roster provisioned ===
"""
        profiles = postinstall._extract_profiles_from_output(output)
        assert profiles == ["project-manager", "developer"]


# ── main() integration tests ─────────────────────────────────────────────────

class TestMain:
    """main() orchestrates checks and provision, returns correct exit codes."""

    def test_all_pass_provision(self, postinstall, tmp_path):
        """All checks pass + provision succeeds → exit 0."""
        home = tmp_path / ".hermes"
        (home / "profiles" / "default" / "config.yaml").parent.mkdir(parents=True, exist_ok=True)
        (home / "profiles" / "default" / "config.yaml").write_text("model: ok\n")
        (home / "plugins" / "agent-skills" / "skills" / "x").mkdir(parents=True)

        script_dir = tmp_path / "scripts"
        script_dir.mkdir()
        (script_dir / "provision_roster.sh").write_text(
            "#!/bin/bash\necho '=== developer ==='\necho 'done'\n"
        )
        (script_dir / "provision_roster.sh").chmod(0o755)

        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            with mock.patch("subprocess.run") as mock_run:
                mock_run.side_effect = [
                    # vcs token check
                    mock.Mock(returncode=0, stderr="Logged in as test\n"),
                    # provision_roster.sh
                    mock.Mock(
                        returncode=0,
                        stdout="=== developer ===\ndone\n",
                        stderr="",
                    ),
                ]
                with mock.patch.object(postinstall.Path, "__init__", lambda s, *a, **kw: None):
                    # Patch Path.__init__ so the script_dir resolution inside main() still works.
                    # Actually, main() resolves script_dir = Path(__file__).resolve().parent
                    # We need to mock that to point at our tmp script_dir.
                    pass

        # Easier approach: mock _run_provision and _check_* directly
        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            with mock.patch.object(postinstall, "_check_vcs_tokens", return_value=(True, "OK")):
                with mock.patch.object(postinstall, "_run_provision", return_value=(True, "=== dev ===\ndone\n")):
                    rc = postinstall.main()
        assert rc == 0

    def test_one_prereq_fails(self, postinstall, tmp_path):
        """One prereq fails → exit 1, no provision invoked."""
        home = tmp_path / ".hermes"
        home.mkdir()

        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            rc = postinstall.main()
        assert rc == 1

    def test_check_only(self, postinstall, tmp_path):
        """--check mode exits 0 after checks, never calls provision."""
        home = tmp_path / ".hermes"
        (home / "profiles" / "default" / "config.yaml").parent.mkdir(parents=True, exist_ok=True)
        (home / "profiles" / "default" / "config.yaml").write_text("model: ok\n")
        (home / "plugins" / "agent-skills" / "skills" / "x").mkdir(parents=True)

        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            with mock.patch.object(postinstall, "_check_vcs_tokens", return_value=(True, "OK")):
                with mock.patch.object(postinstall, "_run_provision") as mock_prov:
                    rc = postinstall.main(check_only=True)
        assert rc == 0
        mock_prov.assert_not_called()

    def test_provision_failure_exit_code(self, postinstall, tmp_path):
        """Provision failure → exit 2."""
        home = tmp_path / ".hermes"
        (home / "profiles" / "default" / "config.yaml").parent.mkdir(parents=True, exist_ok=True)
        (home / "profiles" / "default" / "config.yaml").write_text("model: ok\n")
        (home / "plugins" / "agent-skills" / "skills" / "x").mkdir(parents=True)

        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            with mock.patch.object(postinstall, "_check_vcs_tokens", return_value=(True, "OK")):
                with mock.patch.object(postinstall, "_run_provision",
                                       return_value=(False, "Provision failed")):
                    rc = postinstall.main()
        assert rc == 2

    def test_import_safe(self, postinstall):
        """Module imports without side effects (the fixture already did it)."""
        assert postinstall is not None
        assert hasattr(postinstall, "main")
        assert hasattr(postinstall, "_check_default_profile")
        assert hasattr(postinstall, "_check_agent_skills")
        assert hasattr(postinstall, "_check_vcs_tokens")

"""
Tests for scripts/provision_roster.sh — keychain-free, gh-free provisioning.

Verifies the isolated-home git operations use a per-profile credential store
(never osxkeychain — preventing the macOS "Keychain Not Found" dialog) and that
the script never invokes the gh CLI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def provision_script() -> str:
    """Read the provision_roster.sh script contents."""
    path = _REPO_ROOT / "scripts" / "provision_roster.sh"
    if not path.exists():
        pytest.fail(f"provision_roster.sh not found at {path}")
    return path.read_text()


class TestKeychainFreeProvisioning:
    """The script must authenticate git via a per-profile credential store —
    keychain-free and entirely WITHOUT the gh CLI."""

    def test_credential_helper_is_store(self, provision_script: str):
        """The isolated git config must use the file-based 'store' helper so no
        worker git operation ever reaches osxkeychain."""
        assert 'credential.helper "store"' in provision_script, \
            "Missing: git config --global credential.helper \"store\""

    def test_git_credentials_written_for_github(self, provision_script: str):
        """git push auth comes from ~/.git-credentials with the token inline."""
        assert "x-access-token:%s@github.com" in provision_script, \
            "Missing: github.com entry in the per-profile .git-credentials"

    def test_git_credentials_locked_down(self, provision_script: str):
        assert 'chmod 600 "$home_dir/.git-credentials"' in provision_script
        assert 'chmod 600 "$env_file"' in provision_script
        assert 'chmod 700 "$PROFILES/$name"' in provision_script

    def test_no_gh_cli_anywhere(self, provision_script: str):
        """The roster must not invoke the gh CLI at all (fresh installs don't have it)."""
        assert "gh auth" not in provision_script
        assert "--insecure-storage" not in provision_script
        assert "GH_PROMPT_DISABLED" not in provision_script
        assert not re.search(r"(?<![\w./-])gh\s+(auth|pr|api|issue|project)", provision_script), \
            "found a gh CLI invocation"

    def test_gitlab_and_azure_passthrough(self, provision_script: str):
        """GITLAB_TOKEN / AZURE_DEVOPS_PAT flow into git credentials + .env when set."""
        assert "oauth2:%s@gitlab.com" in provision_script
        assert "pat:%s@dev.azure.com" in provision_script
        assert "GITLAB_TOKEN=" in provision_script
        assert "AZURE_DEVOPS_PAT=" in provision_script

    def test_terminal_env_passthrough_configured(self, provision_script: str):
        """The worker terminal only inherits vars in terminal.env_passthrough
        (default []) — the provisioner must add the provider tokens there or
        agents' API calls would silently see empty tokens."""
        assert "env_passthrough" in provision_script
        for var in ("GITHUB_TOKEN", "GITLAB_TOKEN", "AZURE_DEVOPS_PAT"):
            assert f'"{var}"' in provision_script, f"{var} missing from passthrough setup"


class TestAdvanceHookRegistration:
    """Issue #962: setup_role() must register the session-end advance hook in
    every profile config.yaml, or the pipeline stalls until the hourly cron tick."""

    def test_setup_role_invokes_advance_hook_helper(self, provision_script: str):
        """setup_role must call register_advance_hook.py for each profile."""
        assert "register_advance_hook.py" in provision_script, \
            "setup_role does not register the session-end advance hook"
        assert 'agent-hooks/daedalus-advance.sh' in provision_script, \
            "advance hook path not referenced in provisioning"

    def test_advance_hook_registered_per_profile(self, provision_script: str):
        """The registration runs inside setup_role (per-profile), not once globally."""
        setup_role_start = provision_script.index("setup_role() {")
        # The first role invocation marks the end of the function body.
        setup_role_end = provision_script.index("setup_role validator-daedalus")
        body = provision_script[setup_role_start:setup_role_end]
        assert "register_advance_hook.py" in body, \
            "advance hook registration must live inside setup_role() so every role gets it"

    def test_all_nine_roles_provisioned(self, provision_script: str):
        """Every lifecycle role is provisioned, so none can silently miss the hook."""
        roles = (
            "validator-daedalus",
            "project-manager-daedalus",
            "planner-daedalus",
            "developer-daedalus",
            "reviewer-daedalus",
            "security-analyst-daedalus",
            "qa-daedalus",
            "accessibility-daedalus",
            "documentation-daedalus",
        )
        for role in roles:
            assert f"setup_role {role}" in provision_script, f"{role} not provisioned"

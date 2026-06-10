"""
Tests for scripts/provision_roster.sh — keychain-free provisioning guarantees.

Verifies the isolated-home git/gh operations never invoke osxkeychain, preventing
the macOS "Keychain Not Found" dialog during provisioning or worker operation.
"""

from __future__ import annotations

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
    """The script must set the isolated git credential.helper to empty and use
    --insecure-storage so no worker git/gh operation ever reaches osxkeychain."""

    def test_credential_helper_set_to_empty(self, provision_script: str):
        """After mkdir, the isolated git config must set credential.helper to blank."""
        assert 'credential.helper ""' in provision_script, \
            "Missing: git config --global credential.helper \"\" — required for keychain-free git ops"

    def test_credential_helper_before_gh_auth(self, provision_script: str):
        """credential.helper must be configured BEFORE gh auth login, so gh never
        finds an osxkeychain-triggering git helper in its isolated HOME."""
        cred_hlp_idx = provision_script.find('credential.helper ""')
        gh_login_idx = provision_script.find("gh auth login --with-token")
        assert cred_hlp_idx > 0, "credential.helper line not found"
        assert gh_login_idx > 0, "gh auth login line not found"
        assert cred_hlp_idx < gh_login_idx, (
            "git config credential.helper must run BEFORE gh auth login — "
            "otherwise gh may inherit osxkeychain from the git config"
        )

    def test_gh_uses_insecure_storage(self, provision_script: str):
        """gh auth login must use --insecure-storage (gh 2.93+) so it writes
        file-based creds only, never touching the macOS keychain."""
        assert "--insecure-storage" in provision_script, \
            "Missing: --insecure-storage flag on gh auth login — required for keychain-free auth"

    def test_gh_prompt_disabled(self, provision_script: str):
        """GH_PROMPT_DISABLED=1 must be set so gh never prompts interactively
        (which could trigger credential helper cascades)."""
        assert "GH_PROMPT_DISABLED=1" in provision_script, \
            "Missing: GH_PROMPT_DISABLED=1 env var — required to suppress gh prompts"

    def test_env_vars_strip_gh_token(self, provision_script: str):
        """The gh auth login call must explicitly unset GH_TOKEN and GITHUB_TOKEN
        (env -u) so the pipe-based token injection is the only credential path."""
        assert "-u GH_TOKEN" in provision_script, \
            "Missing: env -u GH_TOKEN — required so gh auth login doesn't inherit host token"
        assert "-u GITHUB_TOKEN" in provision_script, \
            "Missing: env -u GITHUB_TOKEN — required so gh auth login doesn't inherit host token"

    def test_secure_ordering_all_measures_present(self, provision_script: str):
        """Meta-test: all three keychain-free measures appear in the correct order:
        credential.helper → GH_PROMPT_DISABLED → --insecure-storage."""
        cred_idx = provision_script.find('credential.helper ""')
        prompt_idx = provision_script.find("GH_PROMPT_DISABLED=1")
        insecure_idx = provision_script.find("--insecure-storage")
        assert cred_idx < prompt_idx < insecure_idx, (
            "Order must be: credential.helper → GH_PROMPT_DISABLED → --insecure-storage"
        )

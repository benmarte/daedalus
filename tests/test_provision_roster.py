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
        assert 'credential.helper "store"' in provision_script, (
            'Missing: git config --global credential.helper "store"'
        )

    def test_git_credentials_written_for_github(self, provision_script: str):
        """git push auth comes from ~/.git-credentials with the token inline."""
        assert "x-access-token:%s@github.com" in provision_script, (
            "Missing: github.com entry in the per-profile .git-credentials"
        )

    def test_git_credentials_locked_down(self, provision_script: str):
        assert 'chmod 600 "$home_dir/.git-credentials"' in provision_script
        assert 'chmod 600 "$env_file"' in provision_script
        assert 'chmod 700 "$PROFILES/$name"' in provision_script

    def test_no_gh_cli_anywhere(self, provision_script: str):
        """The roster must not invoke the gh CLI at all (fresh installs don't have it)."""
        assert "gh auth" not in provision_script
        assert "--insecure-storage" not in provision_script
        assert "GH_PROMPT_DISABLED" not in provision_script
        assert not re.search(
            r"(?<![\w./-])gh\s+(auth|pr|api|issue|project)", provision_script
        ), "found a gh CLI invocation"

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
            assert f'"{var}"' in provision_script, (
                f"{var} missing from passthrough setup"
            )


class TestPlatformToolsetsPersistence:
    """Issue #1319: the trimmed per-role CLI toolset must be written into each
    profile at provision time so a re-provision reproduces it deterministically
    instead of reverting to the ~20-tool default."""

    # The intended trimmed base — the real lever is platform_toolsets.cli,
    # resolved by kanban_db._resolve_worker_cli_toolsets (NOT top-level toolsets).
    _BASE = (
        "kanban",
        "terminal",
        "file",
        "code_execution",
        "delegation",
        "skills",
        "todo",
        "memory",
    )

    def test_base_toolset_defined_once(self, provision_script: str):
        """The base list is a single source of truth as one bash array."""
        assert provision_script.count("PLATFORM_CLI_TOOLSETS_BASE=(") == 1, (
            "base toolset must be defined exactly once (single source of truth)"
        )
        for tool in self._BASE:
            assert tool in provision_script, f"base tool {tool!r} missing"

    def test_platform_toolsets_cli_written(self, provision_script: str):
        """setup_role writes platform_toolsets.cli (the effective lever), not the
        inert top-level toolsets key."""
        assert (
            'setdefault("platform_toolsets", {})["cli"] = cli_toolsets'
            in provision_script
        )

    def test_browser_gated_to_ui_roles(self, provision_script: str):
        """Only developer + accessibility get the browser tool added on top of
        the base."""
        assert (
            "developer-daedalus|accessibility-daedalus) cli_toolsets+=(browser)"
            in provision_script
        )

    def test_overwrite_not_append_for_idempotency(self, provision_script: str):
        """The cli list is overwritten (assigned), never appended — so re-running
        the script produces no drift or duplication."""
        # An append would look like `.append(` or `+=` on the persisted key;
        # assert the persisted write is a plain assignment.
        assert '["cli"] = cli_toolsets' in provision_script
        assert '["cli"].append' not in provision_script

    def _extract_config_mutation_block(self, script: str) -> str:
        """Pull the embedded python heredoc that mutates env_passthrough +
        platform_toolsets so we can exercise it directly."""
        marker = "cli_toolsets = sys.argv[2:]"
        start = script.index("import sys", script.index(marker) - 200)
        end = script.index("\nPY", start)
        return script[start:end]

    def test_mutation_is_correct_and_idempotent(self, provision_script, tmp_path):
        """Run the real embedded python block against a temp config twice and
        confirm the resulting platform_toolsets.cli matches the intended list
        with no drift on re-run."""
        import subprocess

        code = self._extract_config_mutation_block(provision_script)
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("model: test\n")  # pre-existing unrelated config
        toolsets = list(self._BASE) + ["browser"]

        def run() -> dict:
            subprocess.run(
                ["python3", "-c", code, str(cfg_path), *toolsets],
                check=True,
            )
            import yaml

            return yaml.safe_load(cfg_path.read_text())

        first = run()
        assert first["platform_toolsets"]["cli"] == toolsets
        assert first["model"] == "test"  # unrelated config preserved
        # env_passthrough still configured in the same block
        assert "GITHUB_TOKEN" in first["terminal"]["env_passthrough"]

        second = run()
        assert second == first, "re-run drifted — mutation is not idempotent"

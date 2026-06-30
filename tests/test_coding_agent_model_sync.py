"""Tests for model/provider sync in _resolve_coding_agent_cmd for non-hermes coding agents.

Covers:
- _resolve_active_model_provider: reading Hermes config, graceful fallback on missing/invalid files
- _is_model_compatible_with_coding_agent: compatibility checks for claude-code vs codex/opencode
- _inject_model_into_coding_agent_cmd: --model injection, idempotency (already present), empty cases
- _resolve_coding_agent_cmd: end-to-end behavior for hermes, none, and external agents (claude-code/codex/opencode)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# Ensure plugin root is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import daedalus_dispatch as disp


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_active_model_provider
# ─────────────────────────────────────────────────────────────────────────────


class TestResolveActiveModelProvider:
    """Tests for reading Hermes global model config."""

    def test_returns_none_when_config_missing(self, tmp_path):
        """Missing config.yaml yields None for both fields."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        # No config.yaml file
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            result = disp._resolve_active_model_provider()
        assert result == {"model": None, "provider": None}

    def test_returns_parsed_model_and_provider(self, tmp_path):
        """Parses model.default and model.provider from config.yaml."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "model:\n"
            "  default: anthropic/claude-sonnet-4\n"
            "  provider: anthropic\n"
        )
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            result = disp._resolve_active_model_provider()
        assert result == {"model": "anthropic/claude-sonnet-4", "provider": "anthropic"}

    def test_returns_none_for_empty_fields(self, tmp_path):
        """Empty strings are treated as None."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "model:\n"
            "  default: ''\n"
            "  provider:  \n"
        )
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            result = disp._resolve_active_model_provider()
        assert result == {"model": None, "provider": None}

    def test_returns_none_when_model_block_missing(self, tmp_path):
        """Config without model block yields None for both fields."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text("some_other_key: value\n")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            result = disp._resolve_active_model_provider()
        assert result == {"model": None, "provider": None}

    def test_returns_none_when_config_invalid(self, tmp_path):
        """Invalid YAML content is handled gracefully (returns None)."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        # Write a non-dict value (just a string)
        config_path.write_text("just a plain string\n")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            result = disp._resolve_active_model_provider()
        assert result == {"model": None, "provider": None}

    def test_returns_none_on_read_error(self, tmp_path):
        """I/O errors (e.g. unreadable file) are caught and logged."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text("model:\n  default: test\n")
        # Make file unreadable
        config_path.chmod(0o000)
        try:
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
                result = disp._resolve_active_model_provider()
            assert result == {"model": None, "provider": None}
        finally:
            config_path.chmod(0o644)  # cleanup

    def test_handles_nested_empty_config(self, tmp_path):
        """Empty config file (no model block) yields None for both fields."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text("")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            result = disp._resolve_active_model_provider()
        assert result == {"model": None, "provider": None}


# ─────────────────────────────────────────────────────────────────────────────
# _is_model_compatible_with_coding_agent
# ─────────────────────────────────────────────────────────────────────────────


class TestIsModelCompatibleWithCodingAgent:
    """Tests for compatibility checks between model and agent."""

    def test_empty_model_always_compatible(self):
        """Empty model string is always compatible (no injection needed)."""
        assert disp._is_model_compatible_with_coding_agent("", "claude-code")
        assert disp._is_model_compatible_with_coding_agent("", "codex")
        assert disp._is_model_compatible_with_coding_agent("", "opencode")

    def test_none_model_always_compatible(self):
        """None model is always compatible."""
        assert disp._is_model_compatible_with_coding_agent(None, "claude-code")
        assert disp._is_model_compatible_with_coding_agent(None, "codex")

    def test_claude_code_accepts_claude_model(self):
        """claude-code accepts models with 'claude' in the name."""
        assert disp._is_model_compatible_with_coding_agent("claude-3-7-sonnet", "claude-code")
        assert disp._is_model_compatible_with_coding_agent("anthropic/claude-sonnet-4", "claude-code")

    def test_claude_code_accepts_anthropic_model(self):
        """claude-code accepts models with 'anthropic' in the name."""
        assert disp._is_model_compatible_with_coding_agent("anthropic/something", "claude-code")

    def test_claude_code_rejects_non_claude_model(self):
        """claude-code rejects models without 'claude' or 'anthropic'."""
        assert not disp._is_model_compatible_with_coding_agent("openrouter/qwen3", "claude-code")
        assert not disp._is_model_compatible_with_coding_agent("gpt-4o", "claude-code")

    def test_claude_code_rejection_logs_warning(self, caplog):
        """Rejection emits a warning."""
        import logging
        with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
            result = disp._is_model_compatible_with_coding_agent("openrouter/qwen3", "claude-code")
        assert result is False
        assert any("incompatible" in msg or "not be compatible" in msg
                   for msg in caplog.messages)

    def test_codex_accepts_any_model(self):
        """codex accepts any model name via --model flag."""
        assert disp._is_model_compatible_with_coding_agent("openrouter/qwen3", "codex")
        assert disp._is_model_compatible_with_coding_agent("gpt-4o", "codex")

    def test_opencode_accepts_any_model(self):
        """opencode accepts any model name via --model flag."""
        assert disp._is_model_compatible_with_coding_agent("openrouter/qwen3", "opencode")
        assert disp._is_model_compatible_with_coding_agent("gpt-4o", "opencode")

    def test_case_insensitive_check(self):
        """Compatibility check is case-insensitive."""
        assert disp._is_model_compatible_with_coding_agent("CLAUDE-3", "claude-code")
        assert disp._is_model_compatible_with_coding_agent("Anthropic/Something", "claude-code")


# ─────────────────────────────────────────────────────────────────────────────
# _inject_model_into_coding_agent_cmd
# ─────────────────────────────────────────────────────────────────────────────


class TestInjectModelIntoCodingAgentCmd:
    """Tests for --model flag injection."""

    def test_empty_model_returns_cmd_unchanged(self):
        """No injection when model is None/empty."""
        assert disp._inject_model_into_coding_agent_cmd("claude -p", "claude-code", None) == "claude -p"
        assert disp._inject_model_into_coding_agent_cmd("claude -p", "claude-code", "") == "claude -p"

    def test_empty_cmd_returns_unchanged(self):
        """No injection when cmd is empty."""
        assert disp._inject_model_into_coding_agent_cmd("", "claude-code", "claude-3") == ""
        assert disp._inject_model_into_coding_agent_cmd(None, "claude-code", "claude-3") is None

    def test_appends_model_flag_when_absent(self):
        """Injects --model when not present."""
        result = disp._inject_model_into_coding_agent_cmd("claude -p", "claude-code", "claude-sonnet-4")
        assert result == "claude -p --model claude-sonnet-4"

    def test_does_not_duplicate_when_already_present(self):
        """Skips injection when --model is already in the command."""
        cmd = "claude -p --model claude-3-7-sonnet"
        result = disp._inject_model_into_coding_agent_cmd(cmd, "claude-code", "claude-sonnet-4")
        assert result == cmd  # unchanged

    def test_respects_compatibility_check(self):
        """Skips injection when model is incompatible."""
        cmd = "claude -p"
        result = disp._inject_model_into_coding_agent_cmd(cmd, "claude-code", "openrouter/qwen3")
        assert result == cmd  # unchanged (incompatible)

    def test_appends_model_for_codex(self):
        """Injection works for codex agent."""
        result = disp._inject_model_into_coding_agent_cmd("codex exec --full-auto", "codex", "gpt-4o")
        assert result == "codex exec --full-auto --model gpt-4o"

    def test_appends_model_for_opencode(self):
        """Injection works for opencode agent."""
        result = disp._inject_model_into_coding_agent_cmd("opencode run", "opencode", "openrouter/qwen3")
        assert result == "opencode run --model openrouter/qwen3"


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_coding_agent_cmd — end-to-end
# ─────────────────────────────────────────────────────────────────────────────


class TestResolveCodingAgentCmdModelSync:
    """End-to-end tests for model/provider sync in _resolve_coding_agent_cmd."""

    def test_hermes_agent_unchanged_with_model_config(self, tmp_path):
        """coding_agent=hermes ignores active model (no injection)."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: anthropic/claude-sonnet-4\n"
            "  provider: anthropic\n"
        )
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            result = disp._resolve_coding_agent_cmd(
                {"coding_agent": "hermes", "coding_agent_cmd": "hermes --flag"}
            )
        assert result == "hermes --flag"  # unchanged

    def test_none_agent_unchanged_with_model_config(self, tmp_path):
        """coding_agent=none ignores active model (no injection)."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: anthropic/claude-sonnet-4\n"
            "  provider: anthropic\n"
        )
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            result = disp._resolve_coding_agent_cmd(
                {"coding_agent": "none", "coding_agent_cmd": "my-cmd"}
            )
        assert result == "my-cmd"  # unchanged

    def test_claude_code_injects_model_when_compatible(self, tmp_path):
        """Non-hermes agent injects --model when active model is compatible."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: anthropic/claude-sonnet-4\n"
            "  provider: anthropic\n"
        )
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            result = disp._resolve_coding_agent_cmd(
                {"coding_agent": "claude-code", "coding_agent_cmd": "claude -p"}
            )
        assert result == "claude -p --model anthropic/claude-sonnet-4"

    def test_claude_code_uses_default_cmd_with_injection(self, tmp_path):
        """Non-hermes agent with no coding_agent_cmd uses default + injects model."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: anthropic/claude-sonnet-4\n"
            "  provider: anthropic\n"
        )
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            result = disp._resolve_coding_agent_cmd({"coding_agent": "claude-code"})
        # Uses default cmd and injects model
        assert result.endswith("--model anthropic/claude-sonnet-4")
        assert "claude" in result

    def test_claude_code_skips_injection_for_incompatible_model(self, tmp_path):
        """Non-hermes agent skips injection when model is incompatible."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: openrouter/qwen3\n"
            "  provider: openrouter\n"
        )
        import logging
        with (
            mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}),
            mock.patch("scripts.daedalus_dispatch.logger") as mock_logger,
        ):
            result = disp._resolve_coding_agent_cmd(
                {"coding_agent": "claude-code", "coding_agent_cmd": "claude -p"}
            )
        # Injection skipped due to incompatibility
        assert result == "claude -p"

    def test_claude_code_does_not_duplicate_existing_model(self, tmp_path):
        """Non-hermes agent respects existing --model in command."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: anthropic/claude-sonnet-4\n"
            "  provider: anthropic\n"
        )
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            result = disp._resolve_coding_agent_cmd(
                {
                    "coding_agent": "claude-code",
                    "coding_agent_cmd": "claude -p --model claude-3-7-sonnet",
                }
            )
        # Existing --model is respected; no duplicate
        assert result == "claude -p --model claude-3-7-sonnet"

    def test_graceful_fallback_when_config_missing(self, tmp_path):
        """Missing config → no model injection, command unchanged."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        # No config.yaml file
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            result = disp._resolve_coding_agent_cmd(
                {"coding_agent": "claude-code", "coding_agent_cmd": "claude -p"}
            )
        assert result == "claude -p"

    def test_codex_injects_model(self, tmp_path):
        """codex agent injects --model with any model name."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: gpt-4o\n"
            "  provider: openai\n"
        )
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            result = disp._resolve_coding_agent_cmd(
                {"coding_agent": "codex", "coding_agent_cmd": "codex exec --full-auto"}
            )
        assert result == "codex exec --full-auto --model gpt-4o"

    def test_opencode_injects_model(self, tmp_path):
        """opencode agent injects --model with any model name."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: openrouter/qwen3\n"
            "  provider: openrouter\n"
        )
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            result = disp._resolve_coding_agent_cmd(
                {"coding_agent": "opencode", "coding_agent_cmd": "opencode run"}
            )
        assert result == "opencode run --model openrouter/qwen3"

    def test_default_coding_agent_hermes_unchanged(self, tmp_path):
        """Default coding_agent is 'hermes', no injection occurs."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: anthropic/claude-sonnet-4\n"
        )
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            result = disp._resolve_coding_agent_cmd({"coding_agent_cmd": "my-cmd"})
        # Default is hermes, so no injection
        assert result == "my-cmd"

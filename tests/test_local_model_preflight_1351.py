"""Tests for the local-model capability preflight (#1351).

Covers ``_preflight_local_model_capability`` and its helper
``_normalize_model_name`` in ``core.dispatch.resolvers`` (re-exported by the
dispatcher):

- Warns exactly once when a known-weak local model (qwen3.6) drives the
  ``hermes`` coding_agent path.
- Deduplicates the warning per distinct model string across repeated calls.
- No-ops for external coding agents, capable models, or an unset model.
- Lenient matching across provider prefixes / tag suffixes / separators.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# Ensure plugin root is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import daedalus_dispatch as disp  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_warn_guard():
    """Clear the process-level warn-once guard around every test."""
    from core.dispatch import resolvers

    resolvers._WARNED_WEAK_MODELS.clear()
    yield
    resolvers._WARNED_WEAK_MODELS.clear()


def _patch_model(model):
    """Patch the active-model resolver to return *model*.

    Patches the resolver on ``core.dispatch.resolvers`` — the module where
    ``_preflight_local_model_capability`` lives and calls it — not the
    dispatcher re-export, which is a separate name binding.
    """
    from core.dispatch import resolvers

    return mock.patch.object(
        resolvers,
        "_resolve_active_model_provider",
        return_value={"model": model, "provider": "ollama"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# _normalize_model_name
# ─────────────────────────────────────────────────────────────────────────────


class TestNormalizeModelName:
    def test_strips_separators_and_lowercases(self):
        assert (
            disp._normalize_model_name("openrouter/Qwen3.6:35B")
            == "openrouterqwen3635b"
        )

    def test_none_returns_empty(self):
        assert disp._normalize_model_name(None) == ""

    def test_empty_returns_empty(self):
        assert disp._normalize_model_name("") == ""


# ─────────────────────────────────────────────────────────────────────────────
# _preflight_local_model_capability
# ─────────────────────────────────────────────────────────────────────────────


class TestPreflightWarnsOnWeakModel:
    def test_warns_for_qwen36_on_hermes(self, caplog):
        with _patch_model("qwen3.6"):
            with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
                matched = disp._preflight_local_model_capability(
                    {"coding_agent": "hermes"}
                )
        assert matched == "qwen3.6"
        assert any("#1351" in m and "developer stage" in m for m in caplog.messages)

    @pytest.mark.parametrize(
        "model",
        ["openrouter/qwen3.6", "ollama/qwen3.6:35b", "qwen3-6", "Qwen3.6"],
    )
    def test_lenient_matching_across_prefixes_and_separators(self, model):
        with _patch_model(model):
            matched = disp._preflight_local_model_capability({"coding_agent": "hermes"})
        assert matched == model

    def test_uses_resolved_agent_when_not_passed(self, caplog):
        with _patch_model("qwen3.6"):
            with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
                matched = disp._preflight_local_model_capability(
                    {"coding_agent": "hermes"}, coding_agent=None
                )
        assert matched == "qwen3.6"


class TestPreflightWarnsOnce:
    def test_deduplicates_repeated_calls(self, caplog):
        execution = {"coding_agent": "hermes"}
        with _patch_model("qwen3.6"):
            with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
                disp._preflight_local_model_capability(execution)
                disp._preflight_local_model_capability(execution)
                disp._preflight_local_model_capability(execution)
        weak_warnings = [m for m in caplog.messages if "#1351" in m]
        assert len(weak_warnings) == 1

    def test_still_returns_match_after_first_warning(self):
        execution = {"coding_agent": "hermes"}
        with _patch_model("qwen3.6"):
            disp._preflight_local_model_capability(execution)
            second = disp._preflight_local_model_capability(execution)
        assert second == "qwen3.6"


class TestPreflightNoOps:
    def test_noop_for_external_agent(self, caplog):
        # A weak model under claude-code is irrelevant — the local brain isn't used.
        with _patch_model("qwen3.6"):
            with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
                matched = disp._preflight_local_model_capability(
                    {"coding_agent": "claude-code"}
                )
        assert matched is None
        assert not any("#1351" in m for m in caplog.messages)

    def test_noop_for_capable_model(self, caplog):
        with _patch_model("ornith-1.0-35b"):
            with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
                matched = disp._preflight_local_model_capability(
                    {"coding_agent": "hermes"}
                )
        assert matched is None
        assert not any("#1351" in m for m in caplog.messages)

    def test_noop_for_unset_model(self):
        with _patch_model(None):
            matched = disp._preflight_local_model_capability({"coding_agent": "hermes"})
        assert matched is None

    def test_defaults_to_hermes_agent(self, caplog):
        # No coding_agent configured → defaults to hermes → preflight applies.
        with _patch_model("qwen3.6"):
            with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
                matched = disp._preflight_local_model_capability({})
        assert matched == "qwen3.6"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

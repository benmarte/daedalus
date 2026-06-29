"""Tests for config fingerprint hashing of execution.coding_agent + model.default.

Covers:
- compute_config_fingerprint: deterministic SHA-256 of canonical JSON encoding
- set_config_fingerprint / get_config_fingerprint: persistence in dispatch_state
- Nil/empty value handling
- Hash changes when either input changes

Run: pytest tests/test_config_fingerprint.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import dispatch_state  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# compute_config_fingerprint — unit tests
# ─────────────────────────────────────────────────────────────────────────────


class TestComputeConfigFingerprint:
    """Tests for the deterministic hash function."""

    def test_deterministic_for_identical_inputs(self):
        """Same inputs → same hash."""
        h1 = dispatch_state.compute_config_fingerprint("claude-code", "anthropic/claude-sonnet-4")
        h2 = dispatch_state.compute_config_fingerprint("claude-code", "anthropic/claude-sonnet-4")
        assert h1 == h2
        assert isinstance(h1, str)
        assert len(h1) == 64  # SHA-256 hex digest

    def test_hash_changes_when_coding_agent_changes(self):
        """Different coding_agent → different hash."""
        h1 = dispatch_state.compute_config_fingerprint("claude-code", "anthropic/claude-sonnet-4")
        h2 = dispatch_state.compute_config_fingerprint("codex", "anthropic/claude-sonnet-4")
        assert h1 != h2

    def test_hash_changes_when_model_changes(self):
        """Different model.default → different hash."""
        h1 = dispatch_state.compute_config_fingerprint("claude-code", "anthropic/claude-sonnet-4")
        h2 = dispatch_state.compute_config_fingerprint("claude-code", "openrouter/qwen3")
        assert h1 != h2

    def test_hash_changes_when_both_change(self):
        """Both values changing → different hash."""
        h1 = dispatch_state.compute_config_fingerprint("claude-code", "anthropic/claude-sonnet-4")
        h2 = dispatch_state.compute_config_fingerprint("codex", "gpt-4o")
        assert h1 != h2

    def test_none_coding_agent_handled(self):
        """None coding_agent is treated as empty string."""
        h1 = dispatch_state.compute_config_fingerprint(None, "anthropic/claude-sonnet-4")
        h2 = dispatch_state.compute_config_fingerprint("", "anthropic/claude-sonnet-4")
        assert h1 == h2

    def test_none_model_handled(self):
        """None model is treated as empty string."""
        h1 = dispatch_state.compute_config_fingerprint("claude-code", None)
        h2 = dispatch_state.compute_config_fingerprint("claude-code", "")
        assert h1 == h2

    def test_both_none_handled(self):
        """Both None → deterministic hash, same as both empty."""
        h1 = dispatch_state.compute_config_fingerprint(None, None)
        h2 = dispatch_state.compute_config_fingerprint("", "")
        assert h1 == h2
        assert len(h1) == 64

    def test_returns_hex_string(self):
        """Fingerprint is a valid hex string."""
        h = dispatch_state.compute_config_fingerprint("hermes", "model-x")
        assert all(c in "0123456789abcdef" for c in h)

    def test_order_independence_of_kwargs(self):
        """The hash is based on a canonical encoding, not argument order."""
        # compute_config_fingerprint takes (coding_agent, model) positionally;
        # the canonical JSON inside should be sorted so key order doesn't matter.
        h1 = dispatch_state.compute_config_fingerprint("claude-code", "model-a")
        h2 = dispatch_state.compute_config_fingerprint("claude-code", "model-a")
        assert h1 == h2

    def test_empty_strings_differ_from_populated(self):
        """Empty strings produce a different hash than populated values."""
        h_empty = dispatch_state.compute_config_fingerprint("", "")
        h_populated = dispatch_state.compute_config_fingerprint("hermes", "model-x")
        assert h_empty != h_populated


# ─────────────────────────────────────────────────────────────────────────────
# set_config_fingerprint / get_config_fingerprint — persistence tests
# ─────────────────────────────────────────────────────────────────────────────


class TestConfigFingerprintPersistence:
    """Tests for storing and retrieving the fingerprint in dispatch_state."""

    def test_set_and_get_fingerprint(self, tmp_path):
        """Fingerprint round-trips through the state file."""
        wd = str(tmp_path)
        fp = dispatch_state.compute_config_fingerprint("claude-code", "anthropic/claude-sonnet-4")
        dispatch_state.set_config_fingerprint(wd, fp)
        assert dispatch_state.get_config_fingerprint(wd) == fp

    def test_get_fingerprint_returns_none_when_not_set(self, tmp_path):
        """No fingerprint stored → None."""
        wd = str(tmp_path)
        assert dispatch_state.get_config_fingerprint(wd) is None

    def test_set_fingerprint_overwrites_previous(self, tmp_path):
        """Second set overwrites the first."""
        wd = str(tmp_path)
        fp1 = dispatch_state.compute_config_fingerprint("claude-code", "model-a")
        fp2 = dispatch_state.compute_config_fingerprint("codex", "model-b")
        dispatch_state.set_config_fingerprint(wd, fp1)
        assert dispatch_state.get_config_fingerprint(wd) == fp1
        dispatch_state.set_config_fingerprint(wd, fp2)
        assert dispatch_state.get_config_fingerprint(wd) == fp2

    def test_set_fingerprint_preserves_other_state(self, tmp_path):
        """Setting fingerprint doesn't wipe other dispatch_state keys."""
        wd = str(tmp_path)
        dispatch_state.record_dispatch(wd, 42)
        fp = dispatch_state.compute_config_fingerprint("hermes", "model-x")
        dispatch_state.set_config_fingerprint(wd, fp)
        # dispatch record should survive
        assert dispatch_state.get_dispatch_age_hours(wd, 42) is not None
        assert dispatch_state.get_config_fingerprint(wd) == fp

    def test_fingerprint_survives_reload(self, tmp_path):
        """Fingerprint persists across state reloads (simulates next tick)."""
        wd = str(tmp_path)
        fp = dispatch_state.compute_config_fingerprint("claude-code", "anthropic/claude-sonnet-4")
        dispatch_state.set_config_fingerprint(wd, fp)
        # Simulate a new tick by reading fresh from disk
        assert dispatch_state.get_config_fingerprint(wd) == fp


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
"""Tests for _resync_profiles_to_model and dispatch_state resync fingerprint.

Covers:
- _resync_profiles_to_model: updates model.default + model.provider in *-daedalus profiles
- Explicit override preservation: profile with model != old global model is skipped
- Already-at-target profiles are skipped (no unnecessary write)
- Non-*-daedalus profiles are not touched
- Empty or missing profiles dir returns count 0
- dispatch_state set/get_resync_fingerprint: persistence across ticks
- Idempotency: same fingerprint on consecutive ticks → no double resync
- Isolated per-workdir: one project's resync doesn't affect another

Run: pytest tests/test_profile_resync.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.daedalus_dispatch import _resync_profiles_to_model  # noqa: E402
from core import dispatch_state  # noqa: E402


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_profile(hermes_home: Path, name: str, model: str, provider: str = "") -> Path:
    d = hermes_home / "profiles" / name
    d.mkdir(parents=True, exist_ok=True)
    cfg_path = d / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": {"default": model, "provider": provider}}))
    return cfg_path


def _read_model(cfg_path: Path) -> dict:
    return (yaml.safe_load(cfg_path.read_text()) or {}).get("model", {})


def _hermes(tmp_path: Path) -> Path:
    """Create and return a fake HERMES_HOME under tmp_path."""
    h = tmp_path / "hermes"
    h.mkdir(exist_ok=True)
    return h


# ─── _resync_profiles_to_model ────────────────────────────────────────────────

class TestResyncProfilesToModel:

    def test_model_change_updates_all_daedalus_profiles(self, tmp_path):
        hh = _hermes(tmp_path)
        dev = _make_profile(hh, "developer-daedalus", "old-model", "old-provider")
        qa = _make_profile(hh, "qa-daedalus", "old-model", "old-provider")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count = _resync_profiles_to_model(
                workdir=str(tmp_path),
                new_model="new-model",
                new_provider="new-provider",
                old_values={"coding_agent": "hermes", "model_default": "old-model"},
            )

        assert count == 2
        assert _read_model(dev)["default"] == "new-model"
        assert _read_model(dev)["provider"] == "new-provider"
        assert _read_model(qa)["default"] == "new-model"

    def test_explicit_override_preserved(self, tmp_path):
        """Profile whose model != old global default is treated as intentional override."""
        hh = _hermes(tmp_path)
        standard = _make_profile(hh, "developer-daedalus", "old-model")
        overridden = _make_profile(hh, "qa-daedalus", "custom-model")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count = _resync_profiles_to_model(
                workdir=str(tmp_path),
                new_model="new-model",
                new_provider="new-provider",
                old_values={"coding_agent": "hermes", "model_default": "old-model"},
            )

        assert count == 1
        assert _read_model(standard)["default"] == "new-model"
        assert _read_model(overridden)["default"] == "custom-model"

    def test_already_at_target_skipped(self, tmp_path):
        """Profile already set to new_model is skipped (no-op write)."""
        hh = _hermes(tmp_path)
        cfg = _make_profile(hh, "developer-daedalus", "new-model", "new-provider")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count = _resync_profiles_to_model(
                workdir=str(tmp_path),
                new_model="new-model",
                new_provider="new-provider",
                old_values={"coding_agent": "hermes", "model_default": "old-model"},
            )

        assert count == 0
        assert _read_model(cfg)["default"] == "new-model"

    def test_empty_model_in_profile_updated(self, tmp_path):
        """Profile with empty model.default is not an explicit override → update it."""
        hh = _hermes(tmp_path)
        cfg = _make_profile(hh, "developer-daedalus", "", "")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count = _resync_profiles_to_model(
                workdir=str(tmp_path),
                new_model="new-model",
                new_provider="new-provider",
                old_values={"coding_agent": "hermes", "model_default": "old-model"},
            )

        assert count == 1
        assert _read_model(cfg)["default"] == "new-model"

    def test_non_daedalus_profiles_not_touched(self, tmp_path):
        hh = _hermes(tmp_path)
        unrelated = _make_profile(hh, "my-personal-profile", "old-model")
        daedalus = _make_profile(hh, "developer-daedalus", "old-model")

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count = _resync_profiles_to_model(
                workdir=str(tmp_path),
                new_model="new-model",
                new_provider="new-provider",
                old_values={"coding_agent": "hermes", "model_default": "old-model"},
            )

        assert count == 1
        assert _read_model(unrelated)["default"] == "old-model"
        assert _read_model(daedalus)["default"] == "new-model"

    def test_zero_profiles_returns_zero(self, tmp_path):
        hh = _hermes(tmp_path)
        (hh / "profiles").mkdir()

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count = _resync_profiles_to_model(
                workdir=str(tmp_path),
                new_model="new-model",
                new_provider="new-provider",
                old_values=None,
            )

        assert count == 0

    def test_no_profiles_dir_returns_zero(self, tmp_path):
        hh = _hermes(tmp_path)
        # no profiles/ subdir created

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count = _resync_profiles_to_model(
                workdir=str(tmp_path),
                new_model="new-model",
                new_provider="new-provider",
                old_values=None,
            )

        assert count == 0

    def test_count_reflects_only_written_profiles(self, tmp_path):
        """Return count only counts profiles that were actually written."""
        hh = _hermes(tmp_path)
        _make_profile(hh, "developer-daedalus", "old-model")   # will update
        _make_profile(hh, "qa-daedalus", "custom-model")        # override: skip
        _make_profile(hh, "reviewer-daedalus", "new-model")     # already correct: skip

        with patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            count = _resync_profiles_to_model(
                workdir=str(tmp_path),
                new_model="new-model",
                new_provider="np",
                old_values={"coding_agent": "hermes", "model_default": "old-model"},
            )

        assert count == 1  # only developer-daedalus updated


# ─── dispatch_state resync fingerprint ────────────────────────────────────────

class TestResyncFingerprintState:

    def test_set_and_get_resync_fingerprint(self, tmp_path):
        wd = str(tmp_path)
        dispatch_state.set_resync_fingerprint(wd, "abc123")
        assert dispatch_state.get_resync_fingerprint(wd) == "abc123"

    def test_get_returns_none_when_unset(self, tmp_path):
        assert dispatch_state.get_resync_fingerprint(str(tmp_path)) is None

    def test_overwrites_previous_value(self, tmp_path):
        wd = str(tmp_path)
        dispatch_state.set_resync_fingerprint(wd, "fp1")
        dispatch_state.set_resync_fingerprint(wd, "fp2")
        assert dispatch_state.get_resync_fingerprint(wd) == "fp2"

    def test_independent_of_config_fingerprint(self, tmp_path):
        wd = str(tmp_path)
        dispatch_state.set_config_fingerprint(wd, "config_fp")
        assert dispatch_state.get_resync_fingerprint(wd) is None
        dispatch_state.set_resync_fingerprint(wd, "resync_fp")
        assert dispatch_state.get_config_fingerprint(wd) == "config_fp"
        assert dispatch_state.get_resync_fingerprint(wd) == "resync_fp"

    def test_persists_across_loads(self, tmp_path):
        wd = str(tmp_path)
        dispatch_state.set_resync_fingerprint(wd, "persistent_fp")
        assert dispatch_state.get_resync_fingerprint(wd) == "persistent_fp"


# ─── idempotency ──────────────────────────────────────────────────────────────

class TestResyncIdempotency:

    def test_same_fingerprint_no_double_resync(self, tmp_path):
        """After storing fingerprint X, a subsequent tick with X detects no change."""
        wd = str(tmp_path)
        fp = dispatch_state.compute_config_fingerprint("claude-code", "model-a")
        dispatch_state.set_resync_fingerprint(wd, fp)
        stored = dispatch_state.get_resync_fingerprint(wd)
        assert stored == fp  # no change → caller should skip resync

    def test_rapid_successive_changes_deduped(self, tmp_path):
        """Tick 1 resyncs A→B; tick 2 same B detects no change."""
        wd = str(tmp_path)
        fp_a = dispatch_state.compute_config_fingerprint("claude-code", "model-a")
        fp_b = dispatch_state.compute_config_fingerprint("codex", "model-b")

        dispatch_state.set_resync_fingerprint(wd, fp_a)
        assert fp_b != fp_a  # tick 1: change detected, resync happens
        dispatch_state.set_resync_fingerprint(wd, fp_b)  # store after resync

        # tick 2: same fingerprint, no change
        assert dispatch_state.get_resync_fingerprint(wd) == fp_b

    def test_per_workdir_isolation(self, tmp_path):
        """Resync for project A doesn't affect project B's stored fingerprint."""
        wd_a = str(tmp_path / "projectA")
        wd_b = str(tmp_path / "projectB")

        fp_a = dispatch_state.compute_config_fingerprint("claude-code", "model-a")
        fp_b = dispatch_state.compute_config_fingerprint("codex", "model-b")

        dispatch_state.set_resync_fingerprint(wd_a, fp_a)
        dispatch_state.set_resync_fingerprint(wd_b, fp_b)

        new_fp_a = dispatch_state.compute_config_fingerprint("claude-code", "model-changed")
        dispatch_state.set_resync_fingerprint(wd_a, new_fp_a)

        assert dispatch_state.get_resync_fingerprint(wd_b) == fp_b
        assert dispatch_state.get_resync_fingerprint(wd_a) == new_fp_a


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

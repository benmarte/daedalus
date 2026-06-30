"""Tests for profile model/provider fallback to global config.

These tests verify that _resolve_active_model_provider correctly reads the
Hermes global config and returns model/provider values.

Run: pytest tests/test_profile_model_fallback.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


@pytest.fixture(scope="module")
def dispatch():
    """Load scripts/daedalus_dispatch.py directly by file path."""
    if SCRIPTS not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "daedalus_dispatch_tests_fallback", SCRIPTS / "daedalus_dispatch.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_config(path: Path, default: str | None = None, provider: str | None = None):
    """Write a config.yaml with the given model block."""
    block: dict = {}
    if default or provider:
        block = {"model": {}}
        if default:
            block["model"]["default"] = default
        if provider:
            block["model"]["provider"] = provider
    path.write_text(yaml.safe_dump(block))


# ─────────────────────────────────────────────────────────────────────────────
# Test: _resolve_active_model_provider reads config correctly
# ─────────────────────────────────────────────────────────────────────────────


def test_returns_none_when_config_missing(dispatch, tmp_path, monkeypatch):
    """Missing config.yaml yields None for both fields."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    result = dispatch._resolve_active_model_provider()
    assert result["model"] is None
    assert result["provider"] is None


def test_returns_parsed_model_and_provider(dispatch, tmp_path, monkeypatch):
    """Parses model.default and model.provider from config.yaml."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model:\n"
        "  default: anthropic/claude-sonnet\n"
        "  provider: anthropic\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    result = dispatch._resolve_active_model_provider()
    assert result["model"] == "anthropic/claude-sonnet"
    assert result["provider"] == "anthropic"


def test_returns_none_for_empty_fields(dispatch, tmp_path, monkeypatch):
    """Empty strings are treated as None."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model:\n"
        "  default: ''\n"
        "  provider: ''\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    result = dispatch._resolve_active_model_provider()
    assert result["model"] is None
    assert result["provider"] is None


def test_returns_none_when_model_block_missing(dispatch, tmp_path, monkeypatch):
    """Config without model block yields None for both fields."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("some_other_key: value\n")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    result = dispatch._resolve_active_model_provider()
    assert result["model"] is None
    assert result["provider"] is None


def test_returns_none_on_read_error(dispatch, tmp_path, monkeypatch):
    """I/O errors (e.g. unreadable file) are caught and return None."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text("model:\n  default: test\n  provider: test\n")
    config_path.chmod(0o000)
    try:
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        result = dispatch._resolve_active_model_provider()
        assert result["model"] is None
        assert result["provider"] is None
    finally:
        config_path.chmod(0o644)


def test_handles_nested_empty_config(dispatch, tmp_path, monkeypatch):
    """Empty config file (no model block) yields None for both fields."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    result = dispatch._resolve_active_model_provider()
    assert result["model"] is None
    assert result["provider"] is None

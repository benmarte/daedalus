"""Tests for core.registry — the plain-text project registry.

Run:  pytest tests/test_registry.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the package root importable (config/, core/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.registry import add_project, list_projects, registry_path, remove_project  # noqa: E402


# ── registry_path ────────────────────────────────────────────────────────────

def test_registry_path_default():
    p = registry_path(path=Path("/custom/reg"))
    assert p == Path("/custom/reg")


def test_registry_path_env_override(tmp_path, monkeypatch):
    custom = tmp_path / "custom-registry.txt"
    monkeypatch.setenv("HERMES_ORCH_REGISTRY", str(custom))
    assert registry_path() == custom


def test_registry_path_env_wins_over_default(monkeypatch):
    monkeypatch.setenv("HERMES_ORCH_REGISTRY", "/env/path")
    # even on a clean system this should point to the env value
    assert registry_path() == Path("/env/path")


# ── list_projects ────────────────────────────────────────────────────────────

def test_missing_file_returns_empty(tmp_path):
    reg = tmp_path / "missing.txt"
    assert list_projects(path=reg) == []


def test_empty_file_returns_empty(tmp_path):
    reg = tmp_path / "reg.txt"
    reg.write_text("")
    assert list_projects(path=reg) == []


def test_list_returns_entries(tmp_path):
    reg = tmp_path / "reg.txt"
    reg.write_text("  /a/b  \n/c/d\n")
    assert list_projects(path=reg) == ["/a/b", "/c/d"]


def test_whitespace_and_blank_lines_ignored(tmp_path):
    reg = tmp_path / "reg.txt"
    reg.write_text("\n  \n/a\n   \n# a comment\n  # indented comment\n/b\n")
    assert list_projects(path=reg) == ["/a", "/b"]


# ── add_project ─────────────────────────────────────────────────────────────

def test_add_then_list(tmp_path):
    reg = tmp_path / "reg.txt"
    assert add_project("/foo/bar", path=reg) is True
    assert list_projects(path=reg) == [str(Path("/foo/bar").resolve())]


def test_add_same_twice_is_one_entry(tmp_path):
    reg = tmp_path / "reg.txt"
    assert add_project("/x/y", path=reg) is True
    assert add_project("/x/y", path=reg) is False  # idempotent — duplicate returns False
    assert len(list_projects(path=reg)) == 1


def test_add_creates_parent_dir(tmp_path):
    reg = tmp_path / "deeply" / "nested" / "reg.txt"
    assert not reg.parent.exists()
    assert add_project("/p", path=reg) is True
    assert reg.exists()
    assert str(Path("/p").resolve()) in list_projects(path=reg)


# ── remove_project ──────────────────────────────────────────────────────────

def test_remove_drops_entry(tmp_path):
    reg = tmp_path / "reg.txt"
    add_project("/k", path=reg)
    add_project("/l", path=reg)
    assert remove_project("/k", path=reg) is True
    assert list_projects(path=reg) == [str(Path("/l").resolve())]


def test_remove_missing_is_idempotent(tmp_path):
    reg = tmp_path / "reg.txt"
    add_project("/m", path=reg)
    assert remove_project("/nonexistent", path=reg) is True  # no error
    assert len(list_projects(path=reg)) == 1


def test_remove_last_entry_leaves_empty_file(tmp_path, monkeypatch):
    """Removing the last entry produces an empty file that lists as []."""
    reg = tmp_path / "reg.txt"
    add_project("/last", path=reg)
    assert remove_project("/last", path=reg) is True
    assert reg.read_text() in ("", "\n")  # empty string is fine
    assert list_projects(path=reg) == []


def test_remove_last_entry_env_var(tmp_path, monkeypatch):
    """Same test but using HERMES_ORCH_REGISTRY instead of path=."""
    reg = tmp_path / "viaenv.txt"
    monkeypatch.setenv("HERMES_ORCH_REGISTRY", str(reg))
    add_project("/last")
    remove_project("/last")
    assert list_projects() == []


# ── end-to-end with tmp_path ────────────────────────────────────────────────

def test_full_lifecycle(tmp_path):
    reg = tmp_path / "lifecycle.txt"
    # start empty
    assert list_projects(path=reg) == []
    # add two
    assert add_project("/proj-a", path=reg) is True
    assert add_project("/proj-b", path=reg) is True
    entries = list_projects(path=reg)
    assert len(entries) == 2
    assert str(Path("/proj-a").resolve()) in entries
    assert str(Path("/proj-b").resolve()) in entries
    # add duplicate
    assert add_project("/proj-a", path=reg) is False
    assert len(list_projects(path=reg)) == 2
    # remove one
    assert remove_project("/proj-a", path=reg) is True
    assert list_projects(path=reg) == [str(Path("/proj-b").resolve())]
    # remove again
    assert remove_project("/proj-a", path=reg) is True  # idempotent
    assert len(list_projects(path=reg)) == 1
    # remove last
    assert remove_project("/proj-b", path=reg) is True
    assert list_projects(path=reg) == []

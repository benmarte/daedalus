"""Tests for scripts/daedalus_resolve_project.py — the on_session_end advance
hook's project resolver (issue #1202).

The hook stalled the whole pipeline when the resolver returned nothing: the
on_session_end payload carries a *session* id (not a ``t_`` kanban id), so the
task-id path returned "" and there was no cwd fallback. These lock in the
cwd-first resolution and confirm the module imports without ``core`` on the path
(its deployed location, ~/.hermes/agent-hooks/, has no sibling ``core/``).
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest

RESOLVER = Path(__file__).resolve().parent.parent / "scripts" / "daedalus_resolve_project.py"


def _load():
    spec = importlib.util.spec_from_file_location("daedalus_resolve_project", str(RESOLVER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_module_imports_without_crash():
    """Import must never raise — a crash here silently stalls every handoff."""
    assert _load() is not None


def _setup_registry(tmp_path, project_dir):
    """Point the module's HERMES at a tmp home with a registry listing project_dir."""
    reg_dir = tmp_path / "hermes" / "daedalus"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "projects").write_text(f"{project_dir}\n")
    return tmp_path / "hermes"


def test_cwd_in_registry_resolves(tmp_path, monkeypatch):
    """A session-id payload (no t_ id) resolves via cwd matching the registry."""
    mod = _load()
    proj = tmp_path / "projectA"
    proj.mkdir()
    monkeypatch.setattr(mod, "HERMES", str(_setup_registry(tmp_path, proj)))
    payload = json.dumps({"cwd": str(proj), "extra": {"task_id": "20260702_174548_375a5f"}})
    monkeypatch.setattr(mod.sys, "argv", ["resolver", payload])
    assert mod.cwd_from_payload() == str(proj)


def test_cwd_not_in_registry_returns_empty(tmp_path, monkeypatch):
    """cwd outside the registry never resolves — no global/cross-project sweep."""
    mod = _load()
    proj = tmp_path / "projectA"
    proj.mkdir()
    monkeypatch.setattr(mod, "HERMES", str(_setup_registry(tmp_path, proj)))
    payload = json.dumps({"cwd": str(tmp_path / "somewhere-else"), "extra": {}})
    monkeypatch.setattr(mod.sys, "argv", ["resolver", payload])
    assert mod.cwd_from_payload() == ""


def test_empty_cwd_returns_empty(tmp_path, monkeypatch):
    """Empty/missing cwd falls through to the task-id path, never crashes."""
    mod = _load()
    monkeypatch.setattr(mod, "HERMES", str(_setup_registry(tmp_path, tmp_path / "p")))
    monkeypatch.setattr(mod.sys, "argv", ["resolver", json.dumps({"cwd": "", "extra": {}})])
    assert mod.cwd_from_payload() == ""


def test_trailing_slash_normalized(tmp_path, monkeypatch):
    """A trailing slash on cwd still matches the registry entry."""
    mod = _load()
    proj = tmp_path / "projectA"
    proj.mkdir()
    monkeypatch.setattr(mod, "HERMES", str(_setup_registry(tmp_path, proj)))
    payload = json.dumps({"cwd": f"{proj}/", "extra": {}})
    monkeypatch.setattr(mod.sys, "argv", ["resolver", payload])
    assert mod.cwd_from_payload() == str(proj)

"""Tests for _reconcile_vcs_board — GitLab board auto-configuration (issue #133).

Verifies the runtime self-heal that makes GitLab projects work out of the box:
enable label-board mode, ensure status labels exist, and fix target_branch when
the configured branch does not exist. Non-GitLab providers must be untouched.

Run:  pytest tests/test_vcs_reconcile.py -v
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_STATUS_MAP = {"ready": "Ready", "in_progress": "In progress",
               "in_review": "In review", "done": "Done"}


def _load_dispatch():
    p = ROOT / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeGitLab:
    """Minimal GitLab-shaped provider double for reconcile tests."""

    name = "gitlab"

    def __init__(self, *, board=True, branches=None, default="master",
                 existing_labels=None):
        self.board = board
        self.branches = list(branches or [])
        self.default = default
        self._existing = set(existing_labels or [])
        self.created = []

    def board_configured(self):
        return self.board

    def status_name(self, canonical):
        return _STATUS_MAP.get(canonical, canonical)

    def list_branches(self):
        return list(self.branches)

    def get_default_branch(self):
        return self.default

    def ensure_status_labels(self, status_names):
        made = [n for n in status_names if n and n not in self._existing]
        self._existing.update(made)
        self.created.extend(made)
        return made


@pytest.fixture
def disp():
    return _load_dispatch()


def _patch_factory(disp, monkeypatch, name, rebuilt):
    monkeypatch.setattr(disp.providers, "provider_name", lambda r: name)
    monkeypatch.setattr(disp.providers, "get_provider", lambda r: rebuilt)


def test_enables_board_creates_labels_and_fixes_branch(disp, monkeypatch):
    resolved = {"repo": "g/p",
                "vcs": {"provider": "gitlab", "target_branch": "main"},
                "tracking": None}
    rebuilt = FakeGitLab(board=True, branches=["master", "feature"],
                         default="master", existing_labels=["Ready"])
    _patch_factory(disp, monkeypatch, "gitlab", rebuilt)

    provider, notes = disp._reconcile_vcs_board(resolved, FakeGitLab(board=False))

    assert resolved["tracking"]["label_board"] is True
    assert resolved["vcs"]["target_branch"] == "master"  # main absent → fixed
    assert rebuilt.created == ["In progress", "In review", "Done"]
    assert any("board mode enabled" in n for n in notes)
    assert any("created labels" in n for n in notes)
    assert any("target_branch=master" in n for n in notes)
    assert provider is rebuilt


def test_valid_target_branch_is_not_clobbered(disp, monkeypatch):
    resolved = {"repo": "g/p",
                "vcs": {"provider": "gitlab", "target_branch": "dev"},
                "tracking": {"label_board": True}}
    provider = FakeGitLab(board=True, branches=["dev", "main"], default="main",
                          existing_labels=list(_STATUS_MAP.values()))
    _patch_factory(disp, monkeypatch, "gitlab", provider)

    _, notes = disp._reconcile_vcs_board(resolved, provider)

    assert resolved["vcs"]["target_branch"] == "dev"  # dev exists → untouched
    assert not any("target_branch" in n for n in notes)


def test_explicit_label_board_false_is_respected(disp, monkeypatch):
    resolved = {"repo": "g/p", "vcs": {"provider": "gitlab"},
                "tracking": {"label_board": False}}
    provider = FakeGitLab(board=False, branches=["main"], default="main")
    _patch_factory(disp, monkeypatch, "gitlab", provider)

    _, notes = disp._reconcile_vcs_board(resolved, provider)

    assert resolved["tracking"]["label_board"] is False
    assert not any("board mode" in n for n in notes)


def test_non_gitlab_is_noop(disp, monkeypatch):
    resolved = {"repo": "o/r", "vcs": {"provider": "github"}, "tracking": None}
    provider = FakeGitLab(board=False)
    _patch_factory(disp, monkeypatch, "github", provider)

    out_provider, notes = disp._reconcile_vcs_board(resolved, provider)

    assert notes == []
    assert resolved["tracking"] is None
    assert out_provider is provider


def test_none_provider_is_noop(disp, monkeypatch):
    resolved = {"repo": "g/p", "vcs": {"provider": "gitlab"}, "tracking": None}
    monkeypatch.setattr(disp.providers, "provider_name", lambda r: "gitlab")
    out_provider, notes = disp._reconcile_vcs_board(resolved, None)
    assert out_provider is None and notes == []


def test_dry_run_mutates_nothing(disp, monkeypatch):
    resolved = {"repo": "g/p",
                "vcs": {"provider": "gitlab", "target_branch": "main"},
                "tracking": None}
    provider = FakeGitLab(board=False, branches=["master"], default="master")
    _patch_factory(disp, monkeypatch, "gitlab", provider)

    _, notes = disp._reconcile_vcs_board(resolved, provider, dry_run=True)

    assert resolved["tracking"] is None  # unchanged
    assert resolved["vcs"]["target_branch"] == "main"  # unchanged
    assert provider.created == []
    assert any("would enable board mode" in n for n in notes)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

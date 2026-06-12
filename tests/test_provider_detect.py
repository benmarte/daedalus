"""Tests for core/providers/detect.py — provider auto-detection from git remotes."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.providers.detect import detect_from_url, detect_repo_vcs  # noqa: E402


# ── GitHub ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://github.com/owner/repo.git",
    "https://github.com/owner/repo",
    "git@github.com:owner/repo.git",
    "ssh://git@github.com/owner/repo.git",
])
def test_github_urls(url):
    d = detect_from_url(url)
    assert d == {"provider": "github", "repo": "owner/repo", "vcs_extra": {}}


# ── GitLab ────────────────────────────────────────────────────────────────────

def test_gitlab_com():
    d = detect_from_url("git@gitlab.com:group/proj.git")
    assert d["provider"] == "gitlab"
    assert d["repo"] == "group/proj"
    assert d["vcs_extra"] == {"project_path": "group/proj"}


def test_gitlab_nested_groups():
    d = detect_from_url("https://gitlab.com/group/sub/proj.git")
    assert d["repo"] == "group/sub/proj"
    assert d["vcs_extra"]["project_path"] == "group/sub/proj"


def test_gitlab_self_hosted_sets_base_url():
    d = detect_from_url("https://gitlab.corp.io/team/app.git")
    assert d["provider"] == "gitlab"
    assert d["repo"] == "team/app"
    assert d["vcs_extra"] == {"project_path": "team/app", "base_url": "https://gitlab.corp.io"}


# ── Azure DevOps ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://dev.azure.com/acme/WebApp/_git/web-repo",
    "git@ssh.dev.azure.com:v3/acme/WebApp/web-repo",
    "https://user@dev.azure.com/acme/WebApp/_git/web-repo",
])
def test_azure_urls(url):
    d = detect_from_url(url)
    assert d["provider"] == "azuredevops"
    assert d["repo"] == "acme/WebApp/web-repo"
    assert d["vcs_extra"] == {"org": "acme", "project": "WebApp", "repo": "web-repo"}


def test_azure_visualstudio_legacy():
    d = detect_from_url("https://acme.visualstudio.com/WebApp/_git/web-repo")
    assert d["provider"] == "azuredevops"
    assert d["vcs_extra"] == {"org": "acme", "project": "WebApp", "repo": "web-repo"}


# ── unknown / invalid ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://git.example.com/owner/repo.git",   # unknown host → manual config
    "https://bitbucket.org/owner/repo.git",     # not supported yet
    "not-a-url",
    "",
])
def test_unknown_or_invalid_returns_none(url):
    assert detect_from_url(url) is None


# ── detect_repo_vcs (real git repo) ──────────────────────────────────────────

def test_detect_repo_vcs_from_origin(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                    "https://gitlab.com/group/proj.git"], check=True)
    d = detect_repo_vcs(str(tmp_path))
    assert d["provider"] == "gitlab"
    assert d["repo"] == "group/proj"


def test_detect_repo_vcs_no_remote(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert detect_repo_vcs(str(tmp_path)) is None


def test_detect_repo_vcs_not_a_repo(tmp_path):
    assert detect_repo_vcs(str(tmp_path)) is None

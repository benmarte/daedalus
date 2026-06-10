"""Tests for scripts/setup.sh — target-repo scaffolding.

Run:  pytest tests/test_setup.py -v
"""

import os
import subprocess
from pathlib import Path

import yaml


# Absolute path to setup.sh — tests need this regardless of where they run.
_ORCH_ROOT = Path(__file__).resolve().parent.parent
_SETUP_SH = _ORCH_ROOT / "scripts" / "setup.sh"
_TEMPLATE = _ORCH_ROOT / "templates" / "daedalus.yaml"


def _run_setup(workdir: Path, registry: Path, *, force: bool = False) -> subprocess.CompletedProcess:
    """Run setup.sh inside *workdir* with HERMES_ORCH_REGISTRY=*registry*."""
    env = {**os.environ, "HERMES_ORCH_REGISTRY": str(registry)}
    # Add ORCH_ROOT to PYTHONPATH so setup.sh can import core.registry
    env["PYTHONPATH"] = str(_ORCH_ROOT)
    cmd = ["bash", str(_SETUP_SH)]
    if force:
        cmd.append("--force")
    return subprocess.run(
        cmd, cwd=str(workdir), env=env,
        capture_output=True, text=True, timeout=30,
    )


# ── Helper: create a temp git repo with a fake origin remote ─────────────────

def _init_tmp_repo(tmp_path: Path, remote_url: str) -> Path:
    """Create a bare repo to serve as origin, then clone-like init a worktree repo."""
    repo = tmp_path / "my-project"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True, capture_output=True)
    # Create an initial commit so the repo has a HEAD
    (repo / "README.md").write_text("# test\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    # Set the remote URL (no need for a real remote, just the URL)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", remote_url],
        check=True, capture_output=True,
    )
    return repo


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_setup_creates_config(tmp_path):
    """Running setup.sh inside a git repo creates .hermes/daedalus.yaml."""
    registry = tmp_path / "registry.txt"
    repo = _init_tmp_repo(tmp_path, "https://github.com/acme/widgets.git")

    r = _run_setup(repo, registry)
    assert r.returncode == 0, f"setup.sh failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"

    config = repo / ".hermes" / "daedalus.yaml"
    assert config.exists(), f"Expected {config} to exist"

    data = yaml.safe_load(config.read_text())
    assert data is not None
    assert data["name"] == "widgets"  # repo short name from acme/widgets
    assert data["repo"] == "acme/widgets"
    assert data["workdir"] == str(repo.resolve())


def test_setup_registers_repo(tmp_path):
    """Running setup.sh adds the repo path to the registry."""
    registry = tmp_path / "registry.txt"
    repo = _init_tmp_repo(tmp_path, "https://github.com/acme/widgets.git")

    r = _run_setup(repo, registry)
    assert r.returncode == 0, f"setup.sh failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"

    assert registry.exists()
    lines = [ln.strip() for ln in registry.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert str(repo.resolve()) in lines, f"Registry does not contain {repo.resolve()}: {lines}"


def test_setup_idempotent_no_clobber(tmp_path):
    """Re-running setup.sh (without --force) does not overwrite config."""
    registry = tmp_path / "registry.txt"
    repo = _init_tmp_repo(tmp_path, "https://github.com/acme/widgets.git")

    # First run
    r1 = _run_setup(repo, registry)
    assert r1.returncode == 0

    config = repo / ".hermes" / "daedalus.yaml"
    mtime1 = config.stat().st_mtime
    content1 = config.read_text()

    # Second run — should skip (no --force)
    r2 = _run_setup(repo, registry)
    assert r2.returncode == 0
    assert "SKIP" in r2.stdout, f"Expected 'SKIP' in output, got: {r2.stdout}"

    mtime2 = config.stat().st_mtime
    content2 = config.read_text()
    assert mtime1 == mtime2, "Config mtime changed — file was clobbered"
    assert content1 == content2, "Config content changed — file was clobbered"


def test_setup_idempotent_no_duplicate_registry(tmp_path):
    """Re-running setup.sh does not duplicate the repo in the registry."""
    registry = tmp_path / "registry.txt"
    repo = _init_tmp_repo(tmp_path, "https://github.com/acme/widgets.git")

    _run_setup(repo, registry)
    _run_setup(repo, registry)  # second run

    lines = [ln.strip() for ln in registry.read_text().splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    count = lines.count(str(repo.resolve()))
    assert count == 1, f"Expected 1 registry entry, got {count}: {lines}"


def test_setup_force_overwrites(tmp_path):
    """setup.sh --force overwrites an existing daedalus.yaml."""
    registry = tmp_path / "registry.txt"
    repo = _init_tmp_repo(tmp_path, "https://github.com/acme/widgets.git")

    # First run
    _run_setup(repo, registry)
    config = repo / ".hermes" / "daedalus.yaml"
    content1 = config.read_text()

    # Modify the config in place
    modified = content1.replace("widgets", "not-widgets")
    config.write_text(modified)

    # Force re-run
    r = _run_setup(repo, registry, force=True)
    assert r.returncode == 0

    content3 = config.read_text()
    assert "widgets" in content3
    assert "not-widgets" not in content3, "Force did not restore the template content"


def test_setup_git_ssh_remote(tmp_path):
    """setup.sh parses git@github.com:owner/repo.git remotes correctly."""
    registry = tmp_path / "registry.txt"
    repo = _init_tmp_repo(tmp_path, "git@github.com:myorg/myrepo.git")

    r = _run_setup(repo, registry)
    assert r.returncode == 0, f"setup.sh failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"

    config = repo / ".hermes" / "daedalus.yaml"
    data = yaml.safe_load(config.read_text())
    assert data["repo"] == "myorg/myrepo"


def test_setup_yaml_is_valid(tmp_path):
    """The generated .hermes/daedalus.yaml is valid YAML with expected keys."""
    registry = tmp_path / "registry.txt"
    repo = _init_tmp_repo(tmp_path, "https://github.com/org/proj.git")

    _run_setup(repo, registry)
    config = repo / ".hermes" / "daedalus.yaml"
    data = yaml.safe_load(config.read_text())

    # Required keys — name from repo short name (org/proj -> proj)
    assert data["name"] == "proj"
    assert data["repo"] == "org/proj"
    assert data["workdir"] == str(repo.resolve())

    # Structural keys present
    assert "tracking" in data
    assert "vcs" in data
    assert "vcs" in data and "target_branch" in data["vcs"]
    assert "sources" in data
    assert "sources" in data and "github_issues" in data["sources"]
    assert "sources" in data and "local_specs" in data["sources"]
    assert "sources" in data and "kanban_triage" in data["sources"]
    assert "cron" in data
    assert "cron" in data and "schedule" in data["cron"]


def test_setup_template_exists(tmp_path):
    """Verify the template file exists and is valid YAML."""
    assert _TEMPLATE.exists(), f"Template not found at {_TEMPLATE}"
    data = yaml.safe_load(_TEMPLATE.read_text())
    assert data is not None

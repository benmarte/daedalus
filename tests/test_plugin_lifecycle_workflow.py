"""
Tests for the plugin-lifecycle CI harness (issue #88).

These are fast, hermetic guards on the *shape* of the lifecycle workflow and its
supporting scripts — they do NOT run the full smoke test (that is the workflow's
job: ``tests/ci/lifecycle_smoke.sh``). They catch the cheap regressions: a trigger
silently dropped, a stage removed, the stub losing a subcommand the install
scripts depend on, or an executable bit lost.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_repo_root = Path(__file__).resolve().parent.parent
_workflow = _repo_root / ".github" / "workflows" / "plugin-lifecycle.yml"
_driver = _repo_root / "tests" / "ci" / "lifecycle_smoke.sh"
_stub = _repo_root / "tests" / "ci" / "hermes"


def _load_workflow() -> dict:
    return yaml.safe_load(_workflow.read_text())


def test_workflow_exists() -> None:
    assert _workflow.is_file(), f"missing workflow: {_workflow}"


def test_workflow_triggers() -> None:
    """Acceptance criteria: push→dev, PR→main, and a nightly schedule."""
    wf = _load_workflow()
    # PyYAML parses the bare `on:` key as the boolean True.
    on = wf.get("on", wf.get(True))
    assert on, "workflow has no triggers"
    assert "dev" in (on["push"]["branches"]), "must run on push to dev"
    assert "main" in (on["pull_request"]["branches"]), "must run on PRs targeting main"
    assert "schedule" in on and on["schedule"], "must run on a nightly schedule"


def test_workflow_runs_the_driver() -> None:
    wf = _load_workflow()
    runs = [
        step.get("run", "")
        for job in wf["jobs"].values()
        for step in job.get("steps", [])
    ]
    assert any("tests/ci/lifecycle_smoke.sh" in r for r in runs), (
        "lifecycle job must invoke tests/ci/lifecycle_smoke.sh"
    )


def test_required_gate_job_present() -> None:
    """A single aggregate job branch protection can require for merges to main."""
    wf = _load_workflow()
    gate = wf["jobs"].get("lifecycle-complete")
    assert gate and gate.get("needs") == ["lifecycle"], (
        "expected a lifecycle-complete job gating on [lifecycle]"
    )


def test_driver_and_stub_executable() -> None:
    for path in (_driver, _stub):
        assert path.is_file(), f"missing: {path}"
        mode = path.stat().st_mode
        assert mode & stat.S_IXUSR, f"not executable: {path} (chmod +x)"


def test_driver_covers_all_five_stages() -> None:
    body = _driver.read_text()
    for stage in ("Stage 1", "Stage 2", "Stage 3", "Stage 4", "Stage 5"):
        assert stage in body, f"driver missing {stage}"
    # The regression each upgrade/uninstall stage guards must stay wired up.
    assert "--dry-run" in body, "dispatch smoke must use --dry-run"
    assert "ci-sample-daedalus" in body, "upgrade stage must assert cron self-heal (#80)"


def test_self_test_guard_has_pytest_installed() -> None:
    """Regression for #106: the self-test guard runs ``python -m pytest`` so the
    job MUST install pytest first. pytest is test-only (excluded from
    requirements.txt), so it has to be added explicitly — without it the
    Plugin Lifecycle workflow fails on every run with ``No module named pytest``.
    """
    wf = _load_workflow()
    steps = wf["jobs"]["lifecycle"]["steps"]
    runs = [step.get("run", "") for step in steps]

    pytest_step_idx = next(
        (i for i, r in enumerate(runs) if "python -m pytest" in r), None
    )
    assert pytest_step_idx is not None, "lifecycle job must run the pytest self-test guard"

    install_before = "".join(runs[:pytest_step_idx])
    assert "pytest" in install_before, (
        "a step before the pytest self-test guard must install pytest "
        "(test-only dep, not in requirements.txt) — see #106"
    )


def test_stub_implements_required_subcommands() -> None:
    """The stub must keep handling every hermes verb the install scripts call."""
    body = _stub.read_text()
    for verb in ('"cron"', '"profile"', '"plugins"', '"kanban"'):
        assert verb in body, f"stub missing handler for {verb}"
    # Emits the cron-list field the uninstaller / self-heal parse by name.
    assert "Name:" in body, "stub cron list must emit a Name: field"

"""
Tests for .github/workflows/release.yml — release publishing settings.

Regression guard for issue #65: the "Create GitHub Release" step must set
``prerelease: false`` (and ``draft: false``) so releases created by the
workflow receive the **Latest** badge on GitHub instead of showing as
"Pre-release".
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_repo_root = Path(__file__).resolve().parent.parent
_release_yml = _repo_root / ".github" / "workflows" / "release.yml"


def _gh_release_step() -> dict:
    """Return the ``with:`` block of the softprops/action-gh-release step."""
    workflow = yaml.safe_load(_release_yml.read_text())
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            uses = step.get("uses", "")
            if isinstance(uses, str) and "action-gh-release" in uses:
                return step.get("with", {})
    pytest.fail("No softprops/action-gh-release step found in release.yml")


def test_release_yml_exists() -> None:
    assert _release_yml.is_file(), f"missing workflow: {_release_yml}"


def test_release_not_marked_prerelease() -> None:
    """Issue #65: releases must publish as Latest, not Pre-release."""
    with_block = _gh_release_step()
    assert with_block.get("prerelease") is False, (
        "release.yml must set prerelease: false so releases show as Latest "
        "(see issue #65); got "
        f"{with_block.get('prerelease')!r}"
    )


def test_release_not_a_draft() -> None:
    """A draft release would also never receive the Latest badge."""
    with_block = _gh_release_step()
    assert with_block.get("draft") is False, (
        f"release.yml must set draft: false; got {with_block.get('draft')!r}"
    )

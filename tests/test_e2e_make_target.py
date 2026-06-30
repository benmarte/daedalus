"""Guard tests for the E2E entrypoints (issue #903).

These assert the *plumbing* that #903 adds stays in place: a `make e2e` target
that drives the offline regression suite, an opt-in `make e2e-live` target, and
a nightly GitHub Actions schedule that runs them. They do NOT re-run the
pipeline itself (test_e2e_full_pipeline already does) — they exist so a careless
edit to the Makefile or workflow that silently disables nightly E2E coverage
fails CI instead of going unnoticed.

Dual-mode per the repo convention: runs under pytest AND as a standalone script
(`python3 tests/test_e2e_make_target.py`) via the shared ``check`` helper.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import check  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
NIGHTLY = ROOT / ".github" / "workflows" / "e2e-nightly.yml"


def _makefile_text() -> str:
    return MAKEFILE.read_text() if MAKEFILE.is_file() else ""


def _nightly_text() -> str:
    return NIGHTLY.read_text() if NIGHTLY.is_file() else ""


def test_makefile_exists():
    check("Makefile exists", MAKEFILE.is_file())


def test_makefile_has_e2e_target():
    text = _makefile_text()
    check("Makefile declares an 'e2e' target", "\ne2e:" in text)
    check("Makefile declares an opt-in 'e2e-live' target", "\ne2e-live:" in text)
    check("e2e target is .PHONY", "e2e" in text and ".PHONY" in text)


def test_makefile_e2e_runs_pipeline_suite():
    text = _makefile_text()
    check(
        "e2e target runs the full-pipeline regression test",
        "test_e2e_full_pipeline.py" in text,
    )


def test_makefile_e2e_live_requires_token():
    text = _makefile_text()
    check(
        "e2e-live guards on GITHUB_TOKEN before touching the real dispatcher",
        "GITHUB_TOKEN" in text and "e2e_smoke_test.sh" in text,
    )


def test_nightly_workflow_exists():
    check("e2e-nightly.yml workflow exists", NIGHTLY.is_file())


def test_nightly_workflow_has_cron_schedule():
    text = _nightly_text()
    check("nightly workflow has a schedule trigger", "schedule:" in text)
    check("nightly workflow runs at 02:00 UTC", "cron: '0 2 * * *'" in text)


def test_nightly_workflow_supports_optional_live_run():
    text = _nightly_text()
    check("nightly workflow is manually dispatchable", "workflow_dispatch:" in text)
    check("nightly workflow exposes a run_live opt-in", "run_live" in text)
    check("nightly workflow invokes 'make e2e'", "make e2e" in text)


if __name__ == "__main__":
    print("Daedalus E2E make-target / nightly-schedule guard tests")
    print("=" * 60)
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        print(f"\n{name}")
        print("-" * len(name))
        fn()
    print()
    print("=" * 60)
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)

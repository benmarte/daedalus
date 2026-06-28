"""Tests for the offline dispatcher self-test harness (issue #900).

``core.dispatch_selftest`` powers ``daedalus_dispatch.py --self-test``: a
hermetic, GitHub-free smoke that seeds fake issues/tasks and drives the *real*
dispatcher handoff functions through a controlled tick. These tests assert the
harness reports all-green on a healthy tree, that the CLI flag exits 0/1 on
the report's verdict, and — crucially — that the run touches no real GitHub.

Dual-mode per the repo convention: runs under pytest AND as a standalone script
(`python3 tests/test_dispatch_selftest.py`) via the shared ``check`` helper.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import _load_dispatch, check  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import dispatch_selftest  # noqa: E402


def test_run_selftest_all_checks_pass():
    """The harness reports every check green against the current dispatcher."""
    disp = _load_dispatch()
    report = dispatch_selftest.run_selftest(disp)
    check("self-test report is ok", report.ok)
    check("self-test ran 5 checks", len(report.checks) == 5)
    for c in report.checks:
        check(f"check passed: {c.name}", c.passed)


def test_run_selftest_restores_dispatcher_kanban():
    """The harness swaps disp.kanban only for the run, then restores it."""
    disp = _load_dispatch()
    original = disp.kanban
    dispatch_selftest.run_selftest(disp)
    check("disp.kanban restored after run", disp.kanban is original)


def test_run_selftest_touches_no_real_github():
    """All issue comments go to the in-memory provider; nothing else is hit."""
    disp = _load_dispatch()
    report = dispatch_selftest.run_selftest(disp)
    names = [c.name for c in report.checks]
    check("includes a 'no real GitHub' assertion",
          any("no real GitHub" in n for n in names))
    github_check = next(c for c in report.checks if "no real GitHub" in c.name)
    check("the no-real-GitHub check passes", github_check.passed)


def test_report_format_is_human_readable():
    """format() yields a clear PASS/FAIL block naming each check."""
    disp = _load_dispatch()
    report = dispatch_selftest.run_selftest(disp)
    text = report.format()
    check("format mentions PASSED verdict", "self-test PASSED" in text)
    check("format lists each check", text.count("PASS  ") >= 5)


def test_cli_self_test_flag_exits_zero_on_success():
    """`daedalus_dispatch.py --self-test` returns 0 when the report is ok."""
    disp = _load_dispatch()
    argv = sys.argv
    sys.argv = ["daedalus_dispatch.py", "--self-test"]
    try:
        rc = disp.main()
    finally:
        sys.argv = argv
    check("--self-test exit code is 0 on all-pass", rc == 0)


def test_cli_self_test_flag_exits_nonzero_on_failure():
    """A failing report drives a non-zero exit (CI can gate on it)."""
    disp = _load_dispatch()

    class _BadReport:
        ok = False

        def format(self):
            return "stub failing report"

    orig = dispatch_selftest.run_selftest
    dispatch_selftest.run_selftest = lambda _disp: _BadReport()
    argv = sys.argv
    sys.argv = ["daedalus_dispatch.py", "--self-test"]
    try:
        rc = disp.main()
    finally:
        sys.argv = argv
        dispatch_selftest.run_selftest = orig
    check("--self-test exit code is 1 on failure", rc == 1)


if __name__ == "__main__":
    print("Daedalus dispatcher self-test harness tests")
    print("=" * 60)
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        print(f"\n{name}")
        print("-" * len(name))
        fn()
    print()
    print("=" * 60)
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)

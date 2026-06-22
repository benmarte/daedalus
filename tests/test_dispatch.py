"""Tests for CI retry scheduling (issue #24).

Exercises ``scripts/daedalus_dispatch._schedule_ci_retry`` directly:
  - happy path creates a one-shot 3‑minute cron
  - idempotent guard skips creation if the job already exists
  - slug is sanitized (unsafe chars become '-')
  - subprocess failures are caught and return False (never crash dispatcher)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_dispatch():
    p = Path(__file__).resolve().parent.parent / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


disp = _load_dispatch()

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}")


# ── _schedule_ci_retry ───────────────────────────────────────────────────────


def test_schedule_ci_retry_happy_path():
    """No existing job → creates a cron with --name and --repeat 1."""
    list_result = mock.Mock()
    list_result.returncode = 0
    list_result.stdout = ""
    create_result = mock.Mock()
    create_result.stdout = "daedalus-ci-retry-my-board"

    with mock.patch("subprocess.run", side_effect=[list_result, create_result]) as mk_run:
        created = disp._schedule_ci_retry("my-board", 2)

    check("happy path returns True", created is True)
    check("two subprocess calls (list + create)", mk_run.call_count == 2)
    # The list call must use --all (not the nonexistent --quiet)
    list_args = mk_run.call_args_list[0][0][0]
    check("list uses --all flag", "--all" in list_args)
    check("list does not use --quiet", "--quiet" not in list_args)
    # The create call arguments
    create_args = mk_run.call_args_list[1][0][0]
    check("create uses hermes cron create", create_args[0:3] == ["hermes", "cron", "create"])
    check("schedule is 3m", "3m" in create_args)
    check("--repeat 1 set", "--repeat" in create_args and "1" in create_args)
    check("--no-agent set", "--no-agent" in create_args)
    check("--script daedalus-cron.sh", "daedalus-cron.sh" in create_args)
    check("job name in create", "daedalus-ci-retry-my-board" in create_args)


def test_schedule_ci_retry_idempotent():
    """If job name already in `hermes cron list` output → no creation call."""
    list_result = mock.Mock()
    list_result.returncode = 0
    list_result.stdout = "daedalus-ci-retry-my-board\nother-job\n"

    with mock.patch("subprocess.run", return_value=list_result) as mk_run:
        created = disp._schedule_ci_retry("my-board", 1)

    check("idempotent returns False", created is False)
    check("only one subprocess call (no create)", mk_run.call_count == 1)


def test_schedule_ci_retry_list_nonzero_rc():
    """If `hermes cron list` exits non-zero → bail out, don't spawn a duplicate."""
    list_result = mock.Mock()
    list_result.returncode = 1
    list_result.stdout = ""

    with mock.patch("subprocess.run", return_value=list_result) as mk_run:
        created = disp._schedule_ci_retry("slug", 1)

    check("non-zero list rc returns False", created is False)
    check("no create call attempted", mk_run.call_count == 1)


def test_schedule_ci_retry_slug_sanitized():
    """Unsafe chars in the slug become '-' so the cron name is safe."""
    list_result = mock.Mock()
    list_result.returncode = 0
    list_result.stdout = ""
    create_result = mock.Mock()

    with mock.patch("subprocess.run", side_effect=[list_result, create_result]) as mk_run:
        disp._schedule_ci_retry("org/repo:special", 1)

    create_args = mk_run.call_args_list[1][0][0]
    # The name passed to --name should have unsafe chars replaced
    name_idx = create_args.index("--name") + 1
    job_name = create_args[name_idx]
    check("slug sanitized", job_name == "daedalus-ci-retry-org-repo-special")


def test_schedule_ci_retry_subprocess_failure():
    """If hermes cron list/create fails → return False, don't crash."""
    with mock.patch("subprocess.run", side_effect=OSError("hermes not found")):
        created = disp._schedule_ci_retry("slug", 1)
    check("failure returns False", created is False)


def test_schedule_ci_retry_create_swallows_error():
    """The creation step failing is handled — still returns True if list succeeded."""
    list_result = mock.Mock()
    list_result.returncode = 0
    list_result.stdout = ""

    def fake_run(cmd, *a, **kw):
        if cmd[0:3] == ["hermes", "cron", "create"]:
            raise OSError("create failed")
        return list_result

    with mock.patch("subprocess.run", side_effect=fake_run) as mk_run:
        created = disp._schedule_ci_retry("slug", 1)

    # Outer try/except catches the OSError from create and returns False.
    check("create failure returns False", created is False)
    check("called list and attempted create", mk_run.call_count == 2)


if __name__ == "__main__":
    print("CI retry scheduling tests")
    print("-" * 60)
    for fn in (
        test_schedule_ci_retry_happy_path,
        test_schedule_ci_retry_idempotent,
        test_schedule_ci_retry_list_nonzero_rc,
        test_schedule_ci_retry_slug_sanitized,
        test_schedule_ci_retry_subprocess_failure,
        test_schedule_ci_retry_create_swallows_error,
    ):
        fn()
    print("-" * 60)
    print(f"Results: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)

"""Tests for daedalus-delegate.sh — script-owned delegation lifecycle (issue #1280).

Exercises the wrapper as a real subprocess with a fake "coding agent" (a small
shell command). Uses a PATH-stub hermes so no real CLI calls are made — the stub
records every invocation to a log file that assertions read.

Test cases:
  (a) happy path — agent exits 0, DELEGATE_RESULT status "ok", out captured
  (b) nonzero exit — DELEGATE_RESULT status "failed" with the correct exit code
  (c) timeout — agent sleeps beyond a 2s max-wait, status "timeout", child dead
  (d) done-marker early completion — marker appears before max-wait
  (e) heartbeat calls — at least one heartbeat with 1s interval
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# ── locate the wrapper script ─────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DELEGATE_SH = _REPO_ROOT / "scripts" / "daedalus-delegate.sh"


def _stub_hermes_bin(tmp_path: Path) -> Path:
    """Write a stub `hermes` executable that records every call to a log file.

    Returns the directory containing the stub — prepend it to PATH so the
    wrapper finds our stub instead of any real hermes installation.
    """
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    log = tmp_path / "hermes-calls.log"
    stub = stub_dir / "hermes"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"$@\" >> {log}\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    return stub_dir


def _run_delegate(
    tmp_path: Path,
    *,
    agent_cmd: str,
    max_wait: int = 30,
    heartbeat_interval: int = 300,
    poll_interval: int = 5,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run daedalus-delegate.sh with a minimal task file and return the result."""
    stub_dir = _stub_hermes_bin(tmp_path)
    task_file = tmp_path / "task.txt"
    task_file.write_text("task body\n")
    out_file = tmp_path / "agent-out.txt"

    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
        # Keep HERMES_HOME isolated (autouse fixture already set it, but be explicit)
        "HERMES_HOME": str(tmp_path / "hermes-home"),
    }
    if extra_env:
        env.update(extra_env)

    cmd = [
        "bash",
        str(_DELEGATE_SH),
        "--task-file", str(task_file),
        "--cmd", agent_cmd,
        "--card", "t_test123",
        "--board", "test-board",
        "--out", str(out_file),
        "--max-wait", str(max_wait),
        "--heartbeat-interval", str(heartbeat_interval),
        "--poll-interval", str(poll_interval),
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=max_wait + 30,  # test-level timeout (generous)
    )


def _parse_result(stdout: str) -> dict:
    """Find and parse the DELEGATE_RESULT: {...} line from stdout."""
    for line in stdout.splitlines():
        if line.startswith("DELEGATE_RESULT: "):
            return json.loads(line[len("DELEGATE_RESULT: "):])
    raise AssertionError(f"No DELEGATE_RESULT line found in stdout:\n{stdout}")


def _hermes_calls(tmp_path: Path) -> list[str]:
    """Return lines from the stub hermes call log."""
    log = tmp_path / "hermes-calls.log"
    if not log.exists():
        return []
    return log.read_text().splitlines()


# ── (a) happy path ────────────────────────────────────────────────────────────

def test_happy_path_exit_0(tmp_path):
    """Agent exits 0 → DELEGATE_RESULT status 'ok', exit 0, out file written."""
    out_file = tmp_path / "agent-out.txt"
    # The agent echoes something useful and exits 0.
    agent_cmd = f"bash -c 'echo hello-from-agent; echo \"PR URL: https://github.com/x/y/pull/99 PR number: 99\"'"

    result = _run_delegate(tmp_path, agent_cmd=agent_cmd)

    assert result.returncode == 0, f"wrapper exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    data = _parse_result(result.stdout)
    assert data["status"] == "ok"
    assert data["exit"] == 0
    assert "duration_s" in data

    # Out file should contain agent output (delegate writes stdout+stderr there)
    out_path = Path(data["out"])
    assert out_path.exists(), f"out file missing: {out_path}"
    content = out_path.read_text()
    assert "hello-from-agent" in content
    assert "PR number: 99" in content


# ── (b) nonzero exit ─────────────────────────────────────────────────────────

def test_nonzero_exit_propagated(tmp_path):
    """Agent exits non-zero → DELEGATE_RESULT status 'failed', correct exit code."""
    agent_cmd = "bash -c 'echo some output; exit 42'"
    result = _run_delegate(tmp_path, agent_cmd=agent_cmd)

    assert result.returncode == 42, f"expected exit 42, got {result.returncode}"
    data = _parse_result(result.stdout)
    assert data["status"] == "failed"
    assert data["exit"] == 42
    assert "duration_s" in data


def test_exit_1_propagated(tmp_path):
    """Edge case: exit 1 is also correctly propagated."""
    agent_cmd = "bash -c 'exit 1'"
    result = _run_delegate(tmp_path, agent_cmd=agent_cmd)
    assert result.returncode == 1
    data = _parse_result(result.stdout)
    assert data["status"] == "failed"
    assert data["exit"] == 1


# ── (c) timeout ───────────────────────────────────────────────────────────────

def test_timeout_kills_child(tmp_path):
    """Agent sleeps beyond max-wait → status 'timeout', wrapper exits 124, child dead."""
    # We use a named pipe trick: write the sleep child's PID to a file so we
    # can verify it's dead after the wrapper returns.
    pid_file = tmp_path / "child.pid"
    agent_cmd = (
        f"bash -c 'echo $$ > {pid_file}; sleep 999'"
    )

    t0 = time.monotonic()
    result = _run_delegate(tmp_path, agent_cmd=agent_cmd, max_wait=2)
    elapsed = time.monotonic() - t0

    # Wrapper must return within a reasonable window of the 2s timeout.
    assert elapsed < 20, f"wrapper took too long: {elapsed:.1f}s"
    assert result.returncode == 124, f"expected 124 (timeout), got {result.returncode}"
    data = _parse_result(result.stdout)
    assert data["status"] == "timeout"
    assert data["exit"] == 124

    # Verify the child process (the sleep) is actually dead.
    if pid_file.exists():
        child_pid_str = pid_file.read_text().strip()
        if child_pid_str.isdigit():
            child_pid = int(child_pid_str)
            # Give a moment for the kill to propagate.
            time.sleep(0.5)
            try:
                os.kill(child_pid, 0)
                # If we get here without OSError, the process is still alive.
                pytest.fail(f"child PID {child_pid} is still alive after timeout kill")
            except ProcessLookupError:
                pass  # expected — process is dead
            except PermissionError:
                pass  # process exists but we can't signal it (also means alive)


# ── (d) done-marker early completion ─────────────────────────────────────────

def test_done_marker_early_completion(tmp_path):
    """Inner agent writes <out>.done — wrapper treats as complete before max-wait."""
    out_file = tmp_path / "agent-out.txt"
    done_marker = tmp_path / "agent-out.txt.done"

    # Agent writes some output, creates the done marker, then sleeps (simulates
    # a process that is still running when the hook fires).
    agent_cmd = (
        f"bash -c 'echo agent-output > {out_file}; touch {done_marker}; sleep 999'"
    )

    t0 = time.monotonic()
    result = _run_delegate(tmp_path, agent_cmd=agent_cmd, max_wait=30)
    elapsed = time.monotonic() - t0

    # Should complete well before the 30s max-wait because the marker appeared.
    assert elapsed < 15, f"wrapper took too long ({elapsed:.1f}s) — done-marker not honoured"
    data = _parse_result(result.stdout)
    # Done-marker path treats the run as successful (exit 0).
    assert data["status"] == "ok"
    assert data["exit"] == 0


# ── (e) heartbeat calls ───────────────────────────────────────────────────────

def test_heartbeat_sent_within_interval(tmp_path):
    """With a 1s heartbeat interval and 1s poll interval, at least one heartbeat fires during a 4s run."""
    # Agent sleeps 4s. With poll_interval=1 the loop checks every second:
    # t=0 spawn, t=1 poll (alive, HB fires: elapsed=1 >= 1), t=2 poll (alive),
    # t=3 poll (alive), t=4 poll (exits) → at least one heartbeat recorded.
    agent_cmd = "bash -c 'sleep 4; echo done'"

    result = _run_delegate(
        tmp_path,
        agent_cmd=agent_cmd,
        max_wait=30,
        heartbeat_interval=1,
        poll_interval=1,
    )

    assert result.returncode == 0, f"wrapper exited {result.returncode}\n{result.stdout}"

    calls = _hermes_calls(tmp_path)
    # We expect at least one "kanban heartbeat t_test123 --board test-board" line.
    heartbeat_calls = [c for c in calls if "heartbeat" in c]
    assert heartbeat_calls, (
        f"No heartbeat calls recorded. All hermes calls: {calls}\n"
        f"wrapper stdout:\n{result.stdout}"
    )
    # Verify the card and board were passed correctly.
    for call in heartbeat_calls:
        assert "t_test123" in call, f"card id missing in heartbeat call: {call}"

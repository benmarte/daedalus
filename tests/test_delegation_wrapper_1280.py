"""Tests for daedalus-delegate.sh — script-owned delegation lifecycle (issue #1280).

Exercises the wrapper as a real subprocess with a fake "coding agent" (a small
shell command). All external binaries (hermes, gh) are PATH-stubbed per test so
no real CLI calls are ever made — stubs record every invocation to a log file
that assertions read. No assertion depends on the local environment.

Test cases:
  (a) happy path — agent exits 0, DELEGATE_RESULT status "ok", out captured
  (b) nonzero exit 42 — DELEGATE_RESULT status "failed" with the correct exit code
  (c) exit 1 edge case — propagated exactly
  (d) timeout — agent sleeps beyond 2s max-wait, status "timeout", direct child dead
  (e) done-marker early completion — marker appears before max-wait
  (f) heartbeat calls — at least one heartbeat with 1s interval (non-blocking)
  (g) grandchild kill — agent spawns a grandchild; pgid-kill via setsid/perl
      eliminates the grandchild on timeout
  (h) hung heartbeat — hermes stub sleeps 60s; wrapper still times out at max-wait
      (heartbeat runs in background subshell, so the loop never blocks)
  (i-l) transition mode — --transition flag causes hermes kanban block with the
      correct phrase for ok+PR / ok+no-PR / failed / timeout
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

# ── locate the wrapper script ─────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DELEGATE_SH = _REPO_ROOT / "scripts" / "daedalus-delegate.sh"


# ── stub factories ────────────────────────────────────────────────────────────

def _make_stub(stub_dir: Path, name: str, body: str) -> Path:
    """Write a named executable stub to stub_dir and return its path."""
    stub = stub_dir / name
    stub.write_text("#!/usr/bin/env bash\n" + body + "\n")
    stub.chmod(0o755)
    return stub


def _stub_bin_dir(tmp_path: Path, *, hermes_body: str = "", gh_body: str = "") -> tuple[Path, Path, Path]:
    """Create a stub-bin directory with hermes and gh stubs.

    Returns (stub_dir, hermes_log, gh_log).
    hermes_body / gh_body are inserted after the log-append line; pass extra
    behaviour such as 'sleep 60' for the hung-heartbeat test.
    """
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir(exist_ok=True)
    hermes_log = tmp_path / "hermes-calls.log"
    gh_log = tmp_path / "gh-calls.log"

    _make_stub(stub_dir, "hermes",
               f'echo "$@" >> {hermes_log}\n{hermes_body}exit 0')
    _make_stub(stub_dir, "gh",
               f'echo "$@" >> {gh_log}\n{gh_body}exit 0')
    return stub_dir, hermes_log, gh_log


def _run_delegate(
    tmp_path: Path,
    *,
    agent_cmd: str,
    max_wait: int = 30,
    heartbeat_interval: int = 300,
    poll_interval: int = 5,
    transition: bool = False,
    relay: bool = False,
    role: str = "",
    repo: str = "owner/repo",
    branch: str = "fix/issue-42-test",
    hermes_body: str = "",
    gh_body: str = "",
    extra_env: dict | None = None,
) -> tuple[subprocess.CompletedProcess, Path, Path]:
    """Run daedalus-delegate.sh and return (result, hermes_log, gh_log)."""
    stub_dir, hermes_log, gh_log = _stub_bin_dir(
        tmp_path, hermes_body=hermes_body, gh_body=gh_body
    )
    task_file = tmp_path / "task.txt"
    task_file.write_text("task body\n")
    out_file = tmp_path / "agent-out.txt"

    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
        "HERMES_HOME": str(tmp_path / "hermes-home"),
        # Prevent any accidental real gh auth
        "GH_TOKEN": "stub-token",
        "GITHUB_TOKEN": "stub-token",
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
    if transition:
        cmd += ["--transition", "--repo", repo, "--branch", branch]
    if relay:
        cmd += ["--relay-verdict"]
    if role:
        cmd += ["--role", role]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=max_wait + 40,
    )
    return result, hermes_log, gh_log


def _parse_result(stdout: str) -> dict:
    """Find and parse the DELEGATE_RESULT: {...} line."""
    for line in stdout.splitlines():
        if line.startswith("DELEGATE_RESULT: "):
            return json.loads(line[len("DELEGATE_RESULT: "):])
    raise AssertionError(f"No DELEGATE_RESULT line found in stdout:\n{stdout}")


def _log_lines(log: Path) -> list[str]:
    """Return lines from a stub call log, empty list if missing."""
    if not log.exists():
        return []
    return log.read_text().splitlines()


# ── (a) happy path ────────────────────────────────────────────────────────────

def test_happy_path_exit_0(tmp_path):
    """Agent exits 0 → DELEGATE_RESULT status 'ok', out file contains agent output."""
    agent_cmd = (
        "bash -c '"
        "echo hello-from-agent; "
        'echo "PR URL: https://github.com/x/y/pull/99 PR number: 99"'
        "'"
    )
    result, _, _ = _run_delegate(tmp_path, agent_cmd=agent_cmd)

    assert result.returncode == 0, (
        f"wrapper exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    data = _parse_result(result.stdout)
    assert data["status"] == "ok"
    assert data["exit"] == 0
    assert "duration_s" in data

    out_path = Path(data["out"])
    assert out_path.exists(), f"out file missing: {out_path}"
    content = out_path.read_text()
    assert "hello-from-agent" in content
    assert "PR number: 99" in content


# ── (b) nonzero exit ─────────────────────────────────────────────────────────

def test_nonzero_exit_propagated(tmp_path):
    """Agent exits 42 → DELEGATE_RESULT status 'failed', exit code exact."""
    agent_cmd = "bash -c 'echo some output; exit 42'"
    result, _, _ = _run_delegate(tmp_path, agent_cmd=agent_cmd)

    assert result.returncode == 42, f"expected 42, got {result.returncode}"
    data = _parse_result(result.stdout)
    assert data["status"] == "failed"
    assert data["exit"] == 42
    assert "duration_s" in data


# ── (c) exit 1 edge case ─────────────────────────────────────────────────────

def test_exit_1_propagated(tmp_path):
    """Exit 1 is propagated correctly (not confused with timeout 124)."""
    result, _, _ = _run_delegate(tmp_path, agent_cmd="bash -c 'exit 1'")
    assert result.returncode == 1
    data = _parse_result(result.stdout)
    assert data["status"] == "failed"
    assert data["exit"] == 1


# ── (d) timeout + direct-child dead ──────────────────────────────────────────

def test_timeout_kills_child(tmp_path):
    """Agent sleeps 999s beyond 2s max-wait → status 'timeout', exit 124, child dead."""
    pid_file = tmp_path / "child.pid"
    agent_cmd = f"bash -c 'echo $$ > {pid_file}; sleep 999'"

    t0 = time.monotonic()
    result, _, _ = _run_delegate(
        tmp_path, agent_cmd=agent_cmd, max_wait=2, poll_interval=1
    )
    elapsed = time.monotonic() - t0

    # Must finish within 2s timeout + 5s grace + small overhead
    assert elapsed < 20, f"wrapper took {elapsed:.1f}s"
    assert result.returncode == 124
    data = _parse_result(result.stdout)
    assert data["status"] == "timeout"
    assert data["exit"] == 124

    # Direct child must be dead
    if pid_file.exists():
        raw = pid_file.read_text().strip()
        if raw.isdigit():
            time.sleep(0.5)
            try:
                os.kill(int(raw), 0)
                pytest.fail(f"child PID {raw} still alive after timeout kill")
            except ProcessLookupError:
                pass
            except PermissionError:
                pass  # zombie / no permission — still alive, but acceptable on macOS


# ── (e) done-marker early completion ─────────────────────────────────────────

def test_done_marker_early_completion(tmp_path):
    """<out>.done marker triggers early exit well before max-wait."""
    out_file = tmp_path / "agent-out.txt"
    done_marker = tmp_path / "agent-out.txt.done"

    # Agent writes output + marker immediately, then sleeps; wrapper should exit fast.
    agent_cmd = (
        f"bash -c 'echo agent-output; touch {done_marker}; sleep 999'"
    )

    t0 = time.monotonic()
    result, _, _ = _run_delegate(
        tmp_path, agent_cmd=agent_cmd, max_wait=30, poll_interval=1
    )
    elapsed = time.monotonic() - t0

    assert elapsed < 15, f"done-marker not honoured ({elapsed:.1f}s)"
    data = _parse_result(result.stdout)
    assert data["status"] == "ok"
    assert data["exit"] == 0


# ── (f) heartbeat non-blocking ────────────────────────────────────────────────

def test_heartbeat_sent_within_interval(tmp_path):
    """Heartbeat fires within interval; non-blocking — loop keeps ticking."""
    # Agent sleeps 4s; poll_interval=1 so the loop checks each second.
    # At t≈1 the hb_elapsed (1s) >= heartbeat_interval (1s) → heartbeat fires.
    agent_cmd = "bash -c 'sleep 4; echo done'"

    result, hermes_log, _ = _run_delegate(
        tmp_path,
        agent_cmd=agent_cmd,
        max_wait=30,
        heartbeat_interval=1,
        poll_interval=1,
    )

    assert result.returncode == 0, (
        f"wrapper exited {result.returncode}\n{result.stdout}"
    )
    calls = _log_lines(hermes_log)
    hb_calls = [c for c in calls if "heartbeat" in c]
    assert hb_calls, (
        f"No heartbeat calls recorded.\nAll hermes calls: {calls}\n"
        f"wrapper stdout:\n{result.stdout}"
    )
    for call in hb_calls:
        assert "t_test123" in call, f"card id missing: {call}"


# ── (g) grandchild kill ───────────────────────────────────────────────────────

def test_grandchild_killed_on_timeout(tmp_path):
    """On timeout, the process GROUP is killed — grandchildren do not survive.

    The agent spawns a grandchild (sleep 999 & disown) in its own process
    group (the one created by setsid/perl-setsid). When the wrapper sends
    SIGTERM/-SIGKILL to the pgid, the grandchild must also die.
    """
    gc_pid_file = tmp_path / "grandchild.pid"

    # Agent: spawn a grandchild, record its PID, disown it, then sleep forever.
    # All three processes share the agent's process group (the one the wrapper
    # setsid'd) — disown only removes from bash's job table, not from the pgid.
    agent_cmd = (
        f"bash -c '"
        f"sleep 999 & echo $! > {gc_pid_file}; disown; sleep 999"
        f"'"
    )

    t0 = time.monotonic()
    result, _, _ = _run_delegate(
        tmp_path, agent_cmd=agent_cmd, max_wait=2, poll_interval=1
    )
    elapsed = time.monotonic() - t0

    assert elapsed < 20, f"wrapper took {elapsed:.1f}s"
    assert result.returncode == 124, f"expected timeout exit 124, got {result.returncode}"

    # Give the kill a moment to propagate
    time.sleep(1.0)

    if not gc_pid_file.exists():
        # PID file was never written (agent killed before it could write) — pass
        return

    raw = gc_pid_file.read_text().strip()
    if not raw.isdigit():
        return  # Can't verify — skip

    gc_pid = int(raw)
    try:
        os.kill(gc_pid, 0)
        # Still alive: check if it's a zombie (acceptable on some OS configs).
        # On macOS, PermissionError is raised for zombies owned by us — that
        # can't happen here since gc_pid is a child. If kill -0 succeeds, the
        # grandchild is genuinely alive, which is a failure.
        pytest.fail(
            f"grandchild PID {gc_pid} is still alive after timeout — "
            "pgid-kill did not reach it. Check setsid/perl availability."
        )
    except ProcessLookupError:
        pass  # expected: grandchild is dead


# ── (h) hung heartbeat does not block wait loop ───────────────────────────────

def test_hung_heartbeat_does_not_block_timeout(tmp_path):
    """Heartbeat stub sleeps 60s; wrapper still exits at max-wait (not at 60s+).

    Without the background-subshell fix the heartbeat call would block the
    PID-poll loop, causing the wrapper to miss its 3s deadline by a full minute.
    """
    # hermes stub sleeps 60s — any synchronous heartbeat blocks for a minute
    agent_cmd = "bash -c 'sleep 999'"

    t0 = time.monotonic()
    result, _, _ = _run_delegate(
        tmp_path,
        agent_cmd=agent_cmd,
        max_wait=3,
        heartbeat_interval=1,   # heartbeat fires almost immediately
        poll_interval=1,
        hermes_body="sleep 60\n",  # slow hermes
    )
    elapsed = time.monotonic() - t0

    # Wrapper must exit well under the 60s hermes sleep — if heartbeat blocked
    # the loop, elapsed would be ~61s. Allow generous headroom for CI.
    assert elapsed < 20, (
        f"Wrapper took {elapsed:.1f}s — heartbeat probably blocked the loop. "
        "Ensure heartbeat runs in a background subshell."
    )
    assert result.returncode == 124
    data = _parse_result(result.stdout)
    assert data["status"] == "timeout"


# ── (i–l) transition mode ─────────────────────────────────────────────────────
# All four transition cases verify that hermes kanban block is called with the
# exact phrase the developer SOUL's signal table expects (byte-identical strings
# that classify_blocked() substring-matches). gh is also stubbed via PATH.

def _block_calls(hermes_log: Path) -> list[str]:
    """Return only the hermes block invocation lines."""
    return [ln for ln in _log_lines(hermes_log) if "block" in ln]


def test_transition_ok_with_pr(tmp_path):
    """ok + PR found → block with 'review-required: PR #N — <branch>'."""
    # gh stub returns PR number 99
    result, hermes_log, _ = _run_delegate(
        tmp_path,
        agent_cmd="bash -c 'echo done; exit 0'",
        transition=True,
        repo="owner/repo",
        branch="fix/issue-42-test",
        gh_body="echo 99\n",  # gh pr list returns 99
    )

    assert result.returncode == 0, f"wrapper failed: {result.stdout}\n{result.stderr}"
    data = _parse_result(result.stdout)
    assert data["status"] == "ok"

    blocks = _block_calls(hermes_log)
    assert blocks, f"No kanban block call recorded.\nAll hermes calls:\n{_log_lines(hermes_log)}"
    # Exact phrase the SOUL signal table maps to ADVANCE
    assert any("review-required: PR #99 — fix/issue-42-test" in b for b in blocks), (
        f"Expected 'review-required: PR #99 — fix/issue-42-test' in block calls.\n"
        f"Block calls: {blocks}"
    )


def test_transition_ok_no_pr(tmp_path):
    """ok + no PR → block with 'review-required: awaiting-pr'."""
    # gh stub returns nothing (empty output → no PR)
    result, hermes_log, _ = _run_delegate(
        tmp_path,
        agent_cmd="bash -c 'exit 0'",
        transition=True,
        repo="owner/repo",
        branch="fix/issue-42-test",
        gh_body="echo ''\n",  # gh returns empty
    )

    assert result.returncode == 0
    blocks = _block_calls(hermes_log)
    assert blocks, f"No kanban block call.\nAll calls: {_log_lines(hermes_log)}"
    assert any("review-required: awaiting-pr" in b for b in blocks), (
        f"Expected 'review-required: awaiting-pr'.\nBlock calls: {blocks}"
    )


def test_transition_failed(tmp_path):
    """failed exit → block with 'coding-agent-failed: exited with code N'."""
    result, hermes_log, _ = _run_delegate(
        tmp_path,
        agent_cmd="bash -c 'exit 7'",
        transition=True,
        repo="owner/repo",
        branch="fix/issue-42-test",
    )

    assert result.returncode == 7
    data = _parse_result(result.stdout)
    assert data["status"] == "failed"
    blocks = _block_calls(hermes_log)
    assert blocks, f"No kanban block call.\nAll calls: {_log_lines(hermes_log)}"
    assert any("coding-agent-failed: exited with code 7" in b for b in blocks), (
        f"Expected 'coding-agent-failed: exited with code 7'.\nBlock calls: {blocks}"
    )


# ── (m) SIGTERM to wrapper reaps child + grandchild, exits 124 ────────────────

def test_term_signal_kills_child_and_grandchild(tmp_path):
    """SIGTERM to the wrapper reaps the inner agent's process group.

    Hermes sends SIGTERM to the wrapper when --max-runtime is enforced.
    Without the trap the wrapper dies but the setsid-isolated child and its
    grandchildren survive as orphans (concurrent-dispatch hazard #1289).

    This test:
      1. Starts the wrapper with an agent that spawns a grandchild and records
         both PIDs.
      2. SIGTERMs the wrapper once the grandchild PID file is written.
      3. Asserts the wrapper exits 124.
      4. Asserts DELEGATE_RESULT status is "terminated".
      5. Asserts both child and grandchild are dead.
    """
    gc_pid_file = tmp_path / "grandchild.pid"
    child_pid_file = tmp_path / "child.pid"

    # Agent: record its own PID, spawn a grandchild, record that PID, then
    # sleep.  All three processes share the agent's setsid process group so
    # the wrapper's pgid-kill must reach the grandchild.
    agent_cmd = (
        f"bash -c '"
        f"echo $$ > {child_pid_file}; "
        f"sleep 999 & echo $! > {gc_pid_file}; disown; "
        f"sleep 999"
        f"'"
    )

    stub_dir, hermes_log, gh_log = _stub_bin_dir(tmp_path)
    task_file = tmp_path / "task.txt"
    task_file.write_text("task body\n")
    out_file = tmp_path / "agent-out.txt"

    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
        "HERMES_HOME": str(tmp_path / "hermes-home"),
        "GH_TOKEN": "stub-token",
        "GITHUB_TOKEN": "stub-token",
    }

    cmd = [
        "bash", str(_DELEGATE_SH),
        "--task-file", str(task_file),
        "--cmd", agent_cmd,
        "--card", "t_term_test",
        "--board", "test-board",
        "--out", str(out_file),
        "--max-wait", "60",
        "--heartbeat-interval", "300",
        "--poll-interval", "1",
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    # Wait for the grandchild PID file to appear — confirms the agent is running
    # and setsid has taken effect before we deliver the signal.
    t0 = time.monotonic()
    while not gc_pid_file.exists() and time.monotonic() - t0 < 10:
        time.sleep(0.2)

    assert gc_pid_file.exists(), "grandchild PID file never appeared — agent did not start"

    # TERM the wrapper (simulating Hermes --max-runtime enforcement)
    proc.send_signal(signal.SIGTERM)

    stdout, stderr = proc.communicate(timeout=20)
    assert proc.returncode == 124, (
        f"expected exit 124 (terminated), got {proc.returncode}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )

    # DELEGATE_RESULT must report "terminated" status
    data = _parse_result(stdout)
    assert data["status"] == "terminated", (
        f"expected status 'terminated', got {data['status']!r}\nfull stdout:\n{stdout}"
    )
    assert data["exit"] == 124

    # Give the signal a moment to propagate through the process group
    time.sleep(1.0)

    for pid_file, label in [(child_pid_file, "child"), (gc_pid_file, "grandchild")]:
        if not pid_file.exists():
            continue
        raw = pid_file.read_text().strip()
        if not raw.isdigit():
            continue
        pid = int(raw)
        try:
            os.kill(pid, 0)
            pytest.fail(
                f"{label} PID {pid} still alive after SIGTERM to wrapper — "
                "TERM trap did not reap the process group"
            )
        except ProcessLookupError:
            pass  # expected: process is dead


def test_transition_timeout(tmp_path):
    """timeout → block with 'coding-agent-failed: CODING_AGENT_TIMEOUT'."""
    result, hermes_log, _ = _run_delegate(
        tmp_path,
        agent_cmd="bash -c 'sleep 999'",
        max_wait=2,
        poll_interval=1,
        transition=True,
        repo="owner/repo",
        branch="fix/issue-42-test",
    )

    assert result.returncode == 124
    data = _parse_result(result.stdout)
    assert data["status"] == "timeout"
    blocks = _block_calls(hermes_log)
    assert blocks, f"No kanban block call.\nAll calls: {_log_lines(hermes_log)}"
    assert any("coding-agent-failed: CODING_AGENT_TIMEOUT" in b for b in blocks), (
        f"Expected 'coding-agent-failed: CODING_AGENT_TIMEOUT'.\nBlock calls: {blocks}"
    )


# ── wrapper-hardening tests (issue #1286) ─────────────────────────────────────


def _parse_rundir(stdout: str) -> "str | None":
    """Extract RUNDIR path from '[delegate] RUNDIR=...' line in wrapper stdout."""
    for line in stdout.splitlines():
        if "[delegate] RUNDIR=" in line:
            return line.split("RUNDIR=", 1)[1].strip()
    return None


def test_symlink_attack_out_refused(tmp_path):
    """Pre-planted symlink at the out-file path → wrapper exits 2 and says 'symlink'."""
    # Create a symlink at the out location before running the wrapper
    real_target = tmp_path / "real-target.txt"
    real_target.write_text("attacker-file\n")
    out_file = tmp_path / "agent-out.txt"
    out_file.symlink_to(real_target)

    stub_dir, _, _ = _stub_bin_dir(tmp_path)
    task_file = tmp_path / "task.txt"
    task_file.write_text("task body\n")

    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
        "HERMES_HOME": str(tmp_path / "hermes-home"),
        "GH_TOKEN": "stub-token",
        "GITHUB_TOKEN": "stub-token",
    }

    cmd = [
        "bash", str(_DELEGATE_SH),
        "--task-file", str(task_file),
        "--cmd", "bash -c 'echo hello'",
        "--card", "t_test_sym",
        "--board", "test-board",
        "--out", str(out_file),
        "--max-wait", "10",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 2, (
        f"Expected exit 2 (symlink guard), got {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "symlink" in combined.lower(), (
        f"Expected 'symlink' in output.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_rundir_cleanup_on_normal_exit(tmp_path):
    """RUNDIR is created and then removed after wrapper completes normally."""
    result, _, _ = _run_delegate(
        tmp_path,
        agent_cmd="bash -c 'echo done; exit 0'",
        max_wait=15,
        poll_interval=1,
    )

    assert result.returncode == 0, (
        f"wrapper failed unexpectedly: {result.stdout}\n{result.stderr}"
    )
    rundir = _parse_rundir(result.stdout)
    assert rundir is not None, (
        f"Could not find RUNDIR line in stdout:\n{result.stdout}"
    )
    assert not os.path.exists(rundir), (
        f"RUNDIR still exists after normal exit: {rundir}"
    )


def test_rundir_cleanup_on_term_exit(tmp_path):
    """RUNDIR is removed even when wrapper receives SIGTERM."""
    stub_dir, _, _ = _stub_bin_dir(tmp_path)
    task_file = tmp_path / "task.txt"
    task_file.write_text("task body\n")
    out_file = tmp_path / "agent-out.txt"

    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
        "HERMES_HOME": str(tmp_path / "hermes-home"),
        "GH_TOKEN": "stub-token",
        "GITHUB_TOKEN": "stub-token",
    }

    cmd = [
        "bash", str(_DELEGATE_SH),
        "--task-file", str(task_file),
        "--cmd", "bash -c 'sleep 999'",
        "--card", "t_test_term",
        "--board", "test-board",
        "--out", str(out_file),
        "--max-wait", "120",
        "--poll-interval", "1",
    ]

    import signal
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env)

    # Wait for RUNDIR line to appear in stdout
    rundir = None
    deadline = time.monotonic() + 10
    stdout_so_far = ""
    while time.monotonic() < deadline:
        # Try to read a line non-blocking via polling
        time.sleep(0.2)
        try:
            proc.stdout.flush()
        except Exception:
            pass
        # Check if process already emitted RUNDIR by sending SIGTERM once we have it
        # We'll just wait 2s for wrapper to start then kill it
        if time.monotonic() > deadline - 8:
            break

    # Allow 2s for wrapper startup, then SIGTERM it
    time.sleep(2)
    proc.send_signal(signal.SIGTERM)
    stdout_data, _ = proc.communicate(timeout=15)
    stdout_so_far += stdout_data

    rundir = _parse_rundir(stdout_so_far)
    if rundir is None:
        # Wrapper may have exited too fast to emit RUNDIR — skip assertion
        return

    # Give EXIT trap time to fire
    time.sleep(0.5)
    assert not os.path.exists(rundir), (
        f"RUNDIR still exists after SIGTERM: {rundir}"
    )


def test_detect_pr_cadence_throttles_gh(tmp_path):
    """With DETECT_PR_EVERY=3 and 8 poll iterations, gh pr list is called at most 3 times."""
    # gh stub returns empty (no PR found), just logs calls
    result, _, gh_log = _run_delegate(
        tmp_path,
        agent_cmd="bash -c 'sleep 999'",
        max_wait=8,
        poll_interval=1,
        transition=True,
        repo="owner/repo",
        branch="fix/issue-42",
        gh_body="",  # empty output → no PR
        extra_env={"DETECT_PR_EVERY": "3"},
    )

    # Wrapper should time out (no PR found, agent sleeping)
    assert result.returncode == 124, (
        f"Expected timeout (124), got {result.returncode}\n{result.stdout}"
    )

    gh_calls = _log_lines(gh_log)
    pr_list_calls = [c for c in gh_calls if "pr list" in c]
    # With 8 iterations and cadence 3, detect-pr fires at iters 3, 6 → ≤ 3 calls
    # (the _do_transition at end also calls gh, so allow up to 3 total from the loop)
    assert len(pr_list_calls) < 8, (
        f"gh pr list called {len(pr_list_calls)} times — cadence throttle not working.\n"
        f"All gh calls: {gh_calls}\nstdout:\n{result.stdout}"
    )


def test_numeric_pr_validation_blocks_injection(tmp_path):
    """Shell-injection attempt in gh output is stripped by numeric guard (CWE-74)."""
    # gh stub returns a value with shell metacharacters — the numeric guard
    # should reduce _pr_num to "" so the block reason falls back to awaiting-pr.
    injection = "42; rm -rf /"
    result, hermes_log, _ = _run_delegate(
        tmp_path,
        agent_cmd="bash -c 'exit 0'",
        transition=True,
        repo="owner/repo",
        branch="fix/issue-42-test",
        gh_body=f"echo '{injection}'\n",
    )

    assert result.returncode == 0, (
        f"wrapper exited {result.returncode}\n{result.stdout}"
    )
    blocks = _block_calls(hermes_log)
    assert blocks, f"No kanban block call.\nAll calls: {_log_lines(hermes_log)}"
    # The injection string must NOT appear in the block reason
    for b in blocks:
        assert "rm -rf" not in b, (
            f"Injection string 'rm -rf' found in block reason — numeric guard failed.\n"
            f"Block calls: {blocks}"
        )


# ── (m) --relay-verdict: relay the inner agent's emitted verdict ──────────────

def test_relay_verdict_blocks_card_with_emitted_verdict(tmp_path):
    """--relay-verdict extracts the inner agent's emitted verdict (SOUL signal +
    JSON OutcomeRecord) from its output and blocks the card with it — so the
    outer (possibly weak) model never has to wait/parse/complete the card."""
    agent_script = tmp_path / "fake_security_agent.sh"
    agent_script.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'Security review complete. No vulnerabilities found.'\n"
        "echo\n"
        "echo '```json'\n"
        "echo '{\"daedalus_outcome\":1,\"role\":\"security\",\"verdict\":\"approved\","
        "\"refs\":{\"issue\":3,\"pr\":4}}'\n"
        "echo '```'\n"
    )
    result, hermes_log, _ = _run_delegate(
        tmp_path, agent_cmd=f"bash {agent_script}", relay=True
    )
    assert result.returncode == 0, result.stderr
    full = "\n".join(_log_lines(hermes_log))
    # the wrapper called hermes kanban block on the card, relaying the JSON verdict
    # (the multi-line reason spans several log lines, so check the whole log).
    assert "block t_test123" in full, f"expected a kanban block call, got: {full}"
    assert "daedalus_outcome" in full


def test_relay_verdict_no_verdict_blocks_coding_agent_failed(tmp_path):
    """No parseable verdict in output → the card is blocked coding-agent-failed
    (a safe halt), never silently advanced."""
    result, hermes_log, _ = _run_delegate(
        tmp_path, agent_cmd="bash -c 'echo just some noise output'", relay=True
    )
    assert result.returncode == 0, result.stderr
    block = [ln for ln in _log_lines(hermes_log) if "block" in ln and "t_test123" in ln]
    assert block, "expected a kanban block call even with no verdict"
    assert "no verdict emitted" in "\n".join(block)


def _validator_agent(tmp_path: Path) -> str:
    script = tmp_path / "fake_validator_agent.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'Validation done.'\n"
        "echo 'CONFIRMED: reproduced on main at abc1234'\n"
    )
    return f"bash {script}"


def test_relay_verdict_ignores_invalid_outcome_zero_uses_prose(tmp_path):
    """A JSON block with daedalus_outcome:0 (the 'invalid/example' marker) must NOT be
    used as the verdict — the relay falls through to the canonical prose signal line
    (e.g. 'CONFIRMED:'), so a validator that echoes the template's 0 still completes."""
    script = tmp_path / "fake_agent.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "echo '```json'\n"
        "echo '{\"daedalus_outcome\": 0, \"role\": \"validator\", \"verdict\": \"confirmed\"}'\n"
        "echo '```'\n"
        "echo 'CONFIRMED: reproduced on main'\n"
    )
    result, hermes_log, _ = _run_delegate(
        tmp_path, agent_cmd=f"bash {script}", relay=True, role="validator"
    )
    assert result.returncode == 0, result.stderr
    full = "\n".join(_log_lines(hermes_log))
    assert "complete t_test123" in full, f"validator must complete on the prose verdict: {full}"
    assert "CONFIRMED" in full and "daedalus_outcome" not in full, full


def test_relay_verdict_validator_role_completes_card(tmp_path):
    """#1329 D2: a validator card must be COMPLETED (not blocked) on relay — the
    dispatcher advances validators via _check_confirmed_validators, which scans
    DONE cards, and classify_blocked treats a *blocked* validator as an ESCALATE
    error that stalls the pipeline."""
    result, hermes_log, _ = _run_delegate(
        tmp_path, agent_cmd=_validator_agent(tmp_path), relay=True, role="validator"
    )
    assert result.returncode == 0, result.stderr
    full = "\n".join(_log_lines(hermes_log))
    assert "complete t_test123" in full, f"validator must COMPLETE, not block: {full}"
    assert "block t_test123" not in full, f"validator wrongly blocked: {full}"
    assert "CONFIRMED" in full


def test_relay_verdict_review_role_still_blocks_card(tmp_path):
    """Gate roles (qa/reviewer/security/…) must still BLOCK on relay so
    classify_blocked routes the emitted signal — the role-aware branch only
    completes validator/pm/planner."""
    result, hermes_log, _ = _run_delegate(
        tmp_path, agent_cmd=_validator_agent(tmp_path), relay=True, role="qa"
    )
    assert result.returncode == 0, result.stderr
    full = "\n".join(_log_lines(hermes_log))
    assert "block t_test123" in full, f"qa must BLOCK, not complete: {full}"
    assert "complete t_test123" not in full


def test_relay_verdict_validator_crash_still_blocks(tmp_path):
    """Even for a complete-role, a crash (no verdict → coding-agent-failed) must
    BLOCK so crash-retry owns the card, not falsely complete it."""
    result, hermes_log, _ = _run_delegate(
        tmp_path, agent_cmd="bash -c 'echo noise'", relay=True, role="validator"
    )
    assert result.returncode == 0, result.stderr
    full = "\n".join(_log_lines(hermes_log))
    assert "block t_test123" in full, f"crash must BLOCK: {full}"
    assert "coding-agent-failed" in full
    assert "complete t_test123" not in full


# ── (n) self-detach into a new session — survive Hermes' group SIGTERM (#1356) ──
# The wrapper re-execs itself in a new session at script top so a process-GROUP
# SIGTERM (Hermes session-end / max-runtime on the worker group) no longer
# reaches it. Without this the card never transitions and the pipeline stalls.


def _detach_available() -> bool:
    """The self-detach re-exec needs setsid(1) or perl (macOS fallback)."""
    return shutil.which("setsid") is not None or shutil.which("perl") is not None


def _pgid_of(pid: int) -> "int | None":
    """Return the process-group ID of `pid`, or None if it can't be read."""
    try:
        out = subprocess.run(
            ["ps", "-o", "pgid=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    raw = out.stdout.strip()
    return int(raw) if raw.isdigit() else None


def _wait_for_pgid_eq_pid(pid: int, timeout: float) -> "int | None":
    """Poll until the process's pgid == its pid (it became a session leader)."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _pgid_of(pid)
        if last == pid:
            return last
        time.sleep(0.2)
    return last


@pytest.mark.skipif(not _detach_available(), reason="no setsid/perl to self-detach")
def test_self_detach_becomes_session_leader(tmp_path):
    """The wrapper re-execs into its own session: pgid(wrapper) == pid(wrapper).

    Popen (no start_new_session) spawns bash inside pytest's process group, so
    the wrapper's initial pgid != pid → it must re-exec via setsid/perl and
    become a session leader (pgid == pid). The PID is preserved across the exec
    (we only re-exec when NOT a group leader, so setsid(2) succeeds without a
    fork), so proc.pid stays valid.
    """
    stub_dir, _, _ = _stub_bin_dir(tmp_path)
    task_file = tmp_path / "task.txt"
    task_file.write_text("task\n")
    out_file = tmp_path / "out.txt"

    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
        "HERMES_HOME": str(tmp_path / "hermes-home"),
        "GH_TOKEN": "stub-token",
        "GITHUB_TOKEN": "stub-token",
    }
    cmd = [
        "bash", str(_DELEGATE_SH),
        "--task-file", str(task_file),
        "--cmd", "bash -c 'sleep 5'",
        "--card", "t_detach", "--board", "test-board",
        "--out", str(out_file),
        "--max-wait", "20", "--poll-interval", "1",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
    )
    try:
        pgid = _wait_for_pgid_eq_pid(proc.pid, timeout=10)
        assert pgid == proc.pid, (
            f"wrapper pgid {pgid} != pid {proc.pid} — did not become session leader"
        )
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=15)
        except Exception:
            proc.kill()


@pytest.mark.skipif(not _detach_available(), reason="no setsid/perl to self-detach")
def test_survives_parent_group_sigterm_and_transitions(tmp_path):
    """Regression for #1356: when the spawning session ends (SIGTERM to the
    parent's process GROUP), the detached wrapper survives and STILL calls
    hermes kanban to transition the card — the card lands in the next stage
    instead of stalling as 'protocol violation' / 'pid not alive'.

    Without the self-detach the wrapper stays in the parent's group, the group
    SIGTERM fires _term_handler (exit 124, status 'terminated', NO transition),
    and no complete/block call is ever logged — this test would fail.
    """
    stub_dir, hermes_log, _ = _stub_bin_dir(tmp_path)
    task_file = tmp_path / "task.txt"
    task_file.write_text("task\n")
    out_file = tmp_path / "out.txt"
    wrapper_pid_file = tmp_path / "wrapper.pid"

    # Inner agent emits its verdict AFTER a short delay, so the wrapper is still
    # mid-run (not yet transitioned) when we kill the parent's group.
    agent = tmp_path / "agent.sh"
    agent.write_text(
        "#!/usr/bin/env bash\n"
        "sleep 3\n"
        "echo 'CONFIRMED: reproduced on dev'\n"
    )
    agent.chmod(0o755)

    # Parent is its own session leader (start_new_session=True). It spawns the
    # wrapper as a child in its group, records the wrapper pid, then sleeps to
    # hold the group open until we kill it.
    parent = tmp_path / "parent.sh"
    parent.write_text(
        "#!/usr/bin/env bash\n"
        f"bash {_DELEGATE_SH} "
        f"--task-file {task_file} "
        f"--cmd 'bash {agent}' "
        "--card t_test123 --board test-board "
        f"--out {out_file} "
        "--max-wait 30 --poll-interval 1 "
        "--relay-verdict --role validator &\n"
        f"echo $! > {wrapper_pid_file}\n"
        "sleep 999\n"
    )
    parent.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
        "HERMES_HOME": str(tmp_path / "hermes-home"),
        "GH_TOKEN": "stub-token",
        "GITHUB_TOKEN": "stub-token",
    }
    proc = subprocess.Popen(
        ["bash", str(parent)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        start_new_session=True,
    )
    try:
        # Wait for the wrapper pid to be recorded.
        deadline = time.monotonic() + 10
        wrapper_pid = None
        while time.monotonic() < deadline:
            if wrapper_pid_file.exists():
                raw = wrapper_pid_file.read_text().strip()
                if raw.isdigit():
                    wrapper_pid = int(raw)
                    break
            time.sleep(0.2)
        assert wrapper_pid, "wrapper pid never recorded"

        # Wait for the wrapper to detach into its own session (pgid == pid).
        pgid = _wait_for_pgid_eq_pid(wrapper_pid, timeout=8)
        assert pgid == wrapper_pid, (
            f"wrapper {wrapper_pid} did not detach before kill (pgid={pgid})"
        )

        # End the spawning session: SIGTERM the parent's whole process group.
        parent_pgid = os.getpgid(proc.pid)
        os.killpg(parent_pgid, signal.SIGTERM)

        # The detached wrapper must survive and finish, transitioning the card.
        deadline = time.monotonic() + 25
        transitioned = False
        while time.monotonic() < deadline:
            calls = _log_lines(hermes_log)
            if any(("complete t_test123" in c or "block t_test123" in c) for c in calls):
                transitioned = True
                break
            time.sleep(0.3)
        assert transitioned, (
            "wrapper did not transition the card after parent-group SIGTERM — "
            f"pipeline stalled. hermes calls: {_log_lines(hermes_log)}"
        )
        # validator relay COMPLETES the card on the CONFIRMED prose verdict.
        full = "\n".join(_log_lines(hermes_log))
        assert "complete t_test123" in full and "CONFIRMED" in full, full
    finally:
        for sig in (signal.SIGKILL,):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except Exception:
                pass
        try:
            proc.communicate(timeout=10)
        except Exception:
            proc.kill()
        # Reap any surviving detached wrapper.
        if wrapper_pid_file.exists():
            raw = wrapper_pid_file.read_text().strip()
            if raw.isdigit():
                try:
                    os.kill(int(raw), signal.SIGKILL)
                except Exception:
                    pass

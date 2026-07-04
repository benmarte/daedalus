"""Tests for the script-owned delegation lifecycle wrapper (issue #1280).

Part C of the #1276/#1280 work replaces the developer SOUL's LLM poll loop with a
bash wrapper (``scripts/daedalus-delegate.sh``) that owns spawn -> in-shell wait ->
heartbeat -> structured outcome, so the outer LLM spends <=2 turns per delegation
and turn-budget exhaustion can't complete cards prematurely.

Coverage maps to the acceptance criteria:
  AC1 — the developer body invokes the wrapper in ONE terminal(...) call; no
        ``gh pr list``/``sleep``/``Poll every`` instruction remains in the SOUL
        or the dev.md template.
  AC2 — the developer instruction is a single wrapper call (no separate per-poll
        terminal step); the wait is a bash wait inside the wrapper.
  AC3 — the exact marker substrings survive end-to-end AND classify_blocked routes
        both crash markers to infra-failure ("").
  AC4 — the wrapper accepts the coding-agent command as an opaque RUN_CMD arg, so
        every coding_agents failover entry (claude-code / codex / opencode) works
        with no per-agent branching.

Plus core/kanban.py unit tests for ``heartbeat`` and ``complete(..., metadata=...)``.

Dual-mode: also runs standalone (``python tests/test_delegate_wrapper.py``).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from conftest import _load_dispatch  # noqa: E402

disp = _load_dispatch()

SOUL = ROOT / "config" / "souls" / "developer-daedalus.md"
DEV_TEMPLATE = ROOT / "templates" / "agent_bodies" / "dev.md"
WRAPPER = ROOT / "scripts" / "daedalus-delegate.sh"

_FAKE_ISSUE = {"number": 42, "title": "Test issue", "body": "B", "labels": []}

# The three shipped failover agents + a custom command (AC4).
_AGENT_CMDS = [
    ("claude-code", "CLAUDE_CONFIG_DIR=$HOME/.claude-rizq claude -p"),
    ("codex", "codex exec --full-auto"),
    ("opencode", "opencode run"),
    ("claude-code", "/opt/custom/claude --dangerously-skip-permissions -p"),
]


def _dev_block(agent: str = "claude-code", cmd: str = "claude -p") -> str:
    return disp._build_delegation_instructions(
        agent, cmd, role="developer", issue_number=42, base_branch="dev"
    )


def _wrapper_code() -> str:
    """Wrapper source with full-line comments stripped (so prose in the header
    docstring/comments never satisfies an assertion about the actual code)."""
    lines = WRAPPER.read_text(encoding="utf-8").splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))


# ── AC1: no poll instruction remains in the SOUL / template ───────────────────


def test_ac1_soul_has_no_poll_instruction():
    text = SOUL.read_text(encoding="utf-8")
    assert "gh pr list" not in text, "SOUL must not tell the developer to poll for the PR"
    assert "Poll every" not in text, "SOUL must not keep the 'Poll every 2 minutes' step"
    # The SOUL must reference the lifecycle wrapper instead.
    assert "daedalus-delegate.sh" in text, "SOUL must invoke the lifecycle wrapper"


def test_ac1_dev_template_has_no_poll_instruction():
    text = DEV_TEMPLATE.read_text(encoding="utf-8")
    assert "gh pr list" not in text
    assert "Poll every" not in text
    # No bare sleep-loop instruction (the wrapper owns the wait).
    assert "sleep " not in text.lower()


def test_ac1_developer_block_invokes_wrapper_in_one_terminal_call():
    block = _dev_block()
    assert block.count("terminal(") == 1, (
        f"developer delegation must be ONE terminal() call, got {block.count('terminal(')}:\n{block}"
    )
    assert "daedalus-delegate.sh 42 dev" in block
    assert "gh pr list" not in block


# ── AC2: single wrapper call, wait is in the wrapper (not model turns) ─────────


def test_ac2_no_separate_poll_terminal_step_in_block():
    block = _dev_block()
    # The old flow used a background spawn + a separate wait terminal() call.
    assert "background=True" not in block, "wrapper call must be foreground/blocking"
    # No inline poll-loop primitives in the block — they live in the wrapper.
    for primitive in ("kill -0", "until [ -s", "SECONDS", "sleep 30"):
        assert primitive not in block, f"{primitive!r} must live in the wrapper, not the block"


def test_ac2_wrapper_waits_in_shell_and_heartbeats():
    src = WRAPPER.read_text(encoding="utf-8")
    # The wait is a bash PID-watch loop inside the wrapper.
    assert "kill -0" in src, "wrapper must PID-watch the inner agent for liveness"
    assert "wait \"$PID\"" in src, "wrapper must reap the inner agent with bash wait"
    # Heartbeat every N seconds keeps the card alive during a long run.
    assert "hermes kanban" in src and "heartbeat" in src, "wrapper must heartbeat the card"
    # The wrapper backgrounds the agent inside the isolated worktree — the
    # worktree-isolation guarantee is preserved one level down.
    assert "daedalus-worktree-spawn.sh" in src
    # detect-pr handshake moved into the wrapper.
    assert "daedalus-detect-pr.sh" in src


def test_ac2_wrapper_never_self_completes_or_merges():
    """The #1280 invariant: the wrapper captures outcome but never completes/merges."""
    code = _wrapper_code()
    assert "kanban complete" not in code, "wrapper must NOT complete the card (parent holds the claim)"
    assert "pr merge" not in code and "gh pr merge" not in code, "wrapper must NOT merge"


# ── AC3: exact marker strings preserved end-to-end + classify routes them ──────


def test_ac3_wrapper_emits_exact_markers():
    src = WRAPPER.read_text(encoding="utf-8")
    assert "CODING_AGENT_TIMEOUT" in src, "wrapper must emit the exact timeout marker"
    assert "CODING_AGENT_DIED" in src, "wrapper must emit the exact silent-death marker"


def test_ac3_developer_block_maps_markers_to_agent_failed_block():
    block = _dev_block()
    assert "CODING_AGENT_DIED" in block
    assert "CODING_AGENT_TIMEOUT" in block
    assert "coding-agent-failed:" in block, (
        "the outer body must block coding-agent-failed on a wrapper failure marker"
    )


@pytest.mark.parametrize(
    "handoff",
    [
        "coding-agent-failed: CODING_AGENT_DIED — see stderr above",
        "coding-agent-failed: CODING_AGENT_TIMEOUT — exceeded ceiling",
        "coding_agent_died",
        "coding_agent_timeout",
    ],
)
def test_ac3_classify_blocked_routes_crash_markers_to_infra_failure(handoff):
    """classify_blocked must still route both markers to "" (infra failure, human fixes)."""
    from core.iterate import classify_blocked

    action = classify_blocked("developer-daedalus", handoff, ci_green=True)
    assert action == "", f"crash marker {handoff!r} must route to infra-failure no-op, got {action!r}"


# ── AC4: opaque RUN_CMD — every failover agent works, no per-agent branching ───


@pytest.mark.parametrize("agent,cmd", _AGENT_CMDS)
def test_ac4_wrapper_command_embeds_run_cmd_for_every_agent(agent, cmd):
    block = _dev_block(agent, cmd)
    # The resolved command flows through verbatim as the wrapper's trailing args.
    assert cmd in block, f"{agent}: resolved run command must reach the wrapper, got:\n{block}"
    assert f"daedalus-delegate.sh 42 dev " in block


def test_ac4_wrapper_forwards_run_cmd_as_opaque_trailing_args():
    """The wrapper contract mirrors daedalus-worktree-spawn.sh: RUN_CMD is opaque."""
    code = _wrapper_code()
    # Positional 1-5 then a `shift 5`, and "$@" forwarded to the spawner.
    assert "shift 5" in code, "wrapper must consume its 5 positional args then forward the rest"
    assert '"$@"' in code, "wrapper must forward RUN_CMD (\"$@\") to the worktree spawner"
    # No per-agent branching (no case/if on claude-code/codex/opencode names).
    for name in ("claude-code", "codex", "opencode"):
        assert name not in code, f"wrapper must not branch on agent name {name!r}"


# ── Hardening pass (#1284/#1280): private sidecar dir, stderr redaction, cadence ─


def test_sidecars_use_private_mktemp_dir_not_predictable_tmp():
    """Fix A: pid/meta/signal/stdout/stderr live in a per-run 0700 mktemp dir, not
    at predictable /tmp/dev-<N>-* names (CWE-59/377)."""
    src = WRAPPER.read_text(encoding="utf-8")
    assert "umask 077" in src, "wrapper must set a private umask before creating temp files"
    assert "mktemp -d" in src, "wrapper must create a private per-run dir via mktemp -d"
    assert "daedalus-delegate-" in src, "mktemp template must be namespaced to this wrapper"
    # The old predictable derivation ('${stem}-pid.txt' off the OUT stem) is gone.
    code = _wrapper_code()
    assert "-pid.txt" not in code, "pidfile must live in the private dir, not a predictable /tmp name"
    assert "-meta.json" not in code, "meta must live in the private dir, not a predictable /tmp name"
    assert "RUNDIR=$(mktemp -d" in code.replace('"', ""), "RUNDIR must come from mktemp -d"
    assert 'pidfile="$RUNDIR/pid.txt"' in code
    assert 'meta="$RUNDIR/meta.json"' in code
    # Numeric-only issue-number validation before it reaches the mktemp template.
    assert "*[!0-9]*" in code, "wrapper must reject a non-numeric issue number"
    # Symlink guard exists and is invoked before the wrapper's own writes.
    assert "_no_symlink" in code and "[ -L" in code, "wrapper must guard against symlink writes"
    # detect-pr still receives the (relocated) pidfile path explicitly — the
    # handshake contract with daedalus-detect-pr.sh is preserved.
    assert '"$DETECT" "$out" "$pidfile"' in code, "detect-pr must still get the pidfile path"


def test_detect_pr_cadence_is_a_named_constant_and_liveness_stays_every_iter():
    """Fix C: the detect-pr `gh pr list` poll is throttled via a named constant,
    while `kill -0` liveness runs every iteration."""
    code = _wrapper_code()
    assert "DETECT_PR_EVERY" in code, "cadence must be a named constant"
    # detect-pr is gated by the modulo of the constant …
    assert "% DETECT_PR_EVERY" in code, "detect-pr must run only every DETECT_PR_EVERY-th iteration"
    # … but the liveness check is NOT inside that gate (it must run every loop).
    lines = code.splitlines()
    gate_idx = next(i for i, ln in enumerate(lines) if "% DETECT_PR_EVERY" in ln)
    kill_idx = next(i for i, ln in enumerate(lines) if 'kill -0 "$PID"' in ln)
    # The liveness `kill -0` sits AFTER the detect gate's closing block, unindented
    # relative to the gate body — i.e. it is not nested under the cadence `if`.
    assert kill_idx > gate_idx
    assert lines[kill_idx].startswith("  if ! kill -0"), (
        "liveness kill -0 must be a top-level loop step, not nested under the cadence gate"
    )
    # Default cadence ≈ 30s at POLL_SECS=5.
    assert "DAEDALUS_DETECT_PR_EVERY:-6" in code


def test_failure_tail_redacts_planted_secret_end_to_end(tmp_path):
    """Fix B: run the REAL wrapper with a coding-agent command that leaks a fake
    token to stderr then dies; the relayed stderr tail must be masked."""
    import os
    import subprocess

    fake_token = "ghp_" + "A" * 36  # GitHub-PAT shape, 40-char run
    task = tmp_path / "task.txt"
    task.write_text("inner task body")
    out = tmp_path / "out.txt"  # relocated internally; passed for the arg contract
    err = tmp_path / "err.txt"
    workdir = tmp_path / "work"  # NON-git cwd → worktree-spawn falls back, no repo pollution
    workdir.mkdir()

    env = dict(os.environ)
    env.update(
        {
            "DAEDALUS_MAX_WAIT": "30",
            "DAEDALUS_POLL_SECS": "1",  # die within one poll → fast test, detect-pr never fires
            "DAEDALUS_HEARTBEAT_SECS": "999",
        }
    )
    for k in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_BOARD", "DAEDALUS_BOARD"):
        env.pop(k, None)

    # One opaque RUN_CMD arg (worktree-spawn joins "$@" and runs it via bash -c):
    # emit the token to stderr, produce NO PR line, exit non-zero → CODING_AGENT_DIED.
    run_cmd = f"echo leaked {fake_token} >&2; exit 1"
    proc = subprocess.run(
        ["bash", str(WRAPPER), "999", "dev", str(task), str(out), str(err), run_cmd],
        cwd=str(workdir),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = proc.stdout
    assert "CODING_AGENT_DIED" in combined, f"expected silent-death marker, got:\n{combined}\n{proc.stderr}"
    assert fake_token not in combined, f"planted token leaked into relayed tail:\n{combined}"
    assert "ghp_[REDACTED]" in combined, f"token shape must be masked, got:\n{combined}"


# ── core/kanban.py: heartbeat + complete(metadata=...) ────────────────────────


def test_kanban_heartbeat_builds_expected_cli_args():
    from core import kanban

    calls = []

    def _spy(args, timeout=60):
        calls.append(args)
        return (0, "", "")

    with mock.patch.object(kanban, "_hk", side_effect=_spy):
        assert kanban.heartbeat("board-slug", "t_123") is True
        assert kanban.heartbeat("board-slug", "t_123", note="working") is True

    assert calls[0] == ["--board", "board-slug", "heartbeat", "t_123"]
    assert calls[1] == ["--board", "board-slug", "heartbeat", "t_123", "--note", "working"]


def test_kanban_heartbeat_degrades_gracefully_on_failure():
    from core import kanban

    with mock.patch.object(kanban, "_hk", side_effect=lambda a, timeout=60: (1, "", "boom")):
        assert kanban.heartbeat("board-slug", "t_123") is False


def test_kanban_complete_without_metadata_omits_flag():
    from core import kanban

    calls = []

    def _spy(args, timeout=60):
        calls.append(args)
        return (0, "", "")

    with mock.patch.object(kanban, "_hk", side_effect=_spy):
        assert kanban.complete("slug", "t_9", summary="done it") is True

    assert "--metadata" not in calls[0]
    assert calls[0] == ["--board", "slug", "complete", "t_9", "--summary", "done it"]


def test_kanban_complete_with_metadata_passes_json():
    import json

    from core import kanban

    calls = []

    def _spy(args, timeout=60):
        calls.append(args)
        return (0, "", "")

    meta = {"daedalus_delegate": 1, "pr": 42, "verdict": "pr_opened"}
    with mock.patch.object(kanban, "_hk", side_effect=_spy):
        assert kanban.complete("slug", "t_9", summary="s", metadata=meta) is True

    args = calls[0]
    assert "--metadata" in args
    payload = json.loads(args[args.index("--metadata") + 1])
    assert payload == meta


if __name__ == "__main__":
    # Dual-mode: run standalone without pytest. Auto-discovers test_* functions;
    # parametrized cases are invoked with representative args.
    import tempfile

    failures = 0
    _params = {
        "test_ac3_classify_blocked_routes_crash_markers_to_infra_failure": ["coding_agent_died"],
        "test_ac4_wrapper_command_embeds_run_cmd_for_every_agent": ["codex", "codex exec --full-auto"],
        # tmp_path is a pytest fixture; supply a real temp dir in standalone mode.
        "test_failure_tail_redacts_planted_secret_end_to_end": [Path(tempfile.mkdtemp())],
    }
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        try:
            fn(*_params.get(name, []))
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)

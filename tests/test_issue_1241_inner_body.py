"""Issue #1241 — the inner coding-agent prompt must exclude the delegation wrapper.

The outer orchestrator copies the inner task body into ``/tmp/<pfx>-<n>-task.txt``
and pipes it to the headless coding agent. When the delegation wrapper ("Do NOT
do this work yourself. Spawn Claude Code via terminal.") leaks into that file,
the inner agent re-delegates: it spawns a background subagent, prints a status
line, and exits with no deliverable (observed on PM card t_0ce8beb3 / #1148).

Contract locked here:

- ``_inner_task_body()`` — the extraction the wrapper's step 1 instructs the
  outer agent to perform — yields a prompt free of delegation text for every
  delegated role builder × coding agent.
- Block-first (prepended) bodies carry exactly one ``_INNER_BODY_SEPARATOR``
  line, with the role body only below it.
- Body-first (appended) bodies use the ``⚠️  AGENT DELEGATION`` marker line as
  the boundary and contain no separator line.
- Every template in ``templates/agent_bodies/`` carries the inline-execution
  guard (no subagents / background agents / nested coding-agent process, ignore
  global plan-mode & skill-lifecycle instructions).
- The default claude-code command skips user-scope settings
  (``--setting-sources project``) while keeping ``CLAUDE_CONFIG_DIR`` for auth.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from conftest import _load_dispatch  # noqa: E402

TEMPLATE_DIR = ROOT / "templates" / "agent_bodies"

REPO = "acme/widgets"
WORKDIR = "/tmp/work"
BASE = "dev"
PROVIDER = "github"
ISSUE = {
    "number": 1234,
    "title": "Fix the widget crash",
    "body": "The widget crashes when clicked.\n\n- [ ] repro\n- [ ] fix",
    "url": "https://github.com/acme/widgets/issues/1234",
    "labels": ["bug"],
}

AGENTS = ["claude-code", "codex", "opencode"]

# Text the inner agent must NEVER see: the delegation preamble and the outer
# wrapper's spawn/bookkeeping steps. The spec's literal "kanban complete" is
# deliberately NOT here: role bodies carry their own inner-agent prohibition
# ("do NOT call hermes kanban complete — the outer agent does it for you"),
# which legitimately belongs in the inner prompt. The wrapper's bookkeeping is
# pinned via its unique wait/block steps instead.
FORBIDDEN_IN_INNER = (
    "AGENT DELEGATION",
    "Spawn",
    "write_file(",
    "Wait for the coding agent",
    "kanban_block(",
)

# Block-first composition (delegation prepended, role body below).
PREPEND_CASES = ["pm", "dev", "qa", "reviewer", "security", "docs"]
# Body-first composition (delegation appended after the role body).
APPEND_CASES = ["task_body", "validator", "downstream", "planner_fallback_validator"]
ALL_CASES = PREPEND_CASES + APPEND_CASES


def _render(disp, case: str, agent: str) -> str:
    """Render one delegated role body with canonical inputs."""
    if case == "task_body":
        return disp._task_body(
            REPO, ISSUE, 3, WORKDIR, "slack", BASE, PROVIDER,
            security_notify_targets=["slack"],
            coding_agent=agent, coding_agent_cmd="",
        )
    if case == "validator":
        return disp._validator_body(
            REPO, ISSUE, WORKDIR, BASE, PROVIDER,
            security_notify_targets=["slack"],
            coding_agent=agent, coding_agent_cmd="",
        )
    if case == "planner_fallback_validator":
        return disp._planner_not_suitable_validator_body(
            REPO, ISSUE, "NOT SUITABLE: single-AC bug", WORKDIR, BASE, PROVIDER,
            coding_agent=agent, coding_agent_cmd="",
        )
    if case == "downstream":
        return disp._downstream_body(
            REPO, ISSUE, 3, WORKDIR, "slack", BASE, PROVIDER,
            security_notify_targets=["slack"],
            coding_agent=agent, coding_agent_cmd="",
        )
    if case == "pm":
        return disp._pm_body(
            REPO, ISSUE, "CONFIRMED: reproduced on dev", WORKDIR, BASE, PROVIDER,
            coding_agent=agent, coding_agent_cmd="",
        )
    if case == "dev":
        return disp._dev_task_body(
            REPO, ISSUE, 3, WORKDIR, BASE, PROVIDER,
            coding_agent=agent, coding_agent_cmd="",
        )
    if case == "qa":
        return disp._qa_task_body(
            REPO, ISSUE, WORKDIR, PROVIDER,
            coding_agent=agent, coding_agent_cmd="",
        )
    if case == "reviewer":
        return disp._reviewer_task_body(
            REPO, ISSUE, WORKDIR, PROVIDER,
            coding_agent=agent, coding_agent_cmd="",
        )
    if case == "security":
        return disp._security_task_body(
            REPO, ISSUE, WORKDIR, PROVIDER,
            coding_agent=agent, coding_agent_cmd="",
        )
    if case == "docs":
        return disp._docs_task_body(
            REPO, ISSUE, WORKDIR, PROVIDER, "slack",
            coding_agent=agent, coding_agent_cmd="",
        )
    raise KeyError(case)  # pragma: no cover


# ── golden contract: inner body excludes every delegation artifact ───────────


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("case", ALL_CASES)
def test_inner_body_excludes_delegation(case, agent):
    disp = _load_dispatch()
    body = _render(disp, case, agent)
    assert disp._DELEGATION_MARKER in body, "delegation block missing from card body"
    inner = disp._inner_task_body(body)
    assert inner.strip(), f"{case}/{agent}: extracted inner body is empty"
    for needle in FORBIDDEN_IN_INNER:
        assert needle not in inner, (
            f"{case}/{agent}: delegation text {needle!r} leaked into the inner body"
        )


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("case", PREPEND_CASES)
def test_prepend_body_has_single_separator_with_role_body_below(case, agent):
    disp = _load_dispatch()
    body = _render(disp, case, agent)
    sep = disp._INNER_BODY_SEPARATOR
    assert body.count(sep) == 1, f"{case}/{agent}: expected exactly one separator line"
    assert body.index("You are the") > body.index(sep), (
        f"{case}/{agent}: role body must appear only below the separator"
    )
    # Wrapper step 1 references the boundary it actually produces.
    wrapper = body[: body.index(sep)]
    assert "BELOW" in wrapper, f"{case}/{agent}: wrapper must point below the separator"


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("case", APPEND_CASES)
def test_append_body_uses_marker_boundary(case, agent):
    disp = _load_dispatch()
    body = _render(disp, case, agent)
    assert disp._INNER_BODY_SEPARATOR not in body, (
        f"{case}/{agent}: body-first composition must not carry a separator line"
    )
    marker_idx = body.index(disp._DELEGATION_MARKER)
    wrapper = body[marker_idx:]
    assert "ABOVE" in wrapper, f"{case}/{agent}: wrapper must point above the marker"
    assert disp._inner_task_body(body) == body[:marker_idx].rstrip("\n")


def test_inner_task_body_passthrough_without_delegation():
    disp = _load_dispatch()
    plain = "You are the VALIDATOR for issue #1.\nJust do the work."
    assert disp._inner_task_body(plain) == plain


# ── failover rewrites keep the boundary contract for both compositions ───────


@pytest.mark.parametrize("case,position", [("validator", "above"), ("pm", "below")])
def test_failover_rewrite_keeps_inner_body_clean(case, position):
    disp = _load_dispatch()
    body = _render(disp, case, "claude-code")
    block = disp._build_delegation_instructions(
        "codex",
        "codex exec --full-auto",
        role=case if case == "validator" else "pm",
        issue_number=1234,
        body_position=position,
    )
    rewritten = disp._rewrite_delegation_block(body, block)
    assert rewritten is not None
    assert "CODEX" in rewritten
    inner = disp._inner_task_body(rewritten)
    assert inner.strip()
    for needle in FORBIDDEN_IN_INNER:
        assert needle not in inner, f"{case}: {needle!r} leaked after failover rewrite"


# ── every template ships the inline-execution guard ──────────────────────────

GUARD_PHRASES = (
    "Work entirely in THIS session",
    "Do NOT spawn subagents",
    "background agents",
    "claude/codex/opencode process",
    "Ignore any global instructions about plan mode, skill lifecycles, or subagent delegation",
)


def test_every_agent_body_template_has_inline_execution_guard():
    templates = sorted(TEMPLATE_DIR.glob("*.md"))
    assert len(templates) >= 10, f"expected >=10 templates, found {len(templates)}"
    for path in templates:
        text = path.read_text(encoding="utf-8")
        for phrase in GUARD_PHRASES:
            assert phrase in text, f"{path.name}: missing guard phrase {phrase!r}"


# ── default claude-code cmd skips user-scope settings, keeps auth dir ─────────


def test_default_claude_cmd_skips_user_scope_settings():
    disp = _load_dispatch()
    cmd = disp._CODING_AGENT_DEFAULTS["claude-code"]
    assert "--setting-sources project" in cmd
    # --strict-mcp-config keeps headless workers from hanging on MCP init (daedalus#1323)
    assert "--strict-mcp-config" in cmd, "must disable MCP servers for headless workers"
    assert "CLAUDE_CONFIG_DIR=$HOME/.claude" in cmd, "auth dir must stay untouched"
    assert cmd.rstrip().endswith("-p"), "stdin prompt mode must remain last"


if __name__ == "__main__":  # standalone smoke run
    sys.exit(pytest.main([__file__, "-q"]))

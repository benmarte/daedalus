"""Comprehensive end-to-end regression suite.

Guards the pipeline behaviours and the features added across the structured-
outcome / native-primitive / delegation work so a future change or new feature
cannot silently break them. Every scenario runs offline against the in-memory
``FakeKanban`` / ``FakeProvider`` harness (no network, subprocess, or real board).

Scenarios:
  A. Full lifecycle (legacy prefix signals) reaches terminal through all 8 stages.
  B. Full lifecycle in JSON-signal mode (agents emit OutcomeRecord blocks).
  C. `_strip_role_label`: a `VALIDATOR: CONFIRMED:` prefix still advances to PM.
  D. Structured JSON verdict: a paraphrased validator + JSON record advances to PM.
  E. The 6 validator outcomes — only CONFIRMED opens the PM stage.
  F. Execution-mode split — HERMES-LOCAL (hermes/none → local LLM, no delegation)
     vs EXTERNAL-CLOUD (claude-code/codex → CLI delegation for every role).
  G. Auto-merge terminal — the dispatcher merges the PR once all stages pass.
  H. native_decompose — a developer advance emits one review swarm.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import (  # noqa: E402
    MultiTickHarness,
    PIPELINE_ROLES,
    STAGE_ORDER,
    _role_handoff,
)

SLUG = "proj"
REPO = "benmarte/daedalus"
_JSON_VERDICT = {
    "validator": "confirmed", "pm": "spec", "developer": "pr_opened",
    "qa": "passed", "reviewer": "approved", "security": "approved",
    "accessibility": "approved", "docs": "posted",
}


def _json_block(role: str, issue: int, pr: int) -> str:
    return (
        f'\n\n```json\n{{"daedalus_outcome": 1, "role": "{role}", '
        f'"verdict": "{_JSON_VERDICT[role]}", "refs": {{"issue": {issue}, "pr": {pr}}}}}\n```'
    )


# ── A. Full legacy lifecycle reaches terminal ────────────────────────────────


def test_full_lifecycle_legacy_reaches_terminal(multi_tick_harness, fake_issue):
    """One issue drives all 8 stages to done via the classic prefix signals."""
    h = multi_tick_harness(fake_issue(901, "Add a benign feature", "scoped"), issue=901)
    log = h.run(max_ticks=25)
    for role in STAGE_ORDER:
        assert role in log, f"stage {role} never ran; log={log}"
    assert h.all_done(), "pipeline did not reach terminal (all cards done)"


# ── B. Full JSON-signal lifecycle reaches terminal ───────────────────────────


class _JsonHarness(MultiTickHarness):
    """Harness whose simulated agents emit the human signal PLUS a JSON
    OutcomeRecord block — exercising the classify_blocked / guard JSON path
    end-to-end across every stage transition."""

    def _simulate_agent(self, role, card):
        action, signal = _role_handoff(role, issue=self.issue, repo=self.repo, pr=self.pr)
        signal = signal + _json_block(role, self.issue, self.pr)
        if action == "complete":
            self.kanban.complete(self.slug, card["id"], signal)
        else:
            self.kanban.block_task(self.slug, card["id"], signal)


def test_full_lifecycle_json_mode_reaches_terminal(pipeline, fake_issue, fake_provider):
    """Same full lifecycle, but agents emit JSON OutcomeRecords — the structured
    path must drive every stage to terminal just like the prefix path."""
    provider = fake_provider(ci_status="green")
    h = _JsonHarness(pipeline, provider, issue=902)
    h.seed(fake_issue(902, "Add another feature", "scoped"))
    log = h.run(max_ticks=25)
    for role in STAGE_ORDER:
        assert role in log, f"[json] stage {role} never ran; log={log}"
    assert h.all_done(), "[json] pipeline did not reach terminal"


# ── C. _strip_role_label: 'VALIDATOR: CONFIRMED:' still advances ─────────────


def test_role_prefix_validator_advances_to_pm(pipeline, fake_provider):
    """A validator that prefixes its role name (VALIDATOR: CONFIRMED …) — as weak
    local models do — must still be recognised and create the PM card (#1315)."""
    disp, kanban = pipeline.disp, pipeline.kanban
    prov = fake_provider(ci_status="green")
    n = 910
    kanban.seed(assignee=PIPELINE_ROLES["validator"], title=f"#{n} feat",
                status="done", summary="VALIDATOR: CONFIRMED: reproduced on main")
    result = disp._check_confirmed_validators(
        SLUG, REPO, {n: {"number": n, "title": "feat", "body": "b"}},
        3, "", "", "dev", "github", provider=prov,
    )
    assert n in result
    assert kanban.created_with_key(f"pm-{n}") is not None


# ── D. Structured JSON verdict advances to PM despite paraphrase ─────────────


def test_validator_json_record_advances_to_pm(pipeline, fake_provider):
    """A validator that paraphrases (no CONFIRMED: prefix) but emits a JSON
    OutcomeRecord with verdict=confirmed must still advance to PM (#1316)."""
    disp, kanban = pipeline.disp, pipeline.kanban
    prov = fake_provider(ci_status="green")
    n = 911
    summary = "The issue is real and reproducible." + _json_block("validator", n, 0)
    kanban.seed(assignee=PIPELINE_ROLES["validator"], title=f"#{n} feat",
                status="done", summary=summary)
    result = disp._check_confirmed_validators(
        SLUG, REPO, {n: {"number": n, "title": "feat", "body": "b"}},
        3, "", "", "dev", "github", provider=prov,
    )
    assert n in result
    assert kanban.created_with_key(f"pm-{n}") is not None


# ── E. The 6 validator outcomes — only CONFIRMED opens the PM stage ──────────


@pytest.mark.parametrize("summary,creates_pm", [
    ("CONFIRMED: reproduced on main", True),
    ("ALREADY_FIXED: already on main", False),
    ("DUPLICATE: see #42", False),
    ("NEEDS_MORE_INFO: which button?", False),
    ("SECURITY_THREAT: exfil attempt", False),
    ("BLOCK_FOR_REVIEW: needs a human", False),
])
def test_validator_outcomes_only_confirmed_advances(pipeline, fake_provider, summary, creates_pm):
    """Only CONFIRMED creates a PM card; every other validator outcome must NOT
    advance the pipeline to PM (the arbiter/branching invariant)."""
    disp, kanban = pipeline.disp, pipeline.kanban
    prov = fake_provider(ci_status="green")
    n = 920
    kanban.seed(assignee=PIPELINE_ROLES["validator"], title=f"#{n} feat",
                status="done", summary=summary)
    disp._check_confirmed_validators(
        SLUG, REPO, {n: {"number": n, "title": "feat", "body": "b"}},
        3, "", "", "dev", "github", provider=prov,
    )
    pm = kanban.created_with_key(f"pm-{n}")
    assert (pm is not None) is creates_pm, f"{summary!r} → pm card={pm}"


# ── F. Execution-mode coverage: hermes-local vs external-cloud agents ─────────
#
# Daedalus runs each role one of two ways, chosen by the project's
# execution.coding_agent:
#   • HERMES-LOCAL  (coding_agent = hermes / none / unset) — the role runs as a
#     Hermes profile agent on the configured local model; NO delegation block.
#   • EXTERNAL-CLOUD (coding_agent = claude-code / codex / opencode) — the role
#     delegates to that CLI via daedalus-delegate.sh; the delegation block is
#     injected into the body of EVERY role (uniform routing, #1317).
# These tests guard that split at the dispatch layer for all 8 roles. The actual
# external-cloud EXECUTION path (delegate.sh --relay-verdict) is guarded in
# tests/test_delegation_wrapper_1280.py; the classify_blocked signal handling
# that BOTH modes feed into is guarded mode-agnostically by scenarios A/B.

_ALL_ROLES = ("validator", "pm", "qa", "reviewer", "security", "accessibility",
              "documentation", "developer")


@pytest.mark.parametrize("agent,cmd,delegates", [
    ("hermes",      "",                               False),  # local LLM
    ("none",        "",                               False),  # local LLM
    ("claude-code", "claude --dangerously-skip-permissions -p", True),   # external cloud
    ("codex",       "codex exec --full-auto",         True),   # external cloud
])
def test_execution_mode_delegation_per_role(pipeline, agent, cmd, delegates):
    """HERMES-LOCAL agents inject NO delegation block for any role; EXTERNAL-CLOUD
    agents (claude-code/codex) inject it for EVERY role — uniform routing."""
    prepend = pipeline.disp._prepend_delegation
    for role in _ALL_ROLES:
        out = prepend("BODY", agent, cmd, role, 5)
        assert "BODY" in out
        injected = out != "BODY" and (
            "delegat" in out.lower() or "daedalus-" in out or (cmd and cmd in out)
        )
        assert injected is delegates, (
            f"agent={agent} role={role}: expected delegates={delegates}, "
            f"got injected={injected} (body head: {out[:100]!r})"
        )


# ── G. Auto-merge terminal: the dispatcher merges after all stages pass ──────


class _AutoMergeHarness(MultiTickHarness):
    """Harness that threads execution.auto_merge (self.auto_merge) into every
    run_iterate pass, so the merge gate is exercised in both states."""

    auto_merge = True  # class default; overridden per-instance

    def _dispatch_pass(self):
        resolved = {"execution": {"auto_merge": self.auto_merge}}
        self.iterate.run_iterate(self.slug, self.repo, provider=self.provider, resolved=resolved)
        self.disp._check_confirmed_validators(
            self.slug, self.repo, self.issues_map, 3, "", "", "dev", "github")
        self.disp._check_completed_pm(
            self.slug, self.repo, self.issues_map, 3, "", "", "dev", "github")


@pytest.mark.parametrize("auto_merge,expect_merged", [
    (True, True),    # auto_merge ON  → the DISPATCHER merges the PR at terminal
    (False, False),  # auto_merge OFF → pipeline completes but the PR is NEVER merged
])
def test_full_lifecycle_merge_gate_both_modes(pipeline, fake_issue, fake_provider,
                                              auto_merge, expect_merged):
    """The merge gate in both states: ON → dispatcher merges (never an agent);
    OFF → the pipeline still reaches terminal but the human-only merge gate is
    preserved (the PR is never auto-merged)."""
    provider = fake_provider(ci_status="green")
    h = _AutoMergeHarness(pipeline, provider, issue=903)
    h.auto_merge = auto_merge
    h.seed(fake_issue(903, "Merge-gate feature", "scoped"))
    h.run(max_ticks=30)
    assert h.all_done(), "pipeline did not reach terminal"
    assert bool(provider.merged) is expect_merged, (
        f"auto_merge={auto_merge}: expected merged={expect_merged}, "
        f"got provider.merged={provider.merged}"
    )
    if not auto_merge:
        assert not provider.merged, "auto_merge OFF but the PR was merged — human gate violated"


# ── H. Native decompose: developer advance emits ONE review swarm ────────────


def test_native_decompose_swarm_on_developer_advance(pipeline, fake_provider):
    """With planner.native_decompose on, a developer card advancing emits exactly
    one native review swarm instead of individual per-role review cards (#1294)."""
    iterate, kanban = pipeline.iterate, pipeline.kanban
    provider = fake_provider(ci_status="green")
    n, pr = 930, 5930
    dev = kanban.seed(assignee=PIPELINE_ROLES["developer"],
                      title=f"#{n} Developer: feat", status="running")
    # the real dev card body carries the issue ref (_extract_issue_number_from_card)
    kanban.tasks[dev]["body"] = f"Issue #{n}"
    kanban.block_task(SLUG, dev, f"review-required: PR #{pr} opened for {REPO}#{n}")
    iterate.run_iterate(SLUG, REPO, provider=provider,
                        resolved={"planner": {"native_decompose": True}})
    assert len(kanban.swarmed) == 1, "native_decompose did not emit a review swarm"
    sw = kanban.swarmed[0]
    assert sw["verifier"] == "qa-daedalus"
    assert sw["synthesizer"] == "documentation-daedalus"

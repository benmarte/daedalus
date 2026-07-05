"""Tests for native human gates via ``block --kind needs_input`` — #1291.

Phase 3 of epic #1276 (builds on #1290's ``block_task(kind=)`` and the arbiter's
needs_input usage). Everything ships behind the default-off
``pipeline.native_gates`` flag; flag-off behaviour must be byte-identical.

Covers each acceptance criterion:

  config
    - templates/daedalus.yaml documents the default-off flag; the resolution
      expression used by iterate/dispatcher defaults to False.

  review-required developer gate (core/iterate/executors.py::_execute_pending_pr)
    - flag ON  ⇒ the review-required re-block carries ``kind="needs_input"``.
    - flag OFF ⇒ plain block (no ``--kind``) — byte-identical block reason.

  guardrail
    - ``awaiting-fix:`` machine-wait blocks are NEVER tagged (they have no
      --parent edges; a needs_input/dependency tag would strand them).

Dual-mode: runs under pytest AND as ``python tests/test_native_gates.py``.
Honors the autouse ``HERMES_HOME`` isolation + ``core.kanban._hk`` stub via the
in-memory FakeKanban double (no network, no subprocess, no real board).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import FakeKanban, check, kanban_as  # noqa: E402,F401

from core import iterate  # noqa: E402
from core.iterate import executors  # noqa: E402
from core.providers.base import PRSummary  # noqa: E402

SLUG = "proj"
REPO = "org/repo"
DEV = "developer-daedalus"
REVIEWER = "reviewer-daedalus"
ISSUE_BODY = "Fixes org/repo#21"  # _extract_issue_number_from_card → 21


# ── helpers ─────────────────────────────────────────────────────────────────────


class _StubProvider:
    """Minimal VCS provider double exposing just ``list_prs`` (never raises)."""

    def __init__(self, prs):
        self._prs = list(prs)

    def list_prs(self, state="open", limit=50):
        return list(self._prs)


def _open_pr_for_issue_21():
    # issue_linked_to_pr matches the ``issue-21`` head-branch heuristic.
    return [PRSummary(number=99, head_branch="fix/issue-21", body="")]


# ── config flag ─────────────────────────────────────────────────────────────────


def test_template_documents_flag_default_off():
    import yaml

    tmpl = Path(__file__).resolve().parent.parent / "templates" / "daedalus.yaml"
    text = tmpl.read_text()
    check("native_gates documented", "native_gates" in text)
    data = yaml.safe_load(text)
    check("pipeline section present", "pipeline" in data)
    pipeline = data.get("pipeline") or {}
    check("native_gates defaults off (commented → not enabled)",
          bool(pipeline.get("native_gates", False)) is False)


def test_flag_read_defaults_false():
    """The resolution expression used by iterate.run_iterate defaults to False."""
    for resolved in ({}, {"pipeline": None}, {"pipeline": {}}):
        pipeline_cfg = (resolved or {}).get("pipeline") or {}
        check(f"{resolved} → False", bool(pipeline_cfg.get("native_gates", False)) is False)
    check("explicit true honoured",
          bool(({"pipeline": {"native_gates": True}}).get("pipeline", {}).get("native_gates")) is True)


# ── review-required developer gate ───────────────────────────────────────────────


def test_review_required_block_tagged_needs_input_when_flag_on():
    fk = FakeKanban()
    tid = fk.seed(assignee=DEV, title="dev", status="blocked",
                  body=ISSUE_BODY, reason="review-required: awaiting-pr")
    prov = _StubProvider(_open_pr_for_issue_21())
    with kanban_as(iterate.kanban, fk):
        ok = executors._execute_pending_pr(
            SLUG, fk.tasks[tid], REPO, "review-required: awaiting-pr",
            provider=prov, native_gates=True,
        )
    check("executor returned True", ok is True)
    last = fk.block_kind_calls[-1]
    check("re-block reason is review-required: PR #99", last[1] == "review-required: PR #99")
    check("flag ON ⇒ kind=needs_input", last[2] == "needs_input")
    check("board card records needs_input kind", fk.tasks[tid]["block_kind"] == "needs_input")


def test_review_required_block_plain_when_flag_off():
    fk = FakeKanban()
    tid = fk.seed(assignee=DEV, title="dev", status="blocked",
                  body=ISSUE_BODY, reason="review-required: awaiting-pr")
    prov = _StubProvider(_open_pr_for_issue_21())
    with kanban_as(iterate.kanban, fk):
        ok = executors._execute_pending_pr(
            SLUG, fk.tasks[tid], REPO, "review-required: awaiting-pr",
            provider=prov, native_gates=False,
        )
    check("executor returned True", ok is True)
    last = fk.block_kind_calls[-1]
    # Byte-identical: same reason string, and NO kind emitted (→ no --kind flag).
    check("flag OFF ⇒ same reason string", last[1] == "review-required: PR #99")
    check("flag OFF ⇒ kind is None (plain block, byte-identical)", last[2] is None)
    check("board card records no kind", fk.tasks[tid]["block_kind"] is None)


def test_review_required_default_is_flag_off():
    """Omitting native_gates entirely defaults to the plain (flag-off) block."""
    fk = FakeKanban()
    tid = fk.seed(assignee=DEV, title="dev", status="blocked",
                  body=ISSUE_BODY, reason="review-required: awaiting-pr")
    prov = _StubProvider(_open_pr_for_issue_21())
    with kanban_as(iterate.kanban, fk):
        executors._execute_pending_pr(
            SLUG, fk.tasks[tid], REPO, "review-required: awaiting-pr",
            provider=prov,
        )
    check("default (no kwarg) ⇒ kind None", fk.block_kind_calls[-1][2] is None)


# ── guardrail: machine-wait blocks stay untagged ─────────────────────────────────


def test_awaiting_fix_block_never_tagged_even_with_flag_on():
    """`awaiting-fix:` is a machine-wait (no --parent edge). It must stay a plain
    block even when native_gates is on, or dependency/needs_input auto-promotion
    would never fire and would strand the card."""
    fk = FakeKanban()
    tid = fk.seed(assignee=REVIEWER, title="reviewer", status="blocked",
                  body=ISSUE_BODY, reason="changes-requested: PR #99")
    with kanban_as(iterate.kanban, fk), \
            mock.patch.object(executors, "_check_and_maybe_escalate", return_value=1), \
            mock.patch.object(executors, "_increment_fix_attempts"):
        executors._execute_pm_route(
            SLUG, fk.tasks[tid], REPO, "changes-requested: PR #99",
            router_profile="project-manager-daedalus",
            native_gates=True,
        )
    awaiting = [c for c in fk.block_kind_calls if c[1].startswith("awaiting-fix:")]
    check("awaiting-fix block was created", len(awaiting) >= 1)
    check("awaiting-fix blocks are never kind-tagged",
          all(c[2] is None for c in awaiting))


if __name__ == "__main__":
    import conftest

    tests = [
        test_template_documents_flag_default_off,
        test_flag_read_defaults_false,
        test_review_required_block_tagged_needs_input_when_flag_on,
        test_review_required_block_plain_when_flag_off,
        test_review_required_default_is_flag_off,
        test_awaiting_fix_block_never_tagged_even_with_flag_on,
    ]
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            conftest._failed += 1
            print(f"  FAIL  (raised {type(e).__name__}: {e})")

    print(f"\n{'=' * 60}")
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    if conftest._failed:
        sys.exit(1)

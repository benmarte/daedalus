"""Tests for the upfront pipeline DAG at Ready-time — #1290 (KEYSTONE of #1276).

Everything ships behind the default-off ``pipeline.upfront_dag`` flag; flag-off
behaviour must be byte-identical. Covers each acceptance criterion:

  block_task(kind=)
    - emits ``--kind <value>`` only when set; omitted otherwise; never raises.

  build_pipeline_dag()
    - creates the full stage graph with correct --parent edges + --kind dependency
      blocks; validator is the unblocked root; docs is multi-parented to
      reviewer + security + accessibility.
    - idempotent re-tick: re-running never double-creates cards or re-blocks.

  6-outcome arbiter
    - _map_validator_outcome maps ALL six verdicts + unknown/None → safe-park.
    - _arbitrate_validator_outcome reads native metadata first, then prefix;
      cancels / human-gates / escalates / safe-parks; never fires on a pruned
      branch; skips still-running validators.

  per-tick gating
    - _create_downstream_review_tasks no-ops when upfront_dag=True; runs (creating
      the 5 downstream cards) when False (flag-off byte-identical).

  config
    - templates/daedalus.yaml documents the default-off flag; resolution defaults
      to False.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import FakeKanban, FakeProvider, check, kanban_as  # noqa: E402,F401

import core.kanban as kanban  # noqa: E402
from core import iterate  # noqa: E402
from core.dispatch import stages  # noqa: E402
from core.iterate.outcomes import SCHEMA_VERSION  # noqa: E402

SLUG = "proj"
VALIDATOR = "validator-daedalus"


# ── helpers ───────────────────────────────────────────────────────────────────


def _role_specs() -> dict:
    """Minimal per-stage specs (bodies/skills irrelevant to graph shape)."""
    roles = {
        "validator": "validator-daedalus",
        "pm": "project-manager-daedalus",
        "developer": "developer-daedalus",
        "qa": "qa-daedalus",
        "reviewer": "reviewer-daedalus",
        "security": "security-analyst-daedalus",
        "accessibility": "accessibility-daedalus",
        "docs": "documentation-daedalus",
    }
    return {
        k: {"assignee": v, "body": f"body {k}", "workspace": "dir:/w",
            "skills": None, "extra": {}}
        for k, v in roles.items()
    }


def _validator_meta(verdict: str, *, issue: int = 42) -> dict:
    return {"daedalus_outcome": SCHEMA_VERSION, "role": "validator",
            "verdict": verdict, "refs": {"issue": issue}}


# ── AC1: block_task(kind=) ──────────────────────────────────────────────────────


def test_block_task_kind_emits_flag():
    with mock.patch("core.kanban._hk", side_effect=lambda a, timeout=60: (0, "", "")) as m:
        ok = kanban.block_task(SLUG, "t_1", "dependency: waiting", kind="dependency")
    args = m.call_args[0][0]
    check("block ok", ok is True)
    check("--kind present", "--kind" in args)
    check("--kind value", args[args.index("--kind") + 1] == "dependency")


def test_block_task_no_kind_byte_identical():
    with mock.patch("core.kanban._hk", side_effect=lambda a, timeout=60: (0, "", "")) as m:
        kanban.block_task(SLUG, "t_1", "some reason")
    args = m.call_args[0][0]
    check("--kind absent when kind=None", "--kind" not in args)
    check("legacy argv unchanged", args == ["--board", SLUG, "block", "t_1", "some reason"])


def test_block_task_never_raises():
    with mock.patch("core.kanban._hk", side_effect=lambda a, timeout=60: (1, "", "boom")):
        ok = kanban.block_task(SLUG, "t_1", "r", kind="needs_input")
    check("returns False on rc!=0, no raise", ok is False)


# ── AC2: build_pipeline_dag graph shape ─────────────────────────────────────────


def test_build_pipeline_dag_graph_shape():
    fk = FakeKanban()
    with kanban_as(kanban, fk):
        ids = iterate.build_pipeline_dag(SLUG, 42, _role_specs())

    check("all 8 stages created", len(ids) == 8)
    check("8 create_task calls", len(fk.created) == 8)

    v = fk.tasks[ids["validator"]]
    check("validator is root (no parents)", v["parents"] == [])
    check("validator NOT dependency-blocked (dispatchable)", v.get("block_kind") is None)
    check("validator status running (unblocked root)", v["status"] == "running")

    check("pm parented to validator", fk.tasks[ids["pm"]]["parents"] == [ids["validator"]])
    check("developer parented to pm", fk.tasks[ids["developer"]]["parents"] == [ids["pm"]])
    check("qa parented to developer", fk.tasks[ids["qa"]]["parents"] == [ids["developer"]])
    for r in ("reviewer", "security", "accessibility"):
        check(f"{r} parented to qa", fk.tasks[ids[r]]["parents"] == [ids["qa"]])

    docs_parents = fk.tasks[ids["docs"]]["parents"]
    check("docs multi-parented to reviewer+security+accessibility",
          docs_parents == [ids["reviewer"], ids["security"], ids["accessibility"]])

    # Every non-root stage is dependency-blocked (auto-promotes when parents done).
    for r in ("pm", "developer", "qa", "reviewer", "security", "accessibility", "docs"):
        check(f"{r} dependency-blocked", fk.tasks[ids[r]]["block_kind"] == "dependency")
    check("exactly 7 dependency blocks (all non-root)", len(fk.block_kind_calls) == 7)
    check("all block kinds are 'dependency'",
          all(k == "dependency" for (_t, _r, k) in fk.block_kind_calls))


def test_build_pipeline_dag_stable_role_keys():
    fk = FakeKanban()
    with kanban_as(kanban, fk):
        ids = iterate.build_pipeline_dag(SLUG, 7, _role_specs())
    for role in ("validator", "pm", "developer", "qa", "reviewer",
                 "security", "accessibility", "docs"):
        check(f"{role} uses stable idempotency key {role}-7",
              fk.tasks[ids[role]]["idempotency_key"] == f"{role}-7")


# ── AC6: idempotent re-tick ─────────────────────────────────────────────────────


def test_build_pipeline_dag_idempotent_re_tick():
    fk = FakeKanban()
    with kanban_as(kanban, fk):
        ids1 = iterate.build_pipeline_dag(SLUG, 42, _role_specs())
        ids2 = iterate.build_pipeline_dag(SLUG, 42, _role_specs())
    check("re-tick returns the SAME ids", ids1 == ids2)
    check("no double-create (still 8 cards)", len(fk.created) == 8)
    check("no re-block on re-tick (still 7 dependency blocks)",
          len(fk.block_kind_calls) == 7)


# ── AC3: 6-outcome arbiter mapping (pure) ───────────────────────────────────────


def test_map_validator_outcome_all_six_plus_unknown():
    cases = {
        "confirmed": stages.ARBITER_KEEP,
        "already_fixed": stages.ARBITER_CANCEL,
        "duplicate": stages.ARBITER_CANCEL,
        "needs_more_info": stages.ARBITER_HUMAN,
        "block_for_review": stages.ARBITER_HUMAN,
        "security_threat": stages.ARBITER_ESCALATE,
    }
    for verdict, expected in cases.items():
        check(f"{verdict} → {expected}", stages._map_validator_outcome(verdict) == expected)
    check("None → safe-park", stages._map_validator_outcome(None) == stages.ARBITER_PARK)
    check("unknown → safe-park", stages._map_validator_outcome("wat") == stages.ARBITER_PARK)


# ── AC3/AC5: arbiter read path (metadata first, then prefix) ────────────────────


def _seed_pipeline(fk: FakeKanban, *, verdict_summary: str = "",
                   run_metadata: dict | None = None, status: str = "done") -> dict:
    """Seed a validator card + full downstream set for issue #42."""
    ids = {}
    ids["validator"] = fk.seed(
        assignee=VALIDATOR, title="#42 Something broken", status=status,
        summary=verdict_summary, idempotency_key="validator-42")
    if run_metadata is not None:
        fk.tasks[ids["validator"]]["run_metadata"] = run_metadata
    for role, assignee in (("pm", "project-manager-daedalus"),
                           ("developer", "developer-daedalus"),
                           ("qa", "qa-daedalus"),
                           ("docs", "documentation-daedalus")):
        ids[role] = fk.seed(assignee=assignee, title="#42 Something broken",
                            status="blocked", idempotency_key=f"{role}-42")
    return ids


def test_arbiter_confirmed_via_metadata_keeps_dag():
    fk = FakeKanban()
    ids = _seed_pipeline(fk, run_metadata=_validator_meta("confirmed"))
    prov = FakeProvider(board_configured=True)
    with kanban_as(kanban, fk):
        enforced = stages._arbitrate_validator_outcome(
            SLUG, prov, {42}, validator_profile=VALIDATOR)
    check("confirmed → no notification", enforced == [])
    check("confirmed → no board status change", prov.board_status_calls == [])
    check("confirmed → downstream NOT cancelled",
          fk.tasks[ids["developer"]]["status"] == "blocked")


def test_arbiter_already_fixed_via_prefix_cancels_silently():
    fk = FakeKanban()
    ids = _seed_pipeline(fk, verdict_summary="ALREADY_FIXED: nothing to do")
    prov = FakeProvider(board_configured=True)
    with kanban_as(kanban, fk):
        enforced = stages._arbitrate_validator_outcome(
            SLUG, prov, {42}, validator_profile=VALIDATOR)
    check("cancel is silent (no notification)", enforced == [])
    check("downstream cancelled (developer done)",
          fk.tasks[ids["developer"]]["status"] == "done")
    check("docs branch cancelled too", fk.tasks[ids["docs"]]["status"] == "done")


def test_arbiter_duplicate_cancels():
    fk = FakeKanban()
    ids = _seed_pipeline(fk, run_metadata=_validator_meta("duplicate"))
    prov = FakeProvider(board_configured=True)
    with kanban_as(kanban, fk):
        enforced = stages._arbitrate_validator_outcome(
            SLUG, prov, {42}, validator_profile=VALIDATOR)
    check("duplicate silent", enforced == [])
    check("duplicate cancels downstream", fk.tasks[ids["qa"]]["status"] == "done")


def test_arbiter_needs_more_info_human_gate():
    fk = FakeKanban()
    ids = _seed_pipeline(fk, run_metadata=_validator_meta("needs_more_info"))
    prov = FakeProvider(board_configured=True)
    with kanban_as(kanban, fk):
        enforced = stages._arbitrate_validator_outcome(
            SLUG, prov, {42}, validator_profile=VALIDATOR)
    check("needs_more_info → notifies once", enforced == [42])
    check("board set to Blocked", prov.board_status_calls == [(42, "Blocked")])
    check("validator card tagged needs_input",
          fk.tasks[ids["validator"]]["block_kind"] == "needs_input")


def test_arbiter_block_for_review_human_gate():
    fk = FakeKanban()
    _seed_pipeline(fk, run_metadata=_validator_meta("block_for_review"))
    prov = FakeProvider(board_configured=True)
    with kanban_as(kanban, fk):
        enforced = stages._arbitrate_validator_outcome(
            SLUG, prov, {42}, validator_profile=VALIDATOR)
    check("block_for_review → notifies once", enforced == [42])
    check("board Blocked", prov.board_status_calls == [(42, "Blocked")])


def test_arbiter_security_threat_escalates_and_cancels():
    fk = FakeKanban()
    ids = _seed_pipeline(fk, run_metadata=_validator_meta("security_threat"))
    prov = FakeProvider(board_configured=True)
    with kanban_as(kanban, fk):
        enforced = stages._arbitrate_validator_outcome(
            SLUG, prov, {42}, validator_profile=VALIDATOR)
    check("security_threat → notifies once", enforced == [42])
    check("board Blocked", prov.board_status_calls == [(42, "Blocked")])
    check("downstream cancelled on escalate",
          fk.tasks[ids["developer"]]["status"] == "done")


def test_arbiter_unknown_safe_parks():
    """Done validator with no readable verdict must NOT auto-proceed."""
    fk = FakeKanban()
    ids = _seed_pipeline(fk, verdict_summary="all good, moving on")  # no verdict token
    prov = FakeProvider(board_configured=True)
    with kanban_as(kanban, fk):
        enforced = stages._arbitrate_validator_outcome(
            SLUG, prov, {42}, validator_profile=VALIDATOR)
    check("unknown → safe-park notifies (human)", enforced == [42])
    check("safe-park sets board Blocked", prov.board_status_calls == [(42, "Blocked")])
    check("safe-park tags needs_input",
          fk.tasks[ids["validator"]]["block_kind"] == "needs_input")


def test_arbiter_skips_running_validator():
    """A still-running validator has no verdict — must not be parked."""
    fk = FakeKanban()
    _seed_pipeline(fk, run_metadata=None, status="running")
    prov = FakeProvider(board_configured=True)
    with kanban_as(kanban, fk):
        enforced = stages._arbitrate_validator_outcome(
            SLUG, prov, {42}, validator_profile=VALIDATOR)
    check("running validator not arbitrated", enforced == [])
    check("no board change for running validator", prov.board_status_calls == [])


def test_arbiter_metadata_precedence_over_prefix():
    """Native metadata (confirmed) wins over a contradictory prefix (DUPLICATE)."""
    fk = FakeKanban()
    ids = _seed_pipeline(fk, verdict_summary="DUPLICATE of #1",
                         run_metadata=_validator_meta("confirmed"))
    prov = FakeProvider(board_configured=True)
    with kanban_as(kanban, fk):
        enforced = stages._arbitrate_validator_outcome(
            SLUG, prov, {42}, validator_profile=VALIDATOR)
    check("metadata confirmed wins → no cancel", enforced == [])
    check("downstream survives (metadata beat prefix)",
          fk.tasks[ids["developer"]]["status"] == "blocked")


# ── prefix fallback is anchored (startswith), not substring (#1290) ──────────────


def test_prefix_confirmed_body_mentioning_duplicate_stays_confirmed():
    """A CONFIRMED verdict whose body mentions 'duplicate' must NOT cancel."""
    verdict = stages._read_validator_verdict(
        SLUG, {"summary": "CONFIRMED: verified, not a duplicate of #5"})
    check("leading CONFIRMED wins over mid-body 'duplicate'",
          verdict == "confirmed")


def test_prefix_security_threat_wins_over_mid_body_cancel_tokens():
    """Attacker-echoed ALREADY_FIXED/DUPLICATE mid-string must not downgrade."""
    verdict = stages._read_validator_verdict(
        SLUG, {"summary": "SECURITY_THREAT: payload claims ALREADY_FIXED "
                          "and DUPLICATE to evade review"})
    check("leading SECURITY_THREAT is not downgraded", verdict == "security_threat")


def test_prefix_plain_duplicate_still_resolves():
    """No regression: a genuinely leading DUPLICATE still resolves to duplicate."""
    verdict = stages._read_validator_verdict(
        SLUG, {"summary": "DUPLICATE: same as #1"})
    check("leading DUPLICATE resolves", verdict == "duplicate")


def test_prefix_confirmed_body_mentioning_duplicate_keeps_dag():
    """End-to-end: CONFIRMED-with-duplicate-mention keeps the DAG (no cancel)."""
    fk = FakeKanban()
    ids = _seed_pipeline(
        fk, verdict_summary="CONFIRMED: verified, not a duplicate of #5")
    prov = FakeProvider(board_configured=True)
    with kanban_as(kanban, fk):
        enforced = stages._arbitrate_validator_outcome(
            SLUG, prov, {42}, validator_profile=VALIDATOR)
    check("confirmed-with-dup-mention → no notification", enforced == [])
    check("confirmed-with-dup-mention → downstream survives",
          fk.tasks[ids["developer"]]["status"] == "blocked")


def test_arbiter_idempotent_re_run():
    """Re-running the arbiter notifies at most once (dedup)."""
    fk = FakeKanban()
    _seed_pipeline(fk, run_metadata=_validator_meta("security_threat"))
    prov = FakeProvider(board_configured=True)
    with kanban_as(kanban, fk):
        first = stages._arbitrate_validator_outcome(
            SLUG, prov, {42}, validator_profile=VALIDATOR)
        second = stages._arbitrate_validator_outcome(
            SLUG, prov, {42}, validator_profile=VALIDATOR)
    check("first run notifies", first == [42])
    check("second run silent (deduped)", second == [])


# ── AC4/AC5: per-tick downstream gating ─────────────────────────────────────────


def test_per_tick_downstream_noops_when_flag_on():
    fk = FakeKanban()
    dev_card = {"id": "t_dev", "workspace": "dir:/w"}
    with kanban_as(kanban, fk):
        created = iterate._create_downstream_review_tasks(
            SLUG, 42, dev_card, pr_number=9, upfront_dag=True)
    check("no downstream created when flag ON", created == [])
    check("no cards created when flag ON", fk.created == [])


def test_per_tick_downstream_runs_when_flag_off():
    fk = FakeKanban()
    dev_card = {"id": "t_dev", "workspace": "dir:/w"}
    with kanban_as(kanban, fk):
        created = iterate._create_downstream_review_tasks(
            SLUG, 42, dev_card, pr_number=9, upfront_dag=False)
    check("5 downstream created when flag OFF (byte-identical)", len(created) == 5)
    keys = {fk.tasks[t]["idempotency_key"] for t in created}
    check("expected downstream keys",
          keys == {"qa-42", "reviewer-42", "security-42", "accessibility-42", "docs-42"})


def test_per_tick_default_is_flag_off():
    """Omitting upfront_dag preserves today's behaviour (creates downstream)."""
    fk = FakeKanban()
    dev_card = {"id": "t_dev", "workspace": "dir:/w"}
    with kanban_as(kanban, fk):
        created = iterate._create_downstream_review_tasks(SLUG, 42, dev_card, pr_number=9)
    check("default (no flag) creates downstream", len(created) == 5)


# ── AC8: e2e-style — build DAG then arbitrate each of the 6 outcomes ────────────


def test_e2e_build_then_arbitrate_all_six_outcomes():
    outcomes = {
        "confirmed": ("keep", []),
        "already_fixed": ("cancel", []),
        "duplicate": ("cancel", []),
        "needs_more_info": ("human", [42]),
        "block_for_review": ("human", [42]),
        "security_threat": ("escalate", [42]),
    }
    for verdict, (_kind, expected_enforced) in outcomes.items():
        fk = FakeKanban()
        prov = FakeProvider(board_configured=True)
        with kanban_as(kanban, fk):
            # Build the full DAG upfront.
            ids = iterate.build_pipeline_dag(SLUG, 42, _role_specs())
            check(f"[{verdict}] DAG built with 8 stages", len(ids) == 8)
            # Validator completes with this verdict (native metadata).
            fk.tasks[ids["validator"]]["status"] = "done"
            fk.tasks[ids["validator"]]["run_metadata"] = _validator_meta(verdict)
            enforced = stages._arbitrate_validator_outcome(
                SLUG, prov, {42}, validator_profile=VALIDATOR)
        check(f"[{verdict}] enforced == {expected_enforced}", enforced == expected_enforced)
        if verdict in ("already_fixed", "duplicate", "security_threat"):
            check(f"[{verdict}] downstream cancelled",
                  fk.tasks[ids["developer"]]["status"] == "done")
        if verdict == "confirmed":
            check("[confirmed] developer stays dependency-blocked (auto-promotes later)",
                  fk.tasks[ids["developer"]]["status"] == "blocked")


def test_happy_path_promotion_order_is_topological():
    """The stage graph encodes the invariant promotion order via parent edges."""
    order = [k for k, _ in iterate.executors._PIPELINE_DAG_STAGES]
    check("stage order",
          order == ["validator", "pm", "developer", "qa",
                    "reviewer", "security", "accessibility", "docs"])
    graph = dict(iterate.executors._PIPELINE_DAG_STAGES)
    check("validator root", graph["validator"] == ())
    check("qa gates the three reviews",
          graph["reviewer"] == ("qa",) and graph["security"] == ("qa",)
          and graph["accessibility"] == ("qa",))
    check("docs waits on ALL three reviews",
          graph["docs"] == ("reviewer", "security", "accessibility"))


# ── config flag ─────────────────────────────────────────────────────────────────


def test_template_documents_flag_default_off():
    import yaml
    tmpl = Path(__file__).resolve().parent.parent / "templates" / "daedalus.yaml"
    text = tmpl.read_text()
    check("upfront_dag documented", "upfront_dag" in text)
    data = yaml.safe_load(text)
    check("pipeline section present", "pipeline" in data)
    pipeline = data.get("pipeline") or {}
    check("upfront_dag defaults off (commented → not enabled)",
          bool(pipeline.get("upfront_dag", False)) is False)


def test_flag_read_defaults_false():
    """The resolution expression used by dispatcher + iterate defaults to False."""
    for resolved in ({}, {"pipeline": None}, {"pipeline": {}}):
        pipeline_cfg = (resolved or {}).get("pipeline") or {}
        check(f"{resolved} → False", bool(pipeline_cfg.get("upfront_dag", False)) is False)
    check("explicit true honoured",
          bool(({"pipeline": {"upfront_dag": True}}).get("pipeline", {}).get("upfront_dag")) is True)


if __name__ == "__main__":
    import conftest
    tests = [
        test_block_task_kind_emits_flag,
        test_block_task_no_kind_byte_identical,
        test_block_task_never_raises,
        test_build_pipeline_dag_graph_shape,
        test_build_pipeline_dag_stable_role_keys,
        test_build_pipeline_dag_idempotent_re_tick,
        test_map_validator_outcome_all_six_plus_unknown,
        test_arbiter_confirmed_via_metadata_keeps_dag,
        test_arbiter_already_fixed_via_prefix_cancels_silently,
        test_arbiter_duplicate_cancels,
        test_arbiter_needs_more_info_human_gate,
        test_arbiter_block_for_review_human_gate,
        test_arbiter_security_threat_escalates_and_cancels,
        test_arbiter_unknown_safe_parks,
        test_arbiter_skips_running_validator,
        test_arbiter_metadata_precedence_over_prefix,
        test_prefix_confirmed_body_mentioning_duplicate_stays_confirmed,
        test_prefix_security_threat_wins_over_mid_body_cancel_tokens,
        test_prefix_plain_duplicate_still_resolves,
        test_prefix_confirmed_body_mentioning_duplicate_keeps_dag,
        test_arbiter_idempotent_re_run,
        test_per_tick_downstream_noops_when_flag_on,
        test_per_tick_downstream_runs_when_flag_off,
        test_per_tick_default_is_flag_off,
        test_e2e_build_then_arbitrate_all_six_outcomes,
        test_happy_path_promotion_order_is_topological,
        test_template_documents_flag_default_off,
        test_flag_read_defaults_false,
    ]
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        try:
            t()
        except Exception as e:
            conftest._failed += 1
            print(f"  FAIL  (raised {type(e).__name__}: {e})")

    print(f"\n{'='*60}")
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    if conftest._failed:
        sys.exit(1)

"""Tests for outcome metadata transport — #1288 (Phase 1 of epic #1276).

Covers each acceptance criterion:

  kanban wrappers:
    - complete(metadata=) builds `--metadata <json>`; never raises on bad dict
    - complete() without metadata is unchanged (no --metadata)
    - heartbeat() builds the correct argv; never raises
    - run_outcome() parses a `runs --json` payload → outcome dict / None

  routing (classify_blocked):
    - metadata_transport ON: native run metadata routes first
    - native-less card falls through to handoff_text JSON, then prefix
    - flag OFF (native_outcome=None): byte-identical to today (regression)

  emit vs blocked split:
    - completion handoff (_execute_advance) writes --metadata when flag ON
    - blocked handoff keeps free-text (no --metadata) — asserted via the
      delegate transition contract (block reason carries the signal text)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import check  # noqa: E402,F401

from core import kanban  # noqa: E402
from core.kanban import complete, heartbeat, run_outcome  # noqa: E402
from core.iterate.classify import (  # noqa: E402
    ADVANCE, PM_ROUTE, QA_FIX, classify_blocked,
)
from core.iterate.outcomes import SCHEMA_VERSION  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────


def _outcome(role: str, verdict: str, *, pr: int = 5, issue: int = 42) -> dict:
    return {
        "daedalus_outcome": SCHEMA_VERSION,
        "role": role,
        "verdict": verdict,
        "refs": {"issue": issue, "pr": pr},
    }


def _json_handoff(role: str, verdict: str, *, pr: int = 5, issue: int = 42) -> str:
    """Free-text handoff carrying a fenced JSON OutcomeRecord block."""
    return f"review-required: PR #{pr}\n```json\n{json.dumps(_outcome(role, verdict, pr=pr, issue=issue))}\n```"


# ── kanban.complete(metadata=) ────────────────────────────────────────────────


def test_complete_metadata_builds_argv():
    """complete(metadata=dict) appends --metadata <json.dumps>."""
    md = _outcome("developer", "pr_opened")
    with mock.patch("core.kanban._hk", side_effect=lambda a, timeout=60: (0, "", "")) as m:
        complete("slug", "t_abc", metadata=md)
    args = m.call_args[0][0]
    check("--metadata present", "--metadata" in args)
    payload = args[args.index("--metadata") + 1]
    check("metadata payload is the serialised dict", json.loads(payload) == md)


def test_complete_no_metadata_unchanged():
    """complete() without metadata omits --metadata (backward compat)."""
    with mock.patch("core.kanban._hk", side_effect=lambda a, timeout=60: (0, "", "")) as m:
        complete("slug", "t_abc", summary="done")
    args = m.call_args[0][0]
    check("--metadata absent", "--metadata" not in args)


def test_complete_unserialisable_metadata_never_raises():
    """A non-JSON-serialisable metadata dict is dropped, completion still runs."""
    with mock.patch("core.kanban._hk", side_effect=lambda a, timeout=60: (0, "", "")) as m:
        ok = complete("slug", "t_abc", metadata={"bad": {1, 2, 3}})  # set → not serialisable
    args = m.call_args[0][0]
    check("completion still attempted", ok is True)
    check("unserialisable metadata dropped, not raised", "--metadata" not in args)


# ── kanban.heartbeat ──────────────────────────────────────────────────────────


def test_heartbeat_builds_argv():
    with mock.patch("core.kanban._hk", side_effect=lambda a, timeout=60: (0, "", "")) as m:
        ok = heartbeat("slug", "t_abc", note="alive")
    args = m.call_args[0][0]
    check("heartbeat ok", ok is True)
    check("board flag", args[:2] == ["--board", "slug"])
    check("heartbeat subcommand + task_id", "heartbeat" in args and "t_abc" in args)
    check("--note passed", "--note" in args and "alive" in args)


def test_heartbeat_never_raises_on_failure():
    with mock.patch("core.kanban._hk", side_effect=lambda a, timeout=60: (1, "", "boom")):
        ok = heartbeat("slug", "t_abc")
    check("heartbeat returns False on rc!=0", ok is False)


# ── kanban.run_outcome ────────────────────────────────────────────────────────


def test_run_outcome_parses_closing_run_metadata():
    md = _outcome("qa", "passed")
    payload = json.dumps([
        {"id": 1, "status": "failed", "metadata": None},
        {"id": 2, "status": "completed", "metadata": md},
    ])
    with mock.patch("core.kanban._hk", side_effect=lambda a, timeout=60: (0, payload, "")):
        got = run_outcome("slug", "t_abc")
    check("run_outcome returns the closing-run metadata dict", got == md)


def test_run_outcome_metadata_as_json_string():
    md = _outcome("developer", "pr_opened")
    payload = json.dumps([{"id": 1, "status": "completed", "metadata": json.dumps(md)}])
    with mock.patch("core.kanban._hk", side_effect=lambda a, timeout=60: (0, payload, "")):
        got = run_outcome("slug", "t_abc")
    check("string-encoded metadata is decoded", got == md)


def test_run_outcome_none_when_absent_or_bad():
    # No daedalus_outcome key
    with mock.patch("core.kanban._hk", side_effect=lambda a, timeout=60: (0, json.dumps([{"metadata": {"x": 1}}]), "")):
        check("no outcome key → None", run_outcome("slug", "t_abc") is None)
    # rc != 0
    with mock.patch("core.kanban._hk", side_effect=lambda a, timeout=60: (1, "", "err")):
        check("rc!=0 → None", run_outcome("slug", "t_abc") is None)
    # malformed json
    with mock.patch("core.kanban._hk", side_effect=lambda a, timeout=60: (0, "not json", "")):
        check("malformed → None", run_outcome("slug", "t_abc") is None)


# ── classify_blocked routing: native metadata precedence ──────────────────────


def test_classify_routes_from_native_metadata():
    """metadata_transport ON: native run metadata drives routing."""
    sources: list[str] = []
    # Native says qa/failed → QA_FIX, even though handoff text says qa-passed.
    action = classify_blocked(
        "qa-daedalus", "qa-passed: all green", ci_green=True,
        _source_collector=sources,
        native_outcome=_outcome("qa", "failed"),
    )
    check("native qa/failed → QA_FIX", action == QA_FIX)
    check("telemetry source is 'metadata'", sources == ["metadata"])


def test_classify_native_less_falls_through_to_json_then_prefix():
    """No native metadata → free-text JSON block wins; no JSON → prefix."""
    # (a) native None, handoff has JSON qa/failed → QA_FIX via json
    s1: list[str] = []
    a1 = classify_blocked(
        "qa-daedalus", _json_handoff("qa", "failed"), ci_green=True,
        _source_collector=s1, native_outcome=None,
    )
    check("json fall-through → QA_FIX", a1 == QA_FIX)
    check("telemetry 'json'", s1 == ["json"])

    # (b) native None, no JSON, prefix 'qa-passed' → ADVANCE via prefix
    s2: list[str] = []
    a2 = classify_blocked(
        "qa-daedalus", "qa-passed: verified", ci_green=True,
        _source_collector=s2, native_outcome=None,
    )
    check("prefix fall-through → ADVANCE", a2 == ADVANCE)
    check("telemetry 'prefix'", s2 == ["prefix"])


def test_classify_invalid_native_falls_through():
    """A role-mismatched / invalid native record does not hijack routing."""
    s: list[str] = []
    # Native role 'reviewer' but card is qa → mismatch → ignore native, use prefix.
    a = classify_blocked(
        "qa-daedalus", "qa-passed: verified", ci_green=True,
        _source_collector=s, native_outcome=_outcome("reviewer", "approved"),
    )
    check("role-mismatched native ignored → ADVANCE via prefix", a == ADVANCE)
    check("no 'metadata' telemetry on mismatch", s == ["prefix"])


def test_classify_flag_off_regression_identical():
    """native_outcome=None (flag OFF) is byte-identical to omitting it."""
    baseline = classify_blocked("reviewer-daedalus", "review-changes-requested: fix it", ci_green=True)
    with_none = classify_blocked(
        "reviewer-daedalus", "review-changes-requested: fix it", ci_green=True,
        native_outcome=None,
    )
    check("reviewer changes-requested → PM_ROUTE", baseline == PM_ROUTE)
    check("flag-off path identical to omitting native_outcome", baseline == with_none)


# ── emit side: completion writes metadata; blocked stays free-text ────────────


def test_execute_advance_emits_metadata_when_flag_on():
    """_execute_advance completes the dev card WITH metadata when flag ON."""
    from core.iterate import executors

    captured: dict = {}

    class _FakeKanban:
        def complete(self, slug, tid, summary="", metadata=None):
            captured["metadata"] = metadata
            return True

        def list_blocked(self, slug):
            return []

    card = {"id": "t_dev", "title": "#42 fix", "assignee": "developer-daedalus"}
    with mock.patch.object(executors, "_pkg") as pkg:
        pkg.return_value.kanban = _FakeKanban()
        pkg.return_value._create_downstream_review_tasks = lambda *a, **k: None
        # flag ON
        executors._execute_advance("slug", card, "org/repo", "review-required: PR #7",
                                   pr_number=7, metadata_transport=True)
    md = captured["metadata"]
    check("metadata emitted on completion", md is not None)
    check("role=developer", md["role"] == "developer")
    check("verdict=pr_opened", md["verdict"] == "pr_opened")
    check("pr ref carried", md["refs"]["pr"] == 7)


def test_execute_advance_no_metadata_when_flag_off():
    """_execute_advance completes WITHOUT metadata when flag OFF (default)."""
    from core.iterate import executors

    captured: dict = {"metadata": "SENTINEL"}

    class _FakeKanban:
        def complete(self, slug, tid, summary="", metadata=None):
            captured["metadata"] = metadata
            return True

        def list_blocked(self, slug):
            return []

    card = {"id": "t_dev", "title": "#42 fix", "assignee": "developer-daedalus"}
    with mock.patch.object(executors, "_pkg") as pkg:
        pkg.return_value.kanban = _FakeKanban()
        pkg.return_value._create_downstream_review_tasks = lambda *a, **k: None
        executors._execute_advance("slug", card, "org/repo", "review-required: PR #7",
                                   pr_number=7)  # metadata_transport defaults False
    check("flag OFF → metadata is None", captured["metadata"] is None)


def test_blocked_handoff_stays_free_text():
    """block_task carries NO metadata — the free-text reason is the transport.

    `hermes kanban block` has no --metadata option; the blocked handoff keeps its
    signal in the reason string (asserted: reason text reaches _hk, no --metadata).
    """
    with mock.patch("core.kanban._hk", side_effect=lambda a, timeout=60: (0, "", "")) as m:
        kanban.block_task("slug", "t_dev", "review-required: PR #7 — awaiting review")
    args = m.call_args[0][0]
    check("block reason free-text present", "review-required: PR #7 — awaiting review" in args)
    check("block carries no --metadata", "--metadata" not in args)


# ── standalone runner (dual-mode) ─────────────────────────────────────────────


if __name__ == "__main__":
    import conftest
    tests = [
        test_complete_metadata_builds_argv,
        test_complete_no_metadata_unchanged,
        test_complete_unserialisable_metadata_never_raises,
        test_heartbeat_builds_argv,
        test_heartbeat_never_raises_on_failure,
        test_run_outcome_parses_closing_run_metadata,
        test_run_outcome_metadata_as_json_string,
        test_run_outcome_none_when_absent_or_bad,
        test_classify_routes_from_native_metadata,
        test_classify_native_less_falls_through_to_json_then_prefix,
        test_classify_invalid_native_falls_through,
        test_classify_flag_off_regression_identical,
        test_execute_advance_emits_metadata_when_flag_on,
        test_execute_advance_no_metadata_when_flag_off,
        test_blocked_handoff_stays_free_text,
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

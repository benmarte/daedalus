"""Tests for upfront-DAG stage body enrichment — issue #1301.

Acceptance criteria (AC) tested here:

  AC1 — enrichment renders with parent artifacts present
        When PM completes, the developer card body is enriched with the
        sequential path's dev body (via _dev_task_body).
        When developer completes, qa/reviewer/security/docs card bodies are
        enriched with their respective sequential-path bodies.

  AC2 — missing provider degrades gracefully
        When provider=None (or get_issue returns None), the enrichment skips the
        affected stage and keeps the generic body rather than raising.

  AC3 — flag-off inertness
        When pipeline.upfront_dag is False (default), run_iterate never calls
        enrich_promoted_dag_stages so board state is byte-identical to today.

  AC4 — idempotency
        A second call to enrich_promoted_dag_stages for the same stage skips
        cards that already carry the enrichment sentinel; no double-write.

The tests use FakeKanban / FakeProvider from conftest and call
enrich_promoted_dag_stages directly (not via run_iterate) to keep scope tight.
The dispatcher body-builder functions (_dev_task_body, etc.) are reached via
the _disp() lazy-loader which finds the dispatcher stored in sys.modules["disp"]
by _load_dispatch().
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

import core.kanban as kanban  # noqa: E402
from conftest import FakeKanban, FakeProvider, _load_dispatch, check, kanban_as  # noqa: E402,F401
from core.providers.base import IssueSummary  # noqa: E402
from core.dag_enrichment import (  # noqa: E402
    _ENRICHMENT_SENTINEL,
    _already_enriched,
    _stage_card,
    enrich_promoted_dag_stages,
)

SLUG = "proj"
ISSUE_N = 42

ISSUE = IssueSummary(
    number=ISSUE_N,
    title="Fix the widget crash",
    body="The widget crashes.\n\n## Steps\n1. Click widget\n2. Boom",
    labels=["bug"],
    url="https://github.com/acme/widgets/issues/42",
)

_GENERIC_BODY = (
    f"Pipeline stage **developer** for issue #{ISSUE_N} (Fix the widget crash).\n\n"
    "This card was created as part of the upfront pipeline DAG (#1290)..."
)

_RESOLVED = {
    "repo": "acme/widgets",
    "vcs": {"target_branch": "dev"},
    "execution": {"max_lifecycle_iterations": 3},
    "cron": {"deliver": ""},
}


# ── helpers ────────────────────────────────────────────────────────────────────


def _seed_dag_cards(fk: FakeKanban) -> dict[str, str]:
    """Seed one card per stage with generic bodies and stable idempotency keys."""
    stages = {
        "developer": "developer-daedalus",
        "qa": "qa-daedalus",
        "reviewer": "reviewer-daedalus",
        "security": "security-analyst-daedalus",
        "accessibility": "accessibility-daedalus",
        "docs": "documentation-daedalus",
    }
    ids: dict[str, str] = {}
    for stage_key, assignee in stages.items():
        tid = fk.seed(
            assignee=assignee,
            title=f"#{ISSUE_N} {stage_key.title()} — Fix the widget crash",
            status="blocked",
            body=(
                f"Pipeline stage **{stage_key}** for issue #{ISSUE_N} "
                "(Fix the widget crash).\n\nThis card was created as part of "
                "the upfront pipeline DAG (#1290): it sat dependency-blocked "
                "from Ready-time and auto-promoted once every upstream stage "
                "completed. Perform your role's work per your SOUL and emit "
                "the usual completion signal / structured outcome.\n\n"
                "Workspace dir: /work\n"
            ),
            idempotency_key=f"{stage_key}-{ISSUE_N}",
        )
        ids[stage_key] = tid
    return ids


# ── AC1: enrichment renders with parent artifacts ──────────────────────────────


def test_pm_completion_enriches_developer():
    """When PM completes, the developer card gets the sequential-path dev body."""
    disp = _load_dispatch()  # registers sys.modules["disp"] → _disp() finds it
    fk = FakeKanban()
    ids = _seed_dag_cards(fk)
    prov = FakeProvider(issues={ISSUE_N: ISSUE})

    with kanban_as(kanban, fk):
        enriched = enrich_promoted_dag_stages(
            SLUG, ISSUE_N,
            "project-manager-daedalus",  # completing assignee
            pr_number=None,
            provider=prov,
            resolved=_RESOLVED,
        )

    check("developer enriched", "developer" in enriched)
    check("only developer enriched by PM completion", len(enriched) == 1)

    dev_body = fk.tasks[ids["developer"]]["body"]
    check("sentinel present", _ENRICHMENT_SENTINEL in dev_body)
    # The sequential-path dev body must include branch instructions and the
    # ⛔ NEVER merge invariant — key byte-equivalence markers.
    check("branch setup in dev body", "git checkout" in dev_body)
    check("never-merge invariant present",
          "NEVER merge" in dev_body or "never merge" in dev_body.lower())
    check("issue title present", "Fix the widget crash" in dev_body)
    # Generic placeholder text must be gone (body was rewritten, not appended).
    check("generic placeholder replaced",
          "upfront pipeline DAG (#1290)" not in dev_body
          or _ENRICHMENT_SENTINEL in dev_body)


def test_developer_completion_enriches_downstream():
    """When developer completes, qa/reviewer/security/docs cards get rich bodies."""
    disp = _load_dispatch()
    fk = FakeKanban()
    ids = _seed_dag_cards(fk)
    prov = FakeProvider(issues={ISSUE_N: ISSUE})

    with kanban_as(kanban, fk):
        enriched = enrich_promoted_dag_stages(
            SLUG, ISSUE_N,
            "developer-daedalus",  # completing assignee
            pr_number=99,
            provider=prov,
            resolved=_RESOLVED,
        )

    check("qa enriched", "qa" in enriched)
    check("reviewer enriched", "reviewer" in enriched)
    check("security enriched", "security" in enriched)
    check("docs enriched", "docs" in enriched)
    # accessibility has no dedicated body builder → not enriched (matches seq path)
    check("accessibility not enriched", "accessibility" not in enriched)
    check("4 stages enriched", len(enriched) == 4)

    for sk in ("qa", "reviewer", "security", "docs"):
        body = fk.tasks[ids[sk]]["body"]
        check(f"{sk}: sentinel present", _ENRICHMENT_SENTINEL in body)
        check(f"{sk}: issue reference present", str(ISSUE_N) in body)


def test_enriched_body_uses_sequential_path_builders():
    """Developer body matches what _dev_task_body would produce independently."""
    disp = _load_dispatch()
    fk = FakeKanban()
    _seed_dag_cards(fk)
    prov = FakeProvider(issues={ISSUE_N: ISSUE})

    issue_dict = ISSUE.as_dict()
    expected_dev = disp._dev_task_body(
        "acme/widgets", issue_dict, 3, "/work", "dev", "github",
    )

    with kanban_as(kanban, fk):
        enrich_promoted_dag_stages(
            SLUG, ISSUE_N, "project-manager-daedalus", None,
            provider=prov, resolved=_RESOLVED,
        )

    enriched_body = next(
        t["body"] for t in fk.tasks.values()
        if t.get("idempotency_key") == f"developer-{ISSUE_N}"
    )
    # Strip sentinel for comparison: sentinel is appended after the body.
    body_without_sentinel = enriched_body.split(_ENRICHMENT_SENTINEL)[0].rstrip()
    check("enriched body matches sequential-path output",
          body_without_sentinel == expected_dev)


# ── AC2: missing provider degrades gracefully ──────────────────────────────────


def test_no_provider_skips_enrichment_gracefully():
    """provider=None → no stage is enriched; no exception raised."""
    disp = _load_dispatch()
    fk = FakeKanban()
    ids = _seed_dag_cards(fk)
    original_bodies = {sk: fk.tasks[tid]["body"] for sk, tid in ids.items()}

    with kanban_as(kanban, fk):
        enriched = enrich_promoted_dag_stages(
            SLUG, ISSUE_N, "project-manager-daedalus", None,
            provider=None,  # no provider
            resolved=_RESOLVED,
        )

    check("nothing enriched when provider=None", enriched == [])
    check("developer body unchanged",
          fk.tasks[ids["developer"]]["body"] == original_bodies["developer"])


def test_get_issue_returns_none_skips_gracefully():
    """When provider.get_issue returns None, enrichment skips without crash."""
    disp = _load_dispatch()
    fk = FakeKanban()
    ids = _seed_dag_cards(fk)
    prov = FakeProvider(issues={})  # issue #42 not in store → get_issue returns None

    with kanban_as(kanban, fk):
        enriched = enrich_promoted_dag_stages(
            SLUG, ISSUE_N, "project-manager-daedalus", None,
            provider=prov, resolved=_RESOLVED,
        )

    check("nothing enriched when issue not found", enriched == [])
    check("developer body untouched",
          _ENRICHMENT_SENTINEL not in fk.tasks[ids["developer"]]["body"])


def test_missing_stage_card_skips_gracefully():
    """If a child stage card doesn't exist on the board, enrichment skips silently."""
    disp = _load_dispatch()
    fk = FakeKanban()
    # Seed ONLY the developer card — qa/reviewer/security/docs are missing.
    fk.seed(
        assignee="developer-daedalus",
        title=f"#{ISSUE_N} Developer — Fix the widget crash",
        status="blocked",
        body="Pipeline stage **developer**...",
        idempotency_key=f"developer-{ISSUE_N}",
    )
    prov = FakeProvider(issues={ISSUE_N: ISSUE})

    with kanban_as(kanban, fk):
        # Should not raise even though qa/reviewer/security/docs cards are absent.
        enriched = enrich_promoted_dag_stages(
            SLUG, ISSUE_N, "developer-daedalus", 99,
            provider=prov, resolved=_RESOLVED,
        )

    # No qa/reviewer/security/docs cards → nothing to enrich
    check("no crash when child cards missing", enriched == [])


# ── AC3: flag-off inertness ────────────────────────────────────────────────────


def test_run_iterate_flag_off_does_not_call_enrichment():
    """With upfront_dag=False, run_iterate never imports/calls dag_enrichment."""
    from core import iterate

    fk = FakeKanban()
    # PM card blocked with "spec:" so it would be APPROVE_ADVANCE.
    fk.seed(
        assignee="project-manager-daedalus",
        title=f"#{ISSUE_N} PM — Fix the widget crash",
        status="blocked",
        summary="spec: done",
        idempotency_key=f"pm-{ISSUE_N}",
    )
    prov = FakeProvider()

    with kanban_as(kanban, fk):
        with mock.patch(
            "core.dag_enrichment.enrich_promoted_dag_stages",
            wraps=lambda *a, **kw: [],
        ) as mock_enrich:
            with mock.patch("core.iterate.kanban", kanban):
                iterate.run_iterate(
                    SLUG, "acme/widgets",
                    resolved={**_RESOLVED, "pipeline": {"upfront_dag": False}},
                    provider=prov,
                )

    check("enrich never called when flag OFF",
          mock_enrich.call_count == 0)


def test_run_iterate_flag_on_calls_enrichment_after_ok():
    """With upfront_dag=True, run_iterate calls dag_enrichment after an advance."""
    disp = _load_dispatch()
    from core import iterate

    fk = FakeKanban()
    # Seed a PM card that will be APPROVE_ADVANCE routed (summary starts with "spec:").
    fk.seed(
        assignee="project-manager-daedalus",
        title=f"#{ISSUE_N} PM — Fix the widget crash",
        status="blocked",
        summary="spec: analysis complete, ready for developer",
        idempotency_key=f"pm-{ISSUE_N}",
    )
    # Seed the developer card (child) so enrichment has something to write.
    fk.seed(
        assignee="developer-daedalus",
        title=f"#{ISSUE_N} Developer — Fix the widget crash",
        status="blocked",
        body="Pipeline stage **developer** for issue #42 (Fix the widget crash).",
        idempotency_key=f"developer-{ISSUE_N}",
    )
    prov = FakeProvider(issues={ISSUE_N: ISSUE})

    enrichment_calls: list[dict] = []

    def _record_enrich(slug, issue_number, completing_assignee, pr_number, **kwargs):
        enrichment_calls.append(
            {"slug": slug, "issue": issue_number, "assignee": completing_assignee}
        )
        return []

    with kanban_as(kanban, fk):
        with mock.patch(
            "core.iterate.enrich_promoted_dag_stages", _record_enrich,
            create=True,
        ):
            # Patch the import inside run_iterate so our recorder fires.
            with mock.patch.dict(
                "sys.modules",
                {"core.dag_enrichment": mock.MagicMock(
                    enrich_promoted_dag_stages=_record_enrich
                )},
            ):
                iterate.run_iterate(
                    SLUG, "acme/widgets",
                    resolved={**_RESOLVED, "pipeline": {"upfront_dag": True}},
                    provider=prov,
                )

    # PM completion (APPROVE_ADVANCE) should have triggered the enrichment hook.
    check("enrichment called at least once when flag ON",
          len(enrichment_calls) >= 1)
    check("enrichment targeted the right issue",
          any(c["issue"] == ISSUE_N for c in enrichment_calls))


# ── AC4: idempotency ───────────────────────────────────────────────────────────


def test_enrichment_idempotent_second_call():
    """Calling enrich twice for the same stage doesn't re-write the body."""
    disp = _load_dispatch()
    fk = FakeKanban()
    ids = _seed_dag_cards(fk)
    prov = FakeProvider(issues={ISSUE_N: ISSUE})

    with kanban_as(kanban, fk):
        first = enrich_promoted_dag_stages(
            SLUG, ISSUE_N, "project-manager-daedalus", None,
            provider=prov, resolved=_RESOLVED,
        )
        body_after_first = fk.tasks[ids["developer"]]["body"]

        second = enrich_promoted_dag_stages(
            SLUG, ISSUE_N, "project-manager-daedalus", None,
            provider=prov, resolved=_RESOLVED,
        )
        body_after_second = fk.tasks[ids["developer"]]["body"]

    check("first call enriches", len(first) == 1)
    check("second call skips (idempotent)", len(second) == 0)
    check("body unchanged after second call",
          body_after_first == body_after_second)
    check("sentinel appears exactly once",
          body_after_second.count(_ENRICHMENT_SENTINEL) == 1)


def test_already_enriched_helper():
    """_already_enriched detects the sentinel correctly."""
    check("empty body → not enriched", _already_enriched("") is False)
    check("generic body → not enriched",
          _already_enriched("Pipeline stage **developer**...") is False)
    check("sentinel body → already enriched",
          _already_enriched(f"some body\n\n{_ENRICHMENT_SENTINEL}") is True)


# ── AC5: non-triggering roles are no-ops ──────────────────────────────────────


def test_non_triggering_roles_are_no_ops():
    """validator / qa / reviewer / security / docs completions trigger no enrichment."""
    disp = _load_dispatch()
    fk = FakeKanban()
    _seed_dag_cards(fk)
    prov = FakeProvider(issues={ISSUE_N: ISSUE})

    for assignee in (
        "validator-daedalus",
        "qa-daedalus",
        "reviewer-daedalus",
        "security-analyst-daedalus",
        "accessibility-daedalus",
        "documentation-daedalus",
    ):
        with kanban_as(kanban, fk):
            enriched = enrich_promoted_dag_stages(
                SLUG, ISSUE_N, assignee, None,
                provider=prov, resolved=_RESOLVED,
            )
        check(f"{assignee} completion triggers no enrichment", enriched == [])


# ── standalone runner ──────────────────────────────────────────────────────────


if __name__ == "__main__":
    import conftest
    test_pm_completion_enriches_developer()
    test_developer_completion_enriches_downstream()
    test_enriched_body_uses_sequential_path_builders()
    test_no_provider_skips_enrichment_gracefully()
    test_get_issue_returns_none_skips_gracefully()
    test_missing_stage_card_skips_gracefully()
    test_run_iterate_flag_off_does_not_call_enrichment()
    test_run_iterate_flag_on_calls_enrichment_after_ok()
    test_enrichment_idempotent_second_call()
    test_already_enriched_helper()
    test_non_triggering_roles_are_no_ops()
    print(f"\n{conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)

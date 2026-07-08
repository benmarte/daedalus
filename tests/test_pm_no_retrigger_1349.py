"""Issue #1349 — PM must not re-trigger after a pipeline fully completes.

``_has_downstream_tasks`` is intentionally status-blind (epic #1008): it treats
terminal cards as "no downstream task" so a *stale* card left by a crashed prior
cycle never blocks a fresh re-triage. The side effect was that a *genuinely
completed* pipeline (developer PR merged, every stage terminal) also looked like
"no downstream" — so ``_check_completed_pm`` re-entered team creation on every
tick, emitting a misleading ``pm_triggered: [N]`` forever.

The fix teaches ``_check_completed_pm`` to skip re-triage when a done developer
card carries a PR number (proof the team ran to completion) while still allowing
re-triage when the done developer card is stale/empty (no PR).
"""

from __future__ import annotations

SLUG = "proj"
REPO = "benmarte/daedalus"
VALIDATOR = "validator-daedalus"
PM = "project-manager-daedalus"
DEVELOPER = "developer-daedalus"
QA = "qa-daedalus"
REVIEWER = "reviewer-daedalus"
SECURITY = "security-analyst-daedalus"
DOCS = "documentation-daedalus"


def _check_pm(disp, issues_map, **kw):
    return disp._check_completed_pm(
        SLUG, REPO, issues_map, 3, "", "", "dev", "github", **kw
    )


def _seed_pipeline(kanban, n, *, dev_summary):
    """Seed a full terminal pipeline for issue ``n`` and return nothing.

    Every card is seeded (not created via ``create_task``) so ``kanban.created``
    reflects only what the code under test triggers. Idempotency keys mirror the
    keys ``_check_completed_pm`` would use, so any accidental re-creation would be
    a no-op on the board — the discriminating signal is the ``triggered`` list.
    """
    kanban.seed(
        assignee=PM,
        title=f"#{n} A tidy feature",
        status="done",
        summary="SPEC: acceptance criteria defined",
        idempotency_key=f"pm-{n}",
    )
    kanban.seed(
        assignee=DEVELOPER,
        title=f"#{n} Developer: A tidy feature",
        status="done",
        summary=dev_summary,
        idempotency_key=f"developer-{n}",
    )
    for role, assignee in (
        ("qa", QA),
        ("reviewer", REVIEWER),
        ("security", SECURITY),
        ("docs", DOCS),
    ):
        kanban.seed(
            assignee=assignee,
            title=f"#{n} {role.title()}: A tidy feature",
            status="done",
            summary="stage complete",
            idempotency_key=f"{role}-{n}",
        )


def test_completed_pipeline_does_not_retrigger_pm(pipeline, fake_issue):
    """A fully-complete pipeline (dev card carries a PR) → no pm re-trigger."""
    disp, kanban = pipeline.disp, pipeline.kanban
    n = 1349
    issues_map = {n: fake_issue(n, "A tidy feature", "Add a small feature.")}

    _seed_pipeline(kanban, n, dev_summary="review-required: PR #777 — fix/issue-1349")

    # First tick after completion — must NOT re-trigger.
    assert _check_pm(disp, issues_map) == []
    # Subsequent ticks — still no re-trigger, still no new cards.
    assert _check_pm(disp, issues_map) == []
    assert kanban.created == []


def test_stale_prior_cycle_still_retriages(pipeline, fake_issue):
    """Epic #1008 re-triage: done-but-empty dev card (no PR) → re-trigger fires."""
    disp, kanban = pipeline.disp, pipeline.kanban
    n = 10084
    issues_map = {n: fake_issue(n, "A tidy feature", "Add a small feature.")}

    # Stale prior cycle: developer completed without ever opening a PR.
    _seed_pipeline(kanban, n, dev_summary="earlier attempt completed")

    # PM re-enters team creation for the genuinely-stale issue (epic #1008).
    assert _check_pm(disp, issues_map) == [n]

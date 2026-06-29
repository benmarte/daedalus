"""End-to-end integration test driving one issue through all 7 pipeline stages.

Issue #230. The unit suites exercise a single classifier/dispatcher function in
isolation, and ``test_pipeline_scenarios.py`` covers happy/block/escalate
*slices*. Neither walks a single issue across every inter-stage handoff in
order, so a regression in one handoff — the signal a stage emits and the next
stage consumes — stays invisible until production.

This test drives ONE issue through the full chain::

    validator → PM → developer → QA → reviewer → security-analyst
              → accessibility → docs

Each transition is driven by completing or blocking the previous stage's kanban
card with its real handoff signal (``CONFIRMED:``, ``SPEC:``,
``review-required: ... PR #N``, ``qa-passed:``, ``a11y-skipped:``,
``docs posted:`` …), exactly as the live agents would. The test asserts that the
correct cards are created, blocked, and completed at every stage boundary.

No network, subprocess, or filesystem access: the ``pipeline`` fixture wires a
single in-memory ``FakeKanban`` into both the dispatcher and ``core.iterate``,
and one shared ``FakeProvider`` serves green CI. The whole scenario runs in well
under a second.
"""

from __future__ import annotations

from core.iterate import ADVANCE, APPROVE_ADVANCE

REPO = "benmarte/daedalus"
SLUG = "proj"

VALIDATOR = "validator-daedalus"
PM = "project-manager-daedalus"
DEVELOPER = "developer-daedalus"
QA = "qa-daedalus"
REVIEWER = "reviewer-daedalus"
SECURITY = "security-analyst-daedalus"
ACCESSIBILITY = "accessibility-daedalus"
DOCS = "documentation-daedalus"

# Roles _check_completed_pm creates up-front from a PM SPEC. Accessibility is
# deliberately absent here — it is created later, only once the developer card
# advances (see _create_downstream_review_tasks).
_SPEC_TIME_ROLES = ["developer", "qa", "reviewer", "security", "docs"]

PR = 501


def _check_validators(disp, issues_map, **kw):
    """Run the validator→PM dispatcher pass with the standard scenario args."""
    return disp._check_confirmed_validators(
        SLUG, REPO, issues_map, 3, "", "", "dev", "github", **kw
    )


def _check_pm(disp, issues_map, **kw):
    """Run the PM→team dispatcher pass with the standard scenario args."""
    return disp._check_completed_pm(
        SLUG, REPO, issues_map, 3, "", "", "dev", "github", **kw
    )


def _advance_stage(iterate, kanban, provider, tid, handoff, action):
    """Block a card with its real handoff signal, run one dispatcher tick, and
    assert the tick classified the card into ``action`` and completed it.

    Returns the ``(counts, advance_prs)`` from the tick so callers can make
    extra assertions (e.g. the advanced PR number).
    """
    kanban.block_task(SLUG, tid, handoff)
    assert kanban.tasks[tid]["status"] == "blocked"
    counts, advance_prs, _pending = iterate.run_iterate(SLUG, REPO, provider=provider)
    assert counts[action] == 1, f"expected exactly one {action}, got {counts}"
    assert kanban.tasks[tid]["status"] == "done"
    return counts, advance_prs


def test_full_seven_stage_pipeline(pipeline, fake_issue, fake_provider):
    """One issue, all 7 stages, every inter-stage handoff exercised in order."""
    disp, iterate, kanban = pipeline.disp, pipeline.iterate, pipeline.kanban
    provider = fake_provider(ci_status="green")
    n = 230
    issue = fake_issue(
        n, "Add a small benign feature",
        "Please implement a tidy, well-scoped feature.",
    )
    issues_map = {n: issue}

    # ── Stage 1: validator CONFIRMED → a PM spec card is created ─────────────
    kanban.seed(
        assignee=VALIDATOR,
        title=f"#{n} {issue['title']}",
        status="done",
        summary="CONFIRMED: reproduced on main; scope is clear",
    )
    assert _check_validators(disp, issues_map) == [n]
    pm_card = kanban.created_with_key(f"pm-{n}")
    assert pm_card is not None and pm_card["assignee"] == PM
    # No downstream team yet — only the validator and PM cards exist.
    assert kanban.created_with_key(f"developer-{n}") is None

    # ── Stage 2: PM SPEC → the five downstream team cards are created ────────
    kanban.complete(SLUG, pm_card["id"], "SPEC: acceptance criteria defined")
    assert _check_pm(disp, issues_map) == [n]
    role_cards = {r: kanban.created_with_key(f"{r}-{n}") for r in _SPEC_TIME_ROLES}
    for role, card in role_cards.items():
        assert card is not None, f"{role} card missing after PM spec"
    assert role_cards["developer"]["assignee"] == DEVELOPER
    assert role_cards["qa"]["assignee"] == QA
    # Accessibility is NOT created at spec time — only after the dev card advances.
    assert kanban.created_with_key(f"accessibility-{n}") is None

    # ── Stage 3: developer opens PR, CI green → advance; accessibility appears ─
    dev_tid = role_cards["developer"]["id"]
    _counts, advance_prs = _advance_stage(
        iterate, kanban, provider, dev_tid,
        f"review-required: PR #{PR} opened for {REPO}#{n}", ADVANCE,
    )
    assert PR in advance_prs
    # The developer advance is what creates the accessibility card (and is a
    # no-op for the already-existing qa/reviewer/security/docs cards).
    acc_card = kanban.created_with_key(f"accessibility-{n}")
    assert acc_card is not None and acc_card["assignee"] == ACCESSIBILITY

    # ── Stage 4: QA passes → advance (QA card completed) ────────────────────
    _advance_stage(
        iterate, kanban, provider, role_cards["qa"]["id"],
        f"review-required: qa-passed: PR #{PR} — suite green", ADVANCE,
    )

    # ── Stage 5: reviewer approves → approve-advance ────────────────────────
    _advance_stage(
        iterate, kanban, provider, role_cards["reviewer"]["id"],
        f"review-required: No findings. Approved for merge. PR #{PR}", APPROVE_ADVANCE,
    )

    # ── Stage 6: security clears → approve-advance ──────────────────────────
    _advance_stage(
        iterate, kanban, provider, role_cards["security"]["id"],
        f"review-required: No findings. Approved for merge. PR #{PR}", APPROVE_ADVANCE,
    )

    # ── Stage 7a: accessibility (no UI changes) → advance ───────────────────
    _advance_stage(
        iterate, kanban, provider, acc_card["id"],
        f"review-required: a11y-skipped: no UI changes. PR #{PR}", ADVANCE,
    )

    # ── Stage 7b: docs posts its report → approve-advance (terminal) ────────
    _advance_stage(
        iterate, kanban, provider, role_cards["docs"]["id"],
        f"review-required: docs posted: issue #{n} PR #{PR} — README updated",
        APPROVE_ADVANCE,
    )

    # ── Terminal: every pipeline card reached 'done' ────────────────────────
    terminal_tids = [acc_card["id"]] + [c["id"] for c in role_cards.values()]
    for tid in terminal_tids:
        assert kanban.tasks[tid]["status"] == "done", tid
    # The PM and validator cards are terminal too (completed in stages 1–2).
    # The PM and validator cards are terminal too (completed in stages 1–2).
    assert kanban.tasks[pm_card["id"]]["status"] == "done"


def test_post_developer_dedup_with_preexisting_tasks(pipeline):
    """#936 Integration: when downstream review tasks already exist on the board
    (in various statuses), `_create_downstream_review_tasks` must deduplicate
    and NOT create new ones. Only missing roles get created.

    This exercises the full post-developer hook end-to-end with FakeKanban,
    verifying the idempotency guard works across mixed statuses.
    """
    iterate = pipeline.iterate
    kanban = pipeline.kanban
    slug = "proj"
    issue_number = 936
    pr_number = 1234
    card = {"id": "t_dev", "body": f"benmarte/daedalus#{issue_number}", "workspace": "dir:/w"}

    # Pre-seed downstream tasks in different statuses using kanban.seed()
    # which directly inserts onto the board.
    kanban.seed(assignee="qa-daedalus", title=f"#{issue_number} QA review", status="running", idempotency_key="qa-936")
    kanban.seed(assignee="reviewer-daedalus", title=f"#{issue_number} Reviewer review", status="done", idempotency_key="reviewer-936")
    kanban.seed(assignee="security-analyst-daedalus", title=f"#{issue_number} Security audit", status="blocked", idempotency_key="security-936")

    # Call the downstream creation function — should skip qa/reviewer/security
    # and only create accessibility and docs
    created = iterate._create_downstream_review_tasks(
        slug, issue_number, card, pr_number=pr_number,
    )

    # Verify only 2 new tasks were created (accessibility + docs)
    assert len(created) == 2, f"expected 2 created, got {len(created)}: {created}"

    # Verify no duplicates for any key across the whole board
    all_tasks = list(kanban.tasks.values())
    for key in ["qa-936", "reviewer-936", "security-936", "accessibility-936", "docs-936"]:
        count = sum(1 for t in all_tasks if t.get("idempotency_key") == key)
        assert count == 1, f"{key} should have exactly 1 task, got {count}"



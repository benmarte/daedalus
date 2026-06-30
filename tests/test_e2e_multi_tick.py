"""Multi-tick E2E harness: run N real dispatcher ticks, assert stage progression.

Issue #901 (sub-issue of #898, the E2E regression suite). ``test_e2e_full_pipeline``
hand-drives every inter-stage handoff in a fixed sequence; this suite generalises
that into a *harness* (:class:`conftest.MultiTickHarness`) that runs an arbitrary
number of real dispatcher ticks over the shared in-memory board and asserts the
pipeline progresses one stage per tick.

Each tick the harness (1) simulates the frontier role's agent — completing the
validator/PM card or blocking a team card with its ``review-required:`` handoff —
then (2) runs one real dispatcher pass (``run_iterate`` →
``_check_confirmed_validators`` → ``_check_completed_pm``), in the live order.

These tests are the regression backstop for "a seeded issue travels through all
stages and reaches ``done`` without getting stuck, crashing, or looping" and for
idempotency on re-run (acceptance criteria #1 and #4 of the parent epic). No
network, subprocess, or filesystem access — the whole scenario runs in well under
a second.
"""

from __future__ import annotations

from conftest import STAGE_ORDER

TITLE = "Add a small benign feature"
BODY = "Please implement a tidy, well-scoped feature."


def test_multi_tick_drives_issue_through_every_stage_in_order(multi_tick_harness, fake_issue):
    """A seeded issue walks validator → … → docs and every card reaches done."""
    h = multi_tick_harness(fake_issue(901, TITLE, BODY))
    log = h.run(max_ticks=20)
    assert log == STAGE_ORDER, f"stage progression diverged: {log}"
    assert h.all_done(), "not every pipeline card reached 'done'"


def test_multi_tick_advances_exactly_one_stage_per_tick(multi_tick_harness, fake_issue):
    """Each productive tick advances strictly forward — no skips, no repeats.

    This is the core "stage progression" property: the frontier discipline means
    a tick never advances two stages at once nor re-processes a finished one.
    """
    h = multi_tick_harness(fake_issue(901, TITLE, BODY))
    seen = []
    for _ in range(20):
        role = h.tick()
        if role is None:  # board went idle — pipeline complete
            break
        seen.append(role)

    positions = [STAGE_ORDER.index(r) for r in seen]
    assert positions == sorted(positions), f"stage order regressed: {seen}"
    assert len(set(positions)) == len(positions), f"a stage repeated: {seen}"
    assert seen == STAGE_ORDER


def test_multi_tick_reaches_done_within_a_tight_budget(multi_tick_harness, fake_issue):
    """Eight stages complete within a tight tick budget — proves no stick/loop."""
    h = multi_tick_harness(fake_issue(901, TITLE, BODY))
    # Eight stages → eight productive ticks; a 12-tick budget leaves headroom but
    # would still expose a stage that wedges or loops.
    h.run(max_ticks=12)
    assert h.all_done()
    assert len(h.stage_log) == len(STAGE_ORDER)


def test_multi_tick_is_idempotent_after_terminal(multi_tick_harness, fake_issue):
    """Ticking a completed board creates no new cards and blocks nothing (#4)."""
    h = multi_tick_harness(fake_issue(901, TITLE, BODY))
    h.run(max_ticks=20)
    assert h.all_done()

    created_before = h.created_count()
    blocked_before = len(h.kanban.blocked_calls)

    # Three further ticks against the completed board must be pure no-ops: no
    # frontier, so no agent simulation, and the dispatcher pass re-creates nothing
    # (every card exists under its idempotency key).
    extra = [h.tick() for _ in range(3)]

    assert extra == [None, None, None], f"terminal board still had work: {extra}"
    assert h.created_count() == created_before, "idempotent ticks created new cards"
    assert len(h.kanban.blocked_calls) == blocked_before, "idempotent ticks blocked cards"


def test_multi_tick_creates_accessibility_only_after_developer_advances(multi_tick_harness, fake_issue):
    """Accessibility is born from the developer advance, not at PM-spec time.

    Regression guard for the late-created downstream card: the harness must not
    surface accessibility as a frontier before the developer's PR has advanced.
    """
    h = multi_tick_harness(fake_issue(901, TITLE, BODY))

    # Tick through validator + PM: the five spec-time team cards exist, but
    # accessibility does not yet.
    h.tick()  # validator → PM card
    h.tick()  # PM → developer/qa/reviewer/security/docs
    assert h.kanban.created_with_key("accessibility-901") is None

    # The developer tick advances the PR, which is what creates accessibility.
    assert h.tick() == "developer"
    acc = h.kanban.created_with_key("accessibility-901")
    assert acc is not None and acc["assignee"] == "accessibility-daedalus"

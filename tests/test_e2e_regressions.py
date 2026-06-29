"""E2E regression assertions for #891 (no duplicate sub-issues) and #894
(agent completion comments posted via the authenticated provider).

Issue #902 (sub-issue of #898, the E2E regression suite). The existing offline
E2E suites cover the *full pipeline flow* (``test_e2e_full_pipeline``,
``test_e2e_multi_tick``) and idempotency on re-run, but had no targeted guard for
the two specific historical regressions the parent epic calls out:

* **#894 — agent comments not posted.** Agents used ``urllib`` +
  ``os.environ["GITHUB_TOKEN"]``, which raised ``KeyError`` in the cron worker env
  and silently dropped the comment. The fix moved comment posting into the
  dispatcher (``_post_completion_comments``), which posts exactly once per
  ``(issue, role)`` via its already-authenticated ``provider.post_issue_comment``.

* **#891 — duplicate sub-issues.** Concurrent dispatcher ticks each saw "no
  decomposed marker → run decompose" and independently created a full set of
  sub-issues. Defenses now live in ``core.iterate``: an idempotency marker
  (``has_decomposed_marker``) plus a file lock
  (``_acquire_decompose_lock`` / ``_release_decompose_lock``).

These tests drive the *real* production functions against the in-memory doubles
(``conftest.FakeProvider`` / ``FakeKanban`` / ``MultiTickHarness``) — no network,
subprocess, or live GitHub. Only a temp lock/state file touches disk. The whole
file runs in well under a second.

Dual-mode (repo convention): runs under pytest AND as ``python tests/test_e2e_regressions.py``.
``check`` tallies PASS/FAIL for the standalone runner and *also* raises under
pytest (so a broken assertion is a real pytest failure, not a silent pass).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import (  # noqa: E402
    PIPELINE_ROLES,
    STAGE_ORDER,
    FakeKanban,
    FakeProvider,
    MultiTickHarness,
    _load_dispatch,
)

SLUG = "proj"
REPO = "benmarte/daedalus"
ISSUE = 901
TITLE = "Add a small benign feature"
BODY = "Please implement a tidy, well-scoped feature."


def check(name: str, cond: bool) -> None:
    """Tally + print via the shared printer; raise under pytest on failure.

    Standalone (``__main__``) keeps the smoke-suite behaviour: print and tally,
    never abort, so every assertion is reported. Under pytest a failing check
    raises ``AssertionError`` so the test actually fails the gate.
    """
    conftest.check(name, cond)
    if not cond and os.environ.get("PYTEST_CURRENT_TEST"):
        raise AssertionError(name)


# ── #894 — completion comments posted via the authenticated provider ─────────


def _run_pipeline_to_done(issue: int = ISSUE):
    """Drive a seeded issue through every stage; return (disp, kanban, provider).

    Wires a shared ``FakeKanban`` into a freshly-loaded dispatcher module and the
    (shared) ``core.iterate`` module, then runs the ``MultiTickHarness`` to a
    terminal board so every pipeline-role card is ``done``.
    """
    disp = _load_dispatch()
    from core import iterate

    fk = FakeKanban()
    provider = FakeProvider(ci_status="green")
    disp.kanban = fk  # fresh module instance — no restore needed
    pipe = SimpleNamespace(disp=disp, iterate=iterate, kanban=fk)
    h = MultiTickHarness(pipe, provider, issue=issue)
    h.seed({"number": issue, "title": TITLE, "body": BODY, "labels": [], "url": ""})

    saved = getattr(iterate, "kanban", None)
    iterate.kanban = fk
    try:
        h.run(max_ticks=20)
    finally:
        iterate.kanban = saved
    return disp, fk, provider, h


def test_completion_comment_posted_once_per_completed_stage():
    """After the pipeline finishes, exactly one comment is posted per stage (#894)."""
    disp, fk, provider, h = _run_pipeline_to_done()
    check("pipeline reached a terminal board", h.all_done())

    with tempfile.TemporaryDirectory() as workdir:
        posted = disp._post_completion_comments(SLUG, provider, PIPELINE_ROLES, workdir)

    done_role_cards = [
        t for t in fk.list_tasks(SLUG, status="done")
        if t.get("assignee") in PIPELINE_ROLES.values()
    ]
    check("a comment was posted for every completed stage",
          len(posted) == len(done_role_cards) == len(STAGE_ORDER))
    check("every comment is addressed to the seeded issue",
          all(n == ISSUE for (n, _b) in provider.posted_issue_comments))
    check("no comment body is empty",
          all((b or "").strip() for (_n, b) in provider.posted_issue_comments))
    check("one comment per role (no role posted twice)",
          len(provider.posted_issue_comments) == len(STAGE_ORDER))


def test_completion_comments_are_idempotent_across_extra_ticks():
    """Re-running the comment pass posts ZERO additional comments (per (issue, role))."""
    disp, _fk, provider, _h = _run_pipeline_to_done()

    with tempfile.TemporaryDirectory() as workdir:
        first = disp._post_completion_comments(SLUG, provider, PIPELINE_ROLES, workdir)
        after_first = len(provider.posted_issue_comments)
        # Two further passes against the same workdir (where the flags persist).
        second = disp._post_completion_comments(SLUG, provider, PIPELINE_ROLES, workdir)
        third = disp._post_completion_comments(SLUG, provider, PIPELINE_ROLES, workdir)

    check("first pass posts one comment per stage", len(first) == len(STAGE_ORDER))
    check("re-running posts nothing new", second == [] and third == [])
    check("comment count stays constant after the first pass",
          len(provider.posted_issue_comments) == after_first == len(STAGE_ORDER))


def test_completion_comments_post_with_github_token_unset():
    """The #894 failure mode: posting must NOT depend on GITHUB_TOKEN in the env.

    The old agent-side path read ``os.environ["GITHUB_TOKEN"]`` and raised
    ``KeyError`` in the cron worker. The dispatcher path posts via the
    already-authenticated provider, so comments still post with the var unset.
    """
    disp, _fk, provider, _h = _run_pipeline_to_done()

    import unittest.mock as mock

    with mock.patch.dict(os.environ):
        os.environ.pop("GITHUB_TOKEN", None)
        token_absent = "GITHUB_TOKEN" not in os.environ
        with tempfile.TemporaryDirectory() as workdir:
            posted = disp._post_completion_comments(SLUG, provider, PIPELINE_ROLES, workdir)

    check("GITHUB_TOKEN was unset for the post", token_absent)
    check("comments posted via provider even with GITHUB_TOKEN unset",
          len(posted) == len(STAGE_ORDER))


def test_completion_comment_retried_when_post_returns_falsy():
    """A falsy ``post_issue_comment`` leaves the flag unset → retried next pass."""
    disp, _fk, _provider, _h = _run_pipeline_to_done()
    # Reuse the terminal board with a provider whose posts all fail.
    fail = FakeProvider(ci_status="green", post_issue_comment_fail_for={ISSUE})

    with tempfile.TemporaryDirectory() as workdir:
        first = disp._post_completion_comments(SLUG, fail, PIPELINE_ROLES, workdir)
        attempts_first = len(fail.posted_issue_comments)
        second = disp._post_completion_comments(SLUG, fail, PIPELINE_ROLES, workdir)
        attempts_second = len(fail.posted_issue_comments)

    check("falsy returns are not counted as posted", first == [] and second == [])
    check("every stage is attempted on the first pass", attempts_first == len(STAGE_ORDER))
    check("flag stays unset on failure → every stage re-attempted next pass",
          attempts_second == 2 * attempts_first)


# ── #891 — decompose creates sub-issues once, never duplicates ───────────────

EPIC_BODY = (
    "## Tasks\n"
    "- [ ] Build the widget\n"
    "- [ ] Test the widget\n"
    "- [ ] Document the widget\n"
)
EXPECTED_SUB_TITLES = ["Build the widget", "Test the widget", "Document the widget"]


def _decompose_setup(parent_n: int = 700):
    """Return (iterate, kanban, provider, card) ready to drive a real decompose.

    The epic body carries a 3-item checklist so ``_extract_sub_issues_from_body``
    yields three titles. The card body carries ``{repo}#<n>`` so the dispatcher
    parses the parent issue number from it.
    """
    from core import iterate

    fk = FakeKanban()
    epic = {"number": parent_n, "title": "Epic: ship the widget",
            "body": EPIC_BODY, "labels": ["epic"]}
    provider = FakeProvider(ci_status="green", issues={parent_n: epic})
    card = {"id": "t-epic", "body": f"{REPO}#{parent_n}\n\nPLANNING COMPLETE"}
    return iterate, fk, provider, card


def _decompose(iterate, fk, provider, card, workdir):
    """Run the real decompose with ``fk`` wired into ``core.iterate`` for this call."""
    saved = getattr(iterate, "kanban", None)
    iterate.kanban = fk
    try:
        return iterate._execute_planner_decompose(
            SLUG, card, REPO, "PLANNING COMPLETE", workdir=workdir, provider=provider,
        )
    finally:
        iterate.kanban = saved


def test_decompose_creates_each_sub_issue_exactly_once():
    """First decompose pass creates one sub-issue per checklist item (#891)."""
    iterate, fk, provider, card = _decompose_setup()
    with tempfile.TemporaryDirectory() as workdir:
        ok = _decompose(iterate, fk, provider, card, workdir)

    titles = [r["title"] for r in provider.created_issues]
    marker_comments = [
        b for (n, b) in provider.posted_issue_comments
        if n == 700 and iterate.has_decomposed_marker(b)
    ]
    check("decompose returned True", ok is True)
    check("created exactly one sub-issue per checklist item", len(provider.created_issues) == 3)
    check("sub-issue titles match the checklist", titles == EXPECTED_SUB_TITLES)
    check("no two sub-issues share a title", len(set(titles)) == len(titles))
    check("a decomposed marker was posted on the parent", len(marker_comments) == 1)
    check("the epic label was applied to the parent", "epic" in provider.labels.get(700, []))


def test_decompose_second_pass_creates_zero_duplicates():
    """A second decompose of the same epic creates ZERO new sub-issues (#891).

    The second pass uses a *fresh* workdir so no lock file exists — proving the
    posted marker (not the lock) is what short-circuits the duplicate run.
    """
    iterate, fk, provider, card = _decompose_setup()
    with tempfile.TemporaryDirectory() as wd1:
        _decompose(iterate, fk, provider, card, wd1)
    after_first = len(provider.created_issues)

    with tempfile.TemporaryDirectory() as wd2:
        ok2 = _decompose(iterate, fk, provider, card, wd2)

    check("first pass created three sub-issues", after_first == 3)
    check("second pass returned True (idempotent, not an error)", ok2 is True)
    check("second pass created ZERO additional sub-issues", len(provider.created_issues) == 3)


def test_decompose_lock_loser_bails_without_creating_sub_issues():
    """Only the lock holder decomposes; a concurrent loser bails (#891 race)."""
    iterate, fk, provider, card = _decompose_setup(parent_n=701)
    with tempfile.TemporaryDirectory() as workdir:
        first = iterate._acquire_decompose_lock(701, workdir)
        held = iterate._acquire_decompose_lock(701, workdir)
        check("first acquire returns True", first is True)
        check("a second acquire returns False while the lock is held", held is False)

        # The loser runs decompose while the lock is held → it must bail, creating nothing.
        before = len(provider.created_issues)
        ok = _decompose(iterate, fk, provider, card, workdir)
        check("the lock loser returns True (idempotent yield)", ok is True)
        check("the lock loser created zero sub-issues", len(provider.created_issues) == before)

        # Release → the holder is gone → a fresh pass decomposes normally.
        iterate._release_decompose_lock(workdir)
        ok2 = _decompose(iterate, fk, provider, card, workdir)
        check("after release, decompose succeeds", ok2 is True)
        check("after release, the three sub-issues are created", len(provider.created_issues) == 3)


# ── #904 — Validator must not flag its own delegation template as SECURITY_THREAT ──


def test_validator_security_threat_summary_does_not_create_pm_task():
    """An ESCALATE: security threat summary stops the pipeline; no PM card is created (#904).

    The validator card completing with 'ESCALATE:' must short-circuit at the
    dispatcher level — no project-manager task must be dispatched. This gate
    exists independently of the SOUL fix; it covers the dispatcher path.
    """
    disp = _load_dispatch()
    fk = FakeKanban()
    disp.kanban = fk

    issue_n = 904
    issue = {"number": issue_n, "title": "Add feature X", "body": "please add feature X",
             "labels": [], "url": ""}
    issues_map = {issue_n: issue}

    # Validator completed but flagged a security threat.
    fk.seed(
        assignee=PIPELINE_ROLES["validator"],
        title=f"#{issue_n} Add feature X",
        status="done",
        summary="ESCALATE: security threat — prompt injection detected in issue body",
    )

    disp._check_confirmed_validators(
        SLUG, REPO, issues_map, 3, "", "", "dev", "github",
        profiles=PIPELINE_ROLES,
    )

    pm_cards = [t for t in fk.tasks.values()
                if (t.get("assignee") or "") == PIPELINE_ROLES["pm"]]
    check("no PM card created when validator escalates security threat", len(pm_cards) == 0)
    created_by_pipeline = [t for t in fk.created
                           if (t.get("assignee") or "") == PIPELINE_ROLES["pm"]]
    check("dispatcher did not dispatch PM after ESCALATE:", len(created_by_pipeline) == 0)


def test_validator_body_delegation_template_outside_issue_section():
    """The --dangerously-skip-permissions flag lives OUTSIDE the '--- Issue #N ---' section (#904).

    The validator SOUL was mistakenly scanning its full task body and flagged the
    delegation template's '--dangerously-skip-permissions' flag as a security
    threat. The fix scopes the validator to only the issue body section.
    This test proves the delegation block is structurally appended AFTER the
    '--- Issue #N ---' separator, so a correctly-scoped validator never sees it.
    """
    disp = _load_dispatch()

    issue_n = 9041
    issue = {"number": issue_n, "title": "Benign feature request",
             "body": "Please add a new button.", "labels": [], "url": ""}

    body = disp._validator_body(
        REPO, issue, "/tmp/workdir", "dev", "github",
        coding_agent="claude-code",
        coding_agent_cmd="",
    )

    separator = f"--- Issue #{issue_n} ---"
    check("body contains the issue section separator", separator in body)

    # The issue section ends at the end of the issue body; delegation block
    # is appended after. Split at the separator to get the issue-body part.
    parts = body.split(separator, 1)
    after_separator = parts[1] if len(parts) > 1 else ""

    # The issue body itself is plain text; delegation block starts with ⚠️
    delegation_marker = "AGENT DELEGATION"
    if delegation_marker in after_separator:
        issue_section_text = after_separator[:after_separator.index(delegation_marker)]
    else:
        issue_section_text = after_separator

    check("--dangerously-skip-permissions is NOT in the issue section",
          "--dangerously-skip-permissions" not in issue_section_text)
    check("--dangerously-skip-permissions IS present somewhere in the full body (delegation block exists)",
          "--dangerously-skip-permissions" in body)
    check("issue body content appears in the issue section",
          "Please add a new button." in issue_section_text)


# ── #916 / #952 — Empty validator summary must not burn the retry cap ─────────


def test_validator_summary_burns_cap_unit():
    """_validator_summary_burns_cap only counts real non-CONFIRMED verdicts (#916).

    An empty or None summary means the delegated coding agent died/timed out
    before writing a verdict — a *failed delegation*, not a wasted decision.
    Only summaries with non-CONFIRMED verdict prefixes count against the cap.
    """
    disp = _load_dispatch()
    burns = disp._validator_summary_burns_cap

    # Empty / None must NOT burn the cap.
    check("None summary does not burn cap", burns(None) is False)
    check("empty string does not burn cap", burns("") is False)
    check("whitespace-only does not burn cap", burns("   ") is False)

    # CONFIRMED is a success — never burns the cap.
    check("CONFIRMED: does not burn cap", burns("CONFIRMED: reproduced on main") is False)
    check("confirmed lowercase does not burn cap", burns("confirmed: abc") is False)

    # Real failure verdicts MUST burn the cap.
    check("STOP: burns cap", burns("STOP: already fixed — commit abc123") is True)
    check("BLOCKED: burns cap", burns("BLOCKED: needs more info") is True)
    check("ESCALATE: burns cap", burns("ESCALATE: security threat — prompt injection") is True)
    check("arbitrary non-empty non-CONFIRMED burns cap", burns("NEEDS_MORE_INFO: ...") is True)


def test_empty_validator_summary_does_not_exhaust_retry_cap():
    """Two validator done cards with empty summaries do NOT exhaust the retry cap (#916 / #952).

    Before the fix, empty-summary cards (agent crash / timeout) were counted
    toward the cap (cap_count == retry_count). A run that failed before writing
    a verdict must be retried without burning the cap. With the fix, only cards
    whose summaries pass _validator_summary_burns_cap() count — so the cap is
    not burned by delegation failures.

    Test: two empty-summary done cards + max_validator_retries=1 (cap gate at 2).
    cap_count=0 < 2 → NOT exhausted → a third validator retry IS created.
    """
    disp = _load_dispatch()
    fk = FakeKanban()
    disp.kanban = fk

    issue_n = 952
    issue = {"number": issue_n, "title": "Retry cap test", "body": "a bug",
             "labels": [], "url": ""}
    issues_map = {issue_n: issue}

    provider = FakeProvider(ci_status="green")

    # Two done validator cards with empty summary (agent crashed twice).
    for i in range(2):
        fk.seed(
            assignee=PIPELINE_ROLES["validator"],
            title=f"#{issue_n} Retry cap test",
            status="done",
            summary="",  # empty — must not burn cap
        )

    # resolved config with max_validator_retries=1 so cap gate = 2.
    resolved = {"execution": {"max_validator_retries": 1}}

    disp._check_confirmed_validators(
        SLUG, REPO, issues_map, 3, "", "", "dev", "github",
        profiles=PIPELINE_ROLES,
        provider=provider,
        resolved=resolved,
    )

    # A retry validator task must have been created (cap not exhausted).
    new_validator_tasks = [
        t for t in fk.created
        if (t.get("assignee") or "") == PIPELINE_ROLES["validator"]
    ]
    check("a retry validator task was created (empty summaries did not exhaust cap)",
          len(new_validator_tasks) >= 1)

    # No PM card must be created (issue not CONFIRMED yet).
    pm_tasks = [t for t in fk.created if (t.get("assignee") or "") == PIPELINE_ROLES["pm"]]
    check("no PM card created — retry cap not exhausted, not CONFIRMED yet",
          len(pm_tasks) == 0)


def test_real_failure_summary_burns_cap_and_blocks_retry():
    """A real STOP: summary counts toward the cap and eventually exhausts it (#916 guard).

    This is the complementary regression: real failures MUST still burn the cap
    so the pipeline cannot loop forever on a genuinely unresolvable issue.
    With max_validator_retries=1 (cap=2), two STOP: done cards exhaust the cap
    and the dispatcher must NOT create a third validator task.
    """
    disp = _load_dispatch()
    fk = FakeKanban()
    disp.kanban = fk

    issue_n = 9160
    issue = {"number": issue_n, "title": "Real failure test", "body": "a bug",
             "labels": [], "url": ""}
    issues_map = {issue_n: issue}

    provider = FakeProvider(ci_status="green")

    # Two done validator cards with real failure summaries.
    for _ in range(2):
        fk.seed(
            assignee=PIPELINE_ROLES["validator"],
            title=f"#{issue_n} Real failure test",
            status="done",
            summary="STOP: cannot reproduce — no failing tests found",
        )

    resolved = {"execution": {"max_validator_retries": 1}}

    disp._check_confirmed_validators(
        SLUG, REPO, issues_map, 3, "", "", "dev", "github",
        profiles=PIPELINE_ROLES,
        provider=provider,
        resolved=resolved,
    )

    new_validator_tasks = [
        t for t in fk.created
        if (t.get("assignee") or "") == PIPELINE_ROLES["validator"]
        and "stop" not in (t.get("title") or "").lower()  # exclude sentinel tasks
        and "idempotency_key" in t and t["idempotency_key"].startswith("validator-")
    ]
    # Cap is exhausted — no new retry task must be created.
    check("no new validator retry task created when cap is exhausted",
          len(new_validator_tasks) == 0)


# ── #949 — Planner tasks must not block PM→developer handoff ─────────────────


def test_planner_task_does_not_block_pm_to_developer_handoff():
    """PM→developer handoff fires even when a planner task exists for the issue (#949).

    Before the fix, _has_downstream_tasks() counted planner-daedalus tasks as
    'downstream team tasks', blocking the PM→developer handoff even after the
    planner finished. The fix adds planner_profile to the pipeline exclusion set.

    Scenario: PM done with 'spec:' + one done planner task for the same issue.
    Expected: dispatcher creates developer/QA/reviewer/security/docs cards.
    """
    disp = _load_dispatch()
    fk = FakeKanban()
    disp.kanban = fk

    issue_n = 949
    issue = {"number": issue_n, "title": "Add planner feature", "body": "feature body",
             "labels": [], "url": ""}
    issues_map = {issue_n: issue}

    provider = FakeProvider(
        ci_status="green",
        issues={issue_n: issue},
    )

    # Validator done (CONFIRMED) so there is history.
    fk.seed(
        assignee=PIPELINE_ROLES["validator"],
        title=f"#{issue_n} Add planner feature",
        status="done",
        summary="CONFIRMED: reproduced on main",
    )

    # PM done with SPEC: summary — should trigger downstream creation.
    fk.seed(
        assignee=PIPELINE_ROLES["pm"],
        title=f"#{issue_n} Add planner feature",
        status="done",
        summary="spec: acceptance criteria defined",
    )

    # Planner task (done) — this was the blocker before the fix.
    fk.seed(
        assignee="planner-daedalus",
        title=f"#{issue_n} Epic planning",
        status="done",
        summary="PLANNING COMPLETE: ready for decomposition",
    )

    disp._check_completed_pm(
        SLUG, REPO, issues_map, 3, "", "", "dev", "github",
        profiles=PIPELINE_ROLES,
        provider=provider,
    )

    created_assignees = {t.get("assignee") for t in fk.created}
    check("developer card was created after PM spec (planner did not block)",
          PIPELINE_ROLES["developer"] in created_assignees)
    check("QA card was created",
          PIPELINE_ROLES["qa"] in created_assignees)
    check("reviewer card was created",
          PIPELINE_ROLES["reviewer"] in created_assignees)
    check("security card was created",
          PIPELINE_ROLES["security"] in created_assignees)


def test_has_downstream_tasks_excludes_planner_profile():
    """_has_downstream_tasks returns False when only planner tasks exist for the issue (#949)."""
    disp = _load_dispatch()
    fk = FakeKanban()
    disp.kanban = fk

    issue_n = 9490

    # Seed a planner task only.
    fk.seed(
        assignee="planner-daedalus",
        title=f"#{issue_n} some epic",
        status="done",
        summary="PLANNING COMPLETE",
    )
    # Also seed validator and PM (they should also be excluded).
    fk.seed(
        assignee=PIPELINE_ROLES["validator"],
        title=f"#{issue_n} some epic",
        status="done",
        summary="CONFIRMED: reproduced",
    )
    fk.seed(
        assignee=PIPELINE_ROLES["pm"],
        title=f"#{issue_n} some epic",
        status="done",
        summary="spec: defined",
    )

    result = disp._has_downstream_tasks(
        SLUG, issue_n,
        validator_profile=PIPELINE_ROLES["validator"],
        pm_profile=PIPELINE_ROLES["pm"],
        planner_profile="planner-daedalus",
    )
    check("_has_downstream_tasks returns False when only validator/PM/planner tasks exist",
          result is False)

    # Now add a real downstream task (developer) — must return True.
    fk.seed(
        assignee=PIPELINE_ROLES["developer"],
        title=f"#{issue_n} Developer: some epic",
        status="running",
        summary="",
    )
    result_with_dev = disp._has_downstream_tasks(
        SLUG, issue_n,
        validator_profile=PIPELINE_ROLES["validator"],
        pm_profile=PIPELINE_ROLES["pm"],
        planner_profile="planner-daedalus",
    )
    check("_has_downstream_tasks returns True when a developer task exists",
          result_with_dev is True)


# ── CI gating — developer card must not advance on red CI ────────────────────


def _setup_ci_gate_board(fk: FakeKanban, issue_n: int, pr_n: int) -> str:
    """Seed a developer blocked card and return its task id."""
    return fk.seed(
        assignee=PIPELINE_ROLES["developer"],
        title=f"#{issue_n} Developer: ci gate test",
        status="blocked",
        reason=f"review-required: PR #{pr_n} opened for {REPO}#{issue_n}",
    )


def test_developer_card_stays_blocked_when_ci_is_red():
    """Developer card with 'review-required: PR #N' stays blocked when CI is red (CI gate).

    This is the pre-green-CI invariant: run_iterate must NOT advance the card
    (complete it) when the PR's CI is failing. The card must remain 'blocked'.
    """
    from core import iterate

    fk = FakeKanban()
    saved = getattr(iterate, "kanban", None)
    iterate.kanban = fk
    try:
        issue_n = 8881
        pr_n = 8881
        dev_tid = _setup_ci_gate_board(fk, issue_n, pr_n)

        provider = FakeProvider(ci_status="red", open_prs={pr_n})

        iterate.run_iterate(SLUG, REPO, provider=provider)

        dev_card = fk.tasks[dev_tid]
        check("developer card is still blocked with red CI",
              (dev_card.get("status") or "") == "blocked")
        check("developer card was NOT completed with red CI",
              dev_tid not in [tid for (tid, _) in fk.completed])
    finally:
        iterate.kanban = saved


def test_developer_card_advances_when_ci_turns_green():
    """Developer card with 'review-required: PR #N' advances to done when CI is green (CI gate).

    This is the post-green-CI invariant: run_iterate must complete the blocked
    developer card once CI turns green, releasing any downstream QA children.
    """
    from core import iterate

    fk = FakeKanban()
    saved = getattr(iterate, "kanban", None)
    iterate.kanban = fk
    try:
        issue_n = 8882
        pr_n = 8882
        dev_tid = _setup_ci_gate_board(fk, issue_n, pr_n)

        provider = FakeProvider(ci_status="green", open_prs={pr_n})

        iterate.run_iterate(SLUG, REPO, provider=provider)

        dev_card = fk.tasks[dev_tid]
        check("developer card is done after CI turns green",
              (dev_card.get("status") or "") == "done")
        advanced_tids = [tid for (tid, _) in fk.completed]
        check("developer card was completed by iterate advance",
              dev_tid in advanced_tids)
    finally:
        iterate.kanban = saved


def test_developer_card_stays_blocked_with_pending_ci():
    """Developer card stays blocked (pending, not advanced) when CI is pending (#CI-gate).

    PENDING_CI is a soft-block: the card stays blocked and is deferred to the
    retry cron, not completed. This tests the PENDING_CI branch distinct from
    DEV_FIX_CI (red) and ADVANCE (green).
    """
    from core import iterate

    fk = FakeKanban()
    saved = getattr(iterate, "kanban", None)
    iterate.kanban = fk
    try:
        issue_n = 8883
        pr_n = 8883
        dev_tid = _setup_ci_gate_board(fk, issue_n, pr_n)

        provider = FakeProvider(ci_status="pending", open_prs={pr_n})

        counts, advance_prs, pending_ci_cards = iterate.run_iterate(
            SLUG, REPO, provider=provider
        )

        dev_card = fk.tasks[dev_tid]
        check("developer card is still blocked with pending CI",
              (dev_card.get("status") or "") == "blocked")
        check("run_iterate reported PENDING_CI count >= 1", counts.get("pending_ci", 0) >= 1)
        check("developer card was NOT completed with pending CI",
              dev_tid not in [tid for (tid, _) in fk.completed])
    finally:
        iterate.kanban = saved


if __name__ == "__main__":
    print("Daedalus E2E Regression Suite (#891 / #894)")
    print("=" * 60)
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        print(f"\n{name}")
        print("-" * len(name))
        fn()
    print()
    print("=" * 60)
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)

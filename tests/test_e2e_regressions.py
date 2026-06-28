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

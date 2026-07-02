"""Regression tests for issue #1164 — adopt an in-flight developer PR.

A hermes premature-completion bug can complete a developer kanban card with an
empty summary even though the developer session already opened a PR (observed
live: issue #1160 → duplicate PR #1163 while #1162 was in review; issue #1161
minted three developer cards and duplicate PR #1165).
``_check_completed_developer`` classified such cards as stale and blindly
minted a fresh retry developer card, which opened a duplicate PR.

The fix mirrors the #1161 PM spec-comment adoption: before minting a retry,
the dispatcher asks the provider for an open/merged PR linked to the issue and,
if one exists, rewrites the stale card's summary to ``review-required: PR #N``
so the normal reviewer/QA flow proceeds against the existing PR. The retry path
only runs when no observable work product exists.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock


def _load_dispatch():
    p = Path(__file__).resolve().parent.parent / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp_1164", str(p))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load dispatch module from {p}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _minimal_resolved():
    return {
        "platform": "github",
        "repo": "owner/repo",
        "workdir": "/tmp",
        "notifications": None,
        "execution": {},
    }


class FakeProvider:
    """Provider stub with a configurable PR-for-issue answer."""

    def __init__(self, pr_number=None, raise_on_pr_lookup=False):
        self._pr_number = pr_number
        self._raise = raise_on_pr_lookup
        self.comments: list[tuple[int, str]] = []

    def pr_number_for_issue(self, issue_number: int):
        if self._raise:
            raise RuntimeError("provider exploded")
        return self._pr_number

    def post_issue_comment(self, issue_number: int, body: str) -> bool:
        self.comments.append((issue_number, body))
        return True

    def get_issue_state(self, issue_number: int) -> str:
        return "open"


def _stale_dev_tasks(issue_number, count=1):
    """N done developer cards with empty summaries for issue_number."""
    return [
        {
            "id": f"t_dev{i}",
            "assignee": "developer-daedalus",
            "status": "done",
            "title": f"#{issue_number} Developer: feature",
            "summary": "",
        }
        for i in range(count)
    ]


class TestTryAdoptDeveloperPr(unittest.TestCase):
    """Unit tests for the _try_adopt_developer_pr helper."""

    def setUp(self):
        self.disp = _load_dispatch()

    def _adopt(self, provider, tasks=None, dry_run=False, edit_ok=True):
        with (
            mock.patch.object(
                self.disp.kanban,
                "list_tasks",
                return_value=tasks if tasks is not None else _stale_dev_tasks(42),
            ),
            mock.patch.object(
                self.disp.kanban,
                "show_card",
                return_value={"latest_summary": ""},
            ),
            mock.patch.object(
                self.disp.kanban,
                "edit_summary",
                return_value=edit_ok,
            ) as edit_summary,
        ):
            adopted = self.disp._try_adopt_developer_pr(
                "slug", 42, "developer-daedalus", provider, dry_run=dry_run
            )
        return adopted, edit_summary

    def test_open_pr_adopts_stale_card(self):
        adopted, edit_summary = self._adopt(FakeProvider(pr_number=101))
        self.assertTrue(adopted)
        edit_summary.assert_called_once()
        slug, tid, summary = edit_summary.call_args[0]
        self.assertEqual(slug, "slug")
        self.assertEqual(tid, "t_dev0")
        self.assertTrue(summary.startswith("review-required: PR #101"))

    def test_adopted_summary_is_parseable(self):
        """The rewritten summary must satisfy extract_pr_number_from_summary so
        _developer_task_state reports 'complete' on the next tick."""
        _, edit_summary = self._adopt(FakeProvider(pr_number=101))
        _, _, summary = edit_summary.call_args[0]
        self.assertEqual(self.disp.extract_pr_number_from_summary(summary), 101)

    def test_adoption_edits_newest_stale_card(self):
        adopted, edit_summary = self._adopt(
            FakeProvider(pr_number=7), tasks=_stale_dev_tasks(42, count=3)
        )
        self.assertTrue(adopted)
        _, tid, _ = edit_summary.call_args[0]
        self.assertEqual(tid, "t_dev2")

    def test_no_pr_returns_false(self):
        adopted, edit_summary = self._adopt(FakeProvider(pr_number=None))
        self.assertFalse(adopted)
        edit_summary.assert_not_called()

    def test_no_provider_returns_false(self):
        adopted, edit_summary = self._adopt(None)
        self.assertFalse(adopted)
        edit_summary.assert_not_called()

    def test_provider_exception_returns_false(self):
        """Provider errors fail open to the existing retry path — never crash."""
        adopted, edit_summary = self._adopt(FakeProvider(raise_on_pr_lookup=True))
        self.assertFalse(adopted)
        edit_summary.assert_not_called()

    def test_dry_run_adopts_without_mutation(self):
        adopted, edit_summary = self._adopt(FakeProvider(pr_number=101), dry_run=True)
        self.assertTrue(adopted)
        edit_summary.assert_not_called()

    def test_edit_summary_failure_returns_false(self):
        adopted, _ = self._adopt(FakeProvider(pr_number=101), edit_ok=False)
        self.assertFalse(adopted)

    def test_card_with_pr_in_summary_is_not_a_target(self):
        """Only cards lacking a PR reference are adoption targets."""
        tasks = [
            {
                "id": "t_ok",
                "assignee": "developer-daedalus",
                "status": "done",
                "title": "#42 Developer: feature",
                "summary": "review-required: PR #90 — fix/issue-42-x",
            },
        ]
        adopted, edit_summary = self._adopt(FakeProvider(pr_number=101), tasks=tasks)
        self.assertFalse(adopted)
        edit_summary.assert_not_called()


class _RetryHarness(unittest.TestCase):
    """Shared driver for _check_completed_developer with a stale card board."""

    def setUp(self):
        self.disp = _load_dispatch()

    def _run(self, provider, tasks, dry_run=False):
        """Drive _check_completed_developer with stateful kanban doubles.

        edit_summary mutates the fake board and _mark/_has_notified_block share
        a flag, mirroring real kanban persistence — otherwise every stale card
        of the same issue would re-trigger adoption/cap paths within one tick.
        """
        mocks = {}
        notified = []

        def _fake_edit_summary(slug, tid, summary):
            for t in tasks:
                if t["id"] == tid:
                    t["summary"] = summary
            return True

        with (
            mock.patch.object(
                self.disp.kanban,
                "list_tasks",
                side_effect=lambda slug, status=None: list(tasks),
            ),
            mock.patch.object(
                self.disp.kanban,
                "show_card",
                return_value={"latest_summary": ""},
            ),
            mock.patch.object(self.disp.kanban, "comment"),
            mock.patch.object(
                self.disp.kanban, "edit_summary", side_effect=_fake_edit_summary
            ) as edit_summary,
            mock.patch.object(
                self.disp.kanban, "create_task", return_value="t_new"
            ) as create_task,
            mock.patch.object(self.disp, "_send_retry_cap_notification") as cap_notif,
            mock.patch.object(
                self.disp, "_send_retry_attempt_notification"
            ) as attempt_notif,
            mock.patch.object(
                self.disp,
                "_has_notified_block",
                side_effect=lambda *a, **kw: bool(notified),
            ),
            mock.patch.object(
                self.disp,
                "_mark_notified_block",
                side_effect=lambda *a, **kw: notified.append(True),
            ),
        ):
            triggered = self.disp._check_completed_developer(
                "slug",
                "owner/repo",
                {42: {"number": 42, "title": "feature", "body": ""}},
                3,
                "/tmp",
                "main",
                "github",
                provider=provider,
                resolved=_minimal_resolved(),
                dry_run=dry_run,
            )
            mocks["edit_summary"] = edit_summary
            mocks["create_task"] = create_task
            mocks["cap_notif"] = cap_notif
            mocks["attempt_notif"] = attempt_notif
            mocks["triggered"] = triggered
        return mocks


class TestRetryPathAdoption(_RetryHarness):
    """_check_completed_developer must adopt an in-flight PR, not re-dispatch."""

    def test_stale_card_with_open_pr_adopts_and_skips_retry(self):
        """The #1160→#1163 scenario: empty-summary completion + open PR must
        NOT mint a second developer card (which opened the duplicate PR)."""
        m = self._run(FakeProvider(pr_number=101), _stale_dev_tasks(42))
        m["edit_summary"].assert_called_once()
        _, tid, summary = m["edit_summary"].call_args[0]
        self.assertEqual(tid, "t_dev0")
        self.assertIn("PR #101", summary)
        m["create_task"].assert_not_called()
        m["attempt_notif"].assert_not_called()
        m["cap_notif"].assert_not_called()
        self.assertEqual(m["triggered"], [])

    def test_second_tick_after_adoption_creates_nothing(self):
        """Two dispatcher ticks over an adopted completion → zero new cards.

        After adoption the card summary carries the PR number, so the next tick
        skips it as well-formed — reproduces the double-tick duplicate-PR
        window from #1160 and asserts it stays closed."""
        adopted_tasks = [
            {
                "id": "t_dev0",
                "assignee": "developer-daedalus",
                "status": "done",
                "title": "#42 Developer: feature",
                "summary": "review-required: PR #101 (adopted from provider state"
                " — developer completed with empty summary, #1164)",
            },
        ]
        m = self._run(FakeProvider(pr_number=101), adopted_tasks)
        m["create_task"].assert_not_called()
        m["edit_summary"].assert_not_called()

    def test_stale_card_with_merged_pr_adopts(self):
        m = self._run(FakeProvider(pr_number=202), _stale_dev_tasks(42))
        m["edit_summary"].assert_called_once()
        m["create_task"].assert_not_called()

    def test_stale_card_without_pr_keeps_retry_behavior(self):
        """No PR → retry path byte-identical to today (#1104 behavior)."""
        m = self._run(FakeProvider(pr_number=None), _stale_dev_tasks(42))
        m["edit_summary"].assert_not_called()
        m["create_task"].assert_called_once()
        key = m["create_task"].call_args.kwargs.get("idempotency_key")
        self.assertEqual(key, "developer-42-r1")
        m["attempt_notif"].assert_called_once()
        m["cap_notif"].assert_not_called()

    def test_stale_at_cap_with_open_pr_skips_cap_notification(self):
        """Even at/over the retry cap an in-flight PR rescues the issue —
        no cap notification, no GitHub cap comment."""
        provider = FakeProvider(pr_number=101)
        m = self._run(provider, _stale_dev_tasks(42, count=4))
        m["edit_summary"].assert_called_once()
        m["cap_notif"].assert_not_called()
        self.assertEqual([c for c in provider.comments if c[0] == 42], [])

    def test_stale_at_cap_without_pr_fires_cap_once(self):
        """No PR at cap → cap notification + GitHub comment unchanged."""
        provider = FakeProvider(pr_number=None)
        m = self._run(provider, _stale_dev_tasks(42, count=4))
        m["edit_summary"].assert_not_called()
        m["create_task"].assert_not_called()
        m["cap_notif"].assert_called_once()
        posted = [c for c in provider.comments if c[0] == 42]
        self.assertEqual(len(posted), 1)
        self.assertIn("retry cap exhausted", posted[0][1].lower())

    def test_provider_exception_falls_back_to_retry(self):
        m = self._run(FakeProvider(raise_on_pr_lookup=True), _stale_dev_tasks(42))
        m["edit_summary"].assert_not_called()
        m["create_task"].assert_called_once()

    def test_dry_run_with_open_pr_skips_retry_without_mutation(self):
        m = self._run(FakeProvider(pr_number=101), _stale_dev_tasks(42), dry_run=True)
        m["edit_summary"].assert_not_called()
        m["create_task"].assert_not_called()
        self.assertEqual(m["triggered"], [])


if __name__ == "__main__":
    unittest.main()

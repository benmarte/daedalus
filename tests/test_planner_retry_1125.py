"""Tests for the planner silent-stall retry + escalation path (#1125 F2).

A planner card that completes WITHOUT a recognised signal (PLANNING COMPLETE /
PLAN: / NOT SUITABLE) is a silent stall — context overflow, agent crash. Before
#1125 F2 the dispatcher logged a warning and dropped it, stranding the epic with
no signal to the human. This suite locks the new behaviour:

  * ``_resolve_max_planner_retries`` mirrors the other retry-cap resolvers;
  * a stall under the cap creates a fresh planner retry task;
  * an in-flight planner run suppresses a duplicate retry (idempotency);
  * a NOT SUITABLE signal is NOT treated as a stall (handled elsewhere);
  * a stall past the cap posts a GitHub exhaustion comment (once);
  * a legacy caller with no ``resolved`` context keeps the old warn-and-skip.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import (  # noqa: E402
    FakeKanban,
    FakeProvider,
    _load_dispatch,
)

disp = _load_dispatch()

PLANNER = "planner-daedalus"


# ── Resolver ───────────────────────────────────────────────────────────────


class TestResolveMaxPlannerRetries:
    def test_default_when_unset(self):
        assert disp._resolve_max_planner_retries({}) == 2

    def test_default_when_none(self):
        assert disp._resolve_max_planner_retries({"max_planner_retries": None}) == 2

    def test_custom_value_honoured(self):
        assert disp._resolve_max_planner_retries({"max_planner_retries": 5}) == 5

    def test_string_coerced(self):
        assert disp._resolve_max_planner_retries({"max_planner_retries": "4"}) == 4

    def test_non_numeric_falls_back(self):
        assert disp._resolve_max_planner_retries({"max_planner_retries": "abc"}) == 2

    def test_non_positive_falls_back(self):
        assert disp._resolve_max_planner_retries({"max_planner_retries": 0}) == 2
        assert disp._resolve_max_planner_retries({"max_planner_retries": -3}) == 2

    def test_custom_default(self):
        assert disp._resolve_max_planner_retries({}, default=7) == 7


# ── Helpers ──────────────────────────────────────────────────────────────────


def _board():
    return FakeKanban()


def _patches(fake_kb: FakeKanban):
    return [
        mock.patch.object(disp.kanban, "list_tasks", side_effect=fake_kb.list_tasks),
        mock.patch.object(
            disp.kanban, "show_card", side_effect=fake_kb.show_card
        ),
        mock.patch.object(disp.kanban, "create_task", side_effect=fake_kb.create_task),
        mock.patch.object(disp.kanban, "comment", side_effect=fake_kb.comment),
    ]


def _seed_planner(fake_kb, issue_n, summary, *, status="done"):
    return fake_kb.seed(
        assignee=PLANNER,
        title=f"#{issue_n} Epic under test",
        status=status,
        summary=summary,
        body=f"#{issue_n} epic body",
    )


def _issues_map(issue_n):
    return {issue_n: {"number": issue_n, "title": "Epic under test", "body": "b"}}


def _run(fake_kb, provider, issue_n, *, resolved, dry_run=False):
    with mock.patch.object(disp, "_planner_body", return_value="PLANNER BODY"):
        with _patches(fake_kb)[0], _patches(fake_kb)[1], _patches(fake_kb)[2], _patches(
            fake_kb
        )[3]:
            return disp._check_completed_planner(
                "slug",
                workdir="/tmp/wt",
                dry_run=dry_run,
                provider=provider,
                repo="acme/repo",
                base_branch="dev",
                issues_map=_issues_map(issue_n),
                role_skills={},
                epic_config=None,
                resolved=resolved,
            )


# ── Retry behaviour ──────────────────────────────────────────────────────────


class TestPlannerStallRetry:
    def test_stall_under_cap_creates_retry_task(self):
        fake_kb = _board()
        provider = FakeProvider(issues={500: {"number": 500, "title": "Epic"}})
        _seed_planner(fake_kb, 500, "analysis notes but no signal")

        triggered = _run(fake_kb, provider, 500, resolved={"execution": {}})

        assert triggered == [500]
        retry = fake_kb.created_with_key("planner-retry-500-r1")
        assert retry is not None, "expected a planner-retry-500-r1 card"
        assert retry["assignee"] == PLANNER
        assert "#500" in retry["title"]

    def test_empty_summary_is_a_stall(self):
        fake_kb = _board()
        provider = FakeProvider(issues={501: {"number": 501, "title": "Epic"}})
        _seed_planner(fake_kb, 501, "")

        triggered = _run(fake_kb, provider, 501, resolved={"execution": {}})

        assert triggered == [501]
        assert fake_kb.created_with_key("planner-retry-501-r1") is not None

    def test_in_flight_planner_suppresses_duplicate_retry(self):
        fake_kb = _board()
        provider = FakeProvider(issues={502: {"number": 502, "title": "Epic"}})
        # A done+stalled card AND a still-running planner card for the same issue.
        _seed_planner(fake_kb, 502, "stalled")
        _seed_planner(fake_kb, 502, "", status="running")

        triggered = _run(fake_kb, provider, 502, resolved={"execution": {}})

        assert triggered == []
        assert fake_kb.created == [], "must not spawn a retry while a run is in flight"

    def test_not_suitable_is_not_a_stall(self):
        fake_kb = _board()
        provider = FakeProvider(issues={503: {"number": 503, "title": "Epic"}})
        _seed_planner(
            fake_kb, 503, "NOT SUITABLE FOR DECOMPOSITION: implement directly"
        )

        triggered = _run(fake_kb, provider, 503, resolved={"execution": {}})

        assert triggered == []
        assert fake_kb.created == []

    def test_dry_run_does_not_create_card(self):
        fake_kb = _board()
        provider = FakeProvider(issues={504: {"number": 504, "title": "Epic"}})
        _seed_planner(fake_kb, 504, "stalled")

        triggered = _run(
            fake_kb, provider, 504, resolved={"execution": {}}, dry_run=True
        )

        assert triggered == [504]
        assert fake_kb.created == []

    def test_legacy_caller_without_resolved_keeps_old_behaviour(self):
        fake_kb = _board()
        provider = FakeProvider(issues={505: {"number": 505, "title": "Epic"}})
        _seed_planner(fake_kb, 505, "stalled")

        with mock.patch.object(disp, "_planner_body", return_value="B"):
            with _patches(fake_kb)[0], _patches(fake_kb)[1], _patches(fake_kb)[
                2
            ], _patches(fake_kb)[3]:
                triggered = disp._check_completed_planner(
                    "slug",
                    workdir="/tmp/wt",
                    dry_run=False,
                    provider=provider,
                )

        assert triggered == []
        assert fake_kb.created == []

    def test_respects_custom_cap(self):
        fake_kb = _board()
        provider = FakeProvider(issues={506: {"number": 506, "title": "Epic"}})
        # 2 done stalled cards; cap=1 → retry_count(2) > cap(1) → escalate, no card.
        _seed_planner(fake_kb, 506, "stalled")
        _seed_planner(fake_kb, 506, "stalled")

        with mock.patch.object(disp, "_send_retry_cap_notification"), mock.patch.object(
            disp, "_send_retry_attempt_notification"
        ):
            triggered = _run(
                fake_kb, provider, 506, resolved={"execution": {"max_planner_retries": 1}}
            )

        assert triggered == []
        # No new retry card — escalation instead.
        assert fake_kb.created_with_key("planner-retry-506-r2") is None


# ── Escalation on cap exhaustion ─────────────────────────────────────────────


class TestPlannerStallEscalation:
    def test_cap_exhaustion_posts_github_comment_once(self):
        fake_kb = _board()
        provider = FakeProvider(issues={600: {"number": 600, "title": "Epic"}})
        # Default cap is 2 → original + 2 retries = 3 stalled cards trips the cap.
        _seed_planner(fake_kb, 600, "stalled")
        _seed_planner(fake_kb, 600, "stalled")
        _seed_planner(fake_kb, 600, "stalled")

        with mock.patch.object(disp, "_send_retry_cap_notification") as note, (
            mock.patch.object(disp, "_send_retry_attempt_notification")
        ):
            triggered = _run(fake_kb, provider, 600, resolved={"execution": {}})
            # Second tick must be idempotent (marker stamped).
            triggered2 = _run(fake_kb, provider, 600, resolved={"execution": {}})

        assert triggered == []
        assert triggered2 == []
        # No retry task created past the cap.
        assert not any(
            c.get("assignee") == PLANNER and c.get("idempotency_key", "").startswith(
                "planner-retry-"
            )
            for c in fake_kb.created
        )
        # Exactly one exhaustion comment posted to the issue.
        cap_comments = [
            body
            for (num, body) in provider.posted_issue_comments
            if num == 600 and "retry cap exhausted" in body.lower()
        ]
        assert len(cap_comments) == 1, (
            f"expected exactly one exhaustion comment, got {len(cap_comments)}"
        )
        # Notification fired once (guarded by the persistent marker).
        assert note.call_count == 1

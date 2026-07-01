"""STOP: VCS auto-close tests (issue #115).

Verifies the dispatcher's STOP-handler in _check_confirmed_validators:

- duplicate/already_fixed/cannot_reproduce -> close_issue called, marker written
- marker already present -> no second close call
- provider failure -> warning logged, no marker
- no provider -> warning logged, no crash
- already-closed issues -> marker written, no close call
- dry_run -> triggered but no mutation
- CONFIRMED/BLOCKED/ESCALATE -> never touch close_issue
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any, Dict

import pytest

from conftest import FakeKanban, FakeProvider


# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------
def _load_dispatch() -> Any:
    repo = Path(__file__).resolve().parent.parent
    p = repo / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp_stop", str(p))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture()
def disp():
    return _load_dispatch()


# ---------------------------------------------------------------------------
# Helper: inject fake kanban at the module level, call with real positional args
# ---------------------------------------------------------------------------
SLUG = "proj"
REPO = "octocat/demo"
VALIDATOR = "validator-daedalus"


def _seed_tasks(kanban: FakeKanban, rows):
    for r in rows:
        kanban.seed(
            assignee=r["assignee"],
            status=r["status"],
            title=r["title"],
            summary=r["summary"],
        )


def _call(disp, kanban, issues_map, *, provider, dry_run=False):
    """Wrap the real _check_confirmed_validators (positional args) and return triggered."""
    return disp._check_confirmed_validators(
        SLUG,
        REPO,
        issues_map,
        3,
        "",
        "",
        "dev",
        "github",  # iterations..provider_name
        provider=provider,
        dry_run=dry_run,
    )


def _issue(number: int, title: str = "x") -> Dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": "",
        "labels": [],
        "url": "https://x",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestStopAutoClose:
    def test_stop_duplicate_closes_issue(self, disp, fake_kanban, monkeypatch):
        _seed_tasks(
            fake_kanban,
            [
                {
                    "assignee": VALIDATOR,
                    "status": "done",
                    "title": "#42 duplicate bug",
                    "summary": "STOP: duplicate of #9",
                },
            ],
        )
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        triggered = _call(disp, fake_kanban, {42: _issue(42)}, provider=provider)

        assert 42 in triggered
        assert provider.close_calls == [42]
        assert fake_kanban.created_with_key("validator-stop-closed-42") is not None

    def test_stop_already_fixed_closes_issue(self, disp, fake_kanban, monkeypatch):
        _seed_tasks(
            fake_kanban,
            [
                {
                    "assignee": VALIDATOR,
                    "status": "done",
                    "title": "#43 already fixed",
                    "summary": "STOP: already fixed in abc",
                },
            ],
        )
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        triggered = _call(disp, fake_kanban, {43: _issue(43)}, provider=provider)

        assert 43 in triggered
        assert provider.close_calls == [43]
        assert fake_kanban.created_with_key("validator-stop-closed-43") is not None

    def test_stop_cannot_reproduce_closes_issue(self, disp, fake_kanban, monkeypatch):
        _seed_tasks(
            fake_kanban,
            [
                {
                    "assignee": VALIDATOR,
                    "status": "done",
                    "title": "#44 cannot reproduce",
                    "summary": "STOP: cannot reproduce",
                },
            ],
        )
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        triggered = _call(disp, fake_kanban, {44: _issue(44)}, provider=provider)

        assert 44 in triggered
        assert provider.close_calls == [44]
        assert fake_kanban.created_with_key("validator-stop-closed-44") is not None

    def test_stop_idempotent_no_double_close(self, disp, fake_kanban, monkeypatch):
        _seed_tasks(
            fake_kanban,
            [
                {
                    "assignee": VALIDATOR,
                    "status": "done",
                    "title": "#45 duplicate",
                    "summary": "STOP: duplicate of #9",
                },
            ],
        )
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        t1 = _call(disp, fake_kanban, {45: _issue(45)}, provider=provider)
        assert provider.close_calls == [45]
        assert fake_kanban.created_with_key("validator-stop-closed-45") is not None

        # Second tick: marker present → nothing mutates
        t2 = _call(disp, fake_kanban, {45: _issue(45)}, provider=provider)
        assert provider.close_calls == [45]

    def test_stop_already_closed_no_api_call(self, disp, fake_kanban, monkeypatch):
        # Issue #1115: the closed-issue filter now fires before the STOP handler,
        # so a STOP card for an already-closed issue is skipped entirely.
        # The idempotency marker is unnecessary because future ticks will also
        # filter on issue state rather than relying on the marker.
        _seed_tasks(
            fake_kanban,
            [
                {
                    "assignee": VALIDATOR,
                    "status": "done",
                    "title": "#46 already closed",
                    "summary": "STOP: duplicate of #9",
                },
            ],
        )
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider(closed_issues={46})

        triggered = _call(disp, fake_kanban, {46: _issue(46)}, provider=provider)

        # Card is skipped entirely by the closed-issue filter — no side effects.
        assert triggered == []
        assert provider.close_calls == []  # still not called — correct
        # Marker no longer written; state-based filter is authoritative.
        assert fake_kanban.created_with_key("validator-stop-closed-46") is None

    def test_stop_provider_failure_logs_warning(
        self,
        disp,
        fake_kanban,
        monkeypatch,
        caplog,
    ):
        _seed_tasks(
            fake_kanban,
            [
                {
                    "assignee": VALIDATOR,
                    "status": "done",
                    "title": "#47 flaky",
                    "summary": "STOP: duplicate of #9",
                },
            ],
        )
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider(close_issue_fail_for={47})

        with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
            triggered = _call(disp, fake_kanban, {47: _issue(47)}, provider=provider)

        assert 47 in triggered
        assert provider.close_calls == [47]
        # Failure path must NOT write the marker (next tick retries)
        assert fake_kanban.created_with_key("validator-stop-closed-47") is None
        assert any(
            "failed" in r.getMessage().lower() and "#47" in r.getMessage()
            for r in caplog.records
        )

    def test_stop_without_provider_warns(
        self,
        disp,
        fake_kanban,
        monkeypatch,
        caplog,
    ):
        _seed_tasks(
            fake_kanban,
            [
                {
                    "assignee": VALIDATOR,
                    "status": "done",
                    "title": "#48 no provider",
                    "summary": "STOP: duplicate of #9",
                },
            ],
        )
        monkeypatch.setattr(disp, "kanban", fake_kanban)

        with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
            triggered = _call(
                disp,
                fake_kanban,
                {48: _issue(48)},
                provider=None,
            )

        assert 48 not in triggered
        assert fake_kanban.created_with_key("validator-stop-closed-48") is None
        assert any(
            "no provider" in r.getMessage().lower() and "#48" in r.getMessage()
            for r in caplog.records
        )

    def test_stop_dry_run_no_mutation(self, disp, fake_kanban, monkeypatch):
        _seed_tasks(
            fake_kanban,
            [
                {
                    "assignee": VALIDATOR,
                    "status": "done",
                    "title": "#49 dry run",
                    "summary": "STOP: duplicate of #9",
                },
            ],
        )
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        triggered = _call(
            disp,
            fake_kanban,
            {49: _issue(49)},
            provider=provider,
            dry_run=True,
        )

        assert 49 in triggered
        assert provider.close_calls == []
        assert fake_kanban.created_with_key("validator-stop-closed-49") is None


class TestNonStopDoesNotCloseIssue:
    def test_confirmed_does_not_close_issue(self, disp, fake_kanban, monkeypatch):
        _seed_tasks(
            fake_kanban,
            [
                {
                    "assignee": VALIDATOR,
                    "status": "done",
                    "title": "#50 valid bug",
                    "summary": "CONFIRMED: reproduced",
                },
            ],
        )
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        triggered = _call(disp, fake_kanban, {50: _issue(50)}, provider=provider)

        assert 50 in triggered
        assert provider.close_calls == []
        assert fake_kanban.created_with_key("validator-stop-closed-50") is None
        # CONFIRMED path still creates a PM task
        assert fake_kanban.created_with_key("pm-50") is not None

    def test_blocked_creates_pm_consultation_no_close(
        self,
        disp,
        fake_kanban,
        monkeypatch,
    ):
        _seed_tasks(
            fake_kanban,
            [
                {
                    "assignee": VALIDATOR,
                    "status": "done",
                    "title": "#51 blocked",
                    "summary": "BLOCKED: needs input",
                },
            ],
        )
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        triggered = _call(disp, fake_kanban, {51: _issue(51)}, provider=provider)

        assert 51 in triggered
        assert provider.close_calls == []
        assert fake_kanban.created_with_key("validator-stop-closed-51") is None
        # BLOCKED path creates a PM consultation card
        assert fake_kanban.created_with_key("validator-blocked-51") is not None

    def test_escalate_does_not_close_issue(self, disp, fake_kanban, monkeypatch):
        _seed_tasks(
            fake_kanban,
            [
                {
                    "assignee": VALIDATOR,
                    "status": "done",
                    "title": "#52 security",
                    "summary": "ESCALATE: security threat",
                },
            ],
        )
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        triggered = _call(disp, fake_kanban, {52: _issue(52)}, provider=provider)

        # ESCALATE is a human-escalation path, skipped intentionally
        assert 52 not in triggered
        assert provider.close_calls == []
        assert fake_kanban.created_with_key("validator-stop-closed-52") is None


class TestStopAutoCloseComment:
    """Verify that STOP: validator posts an explanatory comment when auto-closing."""

    def test_stop_post_issue_comment_success(self, disp, fake_kanban, monkeypatch):
        _seed_tasks(
            fake_kanban,
            [
                {
                    "assignee": VALIDATOR,
                    "status": "done",
                    "title": "#72 already fixed",
                    "summary": "STOP: already fixed in abc123",
                },
            ],
        )
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        triggered = _call(disp, fake_kanban, {72: _issue(72)}, provider=provider)

        assert 72 in triggered
        comments = provider.posted_issue_comments
        assert len(comments) == 1, f"Expected 1 comment, got {len(comments)}"
        assert comments[0][0] == 72
        body = comments[0][1]
        assert "Auto-closed by STOP:" in body, (
            f"Comment missing auto-close marker: {body[:80]}"
        )
        assert "already fixed" in body.lower(), f"Comment missing reason: {body[:80]}"

    def test_stop_reason_has_no_leading_colon(self, disp, fake_kanban, monkeypatch):
        """Regression: summary_raw slice must match the 'STOP:' prefix length (5).

        Previously the slice was ``[4:]`` which left a stray ':' in the stop
        reason and produced malformed comment text like
        ``STOP: validator — : duplicate of #9``.
        """
        _seed_tasks(
            fake_kanban,
            [
                {
                    "assignee": VALIDATOR,
                    "status": "done",
                    "title": "#76 duplicate",
                    "summary": "STOP: duplicate of #9",
                },
            ],
        )
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        _call(disp, fake_kanban, {76: _issue(76)}, provider=provider)

        comments = provider.posted_issue_comments
        assert len(comments) == 1
        body = comments[0][1]
        # The reason text must appear without a leading colon.
        marker = "Auto-closed by STOP: validator — "
        assert marker in body, f"Comment missing STOP marker: {body[:120]}"
        reason = body.split(marker, 1)[1].splitlines()[0].strip()
        assert not reason.startswith(":"), f"Leading colon in reason: {reason!r}"
        assert reason.lower().startswith("duplicate of #9"), (
            f"Unexpected reason: {reason!r}"
        )

    def test_stop_post_issue_comment_failure(
        self, disp, fake_kanban, monkeypatch, caplog
    ):
        """If post_issue_comment returns False, close_issue still succeeds and
        the marker task is still created (no crash)."""
        _seed_tasks(
            fake_kanban,
            [
                {
                    "assignee": VALIDATOR,
                    "status": "done",
                    "title": "#73 duplicate",
                    "summary": "STOP: duplicate of #71",
                },
            ],
        )
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider(post_issue_comment_fail_for={73})

        with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
            triggered = _call(disp, fake_kanban, {73: _issue(73)}, provider=provider)

        assert 73 in triggered
        assert 73 in provider.close_calls
        # Comment attempt was made (and False returned), so still in posted list
        assert len(provider.posted_issue_comments) == 1
        # Warning should be logged about the comment failure
        assert any(
            r.levelname == "WARNING"
            and "comment" in r.getMessage().lower()
            and "73" in r.getMessage()
            for r in caplog.records
        ), "Should log warning when post_issue_comment fails"
        # Marker task still created despite comment failure
        assert fake_kanban.created_with_key("validator-stop-closed-73") is not None

    def test_stop_no_comment_when_already_closed(self, disp, fake_kanban, monkeypatch):
        """Issue #1115: the closed-issue filter fires before the STOP handler,
        so an already-closed issue's STOP card is skipped entirely — no close
        call, no comment, no marker, not in triggered."""
        _seed_tasks(
            fake_kanban,
            [
                {
                    "assignee": VALIDATOR,
                    "status": "done",
                    "title": "#74 already closed",
                    "summary": "STOP: cannot reproduce",
                },
            ],
        )
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider(closed_issues={74})

        triggered = _call(disp, fake_kanban, {74: _issue(74)}, provider=provider)

        # Closed-issue filter skips the card entirely.
        assert 74 not in provider.close_calls
        assert len(provider.posted_issue_comments) == 0
        assert triggered == []
        assert fake_kanban.created_with_key("validator-stop-closed-74") is None

    def test_stop_dry_run_no_close_or_comment(self, disp, fake_kanban, monkeypatch):
        """Dry-run mode should not close issue or post comments."""
        _seed_tasks(
            fake_kanban,
            [
                {
                    "assignee": VALIDATOR,
                    "status": "done",
                    "title": "#75 duplicate",
                    "summary": "STOP: duplicate of #70",
                },
            ],
        )
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        triggered = _call(
            disp, fake_kanban, {75: _issue(75)}, provider=provider, dry_run=True
        )

        assert 75 in triggered
        assert 75 not in provider.close_calls
        assert len(provider.posted_issue_comments) == 0
        assert fake_kanban.created_with_key("validator-stop-closed-75") is None

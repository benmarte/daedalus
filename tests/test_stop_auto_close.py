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
        SLUG, REPO, issues_map,
        3, "", "", "dev", "github",  # iterations..provider_name
        provider=provider,
        dry_run=dry_run,
    )


def _issue(number: int, title: str = "x") -> Dict[str, Any]:
    return {
        "number": number, "title": title, "body": "",
        "labels": [], "url": "https://x",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestStopAutoClose:
    def test_stop_duplicate_closes_issue(self, disp, fake_kanban, monkeypatch):
        _seed_tasks(fake_kanban, [
            {"assignee": VALIDATOR, "status": "done",
             "title": "#42 duplicate bug", "summary": "STOP: duplicate of #9"},
        ])
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        triggered = _call(disp, fake_kanban, {42: _issue(42)}, provider=provider)

        assert 42 in triggered
        assert provider.close_calls == [42]
        assert fake_kanban.created_with_key("validator-stop-closed-42") is not None

    def test_stop_already_fixed_closes_issue(self, disp, fake_kanban, monkeypatch):
        _seed_tasks(fake_kanban, [
            {"assignee": VALIDATOR, "status": "done",
             "title": "#43 already fixed", "summary": "STOP: already fixed in abc"},
        ])
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        triggered = _call(disp, fake_kanban, {43: _issue(43)}, provider=provider)

        assert 43 in triggered
        assert provider.close_calls == [43]
        assert fake_kanban.created_with_key("validator-stop-closed-43") is not None

    def test_stop_cannot_reproduce_closes_issue(self, disp, fake_kanban, monkeypatch):
        _seed_tasks(fake_kanban, [
            {"assignee": VALIDATOR, "status": "done",
             "title": "#44 cannot reproduce", "summary": "STOP: cannot reproduce"},
        ])
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        triggered = _call(disp, fake_kanban, {44: _issue(44)}, provider=provider)

        assert 44 in triggered
        assert provider.close_calls == [44]
        assert fake_kanban.created_with_key("validator-stop-closed-44") is not None

    def test_stop_idempotent_no_double_close(self, disp, fake_kanban, monkeypatch):
        _seed_tasks(fake_kanban, [
            {"assignee": VALIDATOR, "status": "done",
             "title": "#45 duplicate", "summary": "STOP: duplicate of #9"},
        ])
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        t1 = _call(disp, fake_kanban, {45: _issue(45)}, provider=provider)
        assert provider.close_calls == [45]
        assert fake_kanban.created_with_key("validator-stop-closed-45") is not None

        # Second tick: marker present → nothing mutates
        t2 = _call(disp, fake_kanban, {45: _issue(45)}, provider=provider)
        assert provider.close_calls == [45]

    def test_stop_already_closed_no_api_call(self, disp, fake_kanban, monkeypatch):
        _seed_tasks(fake_kanban, [
            {"assignee": VALIDATOR, "status": "done",
             "title": "#46 already closed", "summary": "STOP: duplicate of #9"},
        ])
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider(closed_issues={46})

        triggered = _call(disp, fake_kanban, {46: _issue(46)}, provider=provider)

        assert 46 in triggered
        assert provider.close_calls == []  # short-circuited by state guard
        assert fake_kanban.created_with_key("validator-stop-closed-46") is not None

    def test_stop_provider_failure_logs_warning(
        self, disp, fake_kanban, monkeypatch, caplog,
    ):
        _seed_tasks(fake_kanban, [
            {"assignee": VALIDATOR, "status": "done",
             "title": "#47 flaky", "summary": "STOP: duplicate of #9"},
        ])
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider(close_issue_fail_for={47})

        with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
            triggered = _call(disp, fake_kanban, {47: _issue(47)}, provider=provider)

        assert 47 in triggered
        assert provider.close_calls == [47]
        # Failure path must NOT write the marker (next tick retries)
        assert fake_kanban.created_with_key("validator-stop-closed-47") is None
        assert any("failed" in r.getMessage().lower() and "#47" in r.getMessage()
                   for r in caplog.records)

    def test_stop_without_provider_warns(
        self, disp, fake_kanban, monkeypatch, caplog,
    ):
        _seed_tasks(fake_kanban, [
            {"assignee": VALIDATOR, "status": "done",
             "title": "#48 no provider", "summary": "STOP: duplicate of #9"},
        ])
        monkeypatch.setattr(disp, "kanban", fake_kanban)

        with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
            triggered = _call(
                disp, fake_kanban, {48: _issue(48)}, provider=None,
            )

        assert 48 not in triggered
        assert fake_kanban.created_with_key("validator-stop-closed-48") is None
        assert any("no provider" in r.getMessage().lower() and "#48" in r.getMessage()
                   for r in caplog.records)

    def test_stop_dry_run_no_mutation(self, disp, fake_kanban, monkeypatch):
        _seed_tasks(fake_kanban, [
            {"assignee": VALIDATOR, "status": "done",
             "title": "#49 dry run", "summary": "STOP: duplicate of #9"},
        ])
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        triggered = _call(
            disp, fake_kanban, {49: _issue(49)}, provider=provider, dry_run=True,
        )

        assert 49 in triggered
        assert provider.close_calls == []
        assert fake_kanban.created_with_key("validator-stop-closed-49") is None


class TestNonStopDoesNotCloseIssue:
    def test_confirmed_does_not_close_issue(self, disp, fake_kanban, monkeypatch):
        _seed_tasks(fake_kanban, [
            {"assignee": VALIDATOR, "status": "done",
             "title": "#50 valid bug", "summary": "CONFIRMED: reproduced"},
        ])
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        triggered = _call(disp, fake_kanban, {50: _issue(50)}, provider=provider)

        assert 50 in triggered
        assert provider.close_calls == []
        assert fake_kanban.created_with_key("validator-stop-closed-50") is None
        # CONFIRMED path still creates a PM task
        assert fake_kanban.created_with_key("pm-50") is not None

    def test_blocked_creates_pm_consultation_no_close(
        self, disp, fake_kanban, monkeypatch,
    ):
        _seed_tasks(fake_kanban, [
            {"assignee": VALIDATOR, "status": "done",
             "title": "#51 blocked", "summary": "BLOCKED: needs input"},
        ])
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        triggered = _call(disp, fake_kanban, {51: _issue(51)}, provider=provider)

        assert 51 in triggered
        assert provider.close_calls == []
        assert fake_kanban.created_with_key("validator-stop-closed-51") is None
        # BLOCKED path creates a PM consultation card
        assert fake_kanban.created_with_key("validator-blocked-51") is not None

    def test_escalate_does_not_close_issue(self, disp, fake_kanban, monkeypatch):
        _seed_tasks(fake_kanban, [
            {"assignee": VALIDATOR, "status": "done",
             "title": "#52 security", "summary": "ESCALATE: security threat"},
        ])
        monkeypatch.setattr(disp, "kanban", fake_kanban)
        provider = FakeProvider()

        triggered = _call(disp, fake_kanban, {52: _issue(52)}, provider=provider)

        # ESCALATE is a human-escalation path, skipped intentionally
        assert 52 not in triggered
        assert provider.close_calls == []
        assert fake_kanban.created_with_key("validator-stop-closed-52") is None

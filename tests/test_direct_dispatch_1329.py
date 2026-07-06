"""Tests for core.dispatch.direct_dispatch (#1329 structural delegation)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.dispatch import direct_dispatch as dd  # noqa: E402

_DELEG_BODY = (
    "Validate issue benmarte/daedalus#42\n"
    + dd._DELEGATION_MARKER
    + " CLAUDE CODE:\n  ... spawn delegate.sh ...\n"
)

_EXEC_ON = {
    "direct_delegate": True,
    "coding_agent": "claude-code",
    "coding_agent_cmd": "claude --dangerously-skip-permissions -p",
}


def _wire(monkeypatch, tasks, cards, claimed):
    monkeypatch.setattr(dd.kanban, "list_tasks", lambda slug, status="": tasks)
    monkeypatch.setattr(dd.kanban, "show_card", lambda slug, cid: cards.get(cid))
    monkeypatch.setattr(dd.kanban, "claim", lambda slug, cid, **k: claimed.append(cid) or True)


def test_flag_off_is_noop(monkeypatch):
    calls = []
    _wire(monkeypatch, [{"id": "t1", "assignee": "validator-daedalus"}], {}, calls)
    n = dd.direct_dispatch("b", {"execution": {}}, spawn=lambda **k: None)
    assert n == 0 and calls == []  # nothing claimed, byte-identical fallback


def test_local_agent_is_noop(monkeypatch):
    calls = []
    _wire(monkeypatch, [{"id": "t1", "assignee": "validator-daedalus"}], {}, calls)
    n = dd.direct_dispatch("b", {"execution": {"direct_delegate": True, "coding_agent": "hermes"}},
                           spawn=lambda **k: None)
    assert n == 0 and calls == []


def test_validator_card_is_direct_spawned(monkeypatch):
    claimed, spawned = [], []
    tasks = [{"id": "t1", "assignee": "validator-daedalus", "title": "#42 x"}]
    cards = {"t1": {"id": "t1", "title": "#42 x", "body": _DELEG_BODY}}
    _wire(monkeypatch, tasks, cards, claimed)
    n = dd.direct_dispatch("b", {"execution": _EXEC_ON}, max_spawns=5,
                           spawn=lambda **k: spawned.append(k))
    assert n == 1
    assert claimed == ["t1"]  # claimed before spawn
    s = spawned[0]
    assert s["role"] == "validator" and s["card"] == "t1" and s["board"] == "b"
    assert s["role"] in dd._DIRECT_ROLES
    assert "--relay-verdict" not in str(s)  # spawn callback gets structured kwargs, not raw argv


def test_developer_card_is_direct_spawned_with_branch(monkeypatch):
    """#1339: the developer is now delegated too (uniform coding-agent delegation). It's
    spawned with a deterministic branch (fix/issue-N) and base so delegate.sh can build
    the isolated worktree and detect the PR."""
    claimed, spawned = [], []
    tasks = [{"id": "t1", "assignee": "developer-daedalus", "title": "#42 dev"}]
    cards = {"t1": {"id": "t1", "title": "#42 dev", "body": _DELEG_BODY}}
    _wire(monkeypatch, tasks, cards, claimed)
    resolved = {"execution": _EXEC_ON, "workdir": "/repos/x", "vcs": {"target_branch": "main"}}
    n = dd.direct_dispatch("b", resolved, spawn=lambda **k: spawned.append(k))
    assert n == 1 and claimed == ["t1"]
    s = spawned[0]
    assert s["role"] == "developer" and s["branch"] == "fix/issue-42" and s["base"] == "main"


def test_non_delegation_body_is_skipped(monkeypatch):
    claimed, spawned = [], []
    tasks = [{"id": "t1", "assignee": "validator-daedalus", "title": "#42 x"}]
    cards = {"t1": {"id": "t1", "title": "#42 x", "body": "plain body, no delegation block"}}
    _wire(monkeypatch, tasks, cards, claimed)
    n = dd.direct_dispatch("b", {"execution": _EXEC_ON}, spawn=lambda **k: spawned.append(k))
    assert n == 0 and claimed == [] and spawned == []


def test_max_spawns_caps(monkeypatch):
    claimed, spawned = [], []
    tasks = [
        {"id": f"t{i}", "assignee": "reviewer-daedalus", "title": f"#4{i} r"} for i in range(4)
    ]
    cards = {t["id"]: {"id": t["id"], "title": t["title"], "body": _DELEG_BODY} for t in tasks}
    _wire(monkeypatch, tasks, cards, claimed)
    n = dd.direct_dispatch("b", {"execution": _EXEC_ON}, max_spawns=2,
                           spawn=lambda **k: spawned.append(k))
    assert n == 2 and len(claimed) == 2  # capped


def test_resets_tick_cache_before_reading(monkeypatch):
    """direct_dispatch must reset the per-tick list_tasks cache before reading, so a
    card created earlier in the same tick is visible (else the fresh-subprocess
    `hermes kanban dispatch` grabs it first and spawns a qwen agent)."""
    order = []
    tasks = [{"id": "t1", "assignee": "validator-daedalus", "title": "#42 x"}]
    cards = {"t1": {"task": {"id": "t1", "title": "#42 x", "body": _DELEG_BODY}}}
    monkeypatch.setattr(dd.kanban, "reset_tick_cache", lambda: order.append("reset"))
    monkeypatch.setattr(dd.kanban, "list_tasks",
                        lambda slug, status="": (order.append("list"), tasks if status == "ready" else [])[1])
    monkeypatch.setattr(dd.kanban, "show_card", lambda slug, cid: cards.get(cid))
    monkeypatch.setattr(dd.kanban, "claim", lambda slug, cid, **k: True)
    dd.direct_dispatch("b", {"execution": _EXEC_ON}, max_spawns=5, spawn=lambda **k: None)
    assert order and order[0] == "reset", f"reset_tick_cache must precede list_tasks: {order}"


def test_nested_show_card_body_is_read(monkeypatch):
    """Regression: kanban.show_card nests the card fields under a `task` key
    ({"task": {...body...}, "children":...}), NOT at the top level. direct_dispatch
    must read task.body or it sees an empty body and skips every card."""
    claimed, spawned = [], []
    tasks = [{"id": "t1", "assignee": "validator-daedalus", "title": "#42 x"}]
    # show_card returns the REAL nested shape
    nested = {"t1": {"task": {"id": "t1", "title": "#42 x", "body": _DELEG_BODY},
                     "children": [], "events": []}}
    monkeypatch.setattr(dd.kanban, "list_tasks", lambda slug, status="": tasks if status == "ready" else [])
    monkeypatch.setattr(dd.kanban, "show_card", lambda slug, cid: nested.get(cid))
    monkeypatch.setattr(dd.kanban, "claim", lambda slug, cid, **k: claimed.append(cid) or True)
    n = dd.direct_dispatch("b", {"execution": _EXEC_ON}, max_spawns=5,
                           spawn=lambda **k: spawned.append(k))
    assert n == 1 and claimed == ["t1"] and spawned[0]["role"] == "validator"


def test_ready_status_card_is_found(monkeypatch):
    """#1333 regression: a freshly-created daedalus card sits in `ready` (not `todo`)
    with dispatch_in_gateway=false. direct_dispatch must scan `ready` or it no-ops
    and the qwen path wins."""
    claimed, spawned = [], []
    card = {"id": "t1", "assignee": "validator-daedalus", "title": "#42 x"}
    cards = {"t1": {"id": "t1", "title": "#42 x", "body": _DELEG_BODY}}
    # list_tasks returns the card ONLY for status='ready' (empty for 'todo')
    monkeypatch.setattr(dd.kanban, "list_tasks",
                        lambda slug, status="": [card] if status == "ready" else [])
    monkeypatch.setattr(dd.kanban, "show_card", lambda slug, cid: cards.get(cid))
    monkeypatch.setattr(dd.kanban, "claim", lambda slug, cid, **k: claimed.append(cid) or True)
    n = dd.direct_dispatch("b", {"execution": _EXEC_ON}, max_spawns=5,
                           spawn=lambda **k: spawned.append(k))
    assert n == 1 and claimed == ["t1"] and spawned[0]["role"] == "validator"


def test_claim_failure_skips_spawn(monkeypatch):
    spawned = []
    tasks = [{"id": "t1", "assignee": "qa-daedalus", "title": "#42 q"}]
    cards = {"t1": {"id": "t1", "title": "#42 q", "body": _DELEG_BODY}}
    monkeypatch.setattr(dd.kanban, "list_tasks", lambda slug, status="": tasks)
    monkeypatch.setattr(dd.kanban, "show_card", lambda slug, cid: cards.get(cid))
    monkeypatch.setattr(dd.kanban, "claim", lambda slug, cid, **k: False)  # already running
    n = dd.direct_dispatch("b", {"execution": _EXEC_ON}, spawn=lambda **k: spawned.append(k))
    assert n == 0 and spawned == []  # no double-spawn when claim fails


def test_developer_gets_dev_override_not_relay_override(monkeypatch):
    """The developer's inner task must carry the DEV override (open a PR), not the review
    relay override (emit a verdict) — but still be forbidden from touching kanban."""
    claimed, spawned = [], []
    tasks = [{"id": "t1", "assignee": "developer-daedalus", "title": "#42 dev"}]
    cards = {"t1": {"id": "t1", "title": "#42 dev", "body": _DELEG_BODY}}
    _wire(monkeypatch, tasks, cards, claimed)
    dd.direct_dispatch("b", {"execution": _EXEC_ON, "workdir": "/r"},
                       spawn=lambda **k: spawned.append(k))
    written = Path(spawned[0]["taskf"]).read_text(encoding="utf-8")
    assert dd._DEV_MODE_OVERRIDE in written
    assert dd._RELAY_MODE_OVERRIDE not in written
    assert "open the pr" in written.lower() and "hermes kanban complete" in written


def test_repo_workdir_threaded_to_spawn_for_advance(monkeypatch):
    """#1339 near-real-time advance: direct_dispatch must pass the repo path (workdir) to
    the spawn so delegate.sh can fire the scoped advance dispatch (the delegated `claude -p`
    is not a Hermes session and never fires the on_session_end hook)."""
    claimed, spawned = [], []
    tasks = [{"id": "t1", "assignee": "validator-daedalus", "title": "#42 x"}]
    cards = {"t1": {"id": "t1", "title": "#42 x", "body": _DELEG_BODY}}
    _wire(monkeypatch, tasks, cards, claimed)
    resolved = {"execution": _EXEC_ON, "workdir": "/repos/dogfood"}
    dd.direct_dispatch("b", resolved, max_spawns=5, spawn=lambda **k: spawned.append(k))
    assert spawned and spawned[0]["repo"] == "/repos/dogfood"


def test_gated_card_with_incomplete_parent_is_skipped(monkeypatch):
    """#1339 gating: a QA card (parents=[developer]) must NOT be dispatched while its
    developer parent is still running — otherwise the review roles fire before the PR
    exists. direct_dispatch must honor parent-completion like the gateway path does."""
    claimed, spawned = [], []
    qa_card = {"id": "t_qa", "assignee": "qa-daedalus", "title": "#42 QA", "status": "todo"}
    dev_card = {"id": "t_dev", "assignee": "developer-daedalus", "title": "#42 Dev", "status": "running"}
    # list_tasks(status=...) returns the QA card for todo; the no-arg full scan returns both
    def _list(slug, status=""):
        if status == "todo":
            return [qa_card]
        if status == "":
            return [qa_card, dev_card]
        return []
    monkeypatch.setattr(dd.kanban, "list_tasks", _list)
    monkeypatch.setattr(dd.kanban, "show_card",
                        lambda slug, cid: {"task": {"id": "t_qa", "title": "#42 QA", "body": _DELEG_BODY},
                                           "parents": ["t_dev"]})
    monkeypatch.setattr(dd.kanban, "claim", lambda slug, cid, **k: claimed.append(cid) or True)
    n = dd.direct_dispatch("b", {"execution": _EXEC_ON}, max_spawns=5,
                           spawn=lambda **k: spawned.append(k))
    assert n == 0 and spawned == [] and claimed == []  # gated: developer parent not done


def test_gated_card_dispatches_once_parent_done(monkeypatch):
    """Once the developer parent is done, the QA card becomes dispatchable."""
    claimed, spawned = [], []
    qa_card = {"id": "t_qa", "assignee": "qa-daedalus", "title": "#42 QA", "status": "ready"}
    dev_card = {"id": "t_dev", "assignee": "developer-daedalus", "title": "#42 Dev", "status": "done"}
    def _list(slug, status=""):
        if status == "ready":
            return [qa_card]
        if status == "":
            return [qa_card, dev_card]
        return []
    monkeypatch.setattr(dd.kanban, "list_tasks", _list)
    monkeypatch.setattr(dd.kanban, "show_card",
                        lambda slug, cid: {"task": {"id": "t_qa", "title": "#42 QA", "body": _DELEG_BODY},
                                           "parents": ["t_dev"]})
    monkeypatch.setattr(dd.kanban, "claim", lambda slug, cid, **k: claimed.append(cid) or True)
    n = dd.direct_dispatch("b", {"execution": _EXEC_ON}, max_spawns=5,
                           spawn=lambda **k: spawned.append(k))
    assert n == 1 and spawned[0]["role"] == "qa"  # parent done → dispatched


def test_inner_task_file_carries_relay_override(monkeypatch):
    """#1329 race fix: the shared role bodies tell the agent to complete/block its OWN
    card. Under relay mode delegate.sh owns the transition, so the inner task written to
    disk MUST carry the relay-mode override that forbids the agent from running any
    kanban state command — otherwise the agent's bare `complete` (no --result) races and
    wins, the card completes empty, and the dispatcher re-creates it (duplicate loop)."""
    claimed, spawned = [], []
    tasks = [{"id": "t1", "assignee": "project-manager-daedalus", "title": "#42 x"}]
    cards = {"t1": {"id": "t1", "title": "#42 x", "body": _DELEG_BODY}}
    _wire(monkeypatch, tasks, cards, claimed)
    n = dd.direct_dispatch("b", {"execution": _EXEC_ON}, max_spawns=5,
                           spawn=lambda **k: spawned.append(k))
    assert n == 1 and spawned[0]["role"] == "pm"
    written = Path(spawned[0]["taskf"]).read_text(encoding="utf-8")
    # The override is appended verbatim and forbids kanban state writes.
    assert dd._RELAY_MODE_OVERRIDE in written
    assert "Do NOT run `hermes kanban complete`" in written
    assert written.endswith(dd._RELAY_MODE_OVERRIDE)  # appended last, supersedes body steps

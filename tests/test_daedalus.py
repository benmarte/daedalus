#!/usr/bin/env python3
"""Focused unit tests for the live daedalus surface:
config loading/merging, kanban parsing, dispatcher provider integration,
and doc-report delivery.

Run: python3 tests/test_daedalus.py
"""

import sys
from pathlib import Path
from unittest import mock

# Make the package root importable (config/, core/) and the tests dir (conftest).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import FakeProvider, _load_dispatch, check  # noqa: E402,F401
from config import ConfigLoader, deep_merge  # noqa: E402
from core import kanban  # noqa: E402

# Save original kanban function references before any test can monkey-patch them.
# Used by test_ensure_board_* to restore the real functions when prior tests
# have replaced them with lambdas (cross-test contamination guard).
_ORIG_KANBAN = {
    "ensure_board": kanban.ensure_board,
    "_hk": kanban._hk,
    "list_blocked": getattr(kanban, "list_blocked", None),
    "list_issue_numbers": kanban.list_issue_numbers,
    "decompose_all_triage": getattr(kanban, "decompose_all_triage", None),
    "create_triage": kanban.create_triage,
    "decompose": kanban.decompose,
    "dispatch": kanban.dispatch,
    "create_task": kanban.create_task,
    "list_tasks": kanban.list_tasks,
}


class _FakeProvider(FakeProvider):
    """Dispatcher-test double: extends the canonical conftest ``FakeProvider``
    with the board/PR-state stubs the dispatcher ``run()`` path exercises.

    ``name``, ``board_configured`` and ``list_issues`` are inherited from the
    base (identical defaults), so only dispatcher-specific surface lives here.
    """

    def status_name(self, key):
        return {
            "ready": "Ready",
            "in_progress": "In progress",
            "in_review": "In review",
            "done": "Done",
        }.get(key, key)

    def board_numbers_with_statuses(self, names):
        return set()

    def board_set_status(self, n, status):
        return True

    def close_issue(self, n):
        return True

    def pr_state_for_issue(self, n):
        return None

    def pr_number_for_issue(self, n):
        return None

    def _pr_for_issue(self, n):
        from core.providers.base import PRSummary

        state = self.pr_state_for_issue(n)
        if state is None:
            return None
        number = self.pr_number_for_issue(n)
        return PRSummary(number=number or 0, state=state)

    def update_pr_body(self, pr, body):
        return False

    def list_prs(self, state="open", limit=50):
        return []

    def pr_ci_green(self, pr):
        return False

    def list_pr_comments(self, pr):
        return []

    def get_issue_comments(self, issue_number):
        return []

    def pr_has_delivery_marker(self, pr):
        return False

    def post_delivery_marker(self, pr, body=""):
        return True

    # URL builders (new notify_templates integration)
    display_repo = "owner/repo"

    def issue_url(self, n):
        return f"https://github.com/owner/repo/issues/{n}"

    def pr_url(self, n):
        return f"https://github.com/owner/repo/pull/{n}"


gp = _FakeProvider()  # patched per-test via mock.patch.object


class _Comment:
    def __init__(self, body):
        self.body = body


# ── config: deep_merge ───────────────────────────────────────────────────────
def test_deep_merge():
    check(
        "deep_merge replaces lists wholesale",
        deep_merge({"a": [1, 2]}, {"a": [3]})["a"] == [3],
    )
    check(
        "deep_merge merges nested dicts",
        deep_merge({"a": {"x": 1}}, {"a": {"y": 2}})["a"] == {"x": 1, "y": 2},
    )
    check(
        "deep_merge preserves base keys not overridden",
        deep_merge({"a": 1, "b": 2}, {"b": 3}) == {"a": 1, "b": 3},
    )
    base = {"a": {"x": 1}}
    deep_merge(base, {"a": {"y": 2}})
    check("deep_merge does not mutate the base", base == {"a": {"x": 1}})


def test_config_loader_resolve():
    """resolve_repo_config merges the per-repo file over template defaults."""
    import tempfile
    import yaml

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        (repo / ".hermes").mkdir()
        (repo / ".hermes" / "daedalus.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "p1",
                    "repo": "O/p1",
                    "vcs": {"target_branch": "release"},
                }
            )
        )
        r1 = ConfigLoader().resolve_repo_config(str(repo))
    check("resolve_repo_config keeps identity fields", r1["repo"] == "O/p1")
    check(
        "resolve_repo_config pins workdir to the repo path",
        r1["workdir"] == str(repo.resolve()),
    )
    check(
        "resolve_repo_config lets the repo override template defaults",
        r1["vcs"]["target_branch"] == "release",
    )
    check(
        "resolve_repo_config inherits template defaults",
        r1["vcs"]["provider"] == "github" and r1["cron"]["schedule"] == "0 * * * *",
    )


# ── kanban: ls parsing ───────────────────────────────────────────────────────
def test_kanban_list_issue_numbers():
    tasks = [{"title": "#329 foo"}, {"title": "#42 bar"}, {"title": "no-number"}]
    with mock.patch.object(kanban, "list_tasks", return_value=tasks):
        nums = kanban.list_issue_numbers("board")
    check("list_issue_numbers parses #n from board output", nums == {329, 42})


def test_create_triage_pins_workspace():
    """A pinned triage propagates its workspace to every decompose child (native),
    so passing workspace must reach the CLI as --workspace <value>."""
    captured = {}

    def fake_hk(args, timeout=60):
        captured["args"] = args
        return (0, "Created t_abc (triage)", "")

    with mock.patch.object(kanban, "_hk", fake_hk):
        tid = kanban.create_triage(
            "slug", 7, "title", "body", idempotency_key="issue-7", workspace="dir:/w"
        )
    a = captured["args"]
    check("create_triage returns parsed tid", tid == "t_abc")
    check(
        "create_triage pins --workspace",
        "--workspace" in a and a[a.index("--workspace") + 1] == "dir:/w",
    )
    # No workspace -> no flag (kanban-only triage may be created pinned elsewhere).
    with mock.patch.object(kanban, "_hk", fake_hk):
        kanban.create_triage("slug", 7, "title", "body")
    check(
        "create_triage omits --workspace when unset",
        "--workspace" not in captured["args"],
    )


def test_kanban_review_handoff_pr():
    show = '{"runs":[{"reason":"review-required: shipped. PR #363 open."}]}'
    with mock.patch.object(kanban, "_hk", return_value=(0, show, "")):
        pr = kanban.review_handoff_pr("board", "t_x")
    check("review_handoff_pr extracts the PR from a review-required handoff", pr == 363)
    with mock.patch.object(
        kanban, "_hk", return_value=(0, '{"runs":[{"reason":"running"}]}', "")
    ):
        none = kanban.review_handoff_pr("board", "t_y")
    check(
        "review_handoff_pr returns None when not a review-required handoff",
        none is None,
    )


# ── kanban: ensure_board ──────────────────────────────────────────────────────


def test_ensure_board_creates():
    """ensure_board runs 'hermes kanban boards create <slug>' and succeeds on rc=0."""
    # Restore the original ensure_board in case a prior test monkey-patched it.
    kanban.ensure_board = _ORIG_KANBAN["ensure_board"]
    captured = {}

    def fake_hk(args, timeout=60):
        captured["args"] = args
        return (0, "Board my-board created.", "")

    with mock.patch.object(kanban, "_hk", fake_hk):
        ok = kanban.ensure_board("my-board")
    check("ensure_board returns True on rc=0", ok is True)
    check(
        "runs 'boards create' not '--board <slug> init'",
        captured["args"][:3] == ["boards", "create", "my-board"],
    )


def test_ensure_board_already_exists():
    """ensure_board treats 'already exists' stderr as success (idempotent)."""
    kanban.ensure_board = _ORIG_KANBAN["ensure_board"]
    captured = {}

    def fake_hk(args, timeout=60):
        captured["args"] = args
        return (1, "", "board 'my-board' already exists.")

    with mock.patch.object(kanban, "_hk", fake_hk):
        ok = kanban.ensure_board("my-board")
    check("ensure_board returns True when board already exists", ok is True)


def test_ensure_board_failure():
    """ensure_board returns False + warns on genuine failure."""
    kanban.ensure_board = _ORIG_KANBAN["ensure_board"]
    with mock.patch.object(kanban, "_hk", return_value=(1, "", "permission denied")):
        with mock.patch.object(kanban.logger, "warning") as mw:
            ok = kanban.ensure_board("my-board")
    check("ensure_board returns False on genuine failure", ok is False)
    mw.assert_called_once()
    assert "permission denied" in mw.call_args[0][2]


# ── dispatch: dual mode (GitHub board optional, kanban always) ───────────────
# ``_load_dispatch`` is imported from conftest (single source of truth).


# ── config: resolve_repo_config ──────────────────────────────────────────────


def test_resolve_repo_config_valid():
    """resolve_repo_config loads .hermes/daedalus.yaml, deep-merges with
    template defaults, and sets workdir to absolute repo_path."""
    import tempfile
    import yaml

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        hermes_dir = repo / ".hermes"
        hermes_dir.mkdir()
        cfg = {
            "name": "my-project",
            "repo": "org/my-project",
            "tracking": {"github_project_number": 42},
        }
        cfg_path = hermes_dir / "daedalus.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg))

        loader = ConfigLoader()
        result = loader.resolve_repo_config(str(repo))

    check("resolve_repo_config returns a dict", isinstance(result, dict))
    check("resolve_repo_config sets name", result.get("name") == "my-project")
    check("resolve_repo_config sets repo", result.get("repo") == "org/my-project")
    check(
        "resolve_repo_config sets workdir to absolute path",
        result.get("workdir") == str(Path(tmp).resolve()),
    )
    check(
        "resolve_repo_config carries tracking",
        result.get("tracking", {}).get("github_project_number") == 42,
    )
    # Defaults from template must be present (vcs, sources, cron)
    check("resolve_repo_config inherits vcs defaults", "vcs" in result)
    check(
        "resolve_repo_config inherits sources defaults",
        "sources" in result and isinstance(result["sources"], dict),
    )


def test_resolve_repo_config_sources_toggles():
    """Per-repo sources toggles are parsed and override defaults."""
    import tempfile
    import yaml

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        hermes_dir = repo / ".hermes"
        hermes_dir.mkdir()
        cfg = {
            "name": "src-test",
            "repo": "org/src-test",
            "sources": {
                "github_issues": {"enabled": False},
                "local_specs": {"enabled": True},
            },
        }
        (hermes_dir / "daedalus.yaml").write_text(yaml.safe_dump(cfg))

        loader = ConfigLoader()
        result = loader.resolve_repo_config(str(repo))

    check(
        "resolve_repo_config sets github_issues.enabled to False",
        result["sources"]["github_issues"]["enabled"] is False,
    )
    check(
        "resolve_repo_config sets local_specs.enabled to True",
        result["sources"]["local_specs"]["enabled"] is True,
    )


def test_resolve_repo_config_missing_file():
    """Missing .hermes/daedalus.yaml raises a clear error."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        loader = ConfigLoader()
        try:
            loader.resolve_repo_config(tmp)
            check("resolve_repo_config raises on missing file", False)
        except FileNotFoundError as e:
            check("resolve_repo_config error message includes path", tmp in str(e))


def test_dispatch_dual_mode():
    disp = _load_dispatch()
    calls = {
        "decompose_all": 0,
        "create_triage": 0,
        "create_task": 0,
        "fetch_issues": 0,
    }
    # Save originals for every kanban function we monkey-patch so cross-test
    # contamination (e.g. test_ensure_board_creates) cannot occur.
    _saved = {
        k: getattr(disp.kanban, k)
        for k in (
            "ensure_board",
            "list_blocked",
            "list_issue_numbers",
            "decompose_all_triage",
            "create_triage",
            "decompose",
            "dispatch",
            "create_task",
            "list_tasks",
        )
    }
    try:
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: set()
        disp.kanban.decompose_all_triage = lambda s: (
            calls.__setitem__("decompose_all", calls["decompose_all"] + 1) or True
        )
        disp.kanban.create_triage = lambda *a, **k: (
            calls.__setitem__("create_triage", calls["create_triage"] + 1) or "t_x"
        )
        disp.kanban.decompose = lambda *a, **k: True
        disp.kanban.dispatch = lambda s, max_spawns=5: True
        disp._fetch_issues = lambda r, f: (
            calls.__setitem__("fetch_issues", calls["fetch_issues"] + 1)
            or [{"number": 1, "title": "t"}]
        )
        base = {
            "repo": "O/R",
            "workdir": "/tmp",
            "name": "x",
            "issues": {"filters": {}},
            "execution": {},
        }

        disp.kanban.list_tasks = lambda *a, **k: []  # no confirmed validators yet
        disp.kanban.create_task = lambda *a, **k: (
            calls.__setitem__("create_task", calls["create_task"] + 1) or "t_v"
        )

        s1 = disp.run(
            {**base, "tracking": {}}, provider=_FakeProvider()
        )  # no board -> kanban-only
        check("kanban-only mode decomposes triage cards", calls["decompose_all"] == 1)
        check("kanban-only mode does NOT poll VCS issues", calls["fetch_issues"] == 0)
        check("kanban-only mode reports mode=kanban", s1.get("mode") == "kanban")

        class FP(_FakeProvider):
            def board_configured(self):
                return True

            def board_numbers_with_statuses(self, names):
                return {1}

        s2 = disp.run(
            {**base, "tracking": {"github_project_number": 1}}, provider=FP()
        )  # board mode
        check(
            "github mode polls issues and creates a validator task (phase 1 only)",
            calls["fetch_issues"] >= 1 and calls["create_task"] == 1,
        )
        check(
            "github mode does NOT decompose all roles in phase 1",
            calls["create_triage"] == 0,
        )
        check("github mode does NOT use decompose --all", calls["decompose_all"] == 1)
        check("board mode reports provider name", s2.get("mode") == "github")
    finally:
        for k, v in _saved.items():
            setattr(disp.kanban, k, v)


# ── main(): registry sweep ──────────────────────────────────────────────────


def test_main_registry_sweep():
    """main() without --repo: sweeps registry.list_projects(), resolves each
    repo via ConfigLoader().resolve_repo_config(), and calls run() once per repo."""
    import tempfile
    import yaml
    from unittest import mock

    disp = _load_dispatch()

    # Create two temp repos, each with a valid .hermes/daedalus.yaml.
    repos = []
    for name in ("repo-a", "repo-b"):
        tmp = tempfile.TemporaryDirectory()
        repos.append(tmp)
        repo = Path(tmp.name)
        hermes_dir = repo / ".hermes"
        hermes_dir.mkdir()
        cfg = {"name": name, "repo": f"org/{name}"}
        (hermes_dir / "daedalus.yaml").write_text(yaml.safe_dump(cfg))

    run_calls = []

    def fake_run(resolved, *, dry_run=False, max_dispatch=5):
        run_calls.append(resolved)
        return {
            "board": resolved.get("name", "?"),
            "mode": "kanban",
            "created": [],
            "reconciled": [],
            "completed": [],
            "advanced": [],
            "issues_seen": 0,
        }

    with mock.patch.object(
        disp.registry,
        "list_projects",
        return_value=[str(Path(r.name).resolve()) for r in repos],
    ):
        with mock.patch.object(disp, "run", fake_run):
            with mock.patch("sys.argv", ["daedalus_dispatch.py"]):
                disp.main()

    check("registry sweep calls run() once per repo", len(run_calls) == 2)
    names = {r.get("name") for r in run_calls}
    check("registry sweep runs both repos", names == {"repo-a", "repo-b"})

    for r in repos:
        r.cleanup()


def test_main_single_repo():
    """main() with --repo <path>: resolves the single repo and calls run() once."""
    import tempfile
    import yaml
    from unittest import mock

    disp = _load_dispatch()
    tmp = tempfile.TemporaryDirectory()
    try:
        repo = Path(tmp.name)
        hermes_dir = repo / ".hermes"
        hermes_dir.mkdir()
        cfg = {"name": "solo", "repo": "org/solo"}
        (hermes_dir / "daedalus.yaml").write_text(yaml.safe_dump(cfg))

        called = []

        def fake_run(resolved, *, dry_run=False, max_dispatch=5):
            called.append(resolved)
            return {
                "board": "solo",
                "mode": "kanban",
                "created": [],
                "reconciled": [],
                "completed": [],
                "advanced": [],
                "issues_seen": 0,
            }

        with mock.patch.object(disp, "run", fake_run):
            with mock.patch("sys.argv", ["daedalus_dispatch.py", "--repo", str(repo)]):
                disp.main()

        check("--repo calls run() exactly once", len(called) == 1)
        check("--repo resolves the correct repo name", called[0].get("name") == "solo")
        check(
            "--repo sets workdir to the repo path",
            called[0].get("workdir") == str(repo.resolve()),
        )
    finally:
        tmp.cleanup()


# ── GitHub PR helpers (new) ──────────────────────────────────────────────────


def test_parse_pr_from_card():
    """_parse_pr_from_card extracts PR numbers from card body + summary."""
    disp = _load_dispatch()

    card1 = {
        "body": "Implement fix for issue #7. PR #42 is open.",
        "latest_summary": "All tests pass.",
    }
    check(
        "_parse_pr_from_card extracts from body", disp._parse_pr_from_card(card1) == 42
    )

    card2 = {"body": "Implement fix.", "latest_summary": "Shipped as PR #99."}
    check(
        "_parse_pr_from_card extracts from summary",
        disp._parse_pr_from_card(card2) == 99,
    )

    card3 = {"body": "No PR here.", "latest_summary": ""}
    check(
        "_parse_pr_from_card returns None when no PR",
        disp._parse_pr_from_card(card3) is None,
    )

    card4 = {"body": "  \n  PR #7 is open.  \n  ", "latest_summary": None}
    check(
        "_parse_pr_from_card strips whitespace-padded body",
        disp._parse_pr_from_card(card4) == 7,
    )

    card5 = {"body": None, "latest_summary": "PR #13 merged."}
    check(
        "_parse_pr_from_card handles None body", disp._parse_pr_from_card(card5) == 13
    )


def test_find_doc_comment():
    """_find_doc_comment returns the body of the first **Agent: documentation** comment."""
    disp = _load_dispatch()

    comments = [
        _Comment("Regular review comment."),
        _Comment("**Agent: documentation**\n\n# Resolution Report\n\nHere is the fix."),
        _Comment("**Agent: documentation**\n\n# Another report"),
    ]
    with mock.patch.object(gp, "list_pr_comments", return_value=comments):
        result = disp._find_doc_comment(gp, 42)
    check(
        "_find_doc_comment returns first matching body",
        result.startswith("**Agent: documentation**"),
    )
    check("_find_doc_comment has report content", "Resolution Report" in result)

    no_doc = [_Comment("No doc comment here.")]
    with mock.patch.object(gp, "list_pr_comments", return_value=no_doc):
        result2 = disp._find_doc_comment(gp, 42)
    check("_find_doc_comment returns '' when no match", result2 == "")


def test_send_via_hermes():
    """_send_via_hermes calls `hermes send -t <target> --file <tmpfile> --json`."""
    disp = _load_dispatch()
    subprocess_calls = []

    def fake_run(args, **kwargs):
        subprocess_calls.append(args)
        return type(
            "R",
            (),
            {
                "returncode": 0,
                "stderr": "",
                "stdout": '{"success": true, "message_id": "1700.5"}',
            },
        )()

    with mock.patch.object(disp.subprocess, "run", fake_run):
        ok = disp._send_via_hermes("slack:tasks", "Report body here")
    check("_send_via_hermes returns True on success", ok is True)
    check(
        "_send_via_hermes called hermes send",
        subprocess_calls[0][0] == "hermes" and "send" in subprocess_calls[0],
    )
    check(
        "_send_via_hermes passed target",
        "-t" in subprocess_calls[0] and "slack:tasks" in subprocess_calls[0],
    )
    check("_send_via_hermes requests JSON result", "--json" in subprocess_calls[0])

    # Failure case
    subprocess_calls.clear()

    def fake_run_fail(args, **kwargs):
        subprocess_calls.append(args)
        return type(
            "R", (), {"returncode": 1, "stderr": "no such platform", "stdout": ""}
        )()

    with mock.patch.object(disp.subprocess, "run", fake_run_fail):
        ok2 = disp._send_via_hermes("slack:tasks", "Report")
    check("_send_via_hermes returns False on failure", ok2 is False)

    # Empty inputs
    check(
        "_send_via_hermes returns False for empty target",
        disp._send_via_hermes("", "body") is False,
    )
    check(
        "_send_via_hermes returns False for empty body",
        disp._send_via_hermes("slack:tasks", "") is False,
    )


def test_hermes_send_returns_anchor_and_threads():
    """_hermes_send parses message_id as the anchor and threads via target suffix."""
    disp = _load_dispatch()
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return type(
            "R",
            (),
            {
                "returncode": 0,
                "stderr": "",
                "stdout": '{"success": true, "message_id": "ts-123"}',
            },
        )()

    with mock.patch.object(disp.subprocess, "run", fake_run):
        ok, anchor = disp._hermes_send("slack:C1", "body")
        check("root send ok", ok is True)
        check("anchor parsed from message_id", anchor == "ts-123")
        # Threaded reply appends :thread_id to the target.
        disp._hermes_send("slack:C1", "reply", thread_id="ts-123")
    target_arg = calls[1][calls[1].index("-t") + 1]
    check("reply target carries thread id", target_arg == "slack:C1:ts-123")

    # Adapter-reported error in JSON → failure even on rc 0.
    def fake_err(args, **kwargs):
        return type(
            "R",
            (),
            {"returncode": 0, "stderr": "", "stdout": '{"error": "channel_not_found"}'},
        )()

    with mock.patch.object(disp.subprocess, "run", fake_err):
        ok2, anchor2 = disp._hermes_send("slack:C1", "body")
    check("json error → not ok", ok2 is False and anchor2 is None)


def test_hermes_send_broadcast_failure_is_logged():
    """When broadcast subprocess.run raises, a warning is logged (not silently swallowed)."""

    disp = _load_dispatch()

    call_count = [0]

    def fake_run(args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            # First call: primary send succeeds.
            return type(
                "R",
                (),
                {
                    "returncode": 0,
                    "stderr": "",
                    "stdout": '{"success": true, "message_id": "ts-abc"}',
                },
            )()
        # Second call: broadcast send raises.
        raise OSError("network unreachable")

    with mock.patch.object(disp.subprocess, "run", fake_run):
        with mock.patch.object(disp.logger, "warning") as mock_warn:
            ok, anchor = disp._hermes_send(
                "slack:C1", "body", thread_id="ts-abc", broadcast=True
            )

    check("primary send succeeds despite broadcast failure", ok is True)
    check("anchor still returned", anchor == "ts-abc")
    # Ensure warning was called at least once with the broadcast failure message.
    broadcast_warns = [c for c in mock_warn.call_args_list if "broadcast" in str(c)]
    check("broadcast failure logged as warning", len(broadcast_warns) >= 1)


def test_mirror_issue_threads_root_then_comment_reply(tmp_path):
    """_mirror_issue_threads posts a root, then mirrors agent comments as replies."""
    disp = _load_dispatch()

    class P(_FakeProvider):
        def get_issue_comments(self, n):
            return [
                {"id": 7, "body": "**Agent: validator**\nCONFIRMED"},
                {"id": 8, "body": "a human aside"},
            ]

    resolved = {
        "name": "proj",
        "workdir": str(tmp_path),
        "cron": {"notifications": [{"platform": "slack", "target": "slack:C1"}]},
    }
    issue = {"number": 121, "title": "Slack threads"}
    sends = []

    def fake_hermes(target, body, *, thread_id=None, broadcast=False):
        sends.append((target, body, thread_id, broadcast))
        return (True, "ts-root") if thread_id is None else (True, None)

    with mock.patch.object(disp, "_hermes_send", fake_hermes):
        # First tick: posts root + the agent comment (human aside excluded).
        n1 = disp._mirror_issue_threads(
            resolved, P(), issue, 121, str(tmp_path), pr_obj=None, pr_state=None
        )
        # Second tick: everything already mirrored → nothing new.
        n2 = disp._mirror_issue_threads(
            resolved, P(), issue, 121, str(tmp_path), pr_obj=None, pr_state=None
        )

    check("first tick sent root + 1 comment", n1 == 2)
    check("root posted with no thread_id", sends[0][2] is None)
    check("comment posted as reply to anchor", sends[1][2] == "ts-root")
    check("second tick is fully deduped", n2 == 0)
    check(
        "anchor persisted",
        disp.dispatch_state.get_thread_anchor(str(tmp_path), 121, "slack:C1")
        == "ts-root",
    )


def test_mirror_issue_threads_no_targets_is_noop(tmp_path):
    """With no cron.notifications, mirroring is a no-op (legacy projects unaffected)."""
    disp = _load_dispatch()
    sends = []
    with mock.patch.object(
        disp, "_hermes_send", lambda *a, **k: sends.append(a) or (True, "x")
    ):
        n = disp._mirror_issue_threads(
            {"name": "p", "workdir": str(tmp_path), "cron": {}},
            _FakeProvider(),
            {"number": 1, "title": "t"},
            1,
            str(tmp_path),
        )
    check("no targets → 0 sent", n == 0 and not sends)


def test_deliver_doc_reports_idempotent():
    """_deliver_doc_reports sends once, skips on sentinel."""
    disp = _load_dispatch()

    doc_card = {
        "id": "t_doc_1",
        "assignee": "documentation-daedalus",
        "body": "Write report. PR #42 is ready.",
        "latest_summary": "Report posted.",
        "parents": [],
    }

    # Mock kanban.list_tasks to return one done doc card
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[doc_card]):
        # Mock: sentinel NOT present (first pass)
        with mock.patch.object(gp, "pr_has_delivery_marker", return_value=False):
            # Mock: find the doc comment
            with mock.patch.object(
                disp,
                "_find_doc_comment",
                return_value="**Agent: documentation**\n\n# Report",
            ):
                # Mock: subprocess.run succeeds
                with mock.patch.object(disp, "_send_via_hermes", return_value=True):
                    # Mock: sentinel post succeeds
                    with mock.patch.object(
                        gp, "post_delivery_marker", return_value=True
                    ):
                        result1 = disp._deliver_doc_reports(
                            "slug",
                            gp,
                            "slack:tasks",
                        )
    check("first pass delivers one PR", result1 == [42])

    # Second pass: sentinel IS present → skip
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[doc_card]):
        with mock.patch.object(gp, "pr_has_delivery_marker", return_value=True):
            result2 = disp._deliver_doc_reports(
                "slug",
                gp,
                "slack:tasks",
            )
    check("second pass skips (sentinel present)", result2 == [])


def test_deliver_doc_reports_no_target():
    """_deliver_doc_reports returns [] when notify_target is empty."""
    disp = _load_dispatch()
    result = disp._deliver_doc_reports("slug", gp, "")
    check("empty notify_target returns []", result == [])


def test_deliver_doc_reports_send_failure():
    """_deliver_doc_reports does NOT mark sentinel on send failure."""
    disp = _load_dispatch()

    doc_card = {
        "id": "t_doc_1",
        "assignee": "documentation-daedalus",
        "body": "PR #42.",
        "latest_summary": "",
        "parents": [],
    }

    sentinel_calls = []

    def fake_has_marker(pr):
        return False

    def fake_post_marker(pr, body=""):
        sentinel_calls.append(pr)
        return True

    with mock.patch.object(disp.kanban, "list_tasks", return_value=[doc_card]):
        with mock.patch.object(gp, "pr_has_delivery_marker", fake_has_marker):
            with mock.patch.object(
                disp,
                "_find_doc_comment",
                return_value="**Agent: documentation**\n\nReport",
            ):
                with mock.patch.object(disp, "_send_via_hermes", return_value=False):
                    with mock.patch.object(
                        gp, "post_delivery_marker", fake_post_marker
                    ):
                        result = disp._deliver_doc_reports(
                            "slug",
                            gp,
                            "slack:tasks",
                        )
    check("send failure returns empty delivered list", result == [])
    check("send failure does NOT post sentinel", len(sentinel_calls) == 0)


def test_deliver_doc_reports_non_doc_assignee():
    """_deliver_doc_reports skips cards not assigned to documentation."""
    disp = _load_dispatch()
    dev_card = {
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "body": "PR #42.",
        "latest_summary": "",
        "parents": [],
    }
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[dev_card]):
        result = disp._deliver_doc_reports("slug", gp, "slack:tasks")
    check("non-doc assignee skipped", result == [])


def test_deliver_doc_reports_no_pr():
    """_deliver_doc_reports skips doc cards with no resolvable PR."""
    disp = _load_dispatch()
    doc_card = {
        "id": "t_doc",
        "assignee": "documentation-daedalus",
        "body": "No PR ref.",
        "latest_summary": "",
        "parents": [],
    }
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[doc_card]):
        with mock.patch.object(disp, "_parse_pr_from_card", return_value=None):
            with mock.patch.object(disp, "_resolve_pr_from_parents", return_value=None):
                result = disp._deliver_doc_reports("slug", gp, "slack:tasks")
    check("no resolvable PR → skipped", result == [])


def test_deliver_doc_reports_no_doc_comment():
    """_deliver_doc_reports skips when no **Agent: documentation** comment exists."""
    disp = _load_dispatch()
    doc_card = {
        "id": "t_doc",
        "assignee": "documentation-daedalus",
        "body": "PR #42.",
        "latest_summary": "",
        "parents": [],
    }
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[doc_card]):
        with mock.patch.object(gp, "pr_has_delivery_marker", return_value=False):
            with mock.patch.object(disp, "_find_doc_comment", return_value=""):
                result = disp._deliver_doc_reports("slug", gp, "slack:tasks")
    check("no doc comment → skipped", result == [])


def test_deliver_doc_reports_dry_run():
    """_deliver_doc_reports in dry_run mode logs but does NOT send."""
    disp = _load_dispatch()
    doc_card = {
        "id": "t_doc",
        "assignee": "documentation-daedalus",
        "body": "PR #42.",
        "latest_summary": "",
        "parents": [],
    }
    send_calls = []
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[doc_card]):
        with mock.patch.object(gp, "pr_has_delivery_marker", return_value=False):
            with mock.patch.object(
                disp,
                "_find_doc_comment",
                return_value="**Agent: documentation**\n\nReport",
            ):
                with mock.patch.object(
                    disp,
                    "_send_via_hermes",
                    side_effect=lambda *a, **k: send_calls.append(1) or True,
                ):
                    result = disp._deliver_doc_reports(
                        "slug",
                        gp,
                        "slack:tasks",
                        dry_run=True,
                    )
    check("dry_run returns PR number", result == [42])
    check("dry_run does NOT call _send_via_hermes", len(send_calls) == 0)


def test_resolve_pr_from_parents():
    """_resolve_pr_from_parents walks parent cards to find issue→PR."""
    disp = _load_dispatch()
    parent_card = {
        "id": "t_parent",
        "title": "#7 Fix the thing",
        "body": "Triage for #7.",
    }
    with mock.patch.object(disp.kanban, "show_card", return_value=parent_card):
        with mock.patch.object(gp, "pr_number_for_issue", return_value=42):
            result = disp._resolve_pr_from_parents(
                "slug", gp, {"parents": ["t_parent"]}
            )
    check("_resolve_pr_from_parents resolves PR via parent", result == 42)

    # No parents
    result2 = disp._resolve_pr_from_parents("slug", gp, {"parents": []})
    check("_resolve_pr_from_parents returns None with no parents", result2 is None)


def test_human_summary_slack_delivered():
    """_human_summary includes slack_delivered in output."""
    disp = _load_dispatch()
    summary = {
        "board": "test",
        "mode": "github",
        "created": [1, 2],
        "reconciled": [],
        "completed": [],
        "advance_prs": [],
        "routed_actions": {},
        "issues_seen": 5,
        "spec_created": [],
        "slack_delivered": [42, 99],
    }
    msg = disp._human_summary({"test": summary})
    check(
        "_human_summary includes doc-report delivery",
        "PR #42" in msg and "PR #99" in msg,
    )

    # Empty slack_delivered → no mention
    summary2 = {
        "board": "test",
        "mode": "github",
        "created": [],
        "reconciled": [],
        "completed": [],
        "advance_prs": [],
        "routed_actions": {},
        "issues_seen": 0,
        "spec_created": [],
        "slack_delivered": [],
    }
    msg2 = disp._human_summary({"test": summary2})
    check("_human_summary returns '' when nothing happened", msg2 == "")


def test_task_body_no_slack():
    """_task_body no longer instructs the doc agent to call hermes send."""
    disp = _load_dispatch()
    body = disp._task_body(
        "O/R",
        {"number": 7, "title": "Fix bug", "body": ""},
        iterations=3,
        workdir="/tmp",
        notify_target="slack:tasks",
    )
    check("_task_body does NOT mention hermes send", "hermes send" not in body)
    check(
        "_task_body mentions dispatcher handles messaging-platform delivery",
        "dispatcher" in body and "messaging-platform" in body,
    )
    # #894: agents no longer post their own issue comments (GITHUB_TOKEN absent
    # in cron env). The dispatcher mirrors each role's kanban summary to the
    # issue, so the body must NOT carry the urllib/GITHUB_TOKEN comment snippet.
    check(
        "_task_body does NOT instruct agents to POST issue comments themselves",
        "issues/<number>/comments" not in body and "issues/7/comments" not in body,
    )
    check(
        "_task_body tells agents progress comments are posted automatically",
        "PROGRESS COMMENTS ARE AUTOMATIC" in body
        and "do NOT post GitHub comments yourself" in body,
    )
    check(
        "_task_body instructs the API PR-create path",
        "api.github.com/repos/O/R/pulls" in body,
    )
    check(
        "_task_body never mentions the gh CLI",
        "gh pr" not in body and "gh issue" not in body and "gh auth" not in body,
    )
    check(
        "_task_body mentions Agent: documentation prefix",
        "**Agent: documentation**" in body,
    )


def test_format_completion_comment_has_role_title_summary():
    """_format_completion_comment leads with the agent role and includes the
    task title and the kanban summary (issue #894)."""
    disp = _load_dispatch()
    body = disp._format_completion_comment("developer", "#7 Fix bug", "Opened PR #12")
    check("comment leads with Agent: <role>", body.startswith("**Agent: developer**"))
    check("comment includes task title", "#7 Fix bug" in body)
    check("comment includes the summary", "Opened PR #12" in body)


def test_format_completion_comment_handles_empty_summary():
    """A role that completes with no recorded summary still yields a comment."""
    disp = _load_dispatch()
    body = disp._format_completion_comment("qa", "#7 Fix bug", "")
    check("empty-summary comment still attributes the role", "**Agent: qa**" in body)
    check(
        "empty-summary comment notes the missing summary", "no summary" in body.lower()
    )


def test_post_completion_comments_posts_once_per_role():
    """The dispatcher posts each completed role's kanban summary to the issue
    via provider.post_issue_comment — replacing agent self-posting (#894)."""
    import tempfile

    disp = _load_dispatch()
    profiles = dict(disp._DEFAULT_PROFILES)
    cards = [
        {
            "id": "t1",
            "assignee": "developer-daedalus",
            "title": "#7 Fix bug",
            "summary": "Opened PR #12",
        },
        {
            "id": "t2",
            "assignee": "qa-daedalus",
            "title": "#7 Fix bug",
            "summary": "qa-passed: PR #12",
        },
        {
            "id": "t3",
            "assignee": "someone-else",  # not a pipeline role → skipped
            "title": "#7 noise",
            "summary": "ignore me",
        },
    ]
    _orig = disp.kanban.list_tasks
    disp.kanban.list_tasks = lambda *a, **k: cards
    try:
        with tempfile.TemporaryDirectory() as tmp:
            prov = FakeProvider(issues={7: object()})
            posted = disp._post_completion_comments("proj", prov, profiles, tmp)
            check("posted to issue #7 for both pipeline roles", posted == [7, 7])
            check("two issue comments posted", len(prov.posted_issue_comments) == 2)
            roles_in = " ".join(b for _, b in prov.posted_issue_comments)
            check("developer comment posted", "**Agent: developer**" in roles_in)
            check("qa comment posted", "**Agent: qa**" in roles_in)
            check("non-role card was skipped", "ignore me" not in roles_in)
    finally:
        disp.kanban.list_tasks = _orig


def test_post_completion_comments_idempotent_across_ticks():
    """Re-running on the same DONE cards does not re-post (dispatch_state flag)."""
    import tempfile

    disp = _load_dispatch()
    profiles = dict(disp._DEFAULT_PROFILES)
    cards = [
        {
            "id": "t1",
            "assignee": "developer-daedalus",
            "title": "#7 Fix bug",
            "summary": "Opened PR #12",
        }
    ]
    _orig = disp.kanban.list_tasks
    disp.kanban.list_tasks = lambda *a, **k: cards
    try:
        with tempfile.TemporaryDirectory() as tmp:
            prov = FakeProvider(issues={7: object()})
            disp._post_completion_comments("proj", prov, profiles, tmp)
            disp._post_completion_comments("proj", prov, profiles, tmp)  # second tick
            check(
                "comment posted exactly once across two ticks",
                len(prov.posted_issue_comments) == 1,
            )
    finally:
        disp.kanban.list_tasks = _orig


def test_post_completion_comments_skips_closed_issues():
    """Closed issues are skipped so a backlog of historical DONE cards can't
    spam old/closed issues on first run (issue #894)."""
    import tempfile

    disp = _load_dispatch()
    profiles = dict(disp._DEFAULT_PROFILES)
    cards = [
        {
            "id": "t1",
            "assignee": "developer-daedalus",
            "title": "#7 Old bug",
            "summary": "done long ago",
        }
    ]
    _orig = disp.kanban.list_tasks
    disp.kanban.list_tasks = lambda *a, **k: cards
    try:
        with tempfile.TemporaryDirectory() as tmp:
            prov = FakeProvider(issues={7: object()}, closed_issues={7})
            posted = disp._post_completion_comments("proj", prov, profiles, tmp)
            check("no comment posted on a closed issue", posted == [])
            check("closed issue received no comment", prov.posted_issue_comments == [])
    finally:
        disp.kanban.list_tasks = _orig


def test_post_completion_comments_none_provider_is_noop():
    """No provider → no-op (no crash)."""
    disp = _load_dispatch()
    check(
        "None provider returns empty list",
        disp._post_completion_comments("proj", None, {}, "/tmp") == [],
    )


class _LimitRecordingProvider:
    """Records the ``limit`` kwarg passed to ``list_issues`` (issue #203)."""

    def __init__(self):
        self.seen_limit = None

    def list_issues(self, state="open", labels=None, limit=50):
        self.seen_limit = limit
        return []


def test_fetch_issues_default_limit_is_100():
    """_fetch_issues defaults to limit=100 so boards with >20 open issues are
    not silently truncated (issue #203)."""
    disp = _load_dispatch()
    prov = _LimitRecordingProvider()
    disp._fetch_issues(prov, {})
    check("_fetch_issues default limit is 100", prov.seen_limit == 100)


def test_fetch_issues_respects_configured_limit():
    """An explicit issues.filters.limit still overrides the default."""
    disp = _load_dispatch()
    prov = _LimitRecordingProvider()
    disp._fetch_issues(prov, {"limit": 250})
    check("_fetch_issues honors configured limit", prov.seen_limit == 250)


def test_dispatch_summary_has_slack_delivered():
    """run() includes slack_delivered in both kanban-only and github summaries."""
    disp = _load_dispatch()

    # Stub all the heavy machinery
    disp.kanban.ensure_board = lambda s: None
    disp.kanban.list_blocked = lambda s: []
    disp.kanban.list_issue_numbers = lambda s: set()
    disp.kanban.decompose_all_triage = lambda s: True
    disp.kanban.create_triage = lambda *a, **k: "t_x"
    disp.kanban.decompose = lambda *a, **k: True
    disp.kanban.dispatch = lambda s, max_spawns=5: True
    disp._fetch_issues = lambda r, f: [{"number": 1, "title": "t"}]

    # Save and restore create_task + list_tasks to avoid cross-file isolation issues
    _orig_create_task = disp.kanban.create_task
    _orig_list_tasks = disp.kanban.list_tasks
    disp.kanban.create_task = lambda *a, **k: "t_v"
    disp.kanban.list_tasks = lambda *a, **k: []  # no confirmed validators yet

    # Mock _deliver_doc_reports to return a known value
    with mock.patch.object(disp, "_deliver_doc_reports", return_value=[42]):
        base = {
            "repo": "O/R",
            "workdir": "/tmp",
            "name": "x",
            "issues": {"filters": {}},
            "execution": {},
        }

        # kanban-only mode
        s1 = disp.run({**base, "tracking": {}}, provider=_FakeProvider())
        check("kanban summary has slack_delivered", s1.get("slack_delivered") == [42])

        # board mode
        class FP(_FakeProvider):
            def board_configured(self):
                return True

            def board_numbers_with_statuses(self, names):
                return {1}

        s2 = disp.run({**base, "tracking": {"github_project_number": 1}}, provider=FP())
        check("board summary has slack_delivered", s2.get("slack_delivered") == [42])

    disp.kanban.create_task = _orig_create_task
    disp.kanban.list_tasks = _orig_list_tasks

    # kanban-only mode enrollment_failures key
    check("kanban summary has enrollment_failures", "enrollment_failures" in s1)


def test_dispatch_summary_enrollment_failures_contents():
    """kanban-only summary includes enrollment_failures from provider."""
    disp = _load_dispatch()
    disp.kanban.ensure_board = lambda s: None
    disp.kanban.list_blocked = lambda s: []
    disp.kanban.list_issue_numbers = lambda s: set()
    disp.kanban.decompose_all_triage = lambda s: True
    disp.kanban.dispatch = lambda s, max_spawns=5: True

    class _ProviderWithFailures(_FakeProvider):
        def __init__(self):
            super().__init__()
            self.enrollment_failures = [5, 99]

    with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
        s = disp.run(
            {
                "repo": "O/R",
                "workdir": "/tmp",
                "name": "x",
                "issues": {"filters": {}},
                "execution": {},
                "tracking": {},
            },
            provider=_ProviderWithFailures(),
        )
    check(
        "enrollment_failures populated from provider",
        s.get("enrollment_failures") == [5, 99],
    )


def test_dispatch_summary_enrollment_failures_empty_when_no_attr():
    """kanban-only summary returns [] when provider lacks enrollment_failures."""
    disp = _load_dispatch()
    disp.kanban.ensure_board = lambda s: None
    disp.kanban.list_blocked = lambda s: []
    disp.kanban.list_issue_numbers = lambda s: set()
    disp.kanban.decompose_all_triage = lambda s: True
    disp.kanban.dispatch = lambda s, max_spawns=5: True

    with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
        s = disp.run(
            {
                "repo": "O/R",
                "workdir": "/tmp",
                "name": "x",
                "issues": {"filters": {}},
                "execution": {},
                "tracking": {},
            },
            provider=_FakeProvider(),
        )
    check("enrollment_failures defaults to []", s.get("enrollment_failures") == [])


def test_notify_targets():
    """_notify_targets: notifications[] wins, event-filters, falls back to deliver."""
    disp = _load_dispatch()

    legacy = {"cron": {"deliver": "slack:tasks"}}
    check(
        "legacy deliver receives every event",
        disp._notify_targets(legacy, "doc-report") == ["slack:tasks"]
        and disp._notify_targets(legacy, "dispatch-summary") == ["slack:tasks"],
    )

    check("no config → no targets", disp._notify_targets({}, "doc-report") == [])

    multi = {
        "cron": {
            "deliver": "slack:legacy",
            "notifications": [
                {"platform": "Slack", "target": "slack:C1", "events": ["doc-report"]},
                {
                    "platform": "Discord",
                    "target": "discord:#general",
                    "events": ["dispatch-summary", "pr-ready"],
                },
                {
                    "platform": "Telegram",
                    "target": "telegram:-100123",
                },  # no events → all
                {"platform": "Signal", "target": ""},  # invalid → skipped
            ],
        }
    }
    check(
        "notifications[] overrides legacy deliver",
        "slack:legacy" not in disp._notify_targets(multi, "doc-report"),
    )
    check(
        "doc-report goes to subscribed + catch-all targets",
        disp._notify_targets(multi, "doc-report") == ["slack:C1", "telegram:-100123"],
    )
    check(
        "dispatch-summary respects event filters",
        disp._notify_targets(multi, "dispatch-summary")
        == ["discord:#general", "telegram:-100123"],
    )
    check(
        "pipeline-failure reaches only catch-all",
        disp._notify_targets(multi, "pipeline-failure") == ["telegram:-100123"],
    )


def test_summary_events():
    disp = _load_dispatch()
    check(
        "plain tick → dispatch-summary only",
        disp._summary_events({"created": [1]}) == {"dispatch-summary"},
    )
    check(
        "error adds pipeline-failure",
        "pipeline-failure" in disp._summary_events({"error": "boom"}),
    )
    check(
        "advanced PRs add pr-ready",
        "pr-ready" in disp._summary_events({"advance_prs": [7]}),
    )
    check(
        "reconciled adds pr-ready",
        "pr-ready" in disp._summary_events({"reconciled": [(7, "In review")]}),
    )
    check(
        "blocked adds security-escalation",
        "security-escalation" in disp._summary_events({"blocked": [42]}),
    )


def test_notify_project_summary_fans_out():
    """_notify_project_summary sends each project summary to its own targets."""
    disp = _load_dispatch()
    sent = []
    summary = {
        "board": "b",
        "mode": "github",
        "created": [1],
        "reconciled": [],
        "completed": [],
        "advance_prs": [],
        "routed_actions": {},
        "issues_seen": 1,
        "spec_created": [],
        "slack_delivered": [],
    }

    resolved = {
        "cron": {
            "notifications": [
                {
                    "platform": "Slack",
                    "target": "slack:C1",
                    "events": ["dispatch-summary"],
                },
                {
                    "platform": "Discord",
                    "target": "discord:#x",
                    "events": ["doc-report"],
                },
            ]
        }
    }
    with mock.patch.object(
        disp, "_send_via_hermes", side_effect=lambda t, m: sent.append(t) or True
    ):
        handled = disp._notify_project_summary("proj", summary, resolved)
    check("notifications project is handled (excluded from stdout)", handled is True)
    check("summary sent only to dispatch-summary targets", sent == ["slack:C1"])

    sent.clear()
    legacy = {"cron": {"deliver": "slack:tasks"}}
    with mock.patch.object(
        disp, "_send_via_hermes", side_effect=lambda t, m: sent.append(t) or True
    ):
        handled2 = disp._notify_project_summary("proj", summary, legacy)
    check(
        "legacy project flows through cron stdout (not self-sent)",
        handled2 is False and sent == [],
    )


def test_notify_project_summary_threads_and_dedupes():
    """Issue #137: summaries thread under a per-project anchor and dedupe by content."""
    import tempfile

    disp = _load_dispatch()
    summary = {
        "board": "b",
        "mode": "github",
        "created": [1],
        "reconciled": [],
        "completed": [],
        "advance_prs": [],
        "routed_actions": {},
        "issues_seen": 1,
        "spec_created": [],
        "slack_delivered": [],
        "blocked": [],
    }

    with tempfile.TemporaryDirectory() as workdir:
        resolved = {
            "workdir": workdir,
            "cron": {
                "notifications": [
                    {
                        "platform": "Slack",
                        "target": "slack:C1",
                        "events": ["dispatch-summary"],
                    },
                ]
            },
        }
        sends = []  # (target, thread_id)

        def fake_send(target, body, *, thread_id=None):
            sends.append((target, thread_id))
            return (True, "anchor-1")

        with mock.patch.object(disp, "_hermes_send", fake_send):
            disp._notify_project_summary("proj", summary, resolved)
            # identical summary on a later tick → recognised by content hash, skipped
            disp._notify_project_summary("proj", dict(summary), resolved)
        check(
            "first summary posts a root (no thread id); repeat is deduped",
            sends == [("slack:C1", None)],
        )

        # a CHANGED summary posts as a reply under the stored per-project anchor
        sends.clear()
        changed = dict(summary, created=[], completed=[1])
        with mock.patch.object(disp, "_hermes_send", fake_send):
            disp._notify_project_summary("proj", changed, resolved)
        check(
            "changed summary threads as a reply under the anchor",
            sends == [("slack:C1", "anchor-1")],
        )


def test_notify_project_summary_silent_tick_sends_nothing():
    """Issue #137: a no-op tick (empty summary) delivers nothing but is handled."""
    import tempfile

    disp = _load_dispatch()
    empty = {
        "board": "b",
        "mode": "github",
        "created": [],
        "reconciled": [],
        "completed": [],
        "advance_prs": [],
        "routed_actions": {},
        "issues_seen": 1,
        "spec_created": [],
        "slack_delivered": [],
        "blocked": [],
    }
    with tempfile.TemporaryDirectory() as workdir:
        resolved = {
            "workdir": workdir,
            "cron": {
                "notifications": [
                    {
                        "platform": "Slack",
                        "target": "slack:C1",
                        "events": ["dispatch-summary"],
                    },
                ]
            },
        }
        sends = []
        with mock.patch.object(
            disp, "_hermes_send", lambda *a, **k: sends.append(a) or (True, "x")
        ):
            handled = disp._notify_project_summary("proj", empty, resolved)
    check("silent tick is handled (excluded from stdout)", handled is True)
    check("silent tick sends nothing", sends == [])


def test_resolve_repo_arg_path_and_slug():
    """Issue #137: --repo accepts a filesystem path OR a registered owner/repo slug."""
    import tempfile
    import yaml

    disp = _load_dispatch()
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        hermes_dir = repo / ".hermes"
        hermes_dir.mkdir()
        (hermes_dir / "daedalus.yaml").write_text(
            yaml.safe_dump({"name": "solo", "repo": "org/solo"})
        )

        check(
            "an existing path resolves to itself",
            disp._resolve_repo_arg(str(repo)) == str(repo.resolve()),
        )

        with mock.patch.object(
            disp.registry, "list_projects", return_value=[str(repo.resolve())]
        ):
            check(
                "a registered VCS slug resolves to its repo path",
                disp._resolve_repo_arg("org/solo") == str(repo.resolve()),
            )
            check(
                "an unknown slug resolves to None",
                disp._resolve_repo_arg("org/unknown") is None,
            )


def test_resolve_repo_arg_broken_config_warns():
    """Issue #1110: broken project config emits a warning instead of silently skipping."""
    disp = _load_dispatch()
    with (
        mock.patch.object(disp.registry, "list_projects", return_value=["bad/project"]),
        mock.patch.object(
            disp.ConfigLoader,
            "resolve_repo_config",
            side_effect=ValueError("YAML parse error"),
        ),
        mock.patch.object(disp.logger, "warning") as mock_warn,
    ):
        result = disp._resolve_repo_arg("org/missing")

    check("returns None when all configs are broken", result is None)
    check(
        "warning names the project path",
        mock_warn.called and "bad/project" in str(mock_warn.call_args_list),
    )
    check(
        "warning includes the exception message",
        mock_warn.called and "YAML parse error" in str(mock_warn.call_args_list),
    )


def test_resolve_repo_from_cwd():
    """Issue #137: cwd inside a registered repo auto-scopes to that repo."""
    import os
    import tempfile

    disp = _load_dispatch()
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp).resolve()
        sub = repo / "src"
        sub.mkdir()
        orig = os.getcwd()
        try:
            with mock.patch.object(
                disp.registry, "list_projects", return_value=[str(repo)]
            ):
                os.chdir(sub)
                check(
                    "a child dir of a registered repo resolves to the repo",
                    disp._resolve_repo_from_cwd() == str(repo),
                )
                os.chdir(orig)
            with mock.patch.object(disp.registry, "list_projects", return_value=[]):
                check(
                    "cwd outside every registered repo resolves to None",
                    disp._resolve_repo_from_cwd() is None,
                )
        finally:
            os.chdir(orig)


def test_main_scopes_to_cwd_project():
    """Issue #137: main() with no --repo scopes to the registered project at cwd."""
    import os
    import tempfile
    import yaml

    disp = _load_dispatch()
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp).resolve()
        hermes_dir = repo / ".hermes"
        hermes_dir.mkdir()
        (hermes_dir / "daedalus.yaml").write_text(
            yaml.safe_dump({"name": "scoped", "repo": "org/scoped"})
        )

        called = []

        def fake_run(resolved, *, dry_run=False, max_dispatch=5):
            called.append(resolved)
            return {
                "board": "scoped",
                "mode": "kanban",
                "created": [],
                "reconciled": [],
                "completed": [],
                "advanced": [],
                "issues_seen": 0,
            }

        orig = os.getcwd()
        try:
            os.chdir(repo)
            with mock.patch.object(
                disp.registry, "list_projects", return_value=[str(repo)]
            ):
                with mock.patch.object(disp, "run", fake_run):
                    with mock.patch("sys.argv", ["daedalus_dispatch.py"]):
                        disp.main()
        finally:
            os.chdir(orig)

    check("cwd-scoped main() calls run() exactly once", len(called) == 1)
    check(
        "cwd-scoped main() runs the project at cwd", called[0].get("name") == "scoped"
    )


def test_deliver_doc_reports_multi_target():
    """_deliver_doc_reports fans a report out to every configured target."""
    disp = _load_dispatch()
    doc_card = {
        "id": "t_doc",
        "assignee": "documentation-daedalus",
        "body": "PR #42.",
        "latest_summary": "",
        "parents": [],
    }
    sent = []
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[doc_card]):
        with mock.patch.object(gp, "pr_has_delivery_marker", return_value=False):
            with mock.patch.object(
                disp,
                "_find_doc_comment",
                return_value="**Agent: documentation**\n\nReport",
            ):
                with mock.patch.object(
                    disp,
                    "_send_via_hermes",
                    side_effect=lambda t, m: sent.append(t) or True,
                ):
                    with mock.patch.object(
                        gp, "post_delivery_marker", return_value=True
                    ):
                        result = disp._deliver_doc_reports(
                            "slug",
                            gp,
                            ["slack:C1", "discord:#docs"],
                        )
    check("multi-target delivers the PR once", result == [42])
    check("report sent to every target", sent == ["slack:C1", "discord:#docs"])

    # Partial failure: sentinel still posted (one target got it), PR delivered
    sent.clear()
    marker_posts = []
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[doc_card]):
        with mock.patch.object(gp, "pr_has_delivery_marker", return_value=False):
            with mock.patch.object(
                disp,
                "_find_doc_comment",
                return_value="**Agent: documentation**\n\nReport",
            ):
                with mock.patch.object(
                    disp, "_send_via_hermes", side_effect=lambda t, m: t == "slack:C1"
                ):
                    with mock.patch.object(
                        gp,
                        "post_delivery_marker",
                        side_effect=lambda pr, body="": marker_posts.append(pr) or True,
                    ):
                        result2 = disp._deliver_doc_reports(
                            "slug",
                            gp,
                            ["slack:C1", "discord:#docs"],
                        )
    check("partial failure still counts as delivered", result2 == [42])
    check("sentinel posted after partial success", marker_posts == [42])


# ── kanban: list_issue_numbers large ids ─────────────────────────────────────


def test_kanban_list_issue_numbers_large_ids():
    """list_issue_numbers must return 4+ digit issue numbers (JSON-backed path)."""
    tasks = [
        {"id": "t_a", "title": "#1234 Large issue number"},
        {"id": "t_b", "title": "#10000 Five-digit number"},
        {"id": "t_c", "title": "no number here"},
        {"id": "t_d", "title": "#42 normal"},
    ]
    with mock.patch.object(kanban, "list_tasks", return_value=tasks):
        nums = kanban.list_issue_numbers("board")
    check("list_issue_numbers parses 4-digit issue numbers", 1234 in nums)
    check("list_issue_numbers parses 5-digit issue numbers", 10000 in nums)
    check("list_issue_numbers parses normal 2-digit numbers", 42 in nums)
    check("list_issue_numbers skips titles without numbers", len(nums) == 3)


# ── dispatch_state module ─────────────────────────────────────────────────────


def test_dispatch_state_record_and_age():
    """record_dispatch + get_dispatch_age_hours round-trip."""
    import tempfile
    from core import dispatch_state as ds

    with tempfile.TemporaryDirectory() as tmp:
        ds.record_dispatch(tmp, 42)
        age = ds.get_dispatch_age_hours(tmp, 42)
        check(
            "get_dispatch_age_hours returns a non-negative float",
            isinstance(age, float) and age >= 0,
        )
        check(
            "get_dispatch_age_hours returns None for unknown issue",
            ds.get_dispatch_age_hours(tmp, 99) is None,
        )


def test_dispatch_state_clear():
    """clear_dispatch removes the record so age returns None."""
    import tempfile
    from core import dispatch_state as ds

    with tempfile.TemporaryDirectory() as tmp:
        ds.record_dispatch(tmp, 5)
        ds.clear_dispatch(tmp, 5)
        check(
            "clear_dispatch removes dispatch record",
            ds.get_dispatch_age_hours(tmp, 5) is None,
        )


def test_dispatch_state_pr_flags():
    """has_pr_flag / set_pr_flag are idempotent."""
    import tempfile
    from core import dispatch_state as ds

    with tempfile.TemporaryDirectory() as tmp:
        check(
            "has_pr_flag returns False before set",
            not ds.has_pr_flag(tmp, 7, "size_warned"),
        )
        ds.set_pr_flag(tmp, 7, "size_warned")
        check(
            "has_pr_flag returns True after set", ds.has_pr_flag(tmp, 7, "size_warned")
        )
        ds.set_pr_flag(tmp, 7, "size_warned")  # idempotent
        check(
            "set_pr_flag idempotent — flag stays True",
            ds.has_pr_flag(tmp, 7, "size_warned"),
        )
        check("unrelated flag still False", not ds.has_pr_flag(tmp, 7, "other"))


def test_dispatch_state_review_sha():
    """record_review / get_review_sha round-trip."""
    import tempfile
    from core import dispatch_state as ds

    with tempfile.TemporaryDirectory() as tmp:
        ds.record_review(tmp, 10, "reviewer-daedalus", "abc123")
        sha = ds.get_review_sha(tmp, 10, "reviewer-daedalus")
        check("get_review_sha returns stored SHA", sha == "abc123")
        check(
            "get_review_sha returns None for unknown reviewer",
            ds.get_review_sha(tmp, 10, "other") is None,
        )


# ── dispatch: priority sort ───────────────────────────────────────────────────


def test_priority_sort_p0_before_unlabeled():
    """P0-labelled issues are dispatched before unlabelled ones."""
    from core.providers.base import IssueSummary

    disp = _load_dispatch()
    dispatched = []

    class FP(_FakeProvider):
        def board_configured(self):
            return True

        def board_numbers_with_statuses(self, names):
            return {1, 2}

        def list_issues(self, state="open", labels=None, limit=50):
            return [
                IssueSummary(number=2, title="normal", labels=[]),
                IssueSummary(number=1, title="urgent", labels=["P0"]),
            ]

        def pr_state_for_issue(self, n):
            return None

    disp.kanban.ensure_board = lambda s: None
    disp.kanban.list_blocked = lambda s: []
    disp.kanban.list_issue_numbers = lambda s: set()
    disp.kanban.dispatch = lambda s, max_spawns=5: True
    disp.kanban.list_tasks = lambda *a, **k: []

    _orig_create_task = disp.kanban.create_task
    try:

        def fake_create(
            slug,
            title,
            body="",
            *,
            assignee="",
            idempotency_key="",
            workspace="",
            max_retries=None,
            skills=None,
            goal=False,
            goal_max_turns=None,
            parents=None,
        ):
            m = __import__("re").search(r"#(\d+)", title)
            if m:
                dispatched.append(int(m.group(1)))
            return "t_x"

        disp.kanban.create_task = fake_create
        disp.run(
            {
                "repo": "O/R",
                "workdir": "/tmp",
                "name": "x",
                "issues": {"filters": {}},
                "execution": {},
                "tracking": {"github_project_number": 1},
            },
            provider=FP(),
            max_dispatch=1,
        )
    finally:
        disp.kanban.create_task = _orig_create_task

    check(
        "priority sort dispatches P0 issue first",
        len(dispatched) >= 1 and dispatched[0] == 1,
    )


def test_priority_sort_dict_labels():
    """Priority sort handles GitHub-style label dicts ({"name": "P1"})."""
    disp = _load_dispatch()
    sorted_issues = disp._PRIORITY  # just check the constant exists
    check(
        "_PRIORITY dict has p0/P0 entries",
        sorted_issues.get("p0") == 0 and sorted_issues.get("P0") == 0,
    )
    check(
        "_PRIORITY dict has p1/p2 entries",
        sorted_issues.get("p1") == 1 and sorted_issues.get("p2") == 2,
    )


# ── dispatch: PR size gate ────────────────────────────────────────────────────


def test_pr_size_gate_warns_once():
    """size gate posts a PR comment once and sets the size_warned flag."""
    import tempfile
    from core import dispatch_state as ds

    disp = _load_dispatch()
    posted = []

    class FP(_FakeProvider):
        def board_configured(self):
            return True

        def board_numbers_with_statuses(self, names):
            return set()

        def list_issues(self, state="open", labels=None, limit=50):
            from core.providers.base import IssueSummary

            return [IssueSummary(number=5, title="big change")]

        def pr_state_for_issue(self, n):
            return "open"

        def pr_number_for_issue(self, n):
            return 99

        def pr_ci_green(self, pr):
            return False

        def get_pr_files(self, pr):
            return [{"filename": "big.py", "changes": 600}]

        def post_pr_comment(self, pr, body):
            posted.append((pr, body))
            return True

        def post_issue_comment(self, n, body):
            return True

        def board_ensure_status_option(self, *a):
            return True

    with tempfile.TemporaryDirectory() as tmp:
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {5}
        disp.kanban.dispatch = lambda s, max_spawns=5: True
        disp.kanban.list_tasks = lambda *a, **k: []
        disp.run(
            {
                "repo": "O/R",
                "workdir": tmp,
                "name": "x",
                "issues": {"filters": {}},
                "execution": {"max_pr_lines": 500},
                "tracking": {"github_project_number": 1},
            },
            provider=FP(),
        )
        check(
            "size gate posts a warning comment",
            len(posted) == 1 and "too large" in posted[0][1].lower(),
        )
        check("size gate sets size_warned flag", ds.has_pr_flag(tmp, 99, "size_warned"))


def test_pr_size_gate_idempotent():
    """size gate does NOT re-post if size_warned flag is already set."""
    import tempfile
    from core import dispatch_state as ds

    disp = _load_dispatch()
    posted = []

    class FP(_FakeProvider):
        def board_configured(self):
            return True

        def board_numbers_with_statuses(self, names):
            return set()

        def list_issues(self, state="open", labels=None, limit=50):
            from core.providers.base import IssueSummary

            return [IssueSummary(number=5, title="big change")]

        def pr_state_for_issue(self, n):
            return "open"

        def pr_number_for_issue(self, n):
            return 99

        def pr_ci_green(self, pr):
            return False

        def get_pr_files(self, pr):
            return [{"filename": "big.py", "changes": 600}]

        def post_pr_comment(self, pr, body):
            posted.append(pr)
            return True

        def post_issue_comment(self, n, body):
            return True

        def board_ensure_status_option(self, *a):
            return True

    with tempfile.TemporaryDirectory() as tmp:
        ds.set_pr_flag(tmp, 99, "size_warned")  # pre-seed
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {5}
        disp.kanban.dispatch = lambda s, max_spawns=5: True
        disp.kanban.list_tasks = lambda *a, **k: []
        disp.run(
            {
                "repo": "O/R",
                "workdir": tmp,
                "name": "x",
                "issues": {"filters": {}},
                "execution": {"max_pr_lines": 500},
                "tracking": {"github_project_number": 1},
            },
            provider=FP(),
        )
        check("size gate does not re-post when flag already set", posted == [])


def test_pr_size_gate_disabled_when_zero():
    """max_pr_lines=0 disables the size gate entirely."""
    import tempfile

    disp = _load_dispatch()
    posted = []

    class FP(_FakeProvider):
        def board_configured(self):
            return True

        def board_numbers_with_statuses(self, names):
            return set()

        def list_issues(self, state="open", labels=None, limit=50):
            from core.providers.base import IssueSummary

            return [IssueSummary(number=5, title="big change")]

        def pr_state_for_issue(self, n):
            return "open"

        def pr_number_for_issue(self, n):
            return 99

        def pr_ci_green(self, pr):
            return False

        def get_pr_files(self, pr):
            return [{"filename": "big.py", "changes": 9999}]

        def post_pr_comment(self, pr, body):
            posted.append(pr)
            return True

        def post_issue_comment(self, n, body):
            return True

        def board_ensure_status_option(self, *a):
            return True

    with tempfile.TemporaryDirectory() as tmp:
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {5}
        disp.kanban.dispatch = lambda s, max_spawns=5: True
        disp.kanban.list_tasks = lambda *a, **k: []
        disp.run(
            {
                "repo": "O/R",
                "workdir": tmp,
                "name": "x",
                "issues": {"filters": {}},
                "execution": {"max_pr_lines": 0},
                "tracking": {"github_project_number": 1},
            },
            provider=FP(),
        )
        check("size gate disabled when max_pr_lines=0", posted == [])


# ── dispatch: forbidden file guard ───────────────────────────────────────────


def test_forbidden_file_guard_warns_once():
    """.env in PR files triggers a single warning comment."""
    import tempfile
    from core import dispatch_state as ds

    disp = _load_dispatch()
    posted = []

    class FP(_FakeProvider):
        def board_configured(self):
            return True

        def board_numbers_with_statuses(self, names):
            return set()

        def list_issues(self, state="open", labels=None, limit=50):
            from core.providers.base import IssueSummary

            return [IssueSummary(number=6, title="env change")]

        def pr_state_for_issue(self, n):
            return "open"

        def pr_number_for_issue(self, n):
            return 88

        def pr_ci_green(self, pr):
            return False

        def get_pr_files(self, pr):
            return [{"filename": ".env", "changes": 2}]

        def post_pr_comment(self, pr, body):
            posted.append((pr, body))
            return True

        def post_issue_comment(self, n, body):
            return True

        def board_ensure_status_option(self, *a):
            return True

    with tempfile.TemporaryDirectory() as tmp:
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {6}
        disp.kanban.dispatch = lambda s, max_spawns=5: True
        disp.kanban.list_tasks = lambda *a, **k: []
        disp.run(
            {
                "repo": "O/R",
                "workdir": tmp,
                "name": "x",
                "issues": {"filters": {}},
                "execution": {},
                "tracking": {"github_project_number": 1},
            },
            provider=FP(),
        )
        check(
            "forbidden guard posts warning for .env",
            len(posted) == 1 and ".env" in posted[0][1],
        )
        check(
            "forbidden guard sets forbidden_warned flag",
            ds.has_pr_flag(tmp, 88, "forbidden_warned"),
        )


def test_forbidden_file_guard_idempotent():
    """forbidden guard does NOT re-post when flag is already set."""
    import tempfile
    from core import dispatch_state as ds

    disp = _load_dispatch()
    posted = []

    class FP(_FakeProvider):
        def board_configured(self):
            return True

        def board_numbers_with_statuses(self, names):
            return set()

        def list_issues(self, state="open", labels=None, limit=50):
            from core.providers.base import IssueSummary

            return [IssueSummary(number=6, title="env change")]

        def pr_state_for_issue(self, n):
            return "open"

        def pr_number_for_issue(self, n):
            return 88

        def pr_ci_green(self, pr):
            return False

        def get_pr_files(self, pr):
            return [{"filename": ".env", "changes": 2}]

        def post_pr_comment(self, pr, body):
            posted.append(pr)
            return True

        def post_issue_comment(self, n, body):
            return True

        def board_ensure_status_option(self, *a):
            return True

    with tempfile.TemporaryDirectory() as tmp:
        ds.set_pr_flag(tmp, 88, "forbidden_warned")  # pre-seed
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {6}
        disp.kanban.dispatch = lambda s, max_spawns=5: True
        disp.kanban.list_tasks = lambda *a, **k: []
        disp.run(
            {
                "repo": "O/R",
                "workdir": tmp,
                "name": "x",
                "issues": {"filters": {}},
                "execution": {},
                "tracking": {"github_project_number": 1},
            },
            provider=FP(),
        )
        check("forbidden guard does not re-post when flag already set", posted == [])


# ── dispatch: staleness detection ────────────────────────────────────────────


def test_staleness_warns_when_over_threshold():
    """Issues dispatched > staleness_hours ago with no PR trigger a comment."""
    import tempfile
    import json
    import time
    from core import dispatch_state as ds

    disp = _load_dispatch()
    issue_comments = []

    class FP(_FakeProvider):
        def board_configured(self):
            return True

        def board_numbers_with_statuses(self, names):
            return set()

        def list_issues(self, state="open", labels=None, limit=50):
            from core.providers.base import IssueSummary

            return [IssueSummary(number=7, title="stale issue")]

        def pr_state_for_issue(self, n):
            return None

        def post_issue_comment(self, n, body):
            issue_comments.append((n, body))
            return True

    with tempfile.TemporaryDirectory() as tmp:
        # Seed state: dispatched 50 hours ago
        state_path = Path(tmp) / ".hermes" / "daedalus_dispatch_state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"issues": {"7": {"dispatched_at": time.time() - 50 * 3600}}})
        )
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {7}
        disp.kanban.dispatch = lambda s, max_spawns=5: True
        disp.kanban.list_tasks = lambda *a, **k: []
        disp.run(
            {
                "repo": "O/R",
                "workdir": tmp,
                "name": "x",
                "issues": {"filters": {}},
                "execution": {"staleness_hours": 48},
                "tracking": {"github_project_number": 1},
            },
            provider=FP(),
        )
        check(
            "staleness check posts issue comment for stale issue",
            len(issue_comments) == 1 and issue_comments[0][0] == 7,
        )
        check(
            "staleness warning mentions 'stale' or 'hours'",
            "hours" in issue_comments[0][1].lower()
            or "stale" in issue_comments[0][1].lower(),
        )
        check(
            "staleness sets stale_warned flag", ds.has_pr_flag(tmp, 7, "stale_warned")
        )


def test_staleness_silent_when_under_threshold():
    """Issues dispatched < staleness_hours ago do NOT trigger a comment."""
    import tempfile
    import json
    import time

    disp = _load_dispatch()
    issue_comments = []

    class FP(_FakeProvider):
        def board_configured(self):
            return True

        def board_numbers_with_statuses(self, names):
            return set()

        def list_issues(self, state="open", labels=None, limit=50):
            from core.providers.base import IssueSummary

            return [IssueSummary(number=8, title="fresh issue")]

        def pr_state_for_issue(self, n):
            return None

        def post_issue_comment(self, n, body):
            issue_comments.append((n, body))
            return True

    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / ".hermes" / "daedalus_dispatch_state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "issues": {"8": {"dispatched_at": time.time() - 2 * 3600}}  # 2h ago
                }
            )
        )
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {8}
        disp.kanban.dispatch = lambda s, max_spawns=5: True
        disp.kanban.list_tasks = lambda *a, **k: []
        disp.run(
            {
                "repo": "O/R",
                "workdir": tmp,
                "name": "x",
                "issues": {"filters": {}},
                "execution": {"staleness_hours": 48},
                "tracking": {"github_project_number": 1},
            },
            provider=FP(),
        )
        check("staleness check silent when issue is fresh", issue_comments == [])


# ── dispatch: label overrides ─────────────────────────────────────────────────


def test_label_overrides_skip_developer():
    """skip_developer=true removes the DEVELOPER role from the downstream body."""
    disp = _load_dispatch()
    body = disp._downstream_body(
        "O/R",
        {
            "number": 10,
            "title": "doc fix",
            "body": "desc",
            "labels": [{"name": "documentation"}],
        },
        3,
        "/tmp",
        "slack:C1",
        "main",
        "github",
        [],
        label_overrides={"documentation": {"skip_developer": True}},
    )
    check("skip_developer removes DEVELOPER role", "DEVELOPER" not in body)
    check("skip_developer keeps REVIEWER role", "REVIEWER" in body)


def test_label_overrides_security_first():
    """security_first=true puts SECURITY-ANALYST before DEVELOPER in the body."""
    disp = _load_dispatch()
    body = disp._downstream_body(
        "O/R",
        {
            "number": 11,
            "title": "auth change",
            "body": "desc",
            "labels": [{"name": "security"}],
        },
        3,
        "/tmp",
        "slack:C1",
        "main",
        "github",
        [],
        label_overrides={"security": {"security_first": True}},
    )
    sec_pos = body.find("SECURITY-ANALYST")
    dev_pos = body.find("DEVELOPER")
    check(
        "security_first puts SECURITY-ANALYST before DEVELOPER",
        sec_pos != -1 and dev_pos != -1 and sec_pos < dev_pos,
    )


# ── custom profiles ───────────────────────────────────────────────────────────


def test_resolve_profiles_defaults():
    """_resolve_profiles returns all defaults when no overrides supplied."""
    disp = _load_dispatch()
    p = disp._resolve_profiles({})
    check("defaults include validator", p["validator"] == "validator-daedalus")
    check("defaults include pm", p["pm"] == "project-manager-daedalus")
    check("defaults include developer", p["developer"] == "developer-daedalus")
    check("defaults include reviewer", p["reviewer"] == "reviewer-daedalus")
    check("defaults include security", p["security"] == "security-analyst-daedalus")
    check(
        "defaults include documentation", p["documentation"] == "documentation-daedalus"
    )


def test_resolve_profiles_user_overrides():
    """User-specified profiles override defaults; unspecified roles keep defaults."""
    disp = _load_dispatch()
    p = disp._resolve_profiles({"profiles": {"developer": "my-dev", "pm": "my-pm"}})
    check("user developer profile applied", p["developer"] == "my-dev")
    check("user pm profile applied", p["pm"] == "my-pm")
    check("unspecified validator keeps default", p["validator"] == "validator-daedalus")
    check("unspecified reviewer keeps default", p["reviewer"] == "reviewer-daedalus")


def test_resolve_profiles_ignores_unknown_keys():
    """Unknown role keys in execution.profiles are silently ignored."""
    disp = _load_dispatch()
    p = disp._resolve_profiles(
        {"profiles": {"developer": "my-dev", "nonexistent_role": "x"}}
    )
    check("known key applied", p["developer"] == "my-dev")
    check("unknown key not in result", "nonexistent_role" not in p)


def test_resolve_profiles_ignores_empty_strings():
    """Empty-string profile values are ignored (would create tasks with no assignee)."""
    disp = _load_dispatch()
    p = disp._resolve_profiles({"profiles": {"validator": "", "pm": "my-pm"}})
    check("empty string falls back to default", p["validator"] == "validator-daedalus")
    check("non-empty override applied", p["pm"] == "my-pm")


def test_custom_validator_profile_used_in_dispatch():
    """Validator task is created with the custom profile from execution.profiles."""
    from core.providers.base import IssueSummary

    disp = _load_dispatch()
    assigned = []

    class FP(_FakeProvider):
        def board_configured(self):
            return True

        def board_numbers_with_statuses(self, names):
            return {42}

        def list_issues(self, state="open", labels=None, limit=50):
            return [IssueSummary(number=42, title="bug", labels=[])]

        def pr_state_for_issue(self, n):
            return None

        def get_issue_state(self, n):
            return "open"

    disp.kanban.ensure_board = lambda s: None
    disp.kanban.list_blocked = lambda s: []
    disp.kanban.list_issue_numbers = lambda s: set()
    disp.kanban.dispatch = lambda s, max_spawns=5: True
    disp.kanban.list_tasks = lambda *a, **k: []

    _orig = disp.kanban.create_task
    # Pretend every configured profile exists so _validate_profiles doesn't
    # override the custom name with the built-in default.
    _orig_exists = disp._hermes_profile_exists
    try:
        disp._hermes_profile_exists = lambda name: True

        def fake_create(
            slug,
            title,
            body="",
            *,
            assignee="",
            idempotency_key="",
            workspace="",
            max_retries=None,
            skills=None,
            goal=False,
            goal_max_turns=None,
            parents=None,
        ):
            assigned.append(assignee)
            return "t1"

        disp.kanban.create_task = fake_create
        disp.run(
            {
                "repo": "O/R",
                "workdir": "/tmp",
                "name": "x",
                "issues": {"filters": {}},
                "execution": {"profiles": {"validator": "my-validator"}},
                "tracking": {"github_project_number": 1},
            },
            provider=FP(),
        )
    finally:
        disp.kanban.create_task = _orig
        disp._hermes_profile_exists = _orig_exists

    check("custom validator profile used for task creation", "my-validator" in assigned)


def test_custom_pm_profile_used_after_validator_confirms():
    """PM task is created with the custom pm profile when validator confirms."""
    disp = _load_dispatch()
    assigned = []

    disp.kanban.list_blocked = lambda s: []
    disp.kanban.ensure_board = lambda s: None
    disp.kanban.dispatch = lambda s, max_spawns=5: True

    # Validator is done and confirmed; no PM task yet
    def fake_list_tasks(slug, status=None):
        if status == "done":
            return [
                {
                    "title": "#7 some issue",
                    "assignee": "validator-daedalus",
                    "summary": "CONFIRMED: reproduced at main",
                    "status": "done",
                }
            ]
        return []

    _orig = disp.kanban.create_task
    try:
        disp.kanban.list_tasks = fake_list_tasks

        def fake_create(
            slug,
            title,
            body="",
            *,
            assignee="",
            idempotency_key="",
            workspace="",
            max_retries=None,
            skills=None,
            goal=False,
            goal_max_turns=None,
            parents=None,
        ):
            assigned.append(assignee)
            return "t2"

        disp.kanban.create_task = fake_create
        disp._check_confirmed_validators(
            "slug",
            "O/R",
            {7: {"number": 7, "title": "some issue", "body": ""}},
            3,
            "/tmp",
            "",
            "main",
            "github",
            profiles={
                "validator": "validator-daedalus",
                "pm": "my-pm",
                "developer": "developer-daedalus",
                "reviewer": "reviewer-daedalus",
                "security": "security-analyst-daedalus",
                "documentation": "documentation-daedalus",
            },
        )
    finally:
        disp.kanban.create_task = _orig

    check("custom pm profile used for PM task creation", "my-pm" in assigned)


# ── validator github comment fallback (issue #40) ─────────────────────────────


def test_validator_github_comment_outcome_confirmed():
    """Returns 'confirmed' when comment has Agent: validator header + CONFIRMED."""
    disp = _load_dispatch()

    class _FP:
        def get_issue_comments(self, n):
            return [
                {
                    "user": {"login": "benmarte"},
                    "body": "**Agent: validator**\n\nCONFIRMED — issue is real.",
                }
            ]

    result = disp._validator_github_comment_outcome(_FP(), 42)
    check("confirmed detected from github comment", result == "confirmed")


def test_validator_github_comment_outcome_no_match():
    """Returns '' when no comment has the Agent: validator attribution header."""
    disp = _load_dispatch()

    class _FP:
        def get_issue_comments(self, n):
            return [
                {
                    "user": {"login": "some-other-user"},
                    "body": "Just a regular comment.",
                }
            ]

    result = disp._validator_github_comment_outcome(_FP(), 42)
    check("no match returns empty string", result == "")


def test_validator_github_comment_outcome_none_provider():
    """Returns '' safely when provider is None."""
    disp = _load_dispatch()
    result = disp._validator_github_comment_outcome(None, 42)
    check("none provider returns empty string", result == "")


def test_check_confirmed_validators_github_comment_fallback_advances_to_pm():
    """None-summary validator with CONFIRMED github comment → creates PM task, no retry."""
    disp = _load_dispatch()
    created_titles = []
    created_keys = []

    def fake_list_tasks(slug, status=None):
        if status == "done":
            return [
                {
                    "title": "#42 fix some bug",
                    "assignee": "validator-daedalus",
                    "summary": None,
                    "status": "done",
                    "id": "t_v42",
                }
            ]
        return []

    class _FP:
        name = "github"

        def get_issue_comments(self, n):
            return [
                {
                    "user": {"login": "validator-daedalus"},
                    "body": "**Agent: validator**\nCONFIRMED — issue is real and safe.",
                }
            ]

    _orig_create = disp.kanban.create_task
    _orig_show = disp.kanban.show_card
    try:
        disp.kanban.list_tasks = fake_list_tasks
        disp.kanban.show_card = lambda s, tid: {"latest_summary": None}

        def fake_create(slug, title, *, assignee="", idempotency_key="", **kw):
            created_titles.append(title)
            created_keys.append(idempotency_key)
            return "t_pm_new"

        disp.kanban.create_task = fake_create
        disp._check_confirmed_validators(
            "slug",
            "O/R",
            {42: {"number": 42, "title": "fix some bug", "body": ""}},
            3,
            "/tmp",
            "",
            "main",
            "github",
            provider=_FP(),
        )
    finally:
        disp.kanban.create_task = _orig_create
        disp.kanban.show_card = _orig_show

    check("PM task created (not validator retry)", any("pm" in k for k in created_keys))
    check(
        "no validator retry task created",
        not any(
            "validator" in t.lower() for t in created_titles if "validate" in t.lower()
        ),
    )


def test_check_confirmed_validators_none_summary_retries_when_no_github_comment():
    """None-summary validator with no CONFIRMED github comment → creates retry validator."""
    disp = _load_dispatch()
    created_keys = []

    def fake_list_tasks(slug, status=None):
        if status == "done":
            return [
                {
                    "title": "#43 fix bug",
                    "assignee": "validator-daedalus",
                    "summary": None,
                    "status": "done",
                    "id": "t_v43",
                }
            ]
        return [
            {
                "title": "#43 fix bug",
                "assignee": "validator-daedalus",
                "summary": None,
                "status": "done",
                "id": "t_v43",
            }
        ]

    class _FP:
        name = "github"

        def get_issue_comments(self, n):
            return []  # no validator comment

    _orig_create = disp.kanban.create_task
    _orig_show = disp.kanban.show_card
    try:
        disp.kanban.list_tasks = fake_list_tasks
        disp.kanban.show_card = lambda s, tid: {"latest_summary": None}

        def fake_create(slug, title, *, assignee="", idempotency_key="", **kw):
            created_keys.append(idempotency_key)
            return "t_retry"

        disp.kanban.create_task = fake_create
        disp._check_confirmed_validators(
            "slug",
            "O/R",
            {43: {"number": 43, "title": "fix bug", "body": ""}},
            3,
            "/tmp",
            "",
            "main",
            "github",
            provider=_FP(),
        )
    finally:
        disp.kanban.create_task = _orig_create
        disp.kanban.show_card = _orig_show

    check("validator retry created", any("validator-retry" in k for k in created_keys))


# ── profile validation (issue #16) ────────────────────────────────────────────


def test_hermes_profile_exists_directory():
    """_hermes_profile_exists returns True for a directory-based profile."""
    from pathlib import Path
    import tempfile

    disp = _load_dispatch()
    with tempfile.TemporaryDirectory() as tmpdir:
        profile_dir = Path(tmpdir) / ".hermes" / "profiles" / "my-profile"
        profile_dir.mkdir(parents=True)
        with mock.patch.object(disp.Path, "home", return_value=Path(tmpdir)):
            check(
                "directory profile exists",
                disp._hermes_profile_exists("my-profile") is True,
            )
            check(
                "non-existent profile returns False",
                disp._hermes_profile_exists("no-such") is False,
            )


def test_hermes_profile_exists_yaml():
    """_hermes_profile_exists returns True for a single-file YAML profile."""
    from pathlib import Path
    import tempfile

    disp = _load_dispatch()
    with tempfile.TemporaryDirectory() as tmpdir:
        profiles_dir = Path(tmpdir) / ".hermes" / "profiles"
        profiles_dir.mkdir(parents=True)
        (profiles_dir / "yaml-profile.yaml").write_text("name: yaml-profile\n")
        with mock.patch.object(disp.Path, "home", return_value=Path(tmpdir)):
            check(
                "yaml profile exists",
                disp._hermes_profile_exists("yaml-profile") is True,
            )


def test_validate_profiles_all_present_no_warning():
    """When every profile exists, _validate_profiles returns the input unchanged and logs nothing."""
    disp = _load_dispatch()
    profiles = {
        "validator": "validator-daedalus",
        "pm": "project-manager-daedalus",
        "developer": "developer-daedalus",
        "reviewer": "reviewer-daedalus",
        "security": "security-analyst-daedalus",
        "documentation": "documentation-daedalus",
    }
    with mock.patch.object(disp, "_hermes_profile_exists", return_value=True):
        with mock.patch.object(disp.logger, "warning") as warn:
            result = disp._validate_profiles(profiles)
    check("all-present returns original map", result == profiles)
    check("no warning logged when all profiles exist", warn.call_count == 0)


def test_validate_profiles_missing_warns_with_role_and_name():
    """A missing profile logs a warning naming the role AND the missing profile name."""
    disp = _load_dispatch()
    profiles = {"validator": "validator-daedalus", "developer": "does-not-exist"}

    # validator exists, developer does not
    def exists(name):
        return name != "does-not-exist"

    with mock.patch.object(disp, "_hermes_profile_exists", side_effect=exists):
        with mock.patch.object(disp.logger, "warning") as warn:
            result = disp._validate_profiles(profiles)
    # Warning should mention both the role ('developer') and the profile name ('does-not-exist')
    calls_as_text = " ".join(str(c) for c in warn.call_args_list)
    check(
        "warning mentions the missing profile name",
        "'does-not-exist'" in calls_as_text or "does-not-exist" in calls_as_text,
    )
    check(
        "warning mentions the role",
        "'developer'" in calls_as_text or "developer" in calls_as_text,
    )
    # Fallback behavior: developer should be replaced with the built-in default
    check(
        "missing profile falls back to default",
        result["developer"] == disp._DEFAULT_PROFILES["developer"],
    )
    # Existing profiles are untouched
    check("existing profile unchanged", result["validator"] == "validator-daedalus")


def test_validate_profiles_skip_behavior_drops_missing():
    """With fallback_behavior='skip', missing profiles are dropped from the map."""
    disp = _load_dispatch()
    profiles = {"validator": "validator-daedalus", "developer": "does-not-exist"}

    def exists(name):
        return name != "does-not-exist"

    with mock.patch.object(disp, "_hermes_profile_exists", side_effect=exists):
        with mock.patch.object(disp.logger, "warning") as warn:
            result = disp._validate_profiles(profiles, fallback_behavior="skip")
    check("skip behavior drops missing role", "developer" not in result)
    check(
        "skip behavior keeps existing role", result["validator"] == "validator-daedalus"
    )
    check(
        "skip warning mentions skipping",
        "skipping" in str(warn.call_args_list).lower()
        or "Skipping" in str(warn.call_args_list),
    )


def test_validate_profiles_fallback_explicit():
    """With fallback_behavior='fallback' (default), missing roles fall back to built-ins."""
    disp = _load_dispatch()
    profiles = {"validator": "bad-name", "pm": "project-manager-daedalus"}

    def exists(name):
        return name != "bad-name"

    with mock.patch.object(disp, "_hermes_profile_exists", side_effect=exists):
        result = disp._validate_profiles(profiles, fallback_behavior="fallback")
    check(
        "explicit fallback replaces with default",
        result["validator"] == disp._DEFAULT_PROFILES["validator"],
    )
    check("non-missing profile kept as-is", result["pm"] == "project-manager-daedalus")


def test_profile_validation_runs_once_per_dispatch_tick():
    """_validate_profiles is called exactly once per run() invocation (not per issue)."""
    from core.providers.base import IssueSummary

    disp = _load_dispatch()

    call_count = {"n": 0}
    orig_validate = disp._validate_profiles

    def counting_validate(profiles, **kw):
        call_count["n"] += 1
        return orig_validate(profiles, **kw)

    class FP(_FakeProvider):
        def board_configured(self):
            return True

        def board_numbers_with_statuses(self, names):
            return {1, 2, 3}

        def list_issues(self, state="open", labels=None, limit=50):
            return [
                IssueSummary(number=n, title=f"issue {n}", labels=[]) for n in (1, 2, 3)
            ]

        def pr_state_for_issue(self, n):
            return None

        def get_issue_state(self, n):
            return "open"

    disp.kanban.ensure_board = lambda s: None
    disp.kanban.list_blocked = lambda s: []
    disp.kanban.list_issue_numbers = lambda s: set()
    disp.kanban.dispatch = lambda s, max_spawns=5: True
    disp.kanban.list_tasks = lambda *a, **k: []
    disp._fetch_issues = lambda r, f: [
        {"number": n, "title": f"issue {n}"} for n in (1, 2, 3)
    ]
    _orig_create = disp.kanban.create_task
    try:
        disp.kanban.create_task = lambda *a, **k: "t_x"
        disp._validate_profiles = counting_validate
        disp.run(
            {
                "repo": "O/R",
                "workdir": "/tmp",
                "name": "x",
                "tracking": {"github_project_number": 1},
                "issues": {"filters": {}},
                "execution": {},
            },
            provider=FP(),
        )
    finally:
        disp.kanban.create_task = _orig_create
        disp._validate_profiles = orig_validate

    check("validate_profiles called exactly once per tick", call_count["n"] == 1)


# ── PM consultation (team blocker re-activation) ──────────────────────────────


def test_has_active_pm_consultation_true():
    """_has_active_pm_consultation returns True when an open consult task exists."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: [
        {
            "title": "consult: #5 login bug",
            "assignee": "project-manager-daedalus",
            "status": "in_progress",
        },
    ]
    check(
        "active consult detected", disp._has_active_pm_consultation("slug", 5) is True
    )


def test_has_active_pm_consultation_false_when_done():
    """_has_active_pm_consultation returns False when the consult task is done."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: [
        {"title": "consult: #5 login bug", "assignee": "pm-daedalus", "status": "done"},
    ]
    check(
        "done consult not counted as active",
        disp._has_active_pm_consultation("slug", 5) is False,
    )


def test_has_active_pm_consultation_false_for_spec_task():
    """_has_active_pm_consultation ignores regular PM spec tasks."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: [
        {"title": "#5 login bug", "assignee": "pm-daedalus", "status": "in_progress"},
    ]
    check(
        "spec task not counted as consult",
        disp._has_active_pm_consultation("slug", 5) is False,
    )


def test_check_team_blockers_creates_pm_consultation():
    """_check_team_blockers creates a PM consultation for a blocked team card."""
    disp = _load_dispatch()
    created_titles = []
    assigned_to = []

    _summary = "BLOCKED: cannot resolve import path"
    disp.kanban.list_blocked = lambda s: [
        {
            "id": "t_dev01",
            "title": "#9 feature",
            "assignee": "developer-daedalus",
            "summary": _summary,
        },
    ]
    disp.kanban.get_latest_summary = lambda s, t: _summary

    def fake_list_tasks(s):
        return []  # no active consultation

    _orig = disp.kanban.create_task
    try:
        disp.kanban.list_tasks = fake_list_tasks

        def fake_create(
            slug,
            title,
            body="",
            *,
            assignee="",
            idempotency_key="",
            workspace="",
            max_retries=None,
            skills=None,
            goal=False,
            goal_max_turns=None,
            parents=None,
        ):
            created_titles.append(title)
            assigned_to.append(assignee)
            return "consult_t1"

        disp.kanban.create_task = fake_create
        triggered = disp._check_team_blockers(
            "slug",
            "O/R",
            {9: {"number": 9, "title": "feature", "body": "desc"}},
            "/tmp",
            "main",
            "github",
        )
    finally:
        disp.kanban.create_task = _orig

    check("blocker triggers PM consultation", 9 in triggered)
    check(
        "consultation task title starts with consult:",
        any(t.lower().startswith("consult:") for t in created_titles),
    )
    check(
        "consultation assigned to default pm profile",
        assigned_to and assigned_to[0] in ("pm-daedalus", "project-manager-daedalus"),
    )


def test_check_team_blockers_skips_when_consult_active():
    """_check_team_blockers skips creation if an active consultation already exists."""
    disp = _load_dispatch()
    created = []

    _summary = "BLOCKED: still stuck"
    disp.kanban.list_blocked = lambda s: [
        {
            "id": "t_dev02",
            "title": "#9 feature",
            "assignee": "developer-daedalus",
            "summary": _summary,
        },
    ]
    disp.kanban.get_latest_summary = lambda s, t: _summary
    # Active consultation already open
    disp.kanban.list_tasks = lambda s: [
        {
            "title": "consult: #9 feature",
            "assignee": "project-manager-daedalus",
            "status": "in_progress",
        },
    ]

    _orig = disp.kanban.create_task
    try:
        disp.kanban.create_task = lambda *a, **k: created.append(1) or "t"
        triggered = disp._check_team_blockers(
            "slug",
            "O/R",
            {9: {"number": 9, "title": "feature", "body": "desc"}},
            "/tmp",
            "main",
            "github",
        )
    finally:
        disp.kanban.create_task = _orig

    check("no duplicate consultation when one is active", triggered == [])
    check("no task created", created == [])


def test_check_team_blockers_skips_escalation():
    """_check_team_blockers ignores cards with ESCALATE: summaries (security blocks)."""
    disp = _load_dispatch()
    created = []

    _summary = "ESCALATE: security threat detected"
    disp.kanban.list_blocked = lambda s: [
        {
            "id": "t_dev03",
            "title": "#9 feature",
            "assignee": "developer-daedalus",
            "summary": _summary,
        },
    ]
    disp.kanban.get_latest_summary = lambda s, t: _summary
    disp.kanban.list_tasks = lambda s: []

    _orig = disp.kanban.create_task
    try:
        disp.kanban.create_task = lambda *a, **k: created.append(1) or "t"
        triggered = disp._check_team_blockers(
            "slug",
            "O/R",
            {9: {"number": 9, "title": "feature", "body": "desc"}},
            "/tmp",
            "main",
            "github",
        )
    finally:
        disp.kanban.create_task = _orig

    check("ESCALATE: blocks not treated as PM blockers", triggered == [])
    check("no consultation created for security escalation", created == [])


def test_check_team_blockers_skips_validator_cards():
    """_check_team_blockers ignores cards assigned to validator or PM profiles."""
    disp = _load_dispatch()
    created = []

    disp.kanban.list_blocked = lambda s: [
        {
            "title": "#3 issue",
            "assignee": "validator-daedalus",
            "summary": "BLOCKED: needs more info",
        },
        {
            "title": "#3 issue",
            "assignee": "project-manager-daedalus",
            "summary": "BLOCKED: unclear scope",
        },
    ]
    disp.kanban.list_tasks = lambda s: []

    _orig = disp.kanban.create_task
    try:
        disp.kanban.create_task = lambda *a, **k: created.append(1) or "t"
        triggered = disp._check_team_blockers(
            "slug",
            "O/R",
            {3: {"number": 3, "title": "issue", "body": ""}},
            "/tmp",
            "main",
            "github",
        )
    finally:
        disp.kanban.create_task = _orig

    check("validator blocks not treated as PM blockers", triggered == [])
    check("PM blocks not treated as PM blockers (already PM)", created == [])


def test_pm_consultation_body_content():
    """_pm_consultation_body includes blocker info and CLARIFIED: instructions."""
    disp = _load_dispatch()
    body = disp._pm_consultation_body(
        "O/R",
        {"number": 12, "title": "login bug"},
        "BLOCKED: can't find auth middleware",
        "/tmp",
        "github",
    )
    check("consultation body mentions TEAM BLOCKER", "TEAM BLOCKER" in body)
    check(
        "consultation body includes the blocker summary",
        "can't find auth middleware" in body,
    )
    check("consultation body requires CLARIFIED: prefix", "CLARIFIED: " in body)
    check("consultation body forbids writing code", "DO NOT write code" in body)


def test_has_pm_tasks_excludes_consultations():
    """_has_pm_tasks returns False for consultation tasks (title starts with consult:)."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: [
        {
            "title": "consult: #5 login bug",
            "assignee": "pm-daedalus",
            "status": "in_progress",
        },
    ]
    check(
        "consultation task not counted as PM spec task",
        disp._has_pm_tasks("slug", 5) is False,
    )


def test_has_pm_tasks_true_for_spec_task():
    """_has_pm_tasks returns True for a PM spec task (no consult: prefix)."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: [
        {
            "title": "#5 login bug",
            "assignee": "project-manager-daedalus",
            "status": "in_progress",
        },
    ]
    check(
        "spec task correctly detected as PM task", disp._has_pm_tasks("slug", 5) is True
    )


def test_pm_task_state_none():
    """_pm_task_state returns ('none', 0) when no PM spec task exists."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: []
    state, stale = disp._pm_task_state("slug", 5)
    check("no tasks → state is none", state == "none")
    check("no tasks → stale_count is 0", stale == 0)


def test_pm_task_state_running():
    """_pm_task_state returns ('running', 0) for an in-progress PM task."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: [
        {
            "title": "#5 login bug",
            "assignee": "project-manager-daedalus",
            "status": "in_progress",
        },
    ]
    state, stale = disp._pm_task_state("slug", 5)
    check("in-progress PM → state is running", state == "running")
    check("in-progress PM → stale_count is 0", stale == 0)


def test_pm_task_state_complete():
    """_pm_task_state returns ('complete', 0) for a done PM task with SPEC: summary."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: [
        {
            "title": "#5 login bug",
            "assignee": "project-manager-daedalus",
            "status": "done",
            "summary": "SPEC: Add auth middleware",
        },
    ]
    state, stale = disp._pm_task_state("slug", 5)
    check("done+SPEC PM → state is complete", state == "complete")
    check("done+SPEC PM → stale_count is 0", stale == 0)


def test_pm_task_state_stale():
    """_pm_task_state returns ('stale', 1) for a done PM task with no SPEC: summary (premature completion)."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: [
        {
            "title": "#5 login bug",
            "assignee": "project-manager-daedalus",
            "id": "t_stale",
            "status": "done",
            "summary": "",
        },
    ]
    disp.kanban.show_card = lambda s, tid: {"latest_summary": ""}
    state, stale = disp._pm_task_state("slug", 5)
    check("done+no-SPEC PM → state is stale", state == "stale")
    check("done+no-SPEC PM → stale_count is 1", stale == 1)


def test_check_confirmed_validators_retries_stale_pm():
    """_check_confirmed_validators re-creates PM task with retry key when existing task is stale."""
    disp = _load_dispatch()
    created_keys = []

    def fake_list_tasks(slug, status=None):
        if status == "done":
            # Validator confirmed AND a stale PM task (no SPEC:) exist
            return [
                {
                    "title": "#9 crash bug",
                    "assignee": "validator-daedalus",
                    "summary": "CONFIRMED: verified",
                    "status": "done",
                    "id": "t_v",
                },
                {
                    "title": "#9 crash bug",
                    "assignee": "project-manager-daedalus",
                    "summary": "",
                    "status": "done",
                    "id": "t_stale",
                },
            ]
        # Non-filtered list call used by _pm_task_state
        return [
            {
                "title": "#9 crash bug",
                "assignee": "project-manager-daedalus",
                "summary": "",
                "status": "done",
                "id": "t_stale",
            },
        ]

    _orig_create = disp.kanban.create_task
    _orig_show = disp.kanban.show_card
    try:
        disp.kanban.list_tasks = fake_list_tasks
        disp.kanban.show_card = lambda s, tid: {"latest_summary": ""}
        disp.kanban.create_task = (
            lambda slug, title, *, assignee="", idempotency_key="", **kw: (
                created_keys.append(idempotency_key) or "t_new"
            )
        )
        disp._check_confirmed_validators(
            "slug",
            "O/R",
            {9: {"number": 9, "title": "crash bug", "body": ""}},
            3,
            "/tmp",
            "",
            "main",
            "github",
        )
    finally:
        disp.kanban.create_task = _orig_create
        disp.kanban.show_card = _orig_show

    check("retry key used instead of pm-9", any("pm-9-r" in k for k in created_keys))
    check("original pm-9 key not reused", "pm-9" not in created_keys)


# ── validator empty/None summary — warn + notify instead of silent drop (#1099) ─


def test_validator_empty_summary_logs_retry_warning():
    """Empty-string summary + resolvable issue → warning logged + retry validator created."""
    disp = _load_dispatch()
    created_keys = []

    def fake_list_tasks(slug, status=None):
        card = {
            "title": "#55 login bug",
            "assignee": "validator-daedalus",
            "summary": "",
            "status": "done",
            "id": "t_v55",
        }
        return [card]

    class _FP:
        name = "github"

        def get_issue_comments(self, n):
            return []

        def get_issue(self, n):
            return None

    _orig_create = disp.kanban.create_task
    _orig_show = disp.kanban.show_card
    try:
        disp.kanban.list_tasks = fake_list_tasks
        disp.kanban.show_card = lambda s, tid: {"latest_summary": ""}

        def fake_create(slug, title, *, assignee="", idempotency_key="", **kw):
            created_keys.append(idempotency_key)
            return "t_retry"

        disp.kanban.create_task = fake_create
        with mock.patch.object(disp.logger, "warning") as warn:
            disp._check_confirmed_validators(
                "slug",
                "O/R",
                {55: {"number": 55, "title": "login bug", "body": ""}},
                3,
                "/tmp",
                "",
                "main",
                "github",
                provider=_FP(),
            )
        warn_text = " ".join(str(c) for c in warn.call_args_list)
        check(
            "warning mentions 'completed with no summary'",
            "completed with no summary" in warn_text,
        )
        check("warning mentions 'scheduling retry'", "scheduling retry" in warn_text)
    finally:
        disp.kanban.create_task = _orig_create
        disp.kanban.show_card = _orig_show

    check(
        "retry validator task created",
        any("validator-retry" in k for k in created_keys),
    )


def test_validator_none_summary_logs_retry_warning():
    """None summary (stored in kanban) + resolvable issue → warning logged + retry created."""
    disp = _load_dispatch()
    created_keys = []

    def fake_list_tasks(slug, status=None):
        card = {
            "title": "#56 crash bug",
            "assignee": "validator-daedalus",
            "summary": None,
            "status": "done",
            "id": "t_v56",
        }
        return [card]

    class _FP:
        name = "github"

        def get_issue_comments(self, n):
            return []

        def get_issue(self, n):
            return None

    _orig_create = disp.kanban.create_task
    _orig_show = disp.kanban.show_card
    try:
        disp.kanban.list_tasks = fake_list_tasks
        disp.kanban.show_card = lambda s, tid: {"latest_summary": None}

        def fake_create(slug, title, *, assignee="", idempotency_key="", **kw):
            created_keys.append(idempotency_key)
            return "t_retry"

        disp.kanban.create_task = fake_create
        with mock.patch.object(disp.logger, "warning") as warn:
            disp._check_confirmed_validators(
                "slug",
                "O/R",
                {56: {"number": 56, "title": "crash bug", "body": ""}},
                3,
                "/tmp",
                "",
                "main",
                "github",
                provider=_FP(),
            )
        warn_text = " ".join(str(c) for c in warn.call_args_list)
        check(
            "None-summary warning mentions 'completed with no summary'",
            "completed with no summary" in warn_text,
        )
        check(
            "None-summary warning mentions 'scheduling retry'",
            "scheduling retry" in warn_text,
        )
    finally:
        disp.kanban.create_task = _orig_create
        disp.kanban.show_card = _orig_show

    check(
        "None-summary retry validator task created",
        any("validator-retry" in k for k in created_keys),
    )


def test_validator_empty_summary_unresolvable_issue_no_silent_drop():
    """Empty summary + unresolvable issue → warning logged + retry-cap notification sent."""
    disp = _load_dispatch()
    cap_notifications = []

    def fake_list_tasks(slug, status=None):
        card = {
            "title": "#57 missing issue",
            "assignee": "validator-daedalus",
            "summary": "",
            "status": "done",
            "id": "t_v57",
        }
        return [card]

    class _FP:
        name = "github"

        def get_issue_comments(self, n):
            return []

        def get_issue(self, n):
            return None  # unresolvable

    _orig_create = disp.kanban.create_task
    _orig_show = disp.kanban.show_card
    _orig_send = disp._send_retry_cap_notification
    _orig_has = disp._has_notified_block
    _orig_mark = disp._mark_notified_block

    resolved = {"cron": {"notify_target": "slack:test"}}

    try:
        disp.kanban.list_tasks = fake_list_tasks
        disp.kanban.show_card = lambda s, tid: {"latest_summary": ""}
        # Ensure we don't create any tasks
        disp.kanban.create_task = (
            lambda slug, title, *, assignee="", idempotency_key="", **kw: None
        )
        # Capture retry-cap notification calls
        disp._send_retry_cap_notification = lambda **kw: cap_notifications.append(kw)
        disp._has_notified_block = lambda *a, **kw: False
        disp._mark_notified_block = lambda *a, **kw: None

        with mock.patch.object(disp.logger, "warning") as warn:
            disp._check_confirmed_validators(
                "slug",
                "O/R",
                {},  # empty issues_map — unresolvable
                3,
                "/tmp",
                "",
                "main",
                "github",
                provider=_FP(),
                resolved=resolved,
            )
        warn_text = " ".join(str(c) for c in warn.call_args_list)
        check(
            "unresolvable issue warning mentions 'completed with no summary'",
            "completed with no summary" in warn_text,
        )
        check(
            "unresolvable issue warning mentions 'unresolvable'",
            "unresolvable" in warn_text,
        )
        check(
            "retry-cap notification sent for unresolvable issue",
            len(cap_notifications) >= 1,
        )
    finally:
        disp.kanban.create_task = _orig_create
        disp.kanban.show_card = _orig_show
        disp._send_retry_cap_notification = _orig_send
        disp._has_notified_block = _orig_has
        disp._mark_notified_block = _orig_mark


def test_validator_cap_exhausted_with_unresolvable_issue():
    """absolute_max+1 empty-summary validator cards + unresolvable issue → notification fires, no new card."""
    disp = _load_dispatch()
    created_keys = []
    cap_notifications = []

    # absolute_max = max(max_validator_retries * 3, max_validator_retries + 3)
    # default max_validator_retries = 3 → absolute_max = max(9, 6) = 9
    # We seed 10 done validator cards (absolute_max + 1) to trigger exhaustion
    num_cards = 10

    def fake_list_tasks(slug, status=None):
        cards = [
            {
                "title": "#58 ghost issue",
                "assignee": "validator-daedalus",
                "summary": "",
                "status": "done",
                "id": f"t_v58_{i}",
            }
            for i in range(num_cards)
        ]
        return cards

    class _FP:
        name = "github"

        def get_issue_comments(self, n):
            return []

        def get_issue(self, n):
            return None  # unresolvable

    _orig_create = disp.kanban.create_task
    _orig_show = disp.kanban.show_card
    _orig_send = disp._send_retry_cap_notification
    _orig_has = disp._has_notified_block
    _orig_mark = disp._mark_notified_block

    resolved = {"cron": {"notify_target": "slack:test"}}

    try:
        disp.kanban.list_tasks = fake_list_tasks
        disp.kanban.show_card = lambda s, tid: {"latest_summary": ""}

        def fake_create(slug, title, *, assignee="", idempotency_key="", **kw):
            created_keys.append(idempotency_key)
            return "t_retry"

        disp.kanban.create_task = fake_create
        disp._send_retry_cap_notification = lambda **kw: cap_notifications.append(kw)
        disp._has_notified_block = lambda *a, **kw: False
        disp._mark_notified_block = lambda *a, **kw: None

        disp._check_confirmed_validators(
            "slug",
            "O/R",
            {},  # empty issues_map — unresolvable
            3,
            "/tmp",
            "",
            "main",
            "github",
            provider=_FP(),
            resolved=resolved,
        )
    finally:
        disp.kanban.create_task = _orig_create
        disp.kanban.show_card = _orig_show
        disp._send_retry_cap_notification = _orig_send
        disp._has_notified_block = _orig_has
        disp._mark_notified_block = _orig_mark

    check(
        "no new validator task created at cap exhaustion",
        not any("validator-retry" in k for k in created_keys),
    )
    check(
        "retry-cap notification fires for unresolvable cap-exhausted issue",
        len(cap_notifications) >= 1,
    )


# ── _check_completed_pm ───────────────────────────────────────────────────────


def _completed_pm_tasks(issue_number, summary="SPEC: build the thing", title=None):
    """Minimal done PM task fixture for _check_completed_pm tests."""
    return [
        {
            "id": f"t_pm_{issue_number}",
            "assignee": "project-manager-daedalus",
            "status": "done",
            "title": title or f"#{issue_number} some feature",
            "summary": summary,
        }
    ]


def test_check_completed_pm_creates_team_tasks():
    """Normal path: PM done with SPEC: in issues_map → create_task called for each role."""
    disp = _load_dispatch()
    created = []
    orig_create_task = disp.kanban.create_task
    orig_list_tasks = disp.kanban.list_tasks
    try:
        disp.kanban.list_tasks = lambda slug, status=None: (
            _completed_pm_tasks(5) if status == "done" else []
        )
        disp.kanban.create_task = (
            lambda slug, title, *, assignee="", idempotency_key="", **kw: (
                created.append({"title": title, "idempotency_key": idempotency_key})
                or "t_task"
            )
        )
        result = disp._check_completed_pm(
            "slug",
            "O/R",
            {5: {"number": 5, "title": "feature", "body": ""}},
            3,
            "/tmp",
            "",
            "main",
            "github",
        )
    finally:
        disp.kanban.list_tasks = orig_list_tasks
        disp.kanban.create_task = orig_create_task

    check("check_completed_pm returns triggered list", result == [5])
    titles = [c["title"] for c in created]
    check(
        "check_completed_pm creates developer task", "#5 Developer: feature" in titles
    )
    check("check_completed_pm creates qa task", "#5 QA: feature" in titles)
    check("check_completed_pm creates reviewer task", "#5 Reviewer: feature" in titles)
    check("check_completed_pm creates security task", "#5 Security: feature" in titles)
    check("check_completed_pm creates docs task", "#5 Docs: feature" in titles)


def test_check_completed_pm_provider_fallback():
    """Issue not in issues_map → provider.get_issue() called and role tasks created."""
    from core.providers.base import IssueSummary

    disp = _load_dispatch()
    created = []
    fetched = []
    orig_create_task = disp.kanban.create_task
    orig_list_tasks = disp.kanban.list_tasks

    class _Provider:
        def get_issue(self, n):
            fetched.append(n)
            return IssueSummary(number=n, title="feature from provider", body="")

    try:
        disp.kanban.list_tasks = lambda slug, status=None: (
            _completed_pm_tasks(6) if status == "done" else []
        )
        disp.kanban.create_task = (
            lambda slug, title, *, assignee="", idempotency_key="", **kw: (
                created.append({"title": title, "idempotency_key": idempotency_key})
                or "t_task"
            )
        )
        result = disp._check_completed_pm(
            "slug",
            "O/R",
            {},  # empty issues_map — forces fallback
            3,
            "/tmp",
            "",
            "main",
            "github",
            provider=_Provider(),
        )
    finally:
        disp.kanban.list_tasks = orig_list_tasks
        disp.kanban.create_task = orig_create_task

    check("provider fallback: get_issue called", fetched == [6])
    check("provider fallback: team tasks created", result == [6])
    titles = [c["title"] for c in created]
    check(
        "provider fallback: creates developer task",
        "#6 Developer: feature from provider" in titles,
    )
    check(
        "provider fallback: creates qa task", "#6 QA: feature from provider" in titles
    )


def test_check_completed_pm_no_issue_found():
    """Issue not in issues_map AND provider returns None → skipped, no create_triage."""
    disp = _load_dispatch()
    created = []
    orig_create_triage = disp.kanban.create_triage
    orig_list_tasks = disp.kanban.list_tasks

    class _Provider:
        def get_issue(self, n):
            return None

    try:
        disp.kanban.list_tasks = lambda slug, status=None: (
            _completed_pm_tasks(7) if status == "done" else []
        )
        disp.kanban.create_triage = lambda slug, n, title, body, **kw: (
            created.append(n) or "t_triage"
        )
        result = disp._check_completed_pm(
            "slug",
            "O/R",
            {},
            3,
            "/tmp",
            "",
            "main",
            "github",
            provider=_Provider(),
        )
    finally:
        disp.kanban.list_tasks = orig_list_tasks
        disp.kanban.create_triage = orig_create_triage

    check("no issue: nothing triggered", result == [])
    check("no issue: create_triage never called", created == [])


def test_check_completed_pm_idempotent():
    """Downstream tasks already exist → create_triage not called again."""
    disp = _load_dispatch()
    created = []
    orig_create_triage = disp.kanban.create_triage
    orig_list_tasks = disp.kanban.list_tasks

    def fake_list_tasks(slug, status=None):
        if status == "done":
            return _completed_pm_tasks(8)
        # Simulate existing developer task (downstream) for #8
        return [
            {
                "title": "#8 feature",
                "assignee": "developer-daedalus",
                "status": "running",
            }
        ]

    try:
        disp.kanban.list_tasks = fake_list_tasks
        disp.kanban.create_triage = lambda slug, n, title, body, **kw: (
            created.append(n) or "t_triage"
        )
        result = disp._check_completed_pm(
            "slug",
            "O/R",
            {8: {"number": 8, "title": "feature", "body": ""}},
            3,
            "/tmp",
            "",
            "main",
            "github",
        )
    finally:
        disp.kanban.list_tasks = orig_list_tasks
        disp.kanban.create_triage = orig_create_triage

    check("idempotent: already has downstream, nothing triggered", result == [])
    check("idempotent: create_triage never called", created == [])


def test_check_completed_pm_skips_consultation():
    """PM task with title starting 'consult:' must not trigger team tasks."""
    disp = _load_dispatch()
    created = []
    orig_create_triage = disp.kanban.create_triage
    orig_list_tasks = disp.kanban.list_tasks
    try:
        disp.kanban.list_tasks = lambda slug, status=None: (
            _completed_pm_tasks(9, title="consult: #9 blocker question")
            if status == "done"
            else []
        )
        disp.kanban.create_triage = lambda slug, n, title, body, **kw: (
            created.append(n) or "t_triage"
        )
        result = disp._check_completed_pm(
            "slug",
            "O/R",
            {9: {"number": 9, "title": "feature", "body": ""}},
            3,
            "/tmp",
            "",
            "main",
            "github",
        )
    finally:
        disp.kanban.list_tasks = orig_list_tasks
        disp.kanban.create_triage = orig_create_triage

    check("consultation skipped: nothing triggered", result == [])
    check("consultation skipped: create_triage never called", created == [])


def test_pipeline_chain_confirmed_to_team_tasks():
    """Integration: validator CONFIRMED → PM SPEC: done → role tasks created.

    Chains _check_confirmed_validators and _check_completed_pm the way the
    dispatcher does each cron tick, proving the hand-off works end-to-end.
    """
    disp = _load_dispatch()
    pm_created = []
    role_tasks_created = []
    orig_list_tasks = disp.kanban.list_tasks
    orig_show = disp.kanban.show_card
    orig_create_task = disp.kanban.create_task

    # Tick 1: validator is done, no PM task yet
    tick1_tasks = [
        {
            "title": "#10 login bug",
            "assignee": "validator-daedalus",
            "status": "done",
            "summary": "CONFIRMED: reproduced",
            "id": "t_v10",
        },
    ]
    # Tick 2: validator still done, PM task now done with SPEC:
    tick2_tasks = tick1_tasks + [
        {
            "title": "#10 login bug",
            "assignee": "project-manager-daedalus",
            "status": "done",
            "summary": "SPEC: fix auth flow",
            "id": "t_pm10",
        },
    ]

    try:
        # ── Tick 1: create PM task ────────────────────────────────────────────
        disp.kanban.list_tasks = lambda slug, status=None: (
            tick1_tasks if status == "done" else []
        )
        disp.kanban.create_task = (
            lambda slug, title, *, assignee="", idempotency_key="", **kw: (
                pm_created.append(idempotency_key) or "t_pm10"
            )
        )
        disp._check_confirmed_validators(
            "slug",
            "O/R",
            {10: {"number": 10, "title": "login bug", "body": ""}},
            3,
            "/tmp",
            "",
            "main",
            "github",
        )

        # ── Tick 2: PM done with SPEC: → create role tasks ───────────────────
        disp.kanban.list_tasks = lambda slug, status=None: (
            tick2_tasks if status == "done" else []
        )
        disp.kanban.show_card = lambda slug, tid: {
            "latest_summary": "SPEC: fix auth flow"
        }
        disp.kanban.create_task = (
            lambda slug, title, *, assignee="", idempotency_key="", **kw: (
                role_tasks_created.append(
                    {"title": title, "idempotency_key": idempotency_key}
                )
                or "t_task10"
            )
        )
        result = disp._check_completed_pm(
            "slug",
            "O/R",
            {10: {"number": 10, "title": "login bug", "body": ""}},
            3,
            "/tmp",
            "",
            "main",
            "github",
        )
    finally:
        disp.kanban.list_tasks = orig_list_tasks
        disp.kanban.show_card = orig_show
        disp.kanban.create_task = orig_create_task

    check(
        "pipeline chain: PM task created on tick 1",
        any("pm-10" in k for k in pm_created),
    )
    titles = [t["title"] for t in role_tasks_created]
    check(
        "pipeline chain: role tasks created on tick 2",
        "#10 Developer: login bug" in titles and "#10 QA: login bug" in titles,
    )
    check("pipeline chain: _check_completed_pm returns issue", result == [10])


# ── _schedule_to_crontab ─────────────────────────────────────────────────────


def _load_init():
    import importlib.util

    p = Path(__file__).resolve().parent.parent / "__init__.py"
    spec = importlib.util.spec_from_file_location("daedalus_init", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_schedule_to_crontab_60m():
    """60m → every-hour crontab (Repeat: ∞ in Hermes)."""
    init = _load_init()
    check("60m → '0 * * * *'", init._schedule_to_crontab("60m") == "0 * * * *")


def test_schedule_to_crontab_every_60m():
    """'every 60m' prefix is stripped before conversion."""
    init = _load_init()
    check(
        "every 60m → '0 * * * *'", init._schedule_to_crontab("every 60m") == "0 * * * *"
    )


def test_schedule_to_crontab_30m():
    """30m → */30 crontab."""
    init = _load_init()
    check("30m → '*/30 * * * *'", init._schedule_to_crontab("30m") == "*/30 * * * *")


def test_schedule_to_crontab_2h():
    """2h → every-2-hours crontab."""
    init = _load_init()
    check("2h → '0 */2 * * *'", init._schedule_to_crontab("2h") == "0 */2 * * *")


def test_schedule_to_crontab_passthrough_crontab():
    """Already-crontab schedules pass through unchanged."""
    init = _load_init()
    check(
        "0 * * * * passthrough", init._schedule_to_crontab("0 * * * *") == "0 * * * *"
    )
    check(
        "0 9 * * * passthrough", init._schedule_to_crontab("0 9 * * *") == "0 9 * * *"
    )


def test_core_util_schedule_to_crontab_is_source_of_truth():
    """core.util.schedule_to_crontab is the shared helper (issue #134) that both
    _ensure_dispatch_crons and dashboard _reconcile_cron normalise schedules with."""
    from core.util import schedule_to_crontab

    check("60m → '0 * * * *'", schedule_to_crontab("60m") == "0 * * * *")
    check("every 2h → '0 */2 * * *'", schedule_to_crontab("every 2h") == "0 */2 * * *")
    check("15m → '*/15 * * * *'", schedule_to_crontab("15m") == "*/15 * * * *")
    check("crontab passthrough", schedule_to_crontab("0 9 * * *") == "0 9 * * *")
    check("empty stays empty", schedule_to_crontab("") == "")


# ── _FakeProvider base class additions (used by size gate / forbidden tests) ──
# (Patch _FakeProvider at module level to avoid test-isolation issues)

_FakeProvider.get_pr_files = lambda self, pr: []
_FakeProvider.post_issue_comment = lambda self, n, body: True
_FakeProvider.board_ensure_status_option = lambda self, *a: True
_FakeProvider.append_changelog = lambda self, *a: False


# ── issue #1104: empty-summary retry for PM and developer roles ─────────────


def _completed_developer_tasks(issue_number, summary="", title=None):
    """Minimal done developer task fixture for _check_completed_developer tests."""
    return [
        {
            "id": f"t_dev_{issue_number}",
            "assignee": "developer-daedalus",
            "status": "done",
            "title": title or f"#{issue_number} Developer: some feature",
            "summary": summary,
        }
    ]


def test_resolve_max_developer_retries_default():
    """_resolve_max_developer_retries returns default=2 when no config set."""
    disp = _load_dispatch()
    val = disp._resolve_max_developer_retries({})
    check("default max_developer_retries is 2", val == 2)


def test_resolve_max_developer_retries_config_override():
    """_resolve_max_developer_retries reads execution.max_developer_retries."""
    disp = _load_dispatch()
    val = disp._resolve_max_developer_retries({"max_developer_retries": 5})
    check("config override for max_developer_retries", val == 5)


def test_developer_task_state_none():
    """_developer_task_state returns ('none', 0) when no developer task exists."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: []
    state, stale = disp._developer_task_state("slug", 5)
    check("no tasks -> state is none", state == "none")
    check("no tasks -> stale_count is 0", stale == 0)


def test_developer_task_state_running():
    """_developer_task_state returns ('running', 0) for an in-progress developer task."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: [
        {
            "title": "#5 Developer: feature",
            "assignee": "developer-daedalus",
            "status": "in_progress",
        },
    ]
    state, stale = disp._developer_task_state("slug", 5)
    check("in-progress developer -> state is running", state == "running")
    check("in-progress developer -> stale_count is 0", stale == 0)


def test_developer_task_state_complete():
    """_developer_task_state returns ('complete', 0) for done developer task with PR in summary."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: [
        {
            "title": "#5 Developer: feature",
            "assignee": "developer-daedalus",
            "status": "done",
            "summary": "review-required: PR #42 -> dev",
        },
    ]
    state, stale = disp._developer_task_state("slug", 5)
    check("done+PR developer -> state is complete", state == "complete")
    check("done+PR developer -> stale_count is 0", stale == 0)


def test_developer_task_state_stale():
    """_developer_task_state returns ('stale', 1) for done developer task with no PR in summary."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: [
        {
            "title": "#5 Developer: feature",
            "assignee": "developer-daedalus",
            "id": "t_dev",
            "status": "done",
            "summary": "",
        },
    ]
    disp.kanban.show_card = lambda s, tid: {"latest_summary": ""}
    state, stale = disp._developer_task_state("slug", 5)
    check("done+no-PR developer -> state is stale", state == "stale")
    check("done+no-PR developer -> stale_count is 1", stale == 1)


def test_check_confirmed_validators_github_fallback_cap_exhausted():
    """PM github-fallback path enforces max_pm_retries cap — no infinite PM creation."""
    disp = _load_dispatch()
    created_keys = []

    # Simulate 4 stale PM tasks (cap is 3 by default) — should be at cap
    stale_pm_tasks = [
        {
            "title": "#9 crash bug",
            "assignee": "project-manager-daedalus",
            "summary": "",
            "status": "done",
            "id": f"t_pm_stale_{i}",
        }
        for i in range(4)
    ]

    def fake_list_tasks(slug, status=None):
        if status == "done":
            return [
                {
                    "title": "#9 crash bug",
                    "assignee": "validator-daedalus",
                    "summary": None,
                    "status": "done",
                    "id": "t_v",
                }
            ] + stale_pm_tasks
        return stale_pm_tasks

    class _FP:
        name = "github"

        def get_issue_comments(self, n):
            return [
                {
                    "body": "**Agent: validator**\nCONFIRMED — issue is real.",
                }
            ]

    _orig_create = disp.kanban.create_task
    _orig_show = disp.kanban.show_card
    _orig_has_notified = disp._has_notified_block
    try:
        disp.kanban.list_tasks = fake_list_tasks
        disp.kanban.show_card = lambda s, tid: {"latest_summary": ""}
        disp.kanban.create_task = (
            lambda slug, title, *, assignee="", idempotency_key="", **kw: (
                created_keys.append(idempotency_key) or "t_new"
            )
        )
        disp._has_notified_block = lambda *a, **kw: False
        disp._send_retry_cap_notification = lambda **kw: None
        disp._mark_notified_block = lambda *a, **kw: None
        disp._check_confirmed_validators(
            "slug",
            "O/R",
            {9: {"number": 9, "title": "crash bug", "body": ""}},
            3,
            "/tmp",
            "",
            "main",
            "github",
            provider=_FP(),
            resolved={"execution": {}, "notifications": []},
        )
    finally:
        disp.kanban.create_task = _orig_create
        disp.kanban.show_card = _orig_show
        disp._has_notified_block = _orig_has_notified

    # At cap: no new PM task should be created
    pm_keys = [k for k in created_keys if k.startswith("pm-")]
    check("github-fallback at cap: no PM task created", len(pm_keys) == 0)


def test_check_confirmed_validators_github_fallback_under_cap_retries():
    """PM github-fallback path under cap — creates a PM retry task with incrementing key."""
    disp = _load_dispatch()
    created_keys = []

    # Simulate 1 stale PM task (cap is 3, so we're under cap)
    stale_pm_tasks = [
        {
            "title": "#9 crash bug",
            "assignee": "project-manager-daedalus",
            "summary": "",
            "status": "done",
            "id": "t_pm_stale_0",
        },
    ]

    def fake_list_tasks(slug, status=None):
        if status == "done":
            return [
                {
                    "title": "#9 crash bug",
                    "assignee": "validator-daedalus",
                    "summary": None,
                    "status": "done",
                    "id": "t_v",
                }
            ] + stale_pm_tasks
        return stale_pm_tasks

    class _FP:
        name = "github"

        def get_issue_comments(self, n):
            return [
                {
                    "body": "**Agent: validator**\nCONFIRMED — issue is real.",
                }
            ]

    _orig_create = disp.kanban.create_task
    _orig_show = disp.kanban.show_card
    try:
        disp.kanban.list_tasks = fake_list_tasks
        disp.kanban.show_card = lambda s, tid: {"latest_summary": ""}
        disp.kanban.create_task = (
            lambda slug, title, *, assignee="", idempotency_key="", **kw: (
                created_keys.append(idempotency_key) or "t_new"
            )
        )
        disp._send_retry_attempt_notification = lambda **kw: None
        disp._check_confirmed_validators(
            "slug",
            "O/R",
            {9: {"number": 9, "title": "crash bug", "body": ""}},
            3,
            "/tmp",
            "",
            "main",
            "github",
            provider=_FP(),
            resolved={"execution": {}, "notifications": []},
        )
    finally:
        disp.kanban.create_task = _orig_create
        disp.kanban.show_card = _orig_show

    # Under cap: a PM retry task should be created with -r1 key
    pm_keys = [k for k in created_keys if k.startswith("pm-")]
    check("github-fallback under cap: PM retry task created", len(pm_keys) == 1)
    check("github-fallback under cap: retry key is pm-9-r1", pm_keys[0] == "pm-9-r1")


def test_check_completed_pm_warns_on_empty_summary():
    """_check_completed_pm logs a warning (not silent continue) when a PM task has summary=None."""
    disp = _load_dispatch()
    orig_list = disp.kanban.list_tasks
    orig_show = disp.kanban.show_card
    try:
        disp.kanban.list_tasks = lambda slug, status=None: (
            [
                {
                    "id": "t_pm_empty",
                    "assignee": "project-manager-daedalus",
                    "status": "done",
                    "title": "#7 some feature",
                    "summary": "",
                }
            ]
            if status == "done"
            else []
        )
        disp.kanban.show_card = lambda s, tid: {"latest_summary": ""}

        with mock.patch.object(disp.logger, "warning") as mock_warn:
            disp._check_completed_pm(
                "slug",
                "O/R",
                {7: {"number": 7, "title": "some feature", "body": ""}},
                3,
                "/tmp",
                "",
                "main",
                "github",
            )

        # At least one warning should mention the empty summary
        warnings = [str(c) for c in mock_warn.call_args_list]
        check(
            "empty PM summary logs a warning",
            any("no summary" in w.lower() for w in warnings),
        )
    finally:
        disp.kanban.list_tasks = orig_list
        disp.kanban.show_card = orig_show


def test_check_completed_developer_retries_empty_summary():
    """_check_completed_developer creates a retry task when developer completes with no PR in summary."""
    disp = _load_dispatch()
    created_keys = []

    def fake_list_tasks(slug, status=None):
        if status == "done":
            return _completed_developer_tasks(10, summary="")
        # Non-filtered call used by _developer_task_state
        return _completed_developer_tasks(10, summary="")

    _orig_create = disp.kanban.create_task
    _orig_show = disp.kanban.show_card
    _orig_has_notified = disp._has_notified_block
    try:
        disp.kanban.list_tasks = fake_list_tasks
        disp.kanban.show_card = lambda s, tid: {"latest_summary": ""}
        disp.kanban.create_task = (
            lambda slug, title, *, assignee="", idempotency_key="", **kw: (
                created_keys.append(idempotency_key) or "t_new"
            )
        )
        disp._has_notified_block = lambda *a, **kw: False
        disp._send_retry_attempt_notification = lambda **kw: None
        disp._check_completed_developer(
            "slug",
            "O/R",
            {10: {"number": 10, "title": "feature", "body": ""}},
            3,
            "/tmp",
            "main",
            "github",
            provider=None,
            resolved={"execution": {}, "notifications": []},
        )
    finally:
        disp.kanban.create_task = _orig_create
        disp.kanban.show_card = _orig_show
        disp._has_notified_block = _orig_has_notified

    # Under cap (1 stale, max=2): retry task created with developer-10-r1 key
    dev_keys = [k for k in created_keys if k.startswith("developer-")]
    check("developer empty summary: retry task created", len(dev_keys) == 1)
    check(
        "developer empty summary: retry key is developer-10-r1",
        dev_keys[0] == "developer-10-r1",
    )


def test_check_completed_developer_cap_exhausted():
    """_check_completed_developer stops retrying and notifies when cap is exhausted."""
    disp = _load_dispatch()
    created_keys = []

    # Simulate 3 stale developer tasks (cap is 2) — should be at cap
    stale_dev_tasks = [
        {
            "title": "#10 Developer: feature",
            "assignee": "developer-daedalus",
            "summary": "",
            "status": "done",
            "id": f"t_dev_stale_{i}",
        }
        for i in range(3)
    ]

    def fake_list_tasks(slug, status=None):
        if status == "done":
            return stale_dev_tasks
        return stale_dev_tasks

    _orig_create = disp.kanban.create_task
    _orig_show = disp.kanban.show_card
    _orig_has_notified = disp._has_notified_block
    try:
        disp.kanban.list_tasks = fake_list_tasks
        disp.kanban.show_card = lambda s, tid: {"latest_summary": ""}
        disp.kanban.create_task = (
            lambda slug, title, *, assignee="", idempotency_key="", **kw: (
                created_keys.append(idempotency_key) or "t_new"
            )
        )
        disp._has_notified_block = lambda *a, **kw: False
        disp._send_retry_cap_notification = lambda **kw: None
        disp._mark_notified_block = lambda *a, **kw: None
        disp._check_completed_developer(
            "slug",
            "O/R",
            {10: {"number": 10, "title": "feature", "body": ""}},
            3,
            "/tmp",
            "main",
            "github",
            provider=None,
            resolved={"execution": {}, "notifications": []},
        )
    finally:
        disp.kanban.create_task = _orig_create
        disp.kanban.show_card = _orig_show
        disp._has_notified_block = _orig_has_notified

    # At cap: no new developer task should be created
    dev_keys = [k for k in created_keys if k.startswith("developer-")]
    check("developer cap exhausted: no retry task created", len(dev_keys) == 0)


def test_check_completed_developer_well_formed_summary_no_retry():
    """_check_completed_developer does NOT retry when developer summary contains a PR number."""
    disp = _load_dispatch()
    created_keys = []

    def fake_list_tasks(slug, status=None):
        if status == "done":
            return _completed_developer_tasks(
                11, summary="review-required: PR #99 -> dev"
            )
        return _completed_developer_tasks(11, summary="review-required: PR #99 -> dev")

    _orig_create = disp.kanban.create_task
    _orig_show = disp.kanban.show_card
    try:
        disp.kanban.list_tasks = fake_list_tasks
        disp.kanban.show_card = lambda s, tid: {"latest_summary": ""}
        disp.kanban.create_task = (
            lambda slug, title, *, assignee="", idempotency_key="", **kw: (
                created_keys.append(idempotency_key) or "t_new"
            )
        )
        disp._check_completed_developer(
            "slug",
            "O/R",
            {11: {"number": 11, "title": "feature", "body": ""}},
            3,
            "/tmp",
            "main",
            "github",
            provider=None,
            resolved={"execution": {}, "notifications": []},
        )
    finally:
        disp.kanban.create_task = _orig_create
        disp.kanban.show_card = _orig_show

    # Well-formed summary: no retry task should be created
    dev_keys = [k for k in created_keys if k.startswith("developer-")]
    check("developer well-formed summary: no retry", len(dev_keys) == 0)


if __name__ == "__main__":
    print("Daedalus tests")
    print("-" * 60)
    for fn in (
        test_deep_merge,
        test_config_loader_resolve,
        test_kanban_list_issue_numbers,
        test_kanban_list_issue_numbers_large_ids,
        test_create_triage_pins_workspace,
        test_kanban_review_handoff_pr,
        test_dispatch_dual_mode,
        test_resolve_repo_config_valid,
        test_resolve_repo_config_sources_toggles,
        test_resolve_repo_config_missing_file,
        test_main_registry_sweep,
        test_main_single_repo,
        test_parse_pr_from_card,
        test_find_doc_comment,
        test_send_via_hermes,
        test_hermes_send_returns_anchor_and_threads,
        test_hermes_send_broadcast_failure_is_logged,
        test_deliver_doc_reports_idempotent,
        test_deliver_doc_reports_no_target,
        test_deliver_doc_reports_send_failure,
        test_deliver_doc_reports_non_doc_assignee,
        test_deliver_doc_reports_no_pr,
        test_deliver_doc_reports_no_doc_comment,
        test_deliver_doc_reports_dry_run,
        test_resolve_pr_from_parents,
        test_human_summary_slack_delivered,
        test_task_body_no_slack,
        test_format_completion_comment_has_role_title_summary,
        test_format_completion_comment_handles_empty_summary,
        test_post_completion_comments_posts_once_per_role,
        test_post_completion_comments_idempotent_across_ticks,
        test_post_completion_comments_skips_closed_issues,
        test_post_completion_comments_none_provider_is_noop,
        test_fetch_issues_default_limit_is_100,
        test_fetch_issues_respects_configured_limit,
        test_dispatch_summary_has_slack_delivered,
        test_notify_targets,
        test_summary_events,
        test_notify_project_summary_fans_out,
        test_notify_project_summary_threads_and_dedupes,
        test_notify_project_summary_silent_tick_sends_nothing,
        test_resolve_repo_arg_path_and_slug,
        test_resolve_repo_arg_broken_config_warns,
        test_resolve_repo_from_cwd,
        test_main_scopes_to_cwd_project,
        test_deliver_doc_reports_multi_target,
        test_ensure_board_creates,
        test_ensure_board_already_exists,
        test_ensure_board_failure,
        test_dispatch_state_record_and_age,
        test_dispatch_state_clear,
        test_dispatch_state_pr_flags,
        test_dispatch_state_review_sha,
        test_priority_sort_p0_before_unlabeled,
        test_priority_sort_dict_labels,
        test_pr_size_gate_warns_once,
        test_pr_size_gate_idempotent,
        test_pr_size_gate_disabled_when_zero,
        test_forbidden_file_guard_warns_once,
        test_forbidden_file_guard_idempotent,
        test_staleness_warns_when_over_threshold,
        test_staleness_silent_when_under_threshold,
        test_label_overrides_skip_developer,
        test_label_overrides_security_first,
        test_resolve_profiles_defaults,
        test_resolve_profiles_user_overrides,
        test_resolve_profiles_ignores_unknown_keys,
        test_resolve_profiles_ignores_empty_strings,
        test_custom_validator_profile_used_in_dispatch,
        test_custom_pm_profile_used_after_validator_confirms,
        test_hermes_profile_exists_directory,
        test_hermes_profile_exists_yaml,
        test_validate_profiles_all_present_no_warning,
        test_validate_profiles_missing_warns_with_role_and_name,
        test_validate_profiles_skip_behavior_drops_missing,
        test_validate_profiles_fallback_explicit,
        test_profile_validation_runs_once_per_dispatch_tick,
        test_has_active_pm_consultation_true,
        test_has_active_pm_consultation_false_when_done,
        test_has_active_pm_consultation_false_for_spec_task,
        test_check_team_blockers_creates_pm_consultation,
        test_check_team_blockers_skips_when_consult_active,
        test_check_team_blockers_skips_escalation,
        test_check_team_blockers_skips_validator_cards,
        test_pm_consultation_body_content,
        test_has_pm_tasks_excludes_consultations,
        test_has_pm_tasks_true_for_spec_task,
        test_pm_task_state_none,
        test_pm_task_state_running,
        test_pm_task_state_complete,
        test_pm_task_state_stale,
        test_check_confirmed_validators_retries_stale_pm,
        test_validator_github_comment_outcome_confirmed,
        test_validator_github_comment_outcome_no_match,
        test_validator_github_comment_outcome_none_provider,
        test_check_confirmed_validators_github_comment_fallback_advances_to_pm,
        test_check_confirmed_validators_none_summary_retries_when_no_github_comment,
        test_validator_empty_summary_logs_retry_warning,
        test_validator_none_summary_logs_retry_warning,
        test_validator_empty_summary_unresolvable_issue_no_silent_drop,
        test_validator_cap_exhausted_with_unresolvable_issue,
        test_check_completed_pm_creates_team_tasks,
        test_check_completed_pm_provider_fallback,
        test_check_completed_pm_no_issue_found,
        test_check_completed_pm_idempotent,
        test_check_completed_pm_skips_consultation,
        test_pipeline_chain_confirmed_to_team_tasks,
        test_schedule_to_crontab_60m,
        test_schedule_to_crontab_every_60m,
        test_schedule_to_crontab_30m,
        test_schedule_to_crontab_2h,
        test_schedule_to_crontab_passthrough_crontab,
        # ── issue #1104: empty-summary retry for PM and developer roles ──
        test_resolve_max_developer_retries_default,
        test_resolve_max_developer_retries_config_override,
        test_developer_task_state_none,
        test_developer_task_state_running,
        test_developer_task_state_complete,
        test_developer_task_state_stale,
        test_check_confirmed_validators_github_fallback_cap_exhausted,
        test_check_confirmed_validators_github_fallback_under_cap_retries,
        test_check_completed_pm_warns_on_empty_summary,
        test_check_completed_developer_retries_empty_summary,
        test_check_completed_developer_cap_exhausted,
        test_check_completed_developer_well_formed_summary_no_retry,
    ):
        fn()
    print("-" * 60)
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)

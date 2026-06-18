#!/usr/bin/env python3
"""Focused unit tests for the live daedalus surface:
config loading/merging, kanban parsing, dispatcher provider integration,
and doc-report delivery.

Run: python3 tests/test_daedalus.py
"""
import sys
from pathlib import Path
from unittest import mock

# Make the package root importable (config/, core/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


class _FakeProvider:
    """Stands in for a core.providers.VCSProvider in dispatcher tests."""

    name = "github"

    def board_configured(self):
        return False

    def status_name(self, key):
        return {"ready": "Ready", "in_progress": "In progress",
                "in_review": "In review", "done": "Done"}.get(key, key)

    def board_numbers_with_statuses(self, names):
        return set()

    def board_set_status(self, n, status):
        return True

    def list_issues(self, state="open", labels=None, limit=50):
        return []

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

    def list_prs(self, state="open"):
        return []

    def pr_ci_green(self, pr):
        return False

    def list_pr_comments(self, pr):
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

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}")


# ── config: deep_merge ───────────────────────────────────────────────────────
def test_deep_merge():
    check("deep_merge replaces lists wholesale",
          deep_merge({"a": [1, 2]}, {"a": [3]})["a"] == [3])
    check("deep_merge merges nested dicts",
          deep_merge({"a": {"x": 1}}, {"a": {"y": 2}})["a"] == {"x": 1, "y": 2})
    check("deep_merge preserves base keys not overridden",
          deep_merge({"a": 1, "b": 2}, {"b": 3}) == {"a": 1, "b": 3})
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
        (repo / ".hermes" / "daedalus.yaml").write_text(yaml.safe_dump({
            "name": "p1", "repo": "O/p1",
            "vcs": {"target_branch": "release"},
        }))
        r1 = ConfigLoader().resolve_repo_config(str(repo))
    check("resolve_repo_config keeps identity fields", r1["repo"] == "O/p1")
    check("resolve_repo_config pins workdir to the repo path",
          r1["workdir"] == str(repo.resolve()))
    check("resolve_repo_config lets the repo override template defaults",
          r1["vcs"]["target_branch"] == "release")
    check("resolve_repo_config inherits template defaults",
          r1["vcs"]["provider"] == "github" and r1["cron"]["schedule"] == "every 60m")


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
        tid = kanban.create_triage("slug", 7, "title", "body",
                                   idempotency_key="issue-7", workspace="dir:/w")
    a = captured["args"]
    check("create_triage returns parsed tid", tid == "t_abc")
    check("create_triage pins --workspace", "--workspace" in a and a[a.index("--workspace") + 1] == "dir:/w")
    # No workspace -> no flag (kanban-only triage may be created pinned elsewhere).
    with mock.patch.object(kanban, "_hk", fake_hk):
        kanban.create_triage("slug", 7, "title", "body")
    check("create_triage omits --workspace when unset", "--workspace" not in captured["args"])


def test_kanban_review_handoff_pr():
    show = '{"runs":[{"reason":"review-required: shipped. PR #363 open."}]}'
    with mock.patch.object(kanban, "_hk", return_value=(0, show, "")):
        pr = kanban.review_handoff_pr("board", "t_x")
    check("review_handoff_pr extracts the PR from a review-required handoff", pr == 363)
    with mock.patch.object(kanban, "_hk", return_value=(0, '{"runs":[{"reason":"running"}]}', "")):
        none = kanban.review_handoff_pr("board", "t_y")
    check("review_handoff_pr returns None when not a review-required handoff", none is None)


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
    check("runs 'boards create' not '--board <slug> init'",
          captured["args"][:3] == ["boards", "create", "my-board"])


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
def _load_dispatch():
    import importlib.util
    p = Path(__file__).resolve().parent.parent / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── config: resolve_repo_config ──────────────────────────────────────────────

def test_resolve_repo_config_valid():
    """resolve_repo_config loads .hermes/daedalus.yaml, deep-merges with
    template defaults, and sets workdir to absolute repo_path."""
    import tempfile, yaml

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
    check("resolve_repo_config sets workdir to absolute path",
          result.get("workdir") == str(Path(tmp).resolve()))
    check("resolve_repo_config carries tracking",
          result.get("tracking", {}).get("github_project_number") == 42)
    # Defaults from template must be present (vcs, sources, cron)
    check("resolve_repo_config inherits vcs defaults", "vcs" in result)
    check("resolve_repo_config inherits sources defaults",
          "sources" in result and isinstance(result["sources"], dict))


def test_resolve_repo_config_sources_toggles():
    """Per-repo sources toggles are parsed and override defaults."""
    import tempfile, yaml

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

    check("resolve_repo_config sets github_issues.enabled to False",
          result["sources"]["github_issues"]["enabled"] is False)
    check("resolve_repo_config sets local_specs.enabled to True",
          result["sources"]["local_specs"]["enabled"] is True)


def test_resolve_repo_config_missing_file():
    """Missing .hermes/daedalus.yaml raises a clear error."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        loader = ConfigLoader()
        try:
            loader.resolve_repo_config(tmp)
            check("resolve_repo_config raises on missing file", False)
        except FileNotFoundError as e:
            check("resolve_repo_config error message includes path",
                  tmp in str(e))


def test_dispatch_dual_mode():
    disp = _load_dispatch()
    calls = {"decompose_all": 0, "create_triage": 0, "create_task": 0, "fetch_issues": 0}
    # Save originals for every kanban function we monkey-patch so cross-test
    # contamination (e.g. test_ensure_board_creates) cannot occur.
    _saved = {
        k: getattr(disp.kanban, k)
        for k in ("ensure_board", "list_blocked", "list_issue_numbers",
                  "decompose_all_triage", "create_triage", "decompose",
                  "dispatch", "create_task", "list_tasks")
    }
    try:
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: set()
        disp.kanban.decompose_all_triage = lambda s: calls.__setitem__("decompose_all", calls["decompose_all"] + 1) or True
        disp.kanban.create_triage = lambda *a, **k: calls.__setitem__("create_triage", calls["create_triage"] + 1) or "t_x"
        disp.kanban.decompose = lambda *a, **k: True
        disp.kanban.dispatch = lambda s, max_spawns=5: True
        disp._fetch_issues = lambda r, f: (calls.__setitem__("fetch_issues", calls["fetch_issues"] + 1) or [{"number": 1, "title": "t"}])
        base = {"repo": "O/R", "workdir": "/tmp", "name": "x", "issues": {"filters": {}}, "execution": {}}

        disp.kanban.list_tasks = lambda *a, **k: []  # no confirmed validators yet
        disp.kanban.create_task = lambda *a, **k: calls.__setitem__("create_task", calls["create_task"] + 1) or "t_v"

        s1 = disp.run({**base, "tracking": {}}, provider=_FakeProvider())  # no board -> kanban-only
        check("kanban-only mode decomposes triage cards", calls["decompose_all"] == 1)
        check("kanban-only mode does NOT poll VCS issues", calls["fetch_issues"] == 0)
        check("kanban-only mode reports mode=kanban", s1.get("mode") == "kanban")

        class FP(_FakeProvider):
            def board_configured(self): return True
            def board_numbers_with_statuses(self, names): return {1}
        s2 = disp.run({**base, "tracking": {"github_project_number": 1}}, provider=FP())  # board mode
        check("github mode polls issues and creates a validator task (phase 1 only)",
              calls["fetch_issues"] >= 1 and calls["create_task"] == 1)
        check("github mode does NOT decompose all roles in phase 1", calls["create_triage"] == 0)
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

    def fake_run(resolved, *, dry_run=False):
        run_calls.append(resolved)
        return {"board": resolved.get("name", "?"), "mode": "kanban",
                "created": [], "reconciled": [], "completed": [], "advanced": [],
                "issues_seen": 0}

    with mock.patch.object(disp.registry, "list_projects",
                           return_value=[str(Path(r.name).resolve()) for r in repos]):
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

        def fake_run(resolved, *, dry_run=False):
            called.append(resolved)
            return {"board": "solo", "mode": "kanban",
                    "created": [], "reconciled": [], "completed": [], "advanced": [],
                    "issues_seen": 0}

        with mock.patch.object(disp, "run", fake_run):
            with mock.patch("sys.argv", ["daedalus_dispatch.py", "--repo", str(repo)]):
                disp.main()

        check("--repo calls run() exactly once", len(called) == 1)
        check("--repo resolves the correct repo name",
              called[0].get("name") == "solo")
        check("--repo sets workdir to the repo path",
              called[0].get("workdir") == str(repo.resolve()))
    finally:
        tmp.cleanup()


# ── GitHub PR helpers (new) ──────────────────────────────────────────────────


def test_parse_pr_from_card():
    """_parse_pr_from_card extracts PR numbers from card body + summary."""
    disp = _load_dispatch()

    card1 = {"body": "Implement fix for issue #7. PR #42 is open.",
             "latest_summary": "All tests pass."}
    check("_parse_pr_from_card extracts from body", disp._parse_pr_from_card(card1) == 42)

    card2 = {"body": "Implement fix.", "latest_summary": "Shipped as PR #99."}
    check("_parse_pr_from_card extracts from summary", disp._parse_pr_from_card(card2) == 99)

    card3 = {"body": "No PR here.", "latest_summary": ""}
    check("_parse_pr_from_card returns None when no PR", disp._parse_pr_from_card(card3) is None)

    card4 = {"body": "  \n  PR #7 is open.  \n  ", "latest_summary": None}
    check("_parse_pr_from_card strips whitespace-padded body", disp._parse_pr_from_card(card4) == 7)

    card5 = {"body": None, "latest_summary": "PR #13 merged."}
    check("_parse_pr_from_card handles None body", disp._parse_pr_from_card(card5) == 13)


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
    check("_find_doc_comment returns first matching body",
          result.startswith("**Agent: documentation**"))
    check("_find_doc_comment has report content", "Resolution Report" in result)

    no_doc = [_Comment("No doc comment here.")]
    with mock.patch.object(gp, "list_pr_comments", return_value=no_doc):
        result2 = disp._find_doc_comment(gp, 42)
    check("_find_doc_comment returns '' when no match", result2 == "")


def test_send_via_hermes():
    """_send_via_hermes calls `hermes send -t <target> --file <tmpfile>`."""
    disp = _load_dispatch()
    subprocess_calls = []

    def fake_run(args, **kwargs):
        subprocess_calls.append(args)
        return type("R", (), {"returncode": 0, "stderr": ""})()

    with mock.patch.object(disp.subprocess, "run", fake_run):
        ok = disp._send_via_hermes("slack:tasks", "Report body here")
    check("_send_via_hermes returns True on success", ok is True)
    check("_send_via_hermes called hermes send",
          subprocess_calls[0][0] == "hermes" and "send" in subprocess_calls[0])
    check("_send_via_hermes passed target", "-t" in subprocess_calls[0] and
          "slack:tasks" in subprocess_calls[0])

    # Failure case
    subprocess_calls.clear()

    def fake_run_fail(args, **kwargs):
        subprocess_calls.append(args)
        return type("R", (), {"returncode": 1, "stderr": "no such platform"})()

    with mock.patch.object(disp.subprocess, "run", fake_run_fail):
        ok2 = disp._send_via_hermes("slack:tasks", "Report")
    check("_send_via_hermes returns False on failure", ok2 is False)

    # Empty inputs
    check("_send_via_hermes returns False for empty target",
          disp._send_via_hermes("", "body") is False)
    check("_send_via_hermes returns False for empty body",
          disp._send_via_hermes("slack:tasks", "") is False)


def test_deliver_doc_reports_idempotent():
    """_deliver_doc_reports sends once, skips on sentinel."""
    disp = _load_dispatch()

    doc_card = {
        "id": "t_doc_1", "assignee": "documentation-daedalus",
        "body": "Write report. PR #42 is ready.",
        "latest_summary": "Report posted.", "parents": [],
    }

    # Mock kanban.list_tasks to return one done doc card
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[doc_card]):
        # Mock: sentinel NOT present (first pass)
        with mock.patch.object(gp, "pr_has_delivery_marker", return_value=False):
            # Mock: find the doc comment
            with mock.patch.object(disp, "_find_doc_comment",
                                   return_value="**Agent: documentation**\n\n# Report"):
                # Mock: subprocess.run succeeds
                with mock.patch.object(disp, "_send_via_hermes", return_value=True):
                    # Mock: sentinel post succeeds
                    with mock.patch.object(gp, "post_delivery_marker", return_value=True):
                        result1 = disp._deliver_doc_reports(
                            "slug", gp, "slack:tasks",
                        )
    check("first pass delivers one PR", result1 == [42])

    # Second pass: sentinel IS present → skip
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[doc_card]):
        with mock.patch.object(gp, "pr_has_delivery_marker", return_value=True):
            result2 = disp._deliver_doc_reports(
                "slug", gp, "slack:tasks",
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
        "id": "t_doc_1", "assignee": "documentation-daedalus",
        "body": "PR #42.", "latest_summary": "", "parents": [],
    }

    sentinel_calls = []

    def fake_has_marker(pr):
        return False

    def fake_post_marker(pr, body=""):
        sentinel_calls.append(pr)
        return True

    with mock.patch.object(disp.kanban, "list_tasks", return_value=[doc_card]):
        with mock.patch.object(gp, "pr_has_delivery_marker", fake_has_marker):
            with mock.patch.object(disp, "_find_doc_comment",
                                   return_value="**Agent: documentation**\n\nReport"):
                with mock.patch.object(disp, "_send_via_hermes", return_value=False):
                    with mock.patch.object(gp, "post_delivery_marker", fake_post_marker):
                        result = disp._deliver_doc_reports(
                            "slug", gp, "slack:tasks",
                        )
    check("send failure returns empty delivered list", result == [])
    check("send failure does NOT post sentinel", len(sentinel_calls) == 0)


def test_deliver_doc_reports_non_doc_assignee():
    """_deliver_doc_reports skips cards not assigned to documentation."""
    disp = _load_dispatch()
    dev_card = {"id": "t_dev", "assignee": "developer-daedalus", "body": "PR #42.",
                "latest_summary": "", "parents": []}
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[dev_card]):
        result = disp._deliver_doc_reports("slug", gp, "slack:tasks")
    check("non-doc assignee skipped", result == [])


def test_deliver_doc_reports_no_pr():
    """_deliver_doc_reports skips doc cards with no resolvable PR."""
    disp = _load_dispatch()
    doc_card = {"id": "t_doc", "assignee": "documentation-daedalus", "body": "No PR ref.",
                "latest_summary": "", "parents": []}
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[doc_card]):
        with mock.patch.object(disp, "_parse_pr_from_card", return_value=None):
            with mock.patch.object(disp, "_resolve_pr_from_parents", return_value=None):
                result = disp._deliver_doc_reports("slug", gp, "slack:tasks")
    check("no resolvable PR → skipped", result == [])


def test_deliver_doc_reports_no_doc_comment():
    """_deliver_doc_reports skips when no **Agent: documentation** comment exists."""
    disp = _load_dispatch()
    doc_card = {"id": "t_doc", "assignee": "documentation-daedalus", "body": "PR #42.",
                "latest_summary": "", "parents": []}
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[doc_card]):
        with mock.patch.object(gp, "pr_has_delivery_marker", return_value=False):
            with mock.patch.object(disp, "_find_doc_comment", return_value=""):
                result = disp._deliver_doc_reports("slug", gp, "slack:tasks")
    check("no doc comment → skipped", result == [])


def test_deliver_doc_reports_dry_run():
    """_deliver_doc_reports in dry_run mode logs but does NOT send."""
    disp = _load_dispatch()
    doc_card = {"id": "t_doc", "assignee": "documentation-daedalus", "body": "PR #42.",
                "latest_summary": "", "parents": []}
    send_calls = []
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[doc_card]):
        with mock.patch.object(gp, "pr_has_delivery_marker", return_value=False):
            with mock.patch.object(disp, "_find_doc_comment",
                                   return_value="**Agent: documentation**\n\nReport"):
                with mock.patch.object(disp, "_send_via_hermes",
                                       side_effect=lambda *a, **k: send_calls.append(1) or True):
                    result = disp._deliver_doc_reports(
                        "slug", gp, "slack:tasks", dry_run=True,
                    )
    check("dry_run returns PR number", result == [42])
    check("dry_run does NOT call _send_via_hermes", len(send_calls) == 0)


def test_resolve_pr_from_parents():
    """_resolve_pr_from_parents walks parent cards to find issue→PR."""
    disp = _load_dispatch()
    parent_card = {"id": "t_parent", "title": "#7 Fix the thing",
                   "body": "Triage for #7."}
    with mock.patch.object(disp.kanban, "show_card", return_value=parent_card):
        with mock.patch.object(gp, "pr_number_for_issue", return_value=42):
            result = disp._resolve_pr_from_parents("slug", gp,
                                                   {"parents": ["t_parent"]})
    check("_resolve_pr_from_parents resolves PR via parent", result == 42)

    # No parents
    result2 = disp._resolve_pr_from_parents("slug", gp, {"parents": []})
    check("_resolve_pr_from_parents returns None with no parents", result2 is None)


def test_human_summary_slack_delivered():
    """_human_summary includes slack_delivered in output."""
    disp = _load_dispatch()
    summary = {"board": "test", "mode": "github", "created": [1, 2],
               "reconciled": [], "completed": [], "advance_prs": [],
               "routed_actions": {}, "issues_seen": 5, "spec_created": [],
               "slack_delivered": [42, 99]}
    msg = disp._human_summary({"test": summary})
    check("_human_summary includes doc-report delivery", "PR #42" in msg and "PR #99" in msg)

    # Empty slack_delivered → no mention
    summary2 = {"board": "test", "mode": "github", "created": [],
                "reconciled": [], "completed": [], "advance_prs": [],
                "routed_actions": {}, "issues_seen": 0, "spec_created": [],
                "slack_delivered": []}
    msg2 = disp._human_summary({"test": summary2})
    check("_human_summary returns '' when nothing happened", msg2 == "")


def test_task_body_no_slack():
    """_task_body no longer instructs the doc agent to call hermes send."""
    disp = _load_dispatch()
    body = disp._task_body("O/R", {"number": 7, "title": "Fix bug", "body": ""},
                           iterations=3, workdir="/tmp", notify_target="slack:tasks")
    check("_task_body does NOT mention hermes send",
          "hermes send" not in body)
    check("_task_body mentions dispatcher handles messaging-platform delivery",
          "dispatcher" in body and "messaging-platform" in body)
    check("_task_body instructs the API comment path (no CLI)",
          "api.github.com/repos/O/R/issues" in body and "GITHUB_TOKEN" in body)
    check("_task_body instructs the API PR-create path",
          "api.github.com/repos/O/R/pulls" in body)
    check("_task_body never mentions the gh CLI",
          "gh pr" not in body and "gh issue" not in body and "gh auth" not in body)
    check("_task_body mentions Agent: documentation prefix",
          "**Agent: documentation**" in body)


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
        base = {"repo": "O/R", "workdir": "/tmp", "name": "x",
                "issues": {"filters": {}}, "execution": {}}

        # kanban-only mode
        s1 = disp.run({**base, "tracking": {}}, provider=_FakeProvider())
        check("kanban summary has slack_delivered", s1.get("slack_delivered") == [42])

        # board mode
        class FP(_FakeProvider):
            def board_configured(self): return True
            def board_numbers_with_statuses(self, names): return {1}
        s2 = disp.run({**base, "tracking": {"github_project_number": 1}}, provider=FP())
        check("board summary has slack_delivered", s2.get("slack_delivered") == [42])

    disp.kanban.create_task = _orig_create_task
    disp.kanban.list_tasks = _orig_list_tasks


def test_notify_targets():
    """_notify_targets: notifications[] wins, event-filters, falls back to deliver."""
    disp = _load_dispatch()

    legacy = {"cron": {"deliver": "slack:tasks"}}
    check("legacy deliver receives every event",
          disp._notify_targets(legacy, "doc-report") == ["slack:tasks"]
          and disp._notify_targets(legacy, "dispatch-summary") == ["slack:tasks"])

    check("no config → no targets", disp._notify_targets({}, "doc-report") == [])

    multi = {"cron": {"deliver": "slack:legacy", "notifications": [
        {"platform": "Slack", "target": "slack:C1", "events": ["doc-report"]},
        {"platform": "Discord", "target": "discord:#general",
         "events": ["dispatch-summary", "pr-ready"]},
        {"platform": "Telegram", "target": "telegram:-100123"},  # no events → all
        {"platform": "Signal", "target": ""},                    # invalid → skipped
    ]}}
    check("notifications[] overrides legacy deliver",
          "slack:legacy" not in disp._notify_targets(multi, "doc-report"))
    check("doc-report goes to subscribed + catch-all targets",
          disp._notify_targets(multi, "doc-report") == ["slack:C1", "telegram:-100123"])
    check("dispatch-summary respects event filters",
          disp._notify_targets(multi, "dispatch-summary")
          == ["discord:#general", "telegram:-100123"])
    check("pipeline-failure reaches only catch-all",
          disp._notify_targets(multi, "pipeline-failure") == ["telegram:-100123"])


def test_summary_events():
    disp = _load_dispatch()
    check("plain tick → dispatch-summary only",
          disp._summary_events({"created": [1]}) == {"dispatch-summary"})
    check("error adds pipeline-failure",
          "pipeline-failure" in disp._summary_events({"error": "boom"}))
    check("advanced PRs add pr-ready",
          "pr-ready" in disp._summary_events({"advance_prs": [7]}))
    check("reconciled adds pr-ready",
          "pr-ready" in disp._summary_events({"reconciled": [(7, "In review")]}))
    check("blocked adds security-escalation",
          "security-escalation" in disp._summary_events({"blocked": [42]}))


def test_notify_project_summary_fans_out():
    """_notify_project_summary sends each project summary to its own targets."""
    disp = _load_dispatch()
    sent = []
    summary = {"board": "b", "mode": "github", "created": [1], "reconciled": [],
               "completed": [], "advance_prs": [], "routed_actions": {},
               "issues_seen": 1, "spec_created": [], "slack_delivered": []}

    resolved = {"cron": {"notifications": [
        {"platform": "Slack", "target": "slack:C1", "events": ["dispatch-summary"]},
        {"platform": "Discord", "target": "discord:#x", "events": ["doc-report"]},
    ]}}
    with mock.patch.object(disp, "_send_via_hermes",
                           side_effect=lambda t, m: sent.append(t) or True):
        handled = disp._notify_project_summary("proj", summary, resolved)
    check("notifications project is handled (excluded from stdout)", handled is True)
    check("summary sent only to dispatch-summary targets", sent == ["slack:C1"])

    sent.clear()
    legacy = {"cron": {"deliver": "slack:tasks"}}
    with mock.patch.object(disp, "_send_via_hermes",
                           side_effect=lambda t, m: sent.append(t) or True):
        handled2 = disp._notify_project_summary("proj", summary, legacy)
    check("legacy project flows through cron stdout (not self-sent)",
          handled2 is False and sent == [])


def test_deliver_doc_reports_multi_target():
    """_deliver_doc_reports fans a report out to every configured target."""
    disp = _load_dispatch()
    doc_card = {"id": "t_doc", "assignee": "documentation-daedalus", "body": "PR #42.",
                "latest_summary": "", "parents": []}
    sent = []
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[doc_card]):
        with mock.patch.object(gp, "pr_has_delivery_marker", return_value=False):
            with mock.patch.object(disp, "_find_doc_comment",
                                   return_value="**Agent: documentation**\n\nReport"):
                with mock.patch.object(disp, "_send_via_hermes",
                                       side_effect=lambda t, m: sent.append(t) or True):
                    with mock.patch.object(gp, "post_delivery_marker", return_value=True):
                        result = disp._deliver_doc_reports(
                            "slug", gp, ["slack:C1", "discord:#docs"],
                        )
    check("multi-target delivers the PR once", result == [42])
    check("report sent to every target", sent == ["slack:C1", "discord:#docs"])

    # Partial failure: sentinel still posted (one target got it), PR delivered
    sent.clear()
    marker_posts = []
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[doc_card]):
        with mock.patch.object(gp, "pr_has_delivery_marker", return_value=False):
            with mock.patch.object(disp, "_find_doc_comment",
                                   return_value="**Agent: documentation**\n\nReport"):
                with mock.patch.object(disp, "_send_via_hermes",
                                       side_effect=lambda t, m: t == "slack:C1"):
                    with mock.patch.object(gp, "post_delivery_marker",
                                           side_effect=lambda pr, body="": marker_posts.append(pr) or True):
                        result2 = disp._deliver_doc_reports(
                            "slug", gp, ["slack:C1", "discord:#docs"],
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
    import tempfile, time
    from core import dispatch_state as ds
    with tempfile.TemporaryDirectory() as tmp:
        ds.record_dispatch(tmp, 42)
        age = ds.get_dispatch_age_hours(tmp, 42)
        check("get_dispatch_age_hours returns a non-negative float", isinstance(age, float) and age >= 0)
        check("get_dispatch_age_hours returns None for unknown issue", ds.get_dispatch_age_hours(tmp, 99) is None)


def test_dispatch_state_clear():
    """clear_dispatch removes the record so age returns None."""
    import tempfile
    from core import dispatch_state as ds
    with tempfile.TemporaryDirectory() as tmp:
        ds.record_dispatch(tmp, 5)
        ds.clear_dispatch(tmp, 5)
        check("clear_dispatch removes dispatch record", ds.get_dispatch_age_hours(tmp, 5) is None)


def test_dispatch_state_pr_flags():
    """has_pr_flag / set_pr_flag are idempotent."""
    import tempfile
    from core import dispatch_state as ds
    with tempfile.TemporaryDirectory() as tmp:
        check("has_pr_flag returns False before set", not ds.has_pr_flag(tmp, 7, "size_warned"))
        ds.set_pr_flag(tmp, 7, "size_warned")
        check("has_pr_flag returns True after set", ds.has_pr_flag(tmp, 7, "size_warned"))
        ds.set_pr_flag(tmp, 7, "size_warned")  # idempotent
        check("set_pr_flag idempotent — flag stays True", ds.has_pr_flag(tmp, 7, "size_warned"))
        check("unrelated flag still False", not ds.has_pr_flag(tmp, 7, "other"))


def test_dispatch_state_review_sha():
    """record_review / get_review_sha round-trip."""
    import tempfile
    from core import dispatch_state as ds
    with tempfile.TemporaryDirectory() as tmp:
        ds.record_review(tmp, 10, "reviewer-daedalus", "abc123")
        sha = ds.get_review_sha(tmp, 10, "reviewer-daedalus")
        check("get_review_sha returns stored SHA", sha == "abc123")
        check("get_review_sha returns None for unknown reviewer",
              ds.get_review_sha(tmp, 10, "other") is None)


# ── dispatch: priority sort ───────────────────────────────────────────────────

def test_priority_sort_p0_before_unlabeled():
    """P0-labelled issues are dispatched before unlabelled ones."""
    from core.providers.base import IssueSummary
    disp = _load_dispatch()
    dispatched = []

    class FP(_FakeProvider):
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names): return {1, 2}
        def list_issues(self, state="open", labels=None, limit=50):
            return [
                IssueSummary(number=2, title="normal", labels=[]),
                IssueSummary(number=1, title="urgent", labels=["P0"]),
            ]
        def pr_state_for_issue(self, n): return None

    disp.kanban.ensure_board = lambda s: None
    disp.kanban.list_blocked = lambda s: []
    disp.kanban.list_issue_numbers = lambda s: set()
    disp.kanban.dispatch = lambda s, max_spawns=5: True
    disp.kanban.list_tasks = lambda *a, **k: []

    _orig_create_task = disp.kanban.create_task
    try:
        def fake_create(slug, title, body="", *, assignee="", idempotency_key="", workspace="", max_retries=None, skills=None, goal=False, goal_max_turns=None, parents=None):
            m = __import__("re").search(r"#(\d+)", title)
            if m:
                dispatched.append(int(m.group(1)))
            return "t_x"
        disp.kanban.create_task = fake_create
        disp.run(
            {"repo": "O/R", "workdir": "/tmp", "name": "x",
             "issues": {"filters": {}}, "execution": {},
             "tracking": {"github_project_number": 1}},
            provider=FP(), max_dispatch=1,
        )
    finally:
        disp.kanban.create_task = _orig_create_task

    check("priority sort dispatches P0 issue first",
          len(dispatched) >= 1 and dispatched[0] == 1)


def test_priority_sort_dict_labels():
    """Priority sort handles GitHub-style label dicts ({"name": "P1"})."""
    disp = _load_dispatch()
    sorted_issues = disp._PRIORITY  # just check the constant exists
    check("_PRIORITY dict has p0/P0 entries", sorted_issues.get("p0") == 0 and sorted_issues.get("P0") == 0)
    check("_PRIORITY dict has p1/p2 entries", sorted_issues.get("p1") == 1 and sorted_issues.get("p2") == 2)


# ── dispatch: PR size gate ────────────────────────────────────────────────────

def test_pr_size_gate_warns_once():
    """size gate posts a PR comment once and sets the size_warned flag."""
    import tempfile
    from core import dispatch_state as ds
    disp = _load_dispatch()
    posted = []

    class FP(_FakeProvider):
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names): return set()
        def list_issues(self, state="open", labels=None, limit=50):
            from core.providers.base import IssueSummary
            return [IssueSummary(number=5, title="big change")]
        def pr_state_for_issue(self, n): return "open"
        def pr_number_for_issue(self, n): return 99
        def pr_ci_green(self, pr): return False
        def get_pr_files(self, pr):
            return [{"filename": "big.py", "changes": 600}]
        def post_pr_comment(self, pr, body):
            posted.append((pr, body))
            return True
        def post_issue_comment(self, n, body): return True
        def board_ensure_status_option(self, *a): return True

    with tempfile.TemporaryDirectory() as tmp:
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {5}
        disp.kanban.dispatch = lambda s, max_spawns=5: True
        disp.kanban.list_tasks = lambda *a, **k: []
        disp.run(
            {"repo": "O/R", "workdir": tmp, "name": "x",
             "issues": {"filters": {}},
             "execution": {"max_pr_lines": 500},
             "tracking": {"github_project_number": 1}},
            provider=FP(),
        )
        check("size gate posts a warning comment",
              len(posted) == 1 and "too large" in posted[0][1].lower())
        check("size gate sets size_warned flag", ds.has_pr_flag(tmp, 99, "size_warned"))


def test_pr_size_gate_idempotent():
    """size gate does NOT re-post if size_warned flag is already set."""
    import tempfile
    from core import dispatch_state as ds
    disp = _load_dispatch()
    posted = []

    class FP(_FakeProvider):
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names): return set()
        def list_issues(self, state="open", labels=None, limit=50):
            from core.providers.base import IssueSummary
            return [IssueSummary(number=5, title="big change")]
        def pr_state_for_issue(self, n): return "open"
        def pr_number_for_issue(self, n): return 99
        def pr_ci_green(self, pr): return False
        def get_pr_files(self, pr):
            return [{"filename": "big.py", "changes": 600}]
        def post_pr_comment(self, pr, body):
            posted.append(pr)
            return True
        def post_issue_comment(self, n, body): return True
        def board_ensure_status_option(self, *a): return True

    with tempfile.TemporaryDirectory() as tmp:
        ds.set_pr_flag(tmp, 99, "size_warned")  # pre-seed
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {5}
        disp.kanban.dispatch = lambda s, max_spawns=5: True
        disp.kanban.list_tasks = lambda *a, **k: []
        disp.run(
            {"repo": "O/R", "workdir": tmp, "name": "x",
             "issues": {"filters": {}},
             "execution": {"max_pr_lines": 500},
             "tracking": {"github_project_number": 1}},
            provider=FP(),
        )
        check("size gate does not re-post when flag already set", posted == [])


def test_pr_size_gate_disabled_when_zero():
    """max_pr_lines=0 disables the size gate entirely."""
    import tempfile
    disp = _load_dispatch()
    posted = []

    class FP(_FakeProvider):
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names): return set()
        def list_issues(self, state="open", labels=None, limit=50):
            from core.providers.base import IssueSummary
            return [IssueSummary(number=5, title="big change")]
        def pr_state_for_issue(self, n): return "open"
        def pr_number_for_issue(self, n): return 99
        def pr_ci_green(self, pr): return False
        def get_pr_files(self, pr):
            return [{"filename": "big.py", "changes": 9999}]
        def post_pr_comment(self, pr, body):
            posted.append(pr)
            return True
        def post_issue_comment(self, n, body): return True
        def board_ensure_status_option(self, *a): return True

    with tempfile.TemporaryDirectory() as tmp:
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {5}
        disp.kanban.dispatch = lambda s, max_spawns=5: True
        disp.kanban.list_tasks = lambda *a, **k: []
        disp.run(
            {"repo": "O/R", "workdir": tmp, "name": "x",
             "issues": {"filters": {}},
             "execution": {"max_pr_lines": 0},
             "tracking": {"github_project_number": 1}},
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
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names): return set()
        def list_issues(self, state="open", labels=None, limit=50):
            from core.providers.base import IssueSummary
            return [IssueSummary(number=6, title="env change")]
        def pr_state_for_issue(self, n): return "open"
        def pr_number_for_issue(self, n): return 88
        def pr_ci_green(self, pr): return False
        def get_pr_files(self, pr):
            return [{"filename": ".env", "changes": 2}]
        def post_pr_comment(self, pr, body):
            posted.append((pr, body))
            return True
        def post_issue_comment(self, n, body): return True
        def board_ensure_status_option(self, *a): return True

    with tempfile.TemporaryDirectory() as tmp:
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {6}
        disp.kanban.dispatch = lambda s, max_spawns=5: True
        disp.kanban.list_tasks = lambda *a, **k: []
        disp.run(
            {"repo": "O/R", "workdir": tmp, "name": "x",
             "issues": {"filters": {}}, "execution": {},
             "tracking": {"github_project_number": 1}},
            provider=FP(),
        )
        check("forbidden guard posts warning for .env",
              len(posted) == 1 and ".env" in posted[0][1])
        check("forbidden guard sets forbidden_warned flag",
              ds.has_pr_flag(tmp, 88, "forbidden_warned"))


def test_forbidden_file_guard_idempotent():
    """forbidden guard does NOT re-post when flag is already set."""
    import tempfile
    from core import dispatch_state as ds
    disp = _load_dispatch()
    posted = []

    class FP(_FakeProvider):
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names): return set()
        def list_issues(self, state="open", labels=None, limit=50):
            from core.providers.base import IssueSummary
            return [IssueSummary(number=6, title="env change")]
        def pr_state_for_issue(self, n): return "open"
        def pr_number_for_issue(self, n): return 88
        def pr_ci_green(self, pr): return False
        def get_pr_files(self, pr):
            return [{"filename": ".env", "changes": 2}]
        def post_pr_comment(self, pr, body):
            posted.append(pr)
            return True
        def post_issue_comment(self, n, body): return True
        def board_ensure_status_option(self, *a): return True

    with tempfile.TemporaryDirectory() as tmp:
        ds.set_pr_flag(tmp, 88, "forbidden_warned")  # pre-seed
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {6}
        disp.kanban.dispatch = lambda s, max_spawns=5: True
        disp.kanban.list_tasks = lambda *a, **k: []
        disp.run(
            {"repo": "O/R", "workdir": tmp, "name": "x",
             "issues": {"filters": {}}, "execution": {},
             "tracking": {"github_project_number": 1}},
            provider=FP(),
        )
        check("forbidden guard does not re-post when flag already set", posted == [])


# ── dispatch: staleness detection ────────────────────────────────────────────

def test_staleness_warns_when_over_threshold():
    """Issues dispatched > staleness_hours ago with no PR trigger a comment."""
    import tempfile, json, time
    from core import dispatch_state as ds
    disp = _load_dispatch()
    issue_comments = []

    class FP(_FakeProvider):
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names): return set()
        def list_issues(self, state="open", labels=None, limit=50):
            from core.providers.base import IssueSummary
            return [IssueSummary(number=7, title="stale issue")]
        def pr_state_for_issue(self, n): return None
        def post_issue_comment(self, n, body):
            issue_comments.append((n, body))
            return True

    with tempfile.TemporaryDirectory() as tmp:
        # Seed state: dispatched 50 hours ago
        state_path = (Path(tmp) / ".hermes" / "daedalus_dispatch_state.json")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "issues": {"7": {"dispatched_at": time.time() - 50 * 3600}}
        }))
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {7}
        disp.kanban.dispatch = lambda s, max_spawns=5: True
        disp.kanban.list_tasks = lambda *a, **k: []
        disp.run(
            {"repo": "O/R", "workdir": tmp, "name": "x",
             "issues": {"filters": {}},
             "execution": {"staleness_hours": 48},
             "tracking": {"github_project_number": 1}},
            provider=FP(),
        )
        check("staleness check posts issue comment for stale issue",
              len(issue_comments) == 1 and issue_comments[0][0] == 7)
        check("staleness warning mentions 'stale' or 'hours'",
              "hours" in issue_comments[0][1].lower() or "stale" in issue_comments[0][1].lower())
        check("staleness sets stale_warned flag", ds.has_pr_flag(tmp, 7, "stale_warned"))


def test_staleness_silent_when_under_threshold():
    """Issues dispatched < staleness_hours ago do NOT trigger a comment."""
    import tempfile, json, time
    disp = _load_dispatch()
    issue_comments = []

    class FP(_FakeProvider):
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names): return set()
        def list_issues(self, state="open", labels=None, limit=50):
            from core.providers.base import IssueSummary
            return [IssueSummary(number=8, title="fresh issue")]
        def pr_state_for_issue(self, n): return None
        def post_issue_comment(self, n, body):
            issue_comments.append((n, body))
            return True

    with tempfile.TemporaryDirectory() as tmp:
        state_path = (Path(tmp) / ".hermes" / "daedalus_dispatch_state.json")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "issues": {"8": {"dispatched_at": time.time() - 2 * 3600}}  # 2h ago
        }))
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {8}
        disp.kanban.dispatch = lambda s, max_spawns=5: True
        disp.kanban.list_tasks = lambda *a, **k: []
        disp.run(
            {"repo": "O/R", "workdir": tmp, "name": "x",
             "issues": {"filters": {}},
             "execution": {"staleness_hours": 48},
             "tracking": {"github_project_number": 1}},
            provider=FP(),
        )
        check("staleness check silent when issue is fresh", issue_comments == [])


# ── dispatch: label overrides ─────────────────────────────────────────────────

def test_label_overrides_skip_developer():
    """skip_developer=true removes the DEVELOPER role from the downstream body."""
    disp = _load_dispatch()
    body = disp._downstream_body(
        "O/R",
        {"number": 10, "title": "doc fix", "body": "desc", "labels": [{"name": "documentation"}]},
        3, "/tmp", "slack:C1", "main", "github", [],
        label_overrides={"documentation": {"skip_developer": True}},
    )
    check("skip_developer removes DEVELOPER role", "DEVELOPER" not in body)
    check("skip_developer keeps REVIEWER role", "REVIEWER" in body)


def test_label_overrides_security_first():
    """security_first=true puts SECURITY-ANALYST before DEVELOPER in the body."""
    disp = _load_dispatch()
    body = disp._downstream_body(
        "O/R",
        {"number": 11, "title": "auth change", "body": "desc", "labels": [{"name": "security"}]},
        3, "/tmp", "slack:C1", "main", "github", [],
        label_overrides={"security": {"security_first": True}},
    )
    sec_pos = body.find("SECURITY-ANALYST")
    dev_pos = body.find("DEVELOPER")
    check("security_first puts SECURITY-ANALYST before DEVELOPER",
          sec_pos != -1 and dev_pos != -1 and sec_pos < dev_pos)


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
    check("defaults include documentation", p["documentation"] == "documentation-daedalus")


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
    p = disp._resolve_profiles({"profiles": {"developer": "my-dev", "nonexistent_role": "x"}})
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
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names): return {42}
        def list_issues(self, state="open", labels=None, limit=50):
            return [IssueSummary(number=42, title="bug", labels=[])]
        def pr_state_for_issue(self, n): return None
        def get_issue_state(self, n): return "open"

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
        def fake_create(slug, title, body="", *, assignee="", idempotency_key="", workspace="", max_retries=None, skills=None, goal=False, goal_max_turns=None, parents=None):
            assigned.append(assignee)
            return "t1"
        disp.kanban.create_task = fake_create
        disp.run(
            {"repo": "O/R", "workdir": "/tmp", "name": "x",
             "issues": {"filters": {}},
             "execution": {"profiles": {"validator": "my-validator"}},
             "tracking": {"github_project_number": 1}},
            provider=FP(),
        )
    finally:
        disp.kanban.create_task = _orig
        disp._hermes_profile_exists = _orig_exists

    check("custom validator profile used for task creation",
          "my-validator" in assigned)


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
            return [{"title": "#7 some issue", "assignee": "validator-daedalus",
                     "summary": "CONFIRMED: reproduced at main", "status": "done"}]
        return []

    _orig = disp.kanban.create_task
    try:
        disp.kanban.list_tasks = fake_list_tasks
        def fake_create(slug, title, body="", *, assignee="", idempotency_key="", workspace="", max_retries=None, skills=None, goal=False, goal_max_turns=None, parents=None):
            assigned.append(assignee)
            return "t2"
        disp.kanban.create_task = fake_create
        disp._check_confirmed_validators(
            "slug", "O/R",
            {7: {"number": 7, "title": "some issue", "body": ""}},
            3, "/tmp", "", "main", "github",
            profiles={"validator": "validator-daedalus", "pm": "my-pm",
                      "developer": "developer-daedalus", "reviewer": "reviewer-daedalus",
                      "security": "security-analyst-daedalus", "documentation": "documentation-daedalus"},
        )
    finally:
        disp.kanban.create_task = _orig

    check("custom pm profile used for PM task creation", "my-pm" in assigned)


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
            check("directory profile exists", disp._hermes_profile_exists("my-profile") is True)
            check("non-existent profile returns False", disp._hermes_profile_exists("no-such") is False)


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
            check("yaml profile exists", disp._hermes_profile_exists("yaml-profile") is True)


def test_validate_profiles_all_present_no_warning():
    """When every profile exists, _validate_profiles returns the input unchanged and logs nothing."""
    import logging
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
    check("warning mentions the missing profile name", "'does-not-exist'" in calls_as_text or "does-not-exist" in calls_as_text)
    check("warning mentions the role", "'developer'" in calls_as_text or "developer" in calls_as_text)
    # Fallback behavior: developer should be replaced with the built-in default
    check("missing profile falls back to default",
          result["developer"] == disp._DEFAULT_PROFILES["developer"])
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
    check("skip behavior keeps existing role", result["validator"] == "validator-daedalus")
    check("skip warning mentions skipping", "skipping" in str(warn.call_args_list).lower() or "Skipping" in str(warn.call_args_list))


def test_validate_profiles_fallback_explicit():
    """With fallback_behavior='fallback' (default), missing roles fall back to built-ins."""
    disp = _load_dispatch()
    profiles = {"validator": "bad-name", "pm": "project-manager-daedalus"}
    def exists(name):
        return name != "bad-name"
    with mock.patch.object(disp, "_hermes_profile_exists", side_effect=exists):
        result = disp._validate_profiles(profiles, fallback_behavior="fallback")
    check("explicit fallback replaces with default",
          result["validator"] == disp._DEFAULT_PROFILES["validator"])
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
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names): return {1, 2, 3}
        def list_issues(self, state="open", labels=None, limit=50):
            return [
                IssueSummary(number=n, title=f"issue {n}", labels=[])
                for n in (1, 2, 3)
            ]
        def pr_state_for_issue(self, n): return None
        def get_issue_state(self, n): return "open"

    disp.kanban.ensure_board = lambda s: None
    disp.kanban.list_blocked = lambda s: []
    disp.kanban.list_issue_numbers = lambda s: set()
    disp.kanban.dispatch = lambda s, max_spawns=5: True
    disp.kanban.list_tasks = lambda *a, **k: []
    disp._fetch_issues = lambda r, f: [{"number": n, "title": f"issue {n}"} for n in (1, 2, 3)]
    _orig_create = disp.kanban.create_task
    try:
        disp.kanban.create_task = lambda *a, **k: "t_x"
        disp._validate_profiles = counting_validate
        disp.run({
            "repo": "O/R", "workdir": "/tmp", "name": "x",
            "tracking": {"github_project_number": 1},
            "issues": {"filters": {}},
            "execution": {},
        }, provider=FP())
    finally:
        disp.kanban.create_task = _orig_create
        disp._validate_profiles = orig_validate

    check("validate_profiles called exactly once per tick", call_count["n"] == 1)


# ── PM consultation (team blocker re-activation) ──────────────────────────────

def test_has_active_pm_consultation_true():
    """_has_active_pm_consultation returns True when an open consult task exists."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: [
        {"title": "consult: #5 login bug", "assignee": "project-manager-daedalus", "status": "in_progress"},
    ]
    check("active consult detected", disp._has_active_pm_consultation("slug", 5) is True)


def test_has_active_pm_consultation_false_when_done():
    """_has_active_pm_consultation returns False when the consult task is done."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: [
        {"title": "consult: #5 login bug", "assignee": "pm-daedalus", "status": "done"},
    ]
    check("done consult not counted as active", disp._has_active_pm_consultation("slug", 5) is False)


def test_has_active_pm_consultation_false_for_spec_task():
    """_has_active_pm_consultation ignores regular PM spec tasks."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: [
        {"title": "#5 login bug", "assignee": "pm-daedalus", "status": "in_progress"},
    ]
    check("spec task not counted as consult", disp._has_active_pm_consultation("slug", 5) is False)


def test_check_team_blockers_creates_pm_consultation():
    """_check_team_blockers creates a PM consultation for a blocked team card."""
    disp = _load_dispatch()
    created_titles = []
    assigned_to = []

    disp.kanban.list_blocked = lambda s: [
        {"title": "#9 feature", "assignee": "developer-daedalus",
         "summary": "BLOCKED: cannot resolve import path"},
    ]

    def fake_list_tasks(s):
        return []  # no active consultation

    _orig = disp.kanban.create_task
    try:
        disp.kanban.list_tasks = fake_list_tasks
        def fake_create(slug, title, body="", *, assignee="", idempotency_key="", workspace="", max_retries=None, skills=None, goal=False, goal_max_turns=None, parents=None):
            created_titles.append(title)
            assigned_to.append(assignee)
            return "consult_t1"
        disp.kanban.create_task = fake_create
        triggered = disp._check_team_blockers(
            "slug", "O/R",
            {9: {"number": 9, "title": "feature", "body": "desc"}},
            "/tmp", "main", "github",
        )
    finally:
        disp.kanban.create_task = _orig

    check("blocker triggers PM consultation", 9 in triggered)
    check("consultation task title starts with consult:",
          any(t.lower().startswith("consult:") for t in created_titles))
    check("consultation assigned to default pm profile", assigned_to and assigned_to[0] in ("pm-daedalus", "project-manager-daedalus"))


def test_check_team_blockers_skips_when_consult_active():
    """_check_team_blockers skips creation if an active consultation already exists."""
    disp = _load_dispatch()
    created = []

    disp.kanban.list_blocked = lambda s: [
        {"title": "#9 feature", "assignee": "developer-daedalus",
         "summary": "BLOCKED: still stuck"},
    ]
    # Active consultation already open
    disp.kanban.list_tasks = lambda s: [
        {"title": "consult: #9 feature", "assignee": "project-manager-daedalus", "status": "in_progress"},
    ]

    _orig = disp.kanban.create_task
    try:
        disp.kanban.create_task = lambda *a, **k: (created.append(1) or "t")
        triggered = disp._check_team_blockers(
            "slug", "O/R",
            {9: {"number": 9, "title": "feature", "body": "desc"}},
            "/tmp", "main", "github",
        )
    finally:
        disp.kanban.create_task = _orig

    check("no duplicate consultation when one is active", triggered == [])
    check("no task created", created == [])


def test_check_team_blockers_skips_escalation():
    """_check_team_blockers ignores cards with ESCALATE: summaries (security blocks)."""
    disp = _load_dispatch()
    created = []

    disp.kanban.list_blocked = lambda s: [
        {"title": "#9 feature", "assignee": "developer-daedalus",
         "summary": "ESCALATE: security threat detected"},
    ]
    disp.kanban.list_tasks = lambda s: []

    _orig = disp.kanban.create_task
    try:
        disp.kanban.create_task = lambda *a, **k: (created.append(1) or "t")
        triggered = disp._check_team_blockers(
            "slug", "O/R",
            {9: {"number": 9, "title": "feature", "body": "desc"}},
            "/tmp", "main", "github",
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
        {"title": "#3 issue", "assignee": "validator-daedalus",
         "summary": "BLOCKED: needs more info"},
        {"title": "#3 issue", "assignee": "project-manager-daedalus",
         "summary": "BLOCKED: unclear scope"},
    ]
    disp.kanban.list_tasks = lambda s: []

    _orig = disp.kanban.create_task
    try:
        disp.kanban.create_task = lambda *a, **k: (created.append(1) or "t")
        triggered = disp._check_team_blockers(
            "slug", "O/R",
            {3: {"number": 3, "title": "issue", "body": ""}},
            "/tmp", "main", "github",
        )
    finally:
        disp.kanban.create_task = _orig

    check("validator blocks not treated as PM blockers", triggered == [])
    check("PM blocks not treated as PM blockers (already PM)", created == [])


def test_pm_consultation_body_content():
    """_pm_consultation_body includes blocker info and CLARIFIED: instructions."""
    disp = _load_dispatch()
    body = disp._pm_consultation_body(
        "O/R", {"number": 12, "title": "login bug"}, "BLOCKED: can't find auth middleware",
        "/tmp", "github",
    )
    check("consultation body mentions TEAM BLOCKER", "TEAM BLOCKER" in body)
    check("consultation body includes the blocker summary",
          "can't find auth middleware" in body)
    check("consultation body requires CLARIFIED: prefix", "CLARIFIED: " in body)
    check("consultation body forbids writing code", "DO NOT write code" in body)


def test_has_pm_tasks_excludes_consultations():
    """_has_pm_tasks returns False for consultation tasks (title starts with consult:)."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: [
        {"title": "consult: #5 login bug", "assignee": "pm-daedalus", "status": "in_progress"},
    ]
    check("consultation task not counted as PM spec task",
          disp._has_pm_tasks("slug", 5) is False)


def test_has_pm_tasks_true_for_spec_task():
    """_has_pm_tasks returns True for a PM spec task (no consult: prefix)."""
    disp = _load_dispatch()
    disp.kanban.list_tasks = lambda s: [
        {"title": "#5 login bug", "assignee": "project-manager-daedalus", "status": "in_progress"},
    ]
    check("spec task correctly detected as PM task",
          disp._has_pm_tasks("slug", 5) is True)


# ── _FakeProvider base class additions (used by size gate / forbidden tests) ──
# (Patch _FakeProvider at module level to avoid test-isolation issues)

_FakeProvider.get_pr_files = lambda self, pr: []
_FakeProvider.post_issue_comment = lambda self, n, body: True
_FakeProvider.board_ensure_status_option = lambda self, *a: True
_FakeProvider.append_changelog = lambda self, *a: False


if __name__ == "__main__":
    print("Daedalus tests")
    print("-" * 60)
    for fn in (test_deep_merge, test_config_loader_resolve,
               test_kanban_list_issue_numbers, test_kanban_list_issue_numbers_large_ids,
               test_create_triage_pins_workspace,
               test_kanban_review_handoff_pr,
               test_dispatch_dual_mode,
               test_resolve_repo_config_valid, test_resolve_repo_config_sources_toggles,
               test_resolve_repo_config_missing_file,
               test_main_registry_sweep, test_main_single_repo,
               test_parse_pr_from_card, test_find_doc_comment,
               test_send_via_hermes, test_deliver_doc_reports_idempotent,
               test_deliver_doc_reports_no_target,
               test_deliver_doc_reports_send_failure,
               test_deliver_doc_reports_non_doc_assignee,
               test_deliver_doc_reports_no_pr,
               test_deliver_doc_reports_no_doc_comment,
               test_deliver_doc_reports_dry_run,
               test_resolve_pr_from_parents,
               test_human_summary_slack_delivered,
               test_task_body_no_slack,
               test_dispatch_summary_has_slack_delivered,
               test_notify_targets,
               test_summary_events,
               test_notify_project_summary_fans_out,
               test_deliver_doc_reports_multi_target,
               test_ensure_board_creates, test_ensure_board_already_exists,
               test_ensure_board_failure,
               test_dispatch_state_record_and_age, test_dispatch_state_clear,
               test_dispatch_state_pr_flags, test_dispatch_state_review_sha,
               test_priority_sort_p0_before_unlabeled, test_priority_sort_dict_labels,
               test_pr_size_gate_warns_once, test_pr_size_gate_idempotent,
               test_pr_size_gate_disabled_when_zero,
               test_forbidden_file_guard_warns_once, test_forbidden_file_guard_idempotent,
               test_staleness_warns_when_over_threshold,
               test_staleness_silent_when_under_threshold,
               test_label_overrides_skip_developer, test_label_overrides_security_first,
               test_resolve_profiles_defaults, test_resolve_profiles_user_overrides,
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
               test_has_pm_tasks_true_for_spec_task):
        fn()
    print("-" * 60)
    print(f"Results: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)

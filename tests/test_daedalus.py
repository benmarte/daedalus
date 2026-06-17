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

    def _pr_for_issue(self, n):
        return None

    def get_issue_state(self, n):
        return "open"

    def update_pr_body(self, pr, body):
        return True

    def get_pr_files(self, pr):
        return []

    def post_issue_comment(self, n, body):
        return True

    def board_ensure_status_option(self, name, color="RED"):
        return True

    def append_changelog(self, base_branch, entry):
        return True


gp = _FakeProvider()  # patched per-test via mock.patch.object


class _PR:
    """Duck-type PRSummary for reconciliation-loop tests."""
    def __init__(self, number, state="open", head_branch="", base_branch="dev", body=""):
        self.number = number
        self.state = state
        self.head_branch = head_branch
        self.base_branch = base_branch
        self.body = body


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
    tasks = [
        {"title": "#329 foo", "id": "t_a"},
        {"title": "#42 bar", "id": "t_b"},
        {"title": "no-number", "id": "t_c"},
        {"title": None, "id": "t_d"},       # empty title
        {"title": "", "id": "t_e"},
    ]
    with mock.patch.object(kanban, "list_tasks", return_value=tasks):
        nums = kanban.list_issue_numbers("board")
    check("list_issue_numbers parses #n from board output", nums == {329, 42})


def test_kanban_list_issue_numbers_large_ids():
    """Issue numbers with 4+ digits must not be missed (old regex-on-ls-output bug)."""
    tasks = [
        {"title": "#2003 BUG + PERFORMANCE: something", "id": "t_1"},
        {"title": "#999 edge", "id": "t_2"},
        {"title": "#10000 five digits", "id": "t_3"},
        {"title": "refs PR #12345 inside title", "id": "t_4"},
        {"title": "no number", "id": "t_5"},
    ]
    with mock.patch.object(kanban, "list_tasks", return_value=tasks):
        nums = kanban.list_issue_numbers("board")
    check("4+ digit issue numbers are parsed", nums == {2003, 999, 10000, 12345})


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
    captured = {}

    def fake_hk(args, timeout=60):
        captured["args"] = args
        return (1, "", "board 'my-board' already exists.")

    with mock.patch.object(kanban, "_hk", fake_hk):
        ok = kanban.ensure_board("my-board")
    check("ensure_board returns True when board already exists", ok is True)


def test_ensure_board_failure():
    """ensure_board returns False + warns on genuine failure."""
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
    disp.kanban.ensure_board = lambda s: None
    disp.kanban.list_blocked = lambda s: []
    disp.kanban.list_issue_numbers = lambda s: set()
    disp.kanban.decompose_all_triage = lambda s: calls.__setitem__("decompose_all", calls["decompose_all"] + 1) or True
    disp.kanban.create_triage = lambda *a, **k: calls.__setitem__("create_triage", calls["create_triage"] + 1) or "t_x"
    disp.kanban.decompose = lambda *a, **k: True
    disp.kanban.dispatch = lambda s, max_spawns=5: True
    disp._fetch_issues = lambda r, f: (calls.__setitem__("fetch_issues", calls["fetch_issues"] + 1) or [{"number": 1, "title": "t"}])
    base = {"repo": "O/R", "workdir": "/tmp", "name": "x", "issues": {"filters": {}}, "execution": {}}

    # Save and restore create_task + list_tasks to avoid cross-file test isolation issues
    _orig_create_task = disp.kanban.create_task
    _orig_list_tasks = disp.kanban.list_tasks
    try:
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
        disp.kanban.create_task = _orig_create_task
        disp.kanban.list_tasks = _orig_list_tasks


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


# ── _has_notified_block ──────────────────────────────────────────────────────

def test_has_notified_block_true():
    """Returns True when escalation marker comment exists on validator task."""
    disp = _load_dispatch()
    tasks = [{"id": "t_v1", "title": "#7 Fix bug", "assignee": "validator-daedalus"}]
    card_with_marker = {"comments": [{"body": disp._ESCALATION_MARKER}]}
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        with mock.patch.object(disp.kanban, "show_card", return_value=card_with_marker):
            result = disp._has_notified_block("slug", 7)
    check("_has_notified_block True when marker present", result is True)


def test_has_notified_block_false():
    """Returns False when no escalation marker comment on validator task."""
    disp = _load_dispatch()
    tasks = [{"id": "t_v1", "title": "#7 Fix bug", "assignee": "validator-daedalus"}]
    card_no_marker = {"comments": [{"body": "just a regular comment"}]}
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        with mock.patch.object(disp.kanban, "show_card", return_value=card_no_marker):
            result = disp._has_notified_block("slug", 7)
    check("_has_notified_block False when no marker", result is False)


def test_has_notified_block_wrong_assignee():
    """Ignores cards not assigned to validator-daedalus."""
    disp = _load_dispatch()
    tasks = [{"id": "t_dev", "title": "#7 Fix bug", "assignee": "developer-daedalus"}]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        result = disp._has_notified_block("slug", 7)
    check("_has_notified_block ignores non-validator cards", result is False)


# ── _enforce_validator_blocks ────────────────────────────────────────────────

def test_enforce_validator_blocks_no_board():
    """Returns [] when provider has no board configured."""
    disp = _load_dispatch()
    result = disp._enforce_validator_blocks("slug", _FakeProvider(), {1, 2})
    check("no board → []", result == [])


def test_enforce_validator_blocks_no_blocked_cards():
    """Returns [] when no blocked kanban cards exist."""
    disp = _load_dispatch()
    class FP(_FakeProvider):
        def board_configured(self): return True
    with mock.patch.object(disp.kanban, "list_blocked", return_value=[]):
        result = disp._enforce_validator_blocks("slug", FP(), {1})
    check("no blocked cards → []", result == [])


def test_enforce_validator_blocks_sets_blocked():
    """Blocked validator card → board Blocked + downstream cancelled + issue returned."""
    disp = _load_dispatch()
    class FP(_FakeProvider):
        def board_configured(self): return True
    fp = FP()
    board_calls = []
    blocked_card = {
        "id": "t_v", "title": "#5 Fix login",
        "assignee": "validator-daedalus", "summary": "BLOCKED: security issue",
    }
    with mock.patch.object(disp.kanban, "list_blocked", return_value=[blocked_card]):
        with mock.patch.object(fp, "board_set_status",
                               side_effect=lambda n, s: board_calls.append((n, s)) or True):
            with mock.patch.object(disp.kanban, "close_non_blocked_issue_tasks",
                                   return_value=["t_dev1"]):
                with mock.patch.object(disp, "_has_notified_block", return_value=False):
                    with mock.patch.object(disp, "_mark_notified_block"):
                        result = disp._enforce_validator_blocks("slug", fp, {5})
    check("blocked validator → board set to Blocked", board_calls == [(5, "Blocked")])
    check("blocked validator → issue returned", result == [5])


def test_enforce_validator_blocks_idempotent():
    """Already notified → board still enforced but returns [] (silent on repeat ticks)."""
    disp = _load_dispatch()
    class FP(_FakeProvider):
        def board_configured(self): return True
    fp = FP()
    blocked_card = {
        "id": "t_v", "title": "#5 Fix login",
        "assignee": "validator-daedalus", "summary": "BLOCKED: security issue",
    }
    with mock.patch.object(disp.kanban, "list_blocked", return_value=[blocked_card]):
        with mock.patch.object(fp, "board_set_status", return_value=True):
            with mock.patch.object(disp.kanban, "close_non_blocked_issue_tasks",
                                   return_value=[]):
                with mock.patch.object(disp, "_has_notified_block", return_value=True):
                    result = disp._enforce_validator_blocks("slug", fp, {5})
    check("already notified → [] (idempotent)", result == [])


def test_enforce_validator_blocks_ignores_non_validator():
    """Blocked card assigned to developer (not validator) → ignored."""
    disp = _load_dispatch()
    class FP(_FakeProvider):
        def board_configured(self): return True
    fp = FP()
    board_calls = []
    blocked_card = {
        "id": "t_dev", "title": "#5 Fix login",
        "assignee": "developer-daedalus", "summary": "stuck on tests",
    }
    with mock.patch.object(disp.kanban, "list_blocked", return_value=[blocked_card]):
        with mock.patch.object(fp, "board_set_status",
                               side_effect=lambda n, s: board_calls.append((n, s)) or True):
            result = disp._enforce_validator_blocks("slug", fp, {5})
    check("non-validator blocked card → ignored", result == [] and board_calls == [])


def test_enforce_validator_blocks_unmanaged_issue():
    """Blocked validator card for issue not in `existing` set → ignored."""
    disp = _load_dispatch()
    class FP(_FakeProvider):
        def board_configured(self): return True
    fp = FP()
    blocked_card = {
        "id": "t_v", "title": "#99 Other issue",
        "assignee": "validator-daedalus", "summary": "BLOCKED: out of scope",
    }
    with mock.patch.object(disp.kanban, "list_blocked", return_value=[blocked_card]):
        result = disp._enforce_validator_blocks("slug", fp, {5})  # 99 not in existing
    check("unmanaged issue → ignored", result == [])


def test_enforce_validator_blocks_dry_run():
    """dry_run logs intent but does not mutate board or kanban."""
    disp = _load_dispatch()
    class FP(_FakeProvider):
        def board_configured(self): return True
    fp = FP()
    board_calls = []
    blocked_card = {
        "id": "t_v", "title": "#5 Fix login",
        "assignee": "validator-daedalus", "summary": "BLOCKED:",
    }
    with mock.patch.object(disp.kanban, "list_blocked", return_value=[blocked_card]):
        with mock.patch.object(fp, "board_set_status",
                               side_effect=lambda n, s: board_calls.append((n, s)) or True):
            result = disp._enforce_validator_blocks("slug", fp, {5}, dry_run=True)
    check("dry_run returns issue without board mutation", result == [5] and board_calls == [])


# ── _check_confirmed_validators ──────────────────────────────────────────────

def _base_confirmed_task():
    return {
        "id": "t_v1", "title": "#3 Fix crash",
        "assignee": "validator-daedalus",
        "summary": "CONFIRMED: reproducible, no security risk",
    }


def test_check_confirmed_validators_triggered():
    """Validator done with CONFIRMED: → downstream triage created + decomposed."""
    disp = _load_dispatch()
    issues_map = {3: {"number": 3, "title": "Fix crash", "body": "Crashes on login"}}
    triage_calls, decompose_calls = [], []
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[_base_confirmed_task()]):
        with mock.patch.object(disp, "_has_downstream_tasks", return_value=False):
            with mock.patch.object(disp.kanban, "create_triage",
                                   side_effect=lambda *a, **k: triage_calls.append(a) or "t_x"):
                with mock.patch.object(disp.kanban, "decompose",
                                       side_effect=lambda *a: decompose_calls.append(a)):
                    result = disp._check_confirmed_validators(
                        "slug", "O/R", issues_map, 3, "/tmp", "slack:tasks", "dev", "github",
                    )
    check("confirmed → triage created", len(triage_calls) == 1)
    check("confirmed → triage decomposed", len(decompose_calls) == 1)
    check("confirmed → issue returned", result == [3])


def test_check_confirmed_validators_blocked_skipped():
    """Validator done with BLOCKED: summary → NOT triggered."""
    disp = _load_dispatch()
    task = {**_base_confirmed_task(), "summary": "BLOCKED: security issue requires review"}
    issues_map = {3: {"number": 3, "title": "Fix crash", "body": ""}}
    triage_calls = []
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[task]):
        with mock.patch.object(disp.kanban, "create_triage",
                               side_effect=lambda *a, **k: triage_calls.append(a) or "t_x"):
            result = disp._check_confirmed_validators(
                "slug", "O/R", issues_map, 3, "/tmp", "slack:tasks", "dev", "github",
            )
    check("BLOCKED validator → no downstream triage", triage_calls == [])
    check("BLOCKED validator → not returned", result == [])


def test_check_confirmed_validators_idempotent():
    """Downstream tasks already exist → skip (idempotent, no duplicate triage)."""
    disp = _load_dispatch()
    issues_map = {3: {"number": 3, "title": "Fix crash", "body": ""}}
    triage_calls = []
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[_base_confirmed_task()]):
        with mock.patch.object(disp, "_has_downstream_tasks", return_value=True):
            with mock.patch.object(disp.kanban, "create_triage",
                                   side_effect=lambda *a, **k: triage_calls.append(a) or "t_x"):
                result = disp._check_confirmed_validators(
                    "slug", "O/R", issues_map, 3, "/tmp", "slack:tasks", "dev", "github",
                )
    check("downstream exists → no new triage", triage_calls == [])
    check("downstream exists → not returned", result == [])


def test_check_confirmed_validators_dry_run():
    """dry_run: returns issue number but creates no triage card."""
    disp = _load_dispatch()
    issues_map = {3: {"number": 3, "title": "Fix crash", "body": ""}}
    triage_calls = []
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[_base_confirmed_task()]):
        with mock.patch.object(disp, "_has_downstream_tasks", return_value=False):
            with mock.patch.object(disp.kanban, "create_triage",
                                   side_effect=lambda *a, **k: triage_calls.append(a) or "t_x"):
                result = disp._check_confirmed_validators(
                    "slug", "O/R", issues_map, 3, "/tmp", "slack:tasks", "dev", "github",
                    dry_run=True,
                )
    check("dry_run → issue returned", result == [3])
    check("dry_run → no triage created", triage_calls == [])


def test_check_confirmed_validators_non_validator_ignored():
    """Done task NOT assigned to validator-daedalus → ignored."""
    disp = _load_dispatch()
    task = {**_base_confirmed_task(), "assignee": "developer-daedalus"}
    issues_map = {3: {"number": 3, "title": "Fix crash", "body": ""}}
    triage_calls = []
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[task]):
        with mock.patch.object(disp.kanban, "create_triage",
                               side_effect=lambda *a, **k: triage_calls.append(a) or "t_x"):
            result = disp._check_confirmed_validators(
                "slug", "O/R", issues_map, 3, "/tmp", "slack:tasks", "dev", "github",
            )
    check("non-validator done task → ignored", triage_calls == [] and result == [])


# ── Bidirectional sync: VCS board Done → Hermes kanban archive ───────────────

def test_vcs_board_done_syncs_kanban():
    """When a managed issue is Done on VCS board (human-moved), kanban tasks are archived."""
    disp = _load_dispatch()
    class FP(_FakeProvider):
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names):
            if "Done" in names:
                return {9}  # #9 was manually moved to Done
            return set()

    fp = FP()
    closed_calls = []
    disp.kanban.ensure_board = lambda s: True
    disp.kanban.list_blocked = lambda s: []
    disp.kanban.list_issue_numbers = lambda s: {9}   # #9 is managed
    disp.kanban.dispatch = lambda s, max_spawns=5: True

    _orig_list_tasks = disp.kanban.list_tasks
    _orig_create_task = disp.kanban.create_task
    disp.kanban.list_tasks = lambda *a, **k: []
    disp.kanban.create_task = lambda *a, **k: "t_v"

    try:
        with mock.patch.object(disp.kanban, "close_issue_tasks",
                               side_effect=lambda s, n: closed_calls.append(n) or ["t_x"]):
            with mock.patch.object(disp, "_fetch_issues",
                                   return_value=[{"number": 9, "title": "Done externally"}]):
                with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
                    with mock.patch.object(fp, "_pr_for_issue", return_value=None):
                        s = disp.run(
                            {"repo": "O/R", "workdir": "/tmp", "name": "x",
                             "tracking": {"github_project_number": 1},
                             "issues": {"filters": {}}, "execution": {}},
                            provider=fp,
                        )
    finally:
        disp.kanban.list_tasks = _orig_list_tasks
        disp.kanban.create_task = _orig_create_task

    check("board-Done sync archives kanban tasks", 9 in closed_calls)
    check("board-Done sync adds to completed", 9 in s.get("completed", []))


def test_vcs_board_done_sync_dry_run():
    """dry_run: logs intent but does NOT archive kanban tasks."""
    disp = _load_dispatch()
    class FP(_FakeProvider):
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names):
            return {9} if "Done" in names else set()

    fp = FP()
    closed_calls = []
    disp.kanban.ensure_board = lambda s: True
    disp.kanban.list_blocked = lambda s: []
    disp.kanban.list_issue_numbers = lambda s: {9}
    disp.kanban.dispatch = lambda s, max_spawns=5: True

    _orig_list_tasks = disp.kanban.list_tasks
    _orig_create_task = disp.kanban.create_task
    disp.kanban.list_tasks = lambda *a, **k: []
    disp.kanban.create_task = lambda *a, **k: "t_v"

    try:
        with mock.patch.object(disp.kanban, "close_issue_tasks",
                               side_effect=lambda s, n: closed_calls.append(n) or ["t_x"]):
            with mock.patch.object(disp, "_fetch_issues",
                                   return_value=[{"number": 9, "title": "Done externally"}]):
                with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
                    with mock.patch.object(fp, "_pr_for_issue", return_value=None):
                        s = disp.run(
                            {"repo": "O/R", "workdir": "/tmp", "name": "x",
                             "tracking": {"github_project_number": 1},
                             "issues": {"filters": {}}, "execution": {}},
                            provider=fp, dry_run=True,
                        )
    finally:
        disp.kanban.list_tasks = _orig_list_tasks
        disp.kanban.create_task = _orig_create_task

    check("dry_run sync: no kanban archive", closed_calls == [])
    check("dry_run sync: issue in completed list", 9 in s.get("completed", []))


# ── Full workflow integration test ───────────────────────────────────────────

def _stub_kanban(disp):
    """Patch all kanban I/O on `disp` to no-ops; returns mutable state dict."""
    state = {"existing": set(), "list_tasks_ret": [], "create_task_ids": iter(["t_v1"]),
             "create_triage_ids": iter(["t_triage1"]), "close_tasks_ret": []}
    disp.kanban.ensure_board = lambda s: True
    disp.kanban.dispatch = lambda s, max_spawns=5: True
    disp.kanban.decompose = lambda *a, **k: True
    disp.kanban.decompose_all_triage = lambda s: True
    disp.kanban.list_blocked = lambda s: []
    disp.kanban.list_issue_numbers = lambda s: state["existing"]
    disp.kanban.list_tasks = lambda *a, **k: state["list_tasks_ret"]
    disp.kanban.create_task = lambda *a, **k: next(state["create_task_ids"], "t_vN")
    disp.kanban.create_triage = lambda *a, **k: next(state["create_triage_ids"], "t_tN")
    disp.kanban.close_issue_tasks = lambda s, n: state["close_tasks_ret"]
    return state


def test_workflow_full_lifecycle():
    """End-to-end: Phase1 validator → Phase2 confirmed downstream → Phase3 PR open
    In Review → Phase4 PR merged Done + closed + archived → blocked validator Blocked."""
    disp = _load_dispatch()
    base_cfg = {
        "repo": "O/R", "workdir": "/tmp", "name": "proj",
        "tracking": {"github_project_number": 1},
        "issues": {"filters": {}}, "execution": {},
    }

    class FP(_FakeProvider):
        def __init__(self):
            self._board = set()
            self._pr = None
            self._ready = set()
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names):
            if any(n == "Ready" for n in names):
                return self._ready
            return set()

    # ── Phase 1: Ready issue #7 → validator dispatched ──────────────────────
    state = _stub_kanban(disp)
    fp = FP()
    fp._ready = {7}
    board_calls = []

    with mock.patch.object(fp, "board_set_status",
                           side_effect=lambda n, s: board_calls.append((n, s)) or True):
        with mock.patch.object(disp, "_fetch_issues",
                               return_value=[{"number": 7, "title": "Fix login"}]):
            with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
                s1 = disp.run(base_cfg, provider=fp)

    state["existing"].add(7)  # simulate kanban now knows about #7
    check("phase1: board In progress", (7, "In progress") in board_calls)
    check("phase1: issue in created", 7 in s1.get("created", []))

    # ── Phase 2: Validator CONFIRMED → downstream triage ────────────────────
    state["list_tasks_ret"] = [{
        "id": "t_v1", "title": "#7 Fix login", "assignee": "validator-daedalus",
        "summary": "CONFIRMED: reproducible bug, no security concern", "status": "done",
    }]
    triage_calls = []
    _orig_create_triage = disp.kanban.create_triage

    with mock.patch.object(disp.kanban, "create_triage",
                           side_effect=lambda *a, **k: triage_calls.append(a) or "t_tr1"):
        with mock.patch.object(disp, "_has_downstream_tasks", return_value=False):
            with mock.patch.object(fp, "_pr_for_issue", return_value=None):
                with mock.patch.object(disp, "_fetch_issues",
                                       return_value=[{"number": 7, "title": "Fix login"}]):
                    with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
                        s2 = disp.run(base_cfg, provider=fp)

    check("phase2: downstream triage created", len(triage_calls) == 1)
    check("phase2: issue NOT in created again", 7 not in s2.get("created", []))

    # ── Phase 3: PR open → In Review + Closes #7 injected ───────────────────
    state["list_tasks_ret"] = []
    open_pr = _PR(number=42, state="open", base_branch="dev", body="Fix the issue")
    board_calls.clear()
    pr_body_updates = []

    with mock.patch.object(fp, "_pr_for_issue", return_value=open_pr):
        with mock.patch.object(fp, "board_set_status",
                               side_effect=lambda n, s: board_calls.append((n, s)) or True):
            with mock.patch.object(fp, "update_pr_body",
                                   side_effect=lambda pr, body: pr_body_updates.append((pr, body)) or True):
                with mock.patch.object(disp, "_fetch_issues",
                                       return_value=[{"number": 7, "title": "Fix login"}]):
                    with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
                        s3 = disp.run(base_cfg, provider=fp)

    check("phase3: board In review", (7, "In review") in board_calls)
    check("phase3: Closes #7 injected into PR body",
          any("Closes #7" in body for _, body in pr_body_updates))
    check("phase3: issue in reconciled", any(n == 7 for n, _ in s3.get("reconciled", [])))

    # ── Phase 4: PR merged → Done + issue closed + kanban archived ──────────
    merged_pr = _PR(number=42, state="merged", base_branch="dev", body="Closes #7\n\nFix.")
    board_calls.clear()
    close_calls = []
    issue_close_calls = []
    state["close_tasks_ret"] = ["t_v1", "t_dev1"]

    with mock.patch.object(fp, "_pr_for_issue", return_value=merged_pr):
        with mock.patch.object(fp, "board_set_status",
                               side_effect=lambda n, s: board_calls.append((n, s)) or True):
            with mock.patch.object(fp, "close_issue",
                                   side_effect=lambda n: issue_close_calls.append(n) or True):
                with mock.patch.object(disp.kanban, "close_issue_tasks",
                                       side_effect=lambda s, n: close_calls.append(n) or ["t_v1", "t_dev1"]):
                    with mock.patch.object(disp, "_fetch_issues",
                                           return_value=[{"number": 7, "title": "Fix login"}]):
                        with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
                            s4 = disp.run(base_cfg, provider=fp)

    check("phase4: board Done", (7, "Done") in board_calls)
    check("phase4: issue closed", 7 in issue_close_calls)
    check("phase4: kanban archived", 7 in close_calls)
    check("phase4: issue in completed", 7 in s4.get("completed", []))

    # ── Phase 5: Blocked validator → board Blocked + downstream cancelled ────
    state["existing"] = {12}
    state["list_tasks_ret"] = []
    blocked_card = {
        "id": "t_v2", "title": "#12 Security bug",
        "assignee": "validator-daedalus", "summary": "BLOCKED: CVE risk",
    }
    board_calls.clear()
    cancel_calls = []

    with mock.patch.object(disp.kanban, "list_blocked", return_value=[blocked_card]):
        with mock.patch.object(fp, "_pr_for_issue", return_value=None):
            with mock.patch.object(fp, "board_set_status",
                                   side_effect=lambda n, s: board_calls.append((n, s)) or True):
                with mock.patch.object(disp.kanban, "close_non_blocked_issue_tasks",
                                       side_effect=lambda s, n: cancel_calls.append(n) or ["t_dev2"]):
                    with mock.patch.object(disp, "_has_notified_block", return_value=False):
                        with mock.patch.object(disp, "_mark_notified_block"):
                            with mock.patch.object(disp, "_fetch_issues",
                                                   return_value=[{"number": 12, "title": "Security bug"}]):
                                with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
                                    s5 = disp.run(base_cfg, provider=fp)

    check("phase5: board Blocked for #12", (12, "Blocked") in board_calls)
    check("phase5: downstream tasks cancelled", 12 in cancel_calls)
    check("phase5: blocked issues in summary", 12 in s5.get("blocked", []))


# ── dispatch_state module ────────────────────────────────────────────────────


def test_dispatch_state_record_and_age():
    """record_dispatch sets timestamp; get_dispatch_age_hours returns positive float."""
    import tempfile, time
    from core import dispatch_state
    with tempfile.TemporaryDirectory() as tmp:
        dispatch_state.record_dispatch(tmp, 1)
        age = dispatch_state.get_dispatch_age_hours(tmp, 1)
        check("dispatch_state: age is a positive float", isinstance(age, float) and age >= 0)
        # Second call must not overwrite (first-write-wins)
        time.sleep(0.01)
        dispatch_state.record_dispatch(tmp, 1)
        age2 = dispatch_state.get_dispatch_age_hours(tmp, 1)
        check("dispatch_state: second record_dispatch is no-op", abs(age2 - age) < 1)


def test_dispatch_state_clear():
    """clear_issue removes state and unknown issue returns None."""
    import tempfile
    from core import dispatch_state
    with tempfile.TemporaryDirectory() as tmp:
        dispatch_state.record_dispatch(tmp, 5)
        dispatch_state.clear_issue(tmp, 5)
        check("dispatch_state: age None after clear",
              dispatch_state.get_dispatch_age_hours(tmp, 5) is None)
        # Clearing non-existent issue must not raise
        dispatch_state.clear_issue(tmp, 99)
        check("dispatch_state: clear unknown is no-op", True)


def test_dispatch_state_pr_flags():
    """has_pr_flag / set_pr_flag are idempotent."""
    import tempfile
    from core import dispatch_state
    with tempfile.TemporaryDirectory() as tmp:
        check("dispatch_state: flag absent initially",
              not dispatch_state.has_pr_flag(tmp, 7, "size_warned"))
        dispatch_state.set_pr_flag(tmp, 7, "size_warned")
        check("dispatch_state: flag present after set",
              dispatch_state.has_pr_flag(tmp, 7, "size_warned"))
        dispatch_state.set_pr_flag(tmp, 7, "size_warned")  # idempotent
        # Count should not grow
        from core.dispatch_state import _load
        flags = _load(tmp).get("pr_7_flags", [])
        check("dispatch_state: duplicate set_pr_flag not stored",
              flags.count("size_warned") == 1)


def test_dispatch_state_reviewer_sha():
    """record_reviewer_sha / get_reviewer_sha round-trip."""
    import tempfile
    from core import dispatch_state
    with tempfile.TemporaryDirectory() as tmp:
        check("dispatch_state: sha None before record",
              dispatch_state.get_reviewer_sha(tmp, 3) is None)
        dispatch_state.record_reviewer_sha(tmp, 3, "abc123")
        check("dispatch_state: sha matches after record",
              dispatch_state.get_reviewer_sha(tmp, 3) == "abc123")


def test_dispatch_state_get_age_handles_corrupt_state():
    """get_dispatch_age_hours returns None gracefully for non-dict entries
    and non-numeric dispatched_at values (must never raise TypeError)."""
    import tempfile, json
    from pathlib import Path
    from core import dispatch_state
    with tempfile.TemporaryDirectory() as tmp:
        import os; os.makedirs(os.path.join(tmp, '.hermes'), exist_ok=True)
        state_path = Path(tmp) / '.hermes' / 'daedalus_dispatch_state.json'

        def _write(data):
            state_path.write_text(json.dumps(data))

        # Non-dict entry values must not raise
        for bad_entry in (1686000000, "2024-01-01", None, [], True, False):
            _write({"42": bad_entry})
            check("dispatch_state: non-dict entry returns None ({})".format(
                  type(bad_entry).__name__),
                  dispatch_state.get_dispatch_age_hours(tmp, 42) is None)

        # Dict entry with non-numeric dispatched_at must not raise TypeError
        for bad_ts in ("not_a_number", [], {}, None, True):
            _write({"42": {"dispatched_at": bad_ts}})
            check("dispatch_state: non-numeric ts returns None ({})".format(
                  type(bad_ts).__name__),
                  dispatch_state.get_dispatch_age_hours(tmp, 42) is None)

        # Numeric dispatched_at must still work
        import time
        now = time.time()
        _write({"42": {"dispatched_at": now - 7200}})
        age = dispatch_state.get_dispatch_age_hours(tmp, 42)
        check("dispatch_state: numeric ts still returns float",
              isinstance(age, float) and 1.9 < age < 2.1)

        # get_reviewer_sha must also handle non-dict entries
        _write({"42": 1686000000})
        check("dispatch_state: reviewer_sha handles non-dict entry",
              dispatch_state.get_reviewer_sha(tmp, 42) is None)


# ── priority sort ────────────────────────────────────────────────────────────


def test_priority_sort():
    """Issues with P0/P1 labels are dispatched before unlabelled issues."""
    disp = _load_dispatch()
    state = _stub_kanban(disp)
    dispatched_order = []

    class FP(_FakeProvider):
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names):
            return {10, 11, 12}  # all issues are Ready

    def _create_task_spy(*a, **k):
        title = a[1] if len(a) > 1 else k.get("title", "")
        dispatched_order.append(title)
        return "t_v1"
    disp.kanban.create_task = _create_task_spy

    base_cfg = {
        "repo": "O/R", "workdir": "", "name": "proj",
        "tracking": {"github_project_number": 1},
        "issues": {"filters": {}}, "execution": {},
    }
    issues = [
        {"number": 10, "title": "Low prio", "labels": [], "body": ""},
        {"number": 11, "title": "High P0", "labels": [{"name": "P0"}], "body": ""},
        {"number": 12, "title": "Medium P1", "labels": [{"name": "P1"}], "body": ""},
    ]
    with mock.patch.object(disp, "_fetch_issues", return_value=issues):
        with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
            disp.run(base_cfg, provider=FP())

    titles = dispatched_order
    check("priority sort: issues were dispatched", len(titles) > 0)
    if titles:
        p0_idx = next((i for i, t in enumerate(titles) if "P0" in t), None)
        low_idx = next((i for i, t in enumerate(titles) if "Low" in t), None)
        check("priority sort: P0 dispatched before unlabelled",
              p0_idx is not None and low_idx is not None and p0_idx < low_idx)


# ── PR size gate ─────────────────────────────────────────────────────────────


def test_pr_size_gate_posts_warning():
    """PR exceeding max_pr_lines gets a size_warned PR comment (once)."""
    import tempfile
    from core import dispatch_state
    disp = _load_dispatch()
    state = _stub_kanban(disp)
    state["existing"].add(20)
    posted = []

    class FP(_FakeProvider):
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names): return set()
        def _pr_for_issue(self, n):
            return _PR(number=99, state="open", base_branch="main", body="Fixes #20")
        def get_pr_files(self, pr):
            return [{"filename": "a.py", "additions": 400, "deletions": 200, "changes": 600}]
        def post_pr_comment(self, pr, body):
            posted.append((pr, body))
            return True

    with tempfile.TemporaryDirectory() as tmp:
        base_cfg = {
            "repo": "O/R", "workdir": tmp, "name": "proj",
            "tracking": {"github_project_number": 1},
            "issues": {"filters": {}},
            "execution": {"max_pr_lines": 500},
        }
        with mock.patch.object(disp, "_fetch_issues",
                               return_value=[{"number": 20, "title": "Big PR", "labels": []}]):
            with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
                disp.run(base_cfg, provider=FP())
        check("size gate: warning posted", len(posted) == 1)
        check("size gate: flag set", dispatch_state.has_pr_flag(tmp, 99, "size_warned"))

        # Second tick must NOT re-post
        posted.clear()
        with mock.patch.object(disp, "_fetch_issues",
                               return_value=[{"number": 20, "title": "Big PR", "labels": []}]):
            with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
                disp.run(base_cfg, provider=FP())
        check("size gate: no duplicate warning", len(posted) == 0)


def test_pr_size_gate_disabled():
    """max_pr_lines=0 means no warning is ever posted."""
    import tempfile
    disp = _load_dispatch()
    state = _stub_kanban(disp)
    state["existing"].add(21)
    posted = []

    class FP(_FakeProvider):
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names): return set()
        def _pr_for_issue(self, n):
            return _PR(number=100, state="open", base_branch="main", body="Fixes #21")
        def get_pr_files(self, pr):
            return [{"filename": "a.py", "changes": 9999}]
        def post_pr_comment(self, pr, body):
            posted.append(body)
            return True

    with tempfile.TemporaryDirectory() as tmp:
        base_cfg = {
            "repo": "O/R", "workdir": tmp, "name": "proj",
            "tracking": {"github_project_number": 1},
            "issues": {"filters": {}}, "execution": {"max_pr_lines": 0},
        }
        with mock.patch.object(disp, "_fetch_issues",
                               return_value=[{"number": 21, "title": "Huge", "labels": []}]):
            with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
                disp.run(base_cfg, provider=FP())
        check("size gate disabled: no warning posted", len(posted) == 0)


# ── forbidden file guard ─────────────────────────────────────────────────────


def test_forbidden_file_guard_posts_warning():
    """PR containing a .env file gets a forbidden_warned PR comment (once)."""
    import tempfile
    from core import dispatch_state
    disp = _load_dispatch()
    state = _stub_kanban(disp)
    state["existing"].add(30)
    posted = []

    class FP(_FakeProvider):
        def board_configured(self): return True
        def board_numbers_with_statuses(self, names): return set()
        def _pr_for_issue(self, n):
            return _PR(number=101, state="open", base_branch="main", body="Fixes #30")
        def get_pr_files(self, pr):
            return [{"filename": ".env", "changes": 2}, {"filename": "main.py", "changes": 10}]
        def post_pr_comment(self, pr, body):
            posted.append((pr, body))
            return True

    with tempfile.TemporaryDirectory() as tmp:
        base_cfg = {
            "repo": "O/R", "workdir": tmp, "name": "proj",
            "tracking": {"github_project_number": 1},
            "issues": {"filters": {}}, "execution": {},
        }
        with mock.patch.object(disp, "_fetch_issues",
                               return_value=[{"number": 30, "title": "Secret PR", "labels": []}]):
            with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
                disp.run(base_cfg, provider=FP())
        check("forbidden guard: warning posted", len(posted) == 1)
        check("forbidden guard: flag set",
              dispatch_state.has_pr_flag(tmp, 101, "forbidden_warned"))

        posted.clear()
        with mock.patch.object(disp, "_fetch_issues",
                               return_value=[{"number": 30, "title": "Secret PR", "labels": []}]):
            with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
                disp.run(base_cfg, provider=FP())
        check("forbidden guard: no duplicate warning", len(posted) == 0)


# ── staleness detection ──────────────────────────────────────────────────────


def test_staleness_posts_warning():
    """Issue in-progress > staleness_hours without a PR gets warned."""
    import tempfile, time
    from core import dispatch_state
    disp = _load_dispatch()
    state = _stub_kanban(disp)
    state["existing"].add(40)

    posted = []

    class FP(_FakeProvider):
        def board_configured(self): return True
        def _pr_for_issue(self, n): return None
        def pr_state_for_issue(self, n): return None
        def post_issue_comment(self, n, body):
            posted.append((n, body))
            return True

    with tempfile.TemporaryDirectory() as tmp:
        # Manually write a dispatch timestamp from 50 hours ago
        import json
        state_path = Path(tmp) / ".hermes" / "daedalus_dispatch_state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"40": {"dispatched_at": time.time() - 50 * 3600}}))

        base_cfg = {
            "repo": "O/R", "workdir": tmp, "name": "proj",
            "tracking": {"github_project_number": 1},
            "issues": {"filters": {}},
            "execution": {"staleness_hours": 48},
        }
        with mock.patch.object(disp.kanban, "list_blocked", return_value=[]):
        	with mock.patch.object(disp, "_fetch_issues",
                                   return_value=[{"number": 40, "title": "Old issue", "labels": []}]):
        		with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
        			disp.run(base_cfg, provider=FP())

        check("staleness: warning posted", len(posted) == 1)
        check("staleness: flag set", dispatch_state.has_pr_flag(tmp, 40, "stale_warned"))

        # Second tick: no re-post
        posted.clear()
        with mock.patch.object(disp.kanban, "list_blocked", return_value=[]):
        	with mock.patch.object(disp, "_fetch_issues",
                                   return_value=[{"number": 40, "title": "Old issue", "labels": []}]):
        		with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
        			disp.run(base_cfg, provider=FP())
        check("staleness: no duplicate warning", len(posted) == 0)


def test_staleness_no_warning_below_threshold():
    """Issue dispatched 10h ago (< staleness_hours=48) must not get a warning."""
    import tempfile, time, json
    from core import dispatch_state
    disp = _load_dispatch()
    state = _stub_kanban(disp)
    state["existing"].add(41)
    posted = []

    class FP(_FakeProvider):
        def board_configured(self): return True
        def _pr_for_issue(self, n): return None
        def pr_state_for_issue(self, n): return None
        def post_issue_comment(self, n, body):
            posted.append(body)
            return True

    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / ".hermes" / "daedalus_dispatch_state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"41": {"dispatched_at": time.time() - 10 * 3600}}))
        base_cfg = {
            "repo": "O/R", "workdir": tmp, "name": "proj",
            "tracking": {"github_project_number": 1},
            "issues": {"filters": {}}, "execution": {"staleness_hours": 48},
        }
        with mock.patch.object(disp.kanban, "list_blocked", return_value=[]):
        	with mock.patch.object(disp, "_fetch_issues",
                                   return_value=[{"number": 41, "title": "Fresh", "labels": []}]):
        		with mock.patch.object(disp, "_deliver_doc_reports", return_value=[]):
        			disp.run(base_cfg, provider=FP())
        check("staleness: no warning under threshold", len(posted) == 0)


# ── label overrides ──────────────────────────────────────────────────────────


def test_downstream_body_skip_developer():
    """_downstream_body omits developer role when skip_developer=True."""
    disp = _load_dispatch()
    issue = {"number": 5, "title": "Docs only", "body": "", "labels": [{"name": "documentation"}]}
    overrides = {"documentation": {"skip_developer": True}}
    body = disp._downstream_body(
        "O/R", issue, 3, "", "slack:C1", "main", "github", None, overrides
    )
    check("label override skip_developer: developer absent", "developer" not in body.lower() or
          "developer" not in body.split("### Role")[1] if "### Role" in body else True)


def test_downstream_body_security_first():
    """_downstream_body puts security analyst before developer when security_first=True."""
    disp = _load_dispatch()
    issue = {"number": 6, "title": "Sec task", "body": "", "labels": [{"name": "security"}]}
    overrides = {"security": {"security_first": True}}
    body = disp._downstream_body(
        "O/R", issue, 3, "", "slack:C1", "main", "github", None, overrides
    )
    dev_pos = body.find("developer") if "developer" in body else -1
    sec_pos = body.find("security") if "security" in body else -1
    if dev_pos >= 0 and sec_pos >= 0:
        check("label override security_first: security before developer", sec_pos < dev_pos)
    else:
        check("label override security_first: security role present", sec_pos >= 0)


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
               # _has_notified_block
               test_has_notified_block_true,
               test_has_notified_block_false,
               test_has_notified_block_wrong_assignee,
               # _enforce_validator_blocks
               test_enforce_validator_blocks_no_board,
               test_enforce_validator_blocks_no_blocked_cards,
               test_enforce_validator_blocks_sets_blocked,
               test_enforce_validator_blocks_idempotent,
               test_enforce_validator_blocks_ignores_non_validator,
               test_enforce_validator_blocks_unmanaged_issue,
               test_enforce_validator_blocks_dry_run,
               # _check_confirmed_validators
               test_check_confirmed_validators_triggered,
               test_check_confirmed_validators_blocked_skipped,
               test_check_confirmed_validators_idempotent,
               test_check_confirmed_validators_dry_run,
               test_check_confirmed_validators_non_validator_ignored,
               # bidirectional sync
               test_vcs_board_done_syncs_kanban,
               test_vcs_board_done_sync_dry_run,
               # full workflow integration
               test_workflow_full_lifecycle,
               # dispatch_state module
               test_dispatch_state_record_and_age,
               test_dispatch_state_clear,
               test_dispatch_state_pr_flags,
               test_dispatch_state_reviewer_sha,
               # priority sort
               test_priority_sort,
               # PR size gate
               test_pr_size_gate_posts_warning,
               test_pr_size_gate_disabled,
               # forbidden file guard
               test_forbidden_file_guard_posts_warning,
               # staleness detection
               test_staleness_posts_warning,
               test_staleness_no_warning_below_threshold,
               # label overrides
               test_downstream_body_skip_developer,
               test_downstream_body_security_first):
        fn()
    print("-" * 60)
    print(f"Results: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)

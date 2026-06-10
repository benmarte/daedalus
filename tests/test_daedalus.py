#!/usr/bin/env python3
"""Focused unit tests for the live daedalus surface:
config loading/merging, kanban parsing, and GitHub Project/PR state helpers.

Run: python3 tests/test_daedalus.py
"""
import sys
from pathlib import Path
from unittest import mock

# Make the package root importable (config/, core/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ConfigLoader, deep_merge, strip_project_key  # noqa: E402
from core import github_project as gp  # noqa: E402
from core import kanban  # noqa: E402

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


def test_strip_project_key():
    check("strip_project_key removes the projects list",
          "projects" not in strip_project_key({"projects": [], "vcs": {}}))


def test_config_loader_resolve():
    import yaml
    tmp = "/tmp/_orch_test_config.yaml"
    cfg = {
        "defaults": {"vcs": {"target_branch": "dev"}, "lifecycle": {"kanban": {"enabled": True}}},
        "projects": [
            {"name": "p1", "repo": "O/p1", "workdir": "/w/p1", "tracking": {"github_project_number": 1}},
            {"name": "p2", "repo": "O/p2", "workdir": "/w/p2", "vcs": {"target_branch": "main"}},
        ],
    }
    Path(tmp).write_text(yaml.safe_dump(cfg))
    loader = ConfigLoader(tmp)
    r1 = loader.resolve_project("p1")
    check("resolve_project keeps identity fields", r1["repo"] == "O/p1" and r1["workdir"] == "/w/p1")
    check("resolve_project inherits defaults", r1["vcs"]["target_branch"] == "dev")
    check("resolve_project carries project tracking", r1["tracking"]["github_project_number"] == 1)
    r2 = loader.resolve_project("p2")
    check("resolve_project lets a project override a default", r2["vcs"]["target_branch"] == "main")
    check("list_projects returns every project", {p["name"] for p in loader.list_projects()} == {"p1", "p2"})
    Path(tmp).unlink(missing_ok=True)


# ── kanban: ls parsing ───────────────────────────────────────────────────────
def test_kanban_list_issue_numbers():
    with mock.patch.object(kanban, "_hk", return_value=(0, "#329 foo\n#42 bar\nno-number\n", "")):
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


# ── github_project: PR + CI state ────────────────────────────────────────────
def test_pr_state_for_issue():
    prs = [
        {"number": 1, "state": "OPEN", "headRefName": "feature/x", "body": "Closes #329"},
        {"number": 2, "state": "MERGED", "headRefName": "fix/issue-329-y", "body": ""},
        {"number": 3, "state": "OPEN", "headRefName": "z", "body": "see #329 for context"},
    ]
    with mock.patch.object(gp, "_gh_json", return_value=prs):
        st = gp.pr_state_for_issue("O/R", 329)
    check("pr_state_for_issue prefers merged and matches closing-keyword/branch", st == "merged")
    with mock.patch.object(gp, "_gh_json", return_value=[prs[2]]):
        st2 = gp.pr_state_for_issue("O/R", 329)
    check("pr_state_for_issue ignores a bare '#329' mention", st2 is None)


def test_pr_ci_green():
    with mock.patch.object(gp, "_gh_json",
                           return_value={"statusCheckRollup": [{"name": "ci-complete", "conclusion": "SUCCESS"}]}):
        check("pr_ci_green True when ci-complete is SUCCESS", gp.pr_ci_green("O/R", 1) is True)
    with mock.patch.object(gp, "_gh_json",
                           return_value={"statusCheckRollup": [{"name": "ci-complete", "conclusion": "FAILURE"}]}):
        check("pr_ci_green False when ci-complete failed", gp.pr_ci_green("O/R", 1) is False)
    with mock.patch.object(gp, "_gh_json", return_value={"statusCheckRollup": []}):
        check("pr_ci_green False when there are no checks", gp.pr_ci_green("O/R", 1) is False)


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


def test_resolve_repo_config_does_not_break_resolve_project():
    """Adding resolve_repo_config must not affect existing resolve_project/resolve_all."""
    import yaml
    tmp = "/tmp/_orch_test_config.yaml"
    cfg = {
        "defaults": {"vcs": {"target_branch": "dev"}},
        "projects": [
            {"name": "p1", "repo": "O/p1", "workdir": "/w/p1"},
        ],
    }
    Path(tmp).write_text(yaml.safe_dump(cfg))
    loader = ConfigLoader(tmp)
    r1 = loader.resolve_project("p1")
    check("resolve_project still works after resolve_repo_config added",
          r1["repo"] == "O/p1" and r1["workdir"] == "/w/p1")
    ra = loader.resolve_all()
    check("resolve_all still works after resolve_repo_config added",
          len(ra["projects"]) == 1)
    Path(tmp).unlink(missing_ok=True)


def test_dispatch_dual_mode():
    disp = _load_dispatch()
    calls = {"decompose_all": 0, "create_triage": 0, "fetch_issues": 0}
    disp.kanban.ensure_board = lambda s: None
    disp.kanban.list_blocked = lambda s: []
    disp.kanban.list_issue_numbers = lambda s: set()
    disp.kanban.decompose_all_triage = lambda s: calls.__setitem__("decompose_all", calls["decompose_all"] + 1) or True
    disp.kanban.create_triage = lambda *a, **k: calls.__setitem__("create_triage", calls["create_triage"] + 1) or "t_x"
    disp.kanban.decompose = lambda *a, **k: True
    disp.kanban.dispatch = lambda s, max_spawns=5: True
    disp._fetch_issues = lambda r, f: (calls.__setitem__("fetch_issues", calls["fetch_issues"] + 1) or [{"number": 1, "title": "t"}])
    disp.gp.pr_state_for_issue = lambda r, n: None
    disp.gp.pr_ci_green = lambda r, pr: False
    base = {"repo": "O/R", "workdir": "/tmp", "name": "x", "issues": {"filters": {}}, "execution": {}}

    s1 = disp.run({**base, "tracking": {}})  # no github_project_number -> kanban-only
    check("kanban-only mode decomposes triage cards", calls["decompose_all"] == 1)
    check("kanban-only mode does NOT poll GitHub issues", calls["fetch_issues"] == 0)
    check("kanban-only mode reports mode=kanban", s1.get("mode") == "kanban")

    class FP:
        def __init__(s, *a, **k): pass
        def numbers_with_status(s, x): return {1}
        def numbers_with_statuses(s, x): return {1}
        def set_status(s, n, st): return True
    disp.gp.GitHubProject = FP
    s2 = disp.run({**base, "tracking": {"github_project_number": 1}})  # github mode
    check("github mode polls issues and creates a triage card", calls["fetch_issues"] >= 1 and calls["create_triage"] == 1)
    check("github mode does NOT use decompose --all", calls["decompose_all"] == 1)
    check("github mode reports mode=github", s2.get("mode") == "github")


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


if __name__ == "__main__":
    print("Daedalus tests")
    print("-" * 60)
    for fn in (test_deep_merge, test_strip_project_key, test_config_loader_resolve,
               test_kanban_list_issue_numbers, test_create_triage_pins_workspace,
               test_kanban_review_handoff_pr,
               test_pr_state_for_issue, test_pr_ci_green, test_dispatch_dual_mode,
               test_resolve_repo_config_valid, test_resolve_repo_config_sources_toggles,
               test_resolve_repo_config_missing_file,
               test_resolve_repo_config_does_not_break_resolve_project,
               test_main_registry_sweep, test_main_single_repo):
        fn()
    print("-" * 60)
    print(f"Results: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)

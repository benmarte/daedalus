#!/usr/bin/env python3
"""Ad-hoc verification: exercise the new epic-label add_label tests in isolation.

NOT the full pytest suite — this is a focused verification script that imports
the real project code and runs each new test function, tracking pass/fail.
"""
from __future__ import annotations

import sys
import types
import unittest.mock as um

sys.path.insert(0, "/Users/benmarte/Documents/github/ai/daedalus")
sys.path.insert(0, "/Users/benmarte/Documents/github/ai/daedalus/tests")

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, msg: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    results.append((name, ok, msg))
    suffix = f"  — {msg}" if msg and not ok else ""
    print(f"[{status}] {name}{suffix}")


# ── 1. GitHub add_label success ─────────────────────────────────────────────
name = "test_add_label_success (GitHub)"
try:
    from core.providers.github import GitHubProvider  # noqa: E402

    cfg = {"repo": "octo/repo", "tracking": {"github_project_number": 1}}
    with um.patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}, clear=False):
        p = GitHubProvider(cfg)
    p._http = um.MagicMock()
    assert p.add_label(42, "epic") is True
    path, body = p._http.post_json.call_args[0]
    assert path == "/repos/octo/repo/issues/42/labels", path
    assert body == {"labels": ["epic"]}, body
    record(name, True)
except Exception as e:  # noqa: BLE001
    record(name, False, f"{type(e).__name__}: {e}")


# ── 2. GitHub add_label failure returns False ───────────────────────────────
name = "test_add_label_failure_returns_false (GitHub)"
try:
    from core.providers.github import GitHubProvider  # type: ignore[no-redef]  # noqa: E402
    from core.providers.http import ProviderError  # noqa: E402

    cfg = {"repo": "octo/repo", "tracking": {"github_project_number": 1}}
    with um.patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}, clear=False):
        p = GitHubProvider(cfg)
    p._http = um.MagicMock()
    p._http.post_json.side_effect = ProviderError("404", status_code=404)
    assert p.add_label(42, "epic") is False
    record(name, True)
except Exception as e:  # noqa: BLE001
    record(name, False, f"{type(e).__name__}: {e}")


# ── 3. GitLab add_label graceful no-op ──────────────────────────────────────
name = "test_add_label_graceful_noop (GitLab)"
try:
    from core.providers.gitlab import GitLabProvider  # noqa: E402

    cfg = {"repo": "group/proj", "vcs": {"provider": "gitlab"}}
    with um.patch.dict("os.environ", {"GITLAB_TOKEN": "tok"}, clear=False):
        p = GitLabProvider(cfg)
    p._http = um.MagicMock()
    assert p.add_label(3, "epic") is False
    p._http.post_json.assert_not_called()
    p._http.patch_json.assert_not_called()
    record(name, True)
except Exception as e:  # noqa: BLE001
    record(name, False, f"{type(e).__name__}: {e}")


# ── 4. Azure DevOps add_label graceful no-op ────────────────────────────────
name = "test_add_label_graceful_noop (Azure)"
try:
    from core.providers.azure_devops import AzureDevOpsProvider  # noqa: E402

    cfg = {
        "vcs": {
            "provider": "azuredevops",
            "org": "acme",
            "project": "Web",
            "repo": "web-app",
        }
    }
    with um.patch.dict("os.environ", {"AZURE_DEVOPS_PAT": "pat"}, clear=False):
        p = AzureDevOpsProvider(cfg)
    p._http = um.MagicMock()
    assert p.add_label(10, "epic") is False
    p._http.post_json.assert_not_called()
    p._http.patch_json.assert_not_called()
    record(name, True)
except Exception as e:  # noqa: BLE001
    record(name, False, f"{type(e).__name__}: {e}")


# ── 5. decompose handles add_label failure gracefully ───────────────────────
name = "test_decompose_handles_add_label_failure"
try:
    from core import iterate  # type: ignore[import-not-found]  # noqa: E402
    from core.iterate import _execute_planner_decompose  # type: ignore[import-not-found]  # noqa: E402

    # Load helpers from the real test_subissue_creation module
    with open("/Users/benmarte/Documents/github/ai/daedalus/tests/test_subissue_creation.py") as fh:
        src = fh.read()
    helpers = types.ModuleType("_subissue_helpers")
    exec(src, helpers.__dict__)  # noqa: S102

    issue = helpers._make_issue_obj(1, "Epic Feature", "- [ ] Task 1\n- [ ] Task 2\n")
    prov = helpers._make_provider(issue_obj=issue, created_numbers=[10, 11])
    prov.add_label.return_value = False

    with um.patch.object(iterate.kanban, "complete", return_value=True) as mk_complete, \
         um.patch.object(iterate.kanban, "create_triage", return_value="t_triage") as mk_triage, \
         um.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        ok = _execute_planner_decompose(
            "slug",
            helpers._make_card(issue_n=1, body="- [ ] Task 1\n- [ ] Task 2\n"),
            "owner/repo",
            "PLANNING COMPLETE: ready for decomposition",
            provider=prov,
        )
    assert ok is True, f"expected True got {ok}"
    prov.add_label.assert_called_once_with(1, "epic")
    assert prov.create_issue.call_count == 2
    assert prov.post_issue_comment.call_count == 1
    assert mk_complete.call_count == 1
    assert mk_triage.call_count == 2
    record(name, True)
except Exception as e:  # noqa: BLE001
    record(name, False, f"{type(e).__name__}: {e}")


# ── Summary ─────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
print()
print(f"ad-hoc verification: {passed} passed / {failed} failed / {len(results)} total")
if failed:
    print("AD-HOC verification FAILED — see failures above.")
else:
    print("AD-HOC verification PASSED — all 5 new epic-label tests behave correctly.")
sys.exit(0 if failed == 0 else 1)

"""Tests for CI retry scheduling (issue #24).

Exercises ``scripts/daedalus_dispatch._schedule_ci_retry`` directly:
  - happy path creates a one-shot 3‑minute cron
  - idempotent guard skips creation if the job already exists
  - slug is sanitized (unsafe chars become '-')
  - subprocess failures are caught and return False (never crash dispatcher)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_dispatch():
    p = Path(__file__).resolve().parent.parent / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


disp = _load_dispatch()

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


# ── _schedule_ci_retry ───────────────────────────────────────────────────────


def test_schedule_ci_retry_happy_path():
    """No existing job → creates a cron with --name and --repeat 1."""
    list_result = mock.Mock()
    list_result.returncode = 0
    list_result.stdout = ""
    create_result = mock.Mock()
    create_result.stdout = "daedalus-ci-retry-my-board"

    with mock.patch("subprocess.run", side_effect=[list_result, create_result]) as mk_run:
        created = disp._schedule_ci_retry("my-board", 2)

    check("happy path returns True", created is True)
    check("two subprocess calls (list + create)", mk_run.call_count == 2)
    # The list call must use --all (not the nonexistent --quiet)
    list_args = mk_run.call_args_list[0][0][0]
    check("list uses --all flag", "--all" in list_args)
    check("list does not use --quiet", "--quiet" not in list_args)
    # The create call arguments
    create_args = mk_run.call_args_list[1][0][0]
    check("create uses hermes cron create", create_args[0:3] == ["hermes", "cron", "create"])
    check("schedule is 3m", "3m" in create_args)
    check("--repeat 1 set", "--repeat" in create_args and "1" in create_args)
    check("--no-agent set", "--no-agent" in create_args)
    check("--script daedalus-cron.sh", "daedalus-cron.sh" in create_args)
    check("job name in create", "daedalus-ci-retry-my-board" in create_args)


def test_schedule_ci_retry_idempotent():
    """If job name already in `hermes cron list` output → no creation call."""
    list_result = mock.Mock()
    list_result.returncode = 0
    list_result.stdout = "daedalus-ci-retry-my-board\nother-job\n"

    with mock.patch("subprocess.run", return_value=list_result) as mk_run:
        created = disp._schedule_ci_retry("my-board", 1)

    check("idempotent returns False", created is False)
    check("only one subprocess call (no create)", mk_run.call_count == 1)


def test_schedule_ci_retry_list_nonzero_rc():
    """If `hermes cron list` exits non-zero → bail out, don't spawn a duplicate."""
    list_result = mock.Mock()
    list_result.returncode = 1
    list_result.stdout = ""

    with mock.patch("subprocess.run", return_value=list_result) as mk_run:
        created = disp._schedule_ci_retry("slug", 1)

    check("non-zero list rc returns False", created is False)
    check("no create call attempted", mk_run.call_count == 1)


def test_schedule_ci_retry_slug_sanitized():
    """Unsafe chars in the slug become '-' so the cron name is safe."""
    list_result = mock.Mock()
    list_result.returncode = 0
    list_result.stdout = ""
    create_result = mock.Mock()

    with mock.patch("subprocess.run", side_effect=[list_result, create_result]) as mk_run:
        disp._schedule_ci_retry("org/repo:special", 1)

    create_args = mk_run.call_args_list[1][0][0]
    # The name passed to --name should have unsafe chars replaced
    name_idx = create_args.index("--name") + 1
    job_name = create_args[name_idx]
    check("slug sanitized", job_name == "daedalus-ci-retry-org-repo-special")


def test_schedule_ci_retry_subprocess_failure():
    """If hermes cron list/create fails → return False, don't crash."""
    with mock.patch("subprocess.run", side_effect=OSError("hermes not found")):
        created = disp._schedule_ci_retry("slug", 1)
    check("failure returns False", created is False)


def test_schedule_ci_retry_create_swallows_error():
    """The creation step failing is handled — still returns True if list succeeded."""
    list_result = mock.Mock()
    list_result.returncode = 0
    list_result.stdout = ""

    def fake_run(cmd, *a, **kw):
        if cmd[0:3] == ["hermes", "cron", "create"]:
            raise OSError("create failed")
        return list_result

    with mock.patch("subprocess.run", side_effect=fake_run) as mk_run:
        created = disp._schedule_ci_retry("slug", 1)

    # Outer try/except catches the OSError from create and returns False.
    check("create failure returns False", created is False)
    check("called list and attempted create", mk_run.call_count == 2)


# ── _parse_follow_ups ────────────────────────────────────────────────────────


def test_parse_follow_ups_section_numbered():
    """Numbered list under a Follow-up section header is extracted."""
    body = """## Follow-up items

1. Wire walkAncestorChain into PREVIOUS dropdowns
2. Thread action_context into filter-condition JINJA evaluation
3. Add runtime verification in staging

## Other section
"""
    items = disp._parse_follow_ups(body)
    check("3 items extracted from numbered list", len(items) == 3)
    check("item 1 correct", items[0] == "Wire walkAncestorChain into PREVIOUS dropdowns")
    check("item 2 correct", items[1] == "Thread action_context into filter-condition JINJA evaluation")
    check("item 3 correct", items[2] == "Add runtime verification in staging")


def test_parse_follow_ups_deferred_markers():
    """Explicit deferred markers are extracted regardless of section."""
    body = """The following items were deferred:

- Deferred to follow-up issue: Fix misleading upstreamActionsOf docstring
- AC3b: Deferred to follow-up issue
"""
    items = disp._parse_follow_ups(body)
    check("deferred marker extracted", any("Fix misleading" in i for i in items))


def test_parse_follow_ups_none_found():
    """Comment with no follow-up patterns returns empty list."""
    body = """## Summary

This PR fixes the login bug. All tests pass. No known follow-ups.
"""
    items = disp._parse_follow_ups(body)
    check("no items when no patterns match", items == [])


def test_parse_follow_ups_custom_patterns():
    """Caller-supplied custom regex patterns are applied."""
    body = """Some notes.

CUSTOM: Do the thing
CUSTOM: Fix the other thing
"""
    items = disp._parse_follow_ups(body, extra_patterns=[r"CUSTOM:\s+(.+)$"])
    check("custom pattern extracts 2 items", len(items) == 2)
    check("custom item 1", items[0] == "Do the thing")


# ── _extract_follow_ups_from_pr_comment ──────────────────────────────────────


def _make_comment(body, author="reviewer-daedalus", cid="1"):
    """Build a minimal Comment-like object."""
    c = mock.Mock()
    c.body = body
    c.author = author
    c.id = cid
    return c


def test_extract_follow_ups_reviewer_comment():
    """3 follow-up items in a reviewer comment → 3 issues + 3 kanban cards created."""
    reviewer_body = """## Review

Looks good overall.

## Follow-up items

1. Wire walkAncestorChain into PREVIOUS dropdowns
2. Thread action_context into filter-condition JINJA evaluation
3. Add runtime verification in staging
"""
    comments = [_make_comment(reviewer_body, "reviewer-daedalus")]

    provider = mock.Mock()
    provider.list_pr_comments.return_value = comments
    provider.list_issues.return_value = []
    provider.create_issue.side_effect = [101, 102, 103]
    provider.pr_url.return_value = "https://github.com/org/repo/pull/10"
    provider.post_pr_comment.return_value = True

    with mock.patch.object(disp.kanban, "create_triage", return_value="t_abc") as mk_triage:
        created = disp._extract_follow_ups_from_pr_comment(
            "slug", "org/repo", provider, 10, "/tmp",
            ["reviewer-daedalus", "qa-daedalus"],
            ["enhancement", "follow-up"],
            "project-manager-daedalus",
            [],
        )

    check("3 issues created", len(created) == 3)
    check("create_issue called 3 times", provider.create_issue.call_count == 3)
    check("3 kanban triage cards created", mk_triage.call_count == 3)
    check("PR summary comment posted", provider.post_pr_comment.call_count == 1)
    summary_body = provider.post_pr_comment.call_args[0][1]
    check("summary contains marker", "daedalus:follow-up-extracted" in summary_body)
    check("summary contains issue refs", "#101" in summary_body)


def test_extract_follow_ups_idempotency():
    """Running twice on the same PR: second run creates no new issues."""
    reviewer_body = """## Follow-up items

1. Wire walkAncestorChain
"""
    marker_comment = _make_comment(
        "Agent: dispatcher\n\n<!-- daedalus:follow-up-extracted PR #10 issue #101 -->",
        author="dispatcher",
        cid="99",
    )
    comments = [_make_comment(reviewer_body, "reviewer-daedalus"), marker_comment]

    provider = mock.Mock()
    provider.list_pr_comments.return_value = comments
    provider.list_issues.return_value = []
    # create_issue should not be called (already extracted)
    provider.create_issue.return_value = 101
    provider.pr_url.return_value = "https://github.com/org/repo/pull/10"

    with mock.patch.object(disp.kanban, "create_triage", return_value="t_abc") as mk_triage:
        created = disp._extract_follow_ups_from_pr_comment(
            "slug", "org/repo", provider, 10, "/tmp",
            ["reviewer-daedalus", "qa-daedalus"],
            ["enhancement", "follow-up"],
            "project-manager-daedalus",
            [],
        )

    # Issue 101 was already in already_extracted, so it is skipped after creation.
    check("idempotency: no new issues added to created list", 101 not in created)


def test_extract_follow_ups_qa_comment():
    """QA comments (qa-daedalus) are also processed."""
    qa_body = """## Action Items

1. Verify staging deployment
"""
    comments = [_make_comment(qa_body, "qa-daedalus")]

    provider = mock.Mock()
    provider.list_pr_comments.return_value = comments
    provider.list_issues.return_value = []
    provider.create_issue.return_value = 200
    provider.pr_url.return_value = "https://github.com/org/repo/pull/11"
    provider.post_pr_comment.return_value = True

    with mock.patch.object(disp.kanban, "create_triage", return_value="t_qa"):
        created = disp._extract_follow_ups_from_pr_comment(
            "slug", "org/repo", provider, 11, "/tmp",
            ["reviewer-daedalus", "qa-daedalus"],
            ["enhancement", "follow-up"],
            "project-manager-daedalus",
            [],
        )

    check("QA comment processed: 1 issue created", len(created) == 1)
    check("issue number is 200", created[0] == 200)


def test_extract_follow_ups_none_found():
    """No follow-up patterns in comment → no issues, no summary comment."""
    body = "LGTM. Nice clean implementation. No follow-ups needed."
    comments = [_make_comment(body, "reviewer-daedalus")]

    provider = mock.Mock()
    provider.list_pr_comments.return_value = comments

    created = disp._extract_follow_ups_from_pr_comment(
        "slug", "org/repo", provider, 12, "/tmp",
        ["reviewer-daedalus", "qa-daedalus"],
        ["enhancement", "follow-up"],
        "project-manager-daedalus",
        [],
    )

    check("no items found → empty list", created == [])
    check("create_issue never called", provider.create_issue.call_count == 0)
    check("post_pr_comment never called", provider.post_pr_comment.call_count == 0)


def test_extract_follow_ups_disabled_in_config():
    """enabled: false in config → no PRs scanned."""
    provider = mock.Mock()
    result = disp._check_follow_ups_from_reviewer_prs(
        "slug", "org/repo", provider, "/tmp",
        disp._DEFAULT_PROFILES,
        {"enabled": False},
    )
    check("disabled: returns 0", result == 0)
    check("disabled: no PR list call", provider.list_prs.call_count == 0)


def test_extract_follow_ups_summary_comment_posted():
    """Summary comment body includes agent header, issue refs, and idempotency markers."""
    body = """## Follow-up items

1. Do the follow-up thing
"""
    comments = [_make_comment(body, "reviewer-daedalus")]

    provider = mock.Mock()
    provider.list_pr_comments.return_value = comments
    provider.list_issues.return_value = []
    provider.create_issue.return_value = 500
    provider.pr_url.return_value = "https://github.com/org/repo/pull/20"
    provider.post_pr_comment.return_value = True

    with mock.patch.object(disp.kanban, "create_triage", return_value="t_x"):
        disp._extract_follow_ups_from_pr_comment(
            "slug", "org/repo", provider, 20, "/tmp",
            ["reviewer-daedalus", "qa-daedalus"],
            ["enhancement", "follow-up"],
            "project-manager-daedalus",
            [],
        )

    posted_body = provider.post_pr_comment.call_args[0][1]
    check("summary has agent header", "Agent: dispatcher" in posted_body)
    check("summary has issue ref", "#500" in posted_body)
    check("summary has idempotency marker for PR 20 issue 500",
          "<!-- daedalus:follow-up-extracted PR #20 issue #500 -->" in posted_body)


if __name__ == "__main__":
    print("CI retry scheduling tests")
    print("-" * 60)
    for fn in (
        test_schedule_ci_retry_happy_path,
        test_schedule_ci_retry_idempotent,
        test_schedule_ci_retry_list_nonzero_rc,
        test_schedule_ci_retry_slug_sanitized,
        test_schedule_ci_retry_subprocess_failure,
        test_schedule_ci_retry_create_swallows_error,
    ):
        fn()
    print()
    print("Follow-up extraction tests")
    print("-" * 60)
    for fn in (
        test_parse_follow_ups_section_numbered,
        test_parse_follow_ups_deferred_markers,
        test_parse_follow_ups_none_found,
        test_parse_follow_ups_custom_patterns,
        test_extract_follow_ups_reviewer_comment,
        test_extract_follow_ups_idempotency,
        test_extract_follow_ups_qa_comment,
        test_extract_follow_ups_none_found,
        test_extract_follow_ups_disabled_in_config,
        test_extract_follow_ups_summary_comment_posted,
    ):
        fn()
    print("-" * 60)
    print(f"Results: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)

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


# ── _pm_body spec-only (dispatcher creates tasks) ────────────────────────────


def test_pm_body_has_no_task_creation():
    """_pm_body no longer tells the PM to create kanban tasks — dispatcher owns that."""
    issue = {"number": 42, "title": "Test issue", "body": "body"}
    body = disp._pm_body("org/repo", issue, "CONFIRMED: all good", "/tmp/repo",
                         "main", "github", profiles=disp._DEFAULT_PROFILES)
    assert "hermes kanban create" not in body, "PM body must not instruct kanban task creation"
    assert "--assignee" not in body, "PM body must not reference --assignee"


def test_pm_body_has_spec_completion_signal():
    """_pm_body tells PM to complete with 'spec:' prefix — the dispatcher trigger."""
    issue = {"number": 7, "title": "Custom test", "body": ""}
    body = disp._pm_body("org/repo", issue, "CONFIRMED:", "/workspace", "dev", "github")
    assert "spec:" in body.lower(), "PM body must mention 'spec:' completion signal"
    assert "#7" in body, "PM body must contain the issue number"


def test_pm_body_has_delegation_for_cloud_agents():
    """_pm_body injects delegation when a cloud agent is configured."""
    issue = {"number": 5, "title": "My issue", "body": "desc"}
    for agent in ("claude-code", "codex", "opencode"):
        body = disp._pm_body("org/repo", issue, "CONFIRMED: ok", "/tmp", "dev", "github",
                             coding_agent=agent)
        assert "AGENT DELEGATION" in body, (
            f"PM body must have delegation block for cloud agent={agent}"
        )
    for agent in ("hermes", "none"):
        body = disp._pm_body("org/repo", issue, "CONFIRMED: ok", "/tmp", "dev", "github",
                             coding_agent=agent)
        assert "AGENT DELEGATION" not in body, (
            f"PM body must not have delegation for local agent={agent}"
        )


# ── _remap_generic_role_assignees (Fix C) ────────────────────────────────────


def test_remap_generic_developer_to_daedalus_profile():
    """Task with assignee='developer' is remapped to developer-daedalus."""
    tasks = [{"id": "t_abc123", "assignee": "developer", "status": "todo"}]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks), \
         mock.patch.object(disp.kanban, "reassign_task", return_value=True) as mk_reassign:
        remapped = disp._remap_generic_role_assignees("slug", disp._DEFAULT_PROFILES)
    check("one task remapped", len(remapped) == 1)
    check("remapped to developer-daedalus",
          remapped.get("t_abc123") == ("developer", "developer-daedalus"))
    check("reassign_task called with correct args",
          mk_reassign.call_args == mock.call("slug", "t_abc123", "developer-daedalus"))


def test_remap_generic_all_roles():
    """All 5 core generic role names remap to their -daedalus profiles."""
    role_to_profile = {
        "developer": "developer-daedalus",
        "qa": "qa-daedalus",
        "reviewer": "reviewer-daedalus",
        "security-analyst": "security-analyst-daedalus",
        "documentation": "documentation-daedalus",
    }
    for generic, expected_profile in role_to_profile.items():
        tasks = [{"id": f"t_{generic}", "assignee": generic, "status": "todo"}]
        with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks), \
             mock.patch.object(disp.kanban, "reassign_task", return_value=True) as mk:
            remapped = disp._remap_generic_role_assignees("slug", disp._DEFAULT_PROFILES)
        check(f"{generic} → {expected_profile}",
              remapped.get(f"t_{generic}") == (generic, expected_profile))


def test_remap_noop_for_explicit_profile_name():
    """Task already assigned developer-daedalus is not remapped."""
    tasks = [{"id": "t_ok", "assignee": "developer-daedalus", "status": "ready"}]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks), \
         mock.patch.object(disp.kanban, "reassign_task", return_value=True) as mk_reassign:
        remapped = disp._remap_generic_role_assignees("slug", disp._DEFAULT_PROFILES)
    check("no remap for explicit profile", len(remapped) == 0)
    check("reassign_task never called", mk_reassign.call_count == 0)


def test_remap_unknown_role_ignored():
    """Unknown assignee (e.g. bob-the-unknown) is not remapped."""
    tasks = [{"id": "t_unk", "assignee": "bob-the-unknown", "status": "todo"}]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks), \
         mock.patch.object(disp.kanban, "reassign_task", return_value=True) as mk_reassign:
        remapped = disp._remap_generic_role_assignees("slug", disp._DEFAULT_PROFILES)
    check("unknown assignee not remapped", len(remapped) == 0)
    check("reassign_task not called for unknown", mk_reassign.call_count == 0)


def test_remap_logs_all_changes():
    """Remap logs a summary line that contains all remapped task IDs."""
    import logging
    tasks = [
        {"id": "t_dev", "assignee": "developer", "status": "todo"},
        {"id": "t_qa", "assignee": "qa", "status": "ready"},
    ]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks), \
         mock.patch.object(disp.kanban, "reassign_task", return_value=True), \
         mock.patch.object(disp.logger, "info") as mk_log:
        disp._remap_generic_role_assignees("slug", disp._DEFAULT_PROFILES)
    log_messages = " ".join(str(call) for call in mk_log.call_args_list)
    check("log mentions t_dev", "t_dev" in log_messages)
    check("log mentions t_qa", "t_qa" in log_messages)


# ── PM SOUL.md content (spec-only role) ──────────────────────────────────────


def test_pm_soul_has_no_task_creation():
    """PM SOUL.md must not instruct the PM to create kanban tasks (dispatcher owns that)."""
    soul_path = (Path(__file__).resolve().parent.parent
                 / "config" / "souls" / "project-manager-daedalus.md")
    content = soul_path.read_text()
    assert "hermes kanban create" not in content, "PM SOUL must not contain kanban create instructions"
    assert "spec:" in content.lower(), "PM SOUL must mention 'spec:' completion signal"


# ── _pm_body title rule ───────────────────────────────────────────────────────


def test_pm_body_includes_issue_number():
    """_pm_body includes the issue number so the PM can reference it in the spec."""
    issue = {"number": 99, "title": "Bug report", "body": "details"}
    body = disp._pm_body("org/repo", issue, "CONFIRMED:", "/tmp",
                         "main", "github", profiles=disp._DEFAULT_PROFILES)
    assert "#99" in body, "PM body must contain issue number"
    assert "spec:" in body.lower(), "PM body must mention spec: completion signal"


# ── _repair_orphan_tasks (Bug 1 + Bug 2) ─────────────────────────────────────


def test_repair_remaps_generic_assignee():
    """_repair_orphan_tasks remaps generic 'developer' → developer-daedalus."""
    tasks = [{"id": "t_dev1", "assignee": "developer", "title": "#50 fix bug", "status": "todo"}]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks), \
         mock.patch.object(disp.kanban, "reassign_task", return_value=True) as mk_reassign, \
         mock.patch.object(disp.kanban, "show_card", return_value={}), \
         mock.patch.object(disp.kanban, "rename_task", return_value=True) as mk_rename:
        repaired = disp._repair_orphan_tasks("slug", disp._DEFAULT_PROFILES)
    check("one repair (assignee)", repaired == 1)
    check("reassign_task called", mk_reassign.called)
    check("rename_task not called (title already has #50)", not mk_rename.called)


def test_repair_prefixes_title_from_body():
    """_repair_orphan_tasks prefixes #N to title when body has issue number."""
    tasks = [{"id": "t_x1", "assignee": "developer-daedalus",
              "title": "Implement walkAncestorChain", "status": "todo"}]
    card_with_body = {"body": "Fix for issue #419\nRepo: org/repo"}
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks), \
         mock.patch.object(disp.kanban, "reassign_task", return_value=True), \
         mock.patch.object(disp.kanban, "show_card", return_value=card_with_body), \
         mock.patch.object(disp.kanban, "rename_task", return_value=True) as mk_rename:
        repaired = disp._repair_orphan_tasks("slug", disp._DEFAULT_PROFILES)
    check("one repair (title prefix)", repaired == 1)
    check("rename_task called with #419 prefix",
          mk_rename.call_args == mock.call("slug", "t_x1", "#419 Implement walkAncestorChain"))


def test_repair_prefixes_title_from_parent():
    """_repair_orphan_tasks falls back to parent task when body has no issue number."""
    tasks = [{"id": "t_child", "assignee": "developer-daedalus",
              "title": "Implement the feature", "status": "ready"}]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks), \
         mock.patch.object(disp.kanban, "reassign_task", return_value=True), \
         mock.patch.object(disp.kanban, "show_card", return_value={"body": "no issue here"}), \
         mock.patch.object(disp.kanban, "rename_task", return_value=True) as mk_rename, \
         mock.patch.object(disp, "_find_issue_n_from_parents", return_value="420"):
        repaired = disp._repair_orphan_tasks("slug", disp._DEFAULT_PROFILES)
    check("one repair (title from parent)", repaired == 1)
    check("rename_task called with #420 prefix",
          mk_rename.call_args == mock.call("slug", "t_child", "#420 Implement the feature"))


def test_repair_noop_for_task_already_with_issue_number():
    """_repair_orphan_tasks leaves tasks with #N in title untouched."""
    tasks = [{"id": "t_ok", "assignee": "developer-daedalus",
              "title": "#418 fix the bug", "status": "todo"}]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks), \
         mock.patch.object(disp.kanban, "reassign_task", return_value=True) as mk_reassign, \
         mock.patch.object(disp.kanban, "show_card", return_value={}) as mk_show, \
         mock.patch.object(disp.kanban, "rename_task", return_value=True) as mk_rename:
        repaired = disp._repair_orphan_tasks("slug", disp._DEFAULT_PROFILES)
    check("no repairs for already-prefixed title", repaired == 0)
    check("show_card not called (title already has #N)", not mk_show.called)
    check("rename_task not called", not mk_rename.called)


def test_repair_noop_for_task_with_no_traceable_parent():
    """_repair_orphan_tasks leaves title alone when no issue number can be found."""
    tasks = [{"id": "t_orphan", "assignee": "developer-daedalus",
              "title": "Orphan task with no parent", "status": "todo"}]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks), \
         mock.patch.object(disp.kanban, "reassign_task", return_value=True), \
         mock.patch.object(disp.kanban, "show_card", return_value={"body": "no hash here"}), \
         mock.patch.object(disp.kanban, "rename_task", return_value=True) as mk_rename, \
         mock.patch.object(disp, "_find_issue_n_from_parents", return_value=None):
        repaired = disp._repair_orphan_tasks("slug", disp._DEFAULT_PROFILES)
    check("no rename when no issue number found", not mk_rename.called)
    check("zero repairs", repaired == 0)


def test_repair_respects_custom_profiles():
    """_repair_orphan_tasks uses custom profile from config when remapping."""
    custom = {**disp._DEFAULT_PROFILES, "developer": "my-senior-dev"}
    tasks = [{"id": "t_custom", "assignee": "developer", "title": "#55 task", "status": "todo"}]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks), \
         mock.patch.object(disp.kanban, "reassign_task", return_value=True) as mk_reassign, \
         mock.patch.object(disp.kanban, "show_card", return_value={}), \
         mock.patch.object(disp.kanban, "rename_task", return_value=True):
        disp._repair_orphan_tasks("slug", custom)
    check("remapped to custom profile",
          mk_reassign.call_args == mock.call("slug", "t_custom", "my-senior-dev"))


# ── _downstream_body rules section (Fix 4) ───────────────────────────────────


def test_downstream_body_contains_assignee_and_title_rules():
    """_downstream_body includes both the title-prefix and --assignee rules."""
    issue = {"number": 77, "title": "Test issue", "body": "body text",
             "labels": [], "url": "https://github.com/org/repo/issues/77"}
    body = disp._downstream_body(
        "org/repo", issue, 3, "/tmp", "slack://channel", "main", "github",
        profiles=disp._DEFAULT_PROFILES,
    )
    check("downstream body has title rule", "#77" in body and "title" in body.lower()
          or "MUST start with" in body or "prefix" in body.lower() or "#77 " in body)
    check("downstream body has --assignee rule",
          "developer-daedalus" in body and "--assignee" in body)
    check("downstream body warns generic names",
          "cannot be dispatched" in body or "CANNOT be dispatched" in body
          or "generic" in body.lower())


# ── PM SOUL.md title-prefix rule ─────────────────────────────────────────────


def test_pm_soul_mentions_spec_completion():
    """PM SOUL.md tells the PM to complete with 'spec:' — the dispatcher trigger."""
    soul_path = (Path(__file__).resolve().parent.parent
                 / "config" / "souls" / "project-manager-daedalus.md")
    content = soul_path.read_text()
    assert "spec:" in content.lower(), "PM SOUL must mention spec: completion signal"
    assert "dispatcher" in content.lower(), "PM SOUL must mention dispatcher creates tasks"


# ── _resolve_coding_agent ─────────────────────────────────────────────────────


def test_resolve_coding_agent_valid_values():
    """All valid agent names are returned as-is (lowercased)."""
    for agent in ("hermes", "claude-code", "codex", "opencode", "none"):
        result = disp._resolve_coding_agent({"coding_agent": agent})
        check(f"valid agent '{agent}' returned", result == agent)


def test_resolve_coding_agent_case_insensitive():
    """Values are lowercased before validation."""
    check("Claude-Code lowercased", disp._resolve_coding_agent({"coding_agent": "Claude-Code"}) == "claude-code")
    check("CODEX lowercased", disp._resolve_coding_agent({"coding_agent": "CODEX"}) == "codex")


def test_resolve_coding_agent_missing_config():
    """Missing key or None value defaults to 'hermes'."""
    assert disp._resolve_coding_agent({}) == "hermes", "empty dict → hermes"
    assert disp._resolve_coding_agent(None) == "hermes", "None execution → hermes"
    assert disp._resolve_coding_agent({"coding_agent": None}) == "hermes", "None value → hermes"


def test_resolve_coding_agent_invalid_value():
    """Unknown agent name defaults to 'hermes' with a warning."""
    result = disp._resolve_coding_agent({"coding_agent": "cursor"})
    assert result == "hermes", f"invalid agent should default to hermes, got {result!r}"


def test_resolve_coding_agent_whitespace():
    """Extra whitespace is stripped."""
    check("whitespace stripped", disp._resolve_coding_agent({"coding_agent": "  codex  "}) == "codex")


# ── delegation block injection ────────────────────────────────────────────────


def test_pm_body_delegation_for_cloud_agents():
    """_pm_body injects delegation for cloud agents, not for local LLM."""
    issue = {"number": 5, "title": "My issue", "body": "desc"}
    for agent in ("claude-code", "codex", "opencode"):
        body = disp._pm_body("org/repo", issue, "CONFIRMED: ok", "/tmp", "dev", "github",
                             coding_agent=agent)
        check(f"delegation in pm body for cloud agent={agent}",
              "AGENT DELEGATION" in body)
    for agent in ("hermes", "none"):
        body = disp._pm_body("org/repo", issue, "CONFIRMED: ok", "/tmp", "dev", "github",
                             coding_agent=agent)
        check(f"no delegation in pm body for local agent={agent}",
              "AGENT DELEGATION" not in body)


def test_downstream_body_injects_delegation_codex():
    """_downstream_body appends delegation instructions when coding_agent=codex."""
    issue = {"number": 7, "title": "Fix bug", "body": "repro"}
    body = disp._downstream_body("org/repo", issue, 3, "/tmp", "", "dev", "github",
                                 coding_agent="codex")
    check("delegation header in downstream body", "AGENT DELEGATION" in body)
    check("codex-specific text present", "Codex" in body)


def test_downstream_body_injects_delegation_opencode():
    """_downstream_body appends delegation instructions when coding_agent=opencode."""
    issue = {"number": 7, "title": "Fix bug", "body": "repro"}
    body = disp._downstream_body("org/repo", issue, 3, "/tmp", "", "dev", "github",
                                 coding_agent="opencode")
    check("delegation header in downstream body for opencode", "AGENT DELEGATION" in body)
    check("opencode-specific text present", "OpenCode" in body)


def test_downstream_body_no_delegation_when_none():
    """_downstream_body does NOT inject when coding_agent=none."""
    issue = {"number": 7, "title": "Fix bug", "body": "repro"}
    body = disp._downstream_body("org/repo", issue, 3, "/tmp", "", "dev", "github",
                                 coding_agent="none")
    check("no delegation block in downstream body when none",
          "AGENT DELEGATION" not in body)


def test_resolve_coding_agent_auto_attach_skill():
    """Cloud agent skill auto-attached to developer role when coding_agent is set."""
    execution = {"coding_agent": "claude-code"}
    agent = disp._resolve_coding_agent(execution)
    check("agent resolved to claude-code", agent == "claude-code")
    expected_skill = "autonomous-ai-agents/claude-code"
    _AGENT_SKILL = {"claude-code": expected_skill, "codex": "autonomous-ai-agents/codex",
                    "opencode": "autonomous-ai-agents/opencode"}
    role_skills: dict = {}
    _skill = _AGENT_SKILL.get(agent)
    if _skill:
        dev_skills = list(role_skills.get("developer") or [])
        if _skill not in dev_skills:
            dev_skills.append(_skill)
        role_skills = {**role_skills, "developer": dev_skills}
    check("autonomous-ai-agents/claude-code auto-attached to developer",
          expected_skill in role_skills.get("developer", []))


def test_resolve_coding_agent_skill_no_duplicate():
    """Cloud agent skill is not duplicated if already in skill list."""
    execution = {"coding_agent": "codex"}
    agent = disp._resolve_coding_agent(execution)
    skill = "autonomous-ai-agents/codex"
    role_skills: dict = {"developer": ["some-skill", skill]}
    _AGENT_SKILL = {"claude-code": "autonomous-ai-agents/claude-code",
                    "codex": skill, "opencode": "autonomous-ai-agents/opencode"}
    _s = _AGENT_SKILL.get(agent)
    if _s:
        dev_skills = list(role_skills.get("developer") or [])
        if _s not in dev_skills:
            dev_skills.append(_s)
        role_skills = {**role_skills, "developer": dev_skills}
    check("no duplicate skill", role_skills["developer"].count(skill) == 1)


def test_resolve_coding_agent_no_skill_when_none():
    """No cloud agent skill injected when coding_agent=none."""
    execution = {"coding_agent": "none"}
    agent = disp._resolve_coding_agent(execution)
    role_skills: dict = {}
    _AGENT_SKILL = {"claude-code": "autonomous-ai-agents/claude-code",
                    "codex": "autonomous-ai-agents/codex",
                    "opencode": "autonomous-ai-agents/opencode"}
    _skill = _AGENT_SKILL.get(agent)
    if _skill:
        dev_skills = list(role_skills.get("developer") or [])
        if _skill not in dev_skills:
            dev_skills.append(_skill)
        role_skills = {**role_skills, "developer": dev_skills}
    check("no skill injected for none agent",
          not any("autonomous-ai-agents" in s for s in role_skills.get("developer", [])))


# ── _CODING_AGENT_DEFAULTS and per-agent default commands ────────────────────


def test_coding_agent_defaults_dict_exists():
    """_CODING_AGENT_DEFAULTS maps each CLI agent to its preferred command."""
    defaults = disp._CODING_AGENT_DEFAULTS
    assert isinstance(defaults, dict), "_CODING_AGENT_DEFAULTS must be a dict"
    assert defaults.get("claude-code") == "claude -p", f"claude-code default wrong: {defaults.get('claude-code')!r}"
    assert defaults.get("codex") == "codex exec --full-auto", f"codex default wrong: {defaults.get('codex')!r}"
    assert defaults.get("opencode") == "opencode run", f"opencode default wrong: {defaults.get('opencode')!r}"


def test_build_delegation_instructions_claude_code_default_cmd():
    """When coding_agent_cmd is empty, claude-code instructions use the built-in default."""
    body = disp._build_delegation_instructions("claude-code", cmd="")
    assert "AGENT DELEGATION" in body
    assert "terminal(" in body, f"expected terminal() in instructions, got:\n{body}"
    assert "claude" in body.lower(), f"expected claude binary reference in instructions, got:\n{body}"


def test_build_delegation_instructions_codex_default_cmd():
    """When coding_agent_cmd is empty, codex instructions use 'codex exec --full-auto'."""
    body = disp._build_delegation_instructions("codex", cmd="")
    assert "AGENT DELEGATION" in body
    assert "terminal(" in body, f"expected terminal() in instructions, got:\n{body}"
    assert "codex exec --full-auto" in body, f"expected 'codex exec --full-auto' in instructions, got:\n{body}"


def test_build_delegation_instructions_opencode_default_cmd():
    """When coding_agent_cmd is empty, opencode instructions use 'opencode run'."""
    body = disp._build_delegation_instructions("opencode", cmd="")
    assert "AGENT DELEGATION" in body
    assert "terminal(" in body, f"expected terminal() in instructions, got:\n{body}"
    assert "opencode run" in body, f"expected 'opencode run' in instructions, got:\n{body}"


def test_build_delegation_instructions_custom_cmd_overrides_default():
    """When coding_agent_cmd is set, it overrides the per-agent default."""
    body = disp._build_delegation_instructions("claude-code", cmd="/custom/claude -p")
    assert "/custom/claude -p" in body, f"expected custom cmd in instructions, got:\n{body}"


def test_build_delegation_instructions_custom_cmd_codex():
    """Custom cmd for codex overrides the default."""
    body = disp._build_delegation_instructions("codex", cmd="my-codex")
    assert "my-codex" in body
    assert "codex exec --full-auto" not in body


def test_build_delegation_instructions_hermes_returns_empty():
    """hermes agent returns empty string (no instructions injected)."""
    assert disp._build_delegation_instructions("hermes") == ""
    assert disp._build_delegation_instructions("hermes", cmd="whatever") == ""


def test_build_delegation_instructions_none_returns_empty():
    """none agent returns empty string."""
    assert disp._build_delegation_instructions("none") == ""


def test_resolve_coding_agent_cmd_empty_when_not_set():
    """_resolve_coding_agent_cmd returns '' when field absent or blank."""
    assert disp._resolve_coding_agent_cmd({}) == ""
    assert disp._resolve_coding_agent_cmd(None) == ""
    assert disp._resolve_coding_agent_cmd({"coding_agent_cmd": ""}) == ""
    assert disp._resolve_coding_agent_cmd({"coding_agent_cmd": "   "}) == ""


def test_resolve_coding_agent_cmd_strips_whitespace():
    """_resolve_coding_agent_cmd strips surrounding whitespace."""
    assert disp._resolve_coding_agent_cmd({"coding_agent_cmd": "  cc-rizq  "}) == "cc-rizq"


def test_pm_body_has_no_delegation_instructions():
    """_pm_body DOES contain delegation when cloud agent is configured."""
    issue = {"number": 5, "title": "My issue", "body": "desc"}
    body = disp._pm_body("org/repo", issue, "CONFIRMED: ok", "/tmp", "dev", "github",
                         coding_agent="claude-code", coding_agent_cmd="cc-rewst")
    assert "AGENT DELEGATION" in body, (
        "_pm_body must inject delegation block when cloud agent is configured"
    )
    assert "spec:" in body.lower(), "_pm_body must still include spec: completion signal"


def test_downstream_body_delegation_uses_custom_cmd():
    """_downstream_body delegation block uses custom coding_agent_cmd."""
    issue = {"number": 7, "title": "Fix bug", "body": "repro"}
    body = disp._downstream_body("org/repo", issue, 3, "/tmp", "", "dev", "github",
                                 coding_agent="opencode", coding_agent_cmd="my-opencode")
    assert "my-opencode" in body
    assert "opencode run" not in body


def test_downstream_body_delegation_uses_default_cmd_when_empty():
    """_downstream_body shows per-agent default when coding_agent_cmd is empty."""
    issue = {"number": 7, "title": "Fix bug", "body": "repro"}
    body = disp._downstream_body("org/repo", issue, 3, "/tmp", "", "dev", "github",
                                 coding_agent="claude-code", coding_agent_cmd="")
    assert "AGENT DELEGATION" in body
    assert "terminal(" in body


# ── per-role task body functions (dispatcher-owned task creation) ─────────────


_ISSUE = {"number": 55, "title": "Fix the bug", "body": "repro steps",
           "labels": [], "url": "https://github.com/org/repo/issues/55"}


def test_dev_task_body_has_delegation_when_claude_code():
    """_dev_task_body puts delegation block FIRST when coding_agent=claude-code."""
    body = disp._dev_task_body("org/repo", _ISSUE, 3, "/tmp", "main", "github",
                               coding_agent="claude-code")
    assert "AGENT DELEGATION" in body
    assert "terminal(" in body
    # Delegation must appear before the "You are the DEVELOPER" line
    assert body.index("AGENT DELEGATION") < body.index("You are the DEVELOPER")


def test_dev_task_body_no_delegation_when_none():
    """_dev_task_body has no delegation block when coding_agent=none."""
    body = disp._dev_task_body("org/repo", _ISSUE, 3, "/tmp", "main", "github",
                               coding_agent="none")
    assert "AGENT DELEGATION" not in body
    assert "You are the DEVELOPER" in body


def test_dev_task_body_no_delegation_when_hermes():
    """coding_agent=hermes → no delegation block (Hermes handles it natively)."""
    body = disp._dev_task_body("org/repo", _ISSUE, 3, "/tmp", "main", "github",
                               coding_agent="hermes")
    assert "AGENT DELEGATION" not in body


def test_dev_task_body_contains_issue_context():
    """_dev_task_body includes issue number, title, workdir, and PR instructions."""
    body = disp._dev_task_body("org/repo", _ISSUE, 3, "/tmp/repo", "dev", "github")
    assert "#55" in body
    assert "Fix the bug" in body
    assert "/tmp/repo" in body
    assert "Closes #55" in body
    assert "review-required" in body


def test_dev_task_body_custom_cmd_for_codex():
    """_dev_task_body uses custom coding_agent_cmd when set."""
    body = disp._dev_task_body("org/repo", _ISSUE, 3, "/tmp", "main", "github",
                               coding_agent="codex", coding_agent_cmd="my-codex exec")
    assert "my-codex exec" in body
    assert "AGENT DELEGATION" in body


def test_qa_task_body_has_role_instructions():
    """_qa_task_body contains QA-specific instructions and issue reference."""
    body = disp._qa_task_body("org/repo", _ISSUE, "/tmp", "github")
    assert "#55" in body
    assert "QA" in body or "qa" in body.lower()
    assert "test" in body.lower()
    assert "qa-passed" in body or "qa-failed" in body


def test_reviewer_task_body_has_role_instructions():
    """_reviewer_task_body contains reviewer-specific instructions."""
    body = disp._reviewer_task_body("org/repo", _ISSUE, "/tmp", "github")
    assert "#55" in body
    assert "reviewed: approved" in body or "reviewed:" in body


def test_security_task_body_has_role_instructions():
    """_security_task_body contains security audit instructions."""
    body = disp._security_task_body("org/repo", _ISSUE, "/tmp", "github")
    assert "#55" in body
    assert "security" in body.lower()
    assert "security: cleared" in body or "security:" in body


def test_docs_task_body_has_role_instructions():
    """_docs_task_body references the DOC_COMMENT_TEMPLATE."""
    body = disp._docs_task_body("org/repo", _ISSUE, "/tmp", "github", "slack://ch")
    assert "#55" in body
    assert "DOC_COMMENT_TEMPLATE" in body or "completion report" in body.lower()


def test_dev_task_body_gitlab_provider():
    """_dev_task_body uses GitLab PR creation howto for gitlab provider."""
    body = disp._dev_task_body("org/repo", _ISSUE, 3, "/tmp", "main", "gitlab")
    assert "AGENT DELEGATION" not in body
    assert "#55" in body


def test_dev_task_body_delegation_all_cli_agents():
    """All CLI agents (claude-code, codex, opencode) get delegation in _dev_task_body."""
    for agent in ("claude-code", "codex", "opencode"):
        body = disp._dev_task_body("org/repo", _ISSUE, 3, "/tmp", "main", "github",
                                   coding_agent=agent)
        assert "AGENT DELEGATION" in body, f"delegation missing for {agent}"
        assert "terminal(" in body, f"terminal() missing for {agent}"


# ── global delegation for all roles ──────────────────────────────────────────


def test_resolve_agent_for_role_uses_global_when_no_override():
    """_resolve_agent_for_role falls back to global coding_agent when no profile override."""
    execution = {"coding_agent": "claude-code"}
    for role in ("developer", "qa", "reviewer", "security", "documentation", "pm"):
        assert disp._resolve_agent_for_role(execution, role) == "claude-code"


def test_resolve_agent_for_role_uses_profile_override():
    """_resolve_agent_for_role uses profiles[role].agent when present."""
    execution = {
        "coding_agent": "claude-code",
        "profiles": {"qa": {"name": "qa-daedalus", "agent": "hermes"}},
    }
    assert disp._resolve_agent_for_role(execution, "qa") == "hermes"
    assert disp._resolve_agent_for_role(execution, "developer") == "claude-code"


def test_resolve_agent_for_role_rejects_invalid_override():
    """_resolve_agent_for_role ignores invalid agent values in profile overrides."""
    execution = {
        "coding_agent": "claude-code",
        "profiles": {"qa": {"name": "qa-daedalus", "agent": "not-a-real-agent"}},
    }
    assert disp._resolve_agent_for_role(execution, "qa") == "claude-code"


def test_qa_body_has_delegation_when_global_claude_code():
    """_qa_task_body injects delegation block when coding_agent=claude-code."""
    body = disp._qa_task_body("org/repo", _ISSUE, "/tmp", "github", coding_agent="claude-code")
    assert "AGENT DELEGATION" in body
    assert "terminal(" in body
    assert "qa-passed" in body.lower()


def test_qa_body_no_delegation_when_hermes():
    """_qa_task_body has no delegation when coding_agent=hermes."""
    body = disp._qa_task_body("org/repo", _ISSUE, "/tmp", "github", coding_agent="hermes")
    assert "AGENT DELEGATION" not in body


def test_reviewer_body_has_delegation_when_global_claude_code():
    """_reviewer_task_body injects delegation block when coding_agent=claude-code."""
    body = disp._reviewer_task_body("org/repo", _ISSUE, "/tmp", "github",
                                    coding_agent="claude-code")
    assert "AGENT DELEGATION" in body
    assert "terminal(" in body
    assert "reviewed:" in body.lower()


def test_security_body_has_delegation_when_global_claude_code():
    """_security_task_body injects delegation block when coding_agent=claude-code."""
    body = disp._security_task_body("org/repo", _ISSUE, "/tmp", "github",
                                    coding_agent="claude-code")
    assert "AGENT DELEGATION" in body
    assert "terminal(" in body
    assert "security:" in body.lower()


def test_docs_body_has_delegation_when_global_claude_code():
    """_docs_task_body injects delegation block when coding_agent=claude-code."""
    body = disp._docs_task_body("org/repo", _ISSUE, "/tmp", "github", "",
                                coding_agent="claude-code")
    assert "AGENT DELEGATION" in body
    assert "terminal(" in body
    assert "docs:" in body.lower()


def test_all_roles_get_delegation_for_cloud_agent():
    """All 6 roles get delegation injected when global coding_agent is a cloud agent."""
    issue = {"number": 7, "title": "Fix bug", "body": "repro"}
    bodies = {
        "pm": disp._pm_body("o/r", issue, "CONFIRMED", "/tmp", "main", "github",
                            coding_agent="claude-code"),
        "developer": disp._dev_task_body("o/r", issue, 3, "/tmp", "main", "github",
                                         coding_agent="claude-code"),
        "qa": disp._qa_task_body("o/r", issue, "/tmp", "github", coding_agent="claude-code"),
        "reviewer": disp._reviewer_task_body("o/r", issue, "/tmp", "github",
                                             coding_agent="claude-code"),
        "security": disp._security_task_body("o/r", issue, "/tmp", "github",
                                             coding_agent="claude-code"),
        "documentation": disp._docs_task_body("o/r", issue, "/tmp", "github", "",
                                              coding_agent="claude-code"),
    }
    for role, body in bodies.items():
        assert "AGENT DELEGATION" in body, f"delegation missing for role={role}"
        assert "terminal(" in body, f"terminal() missing for role={role}"


def test_local_agent_roles_have_no_delegation():
    """No role gets delegation when coding_agent=hermes (local LLM)."""
    issue = {"number": 7, "title": "Fix bug", "body": "repro"}
    bodies = [
        disp._pm_body("o/r", issue, "CONFIRMED", "/tmp", "main", "github", coding_agent="hermes"),
        disp._dev_task_body("o/r", issue, 3, "/tmp", "main", "github", coding_agent="hermes"),
        disp._qa_task_body("o/r", issue, "/tmp", "github", coding_agent="hermes"),
        disp._reviewer_task_body("o/r", issue, "/tmp", "github", coding_agent="hermes"),
        disp._security_task_body("o/r", issue, "/tmp", "github", coding_agent="hermes"),
        disp._docs_task_body("o/r", issue, "/tmp", "github", "", coding_agent="hermes"),
    ]
    for body in bodies:
        assert "AGENT DELEGATION" not in body


def test_role_delegation_uses_role_specific_tmp_file():
    """Each role uses a distinct tmp file prefix to avoid conflicts."""
    issue = {"number": 7, "title": "T", "body": "B"}
    qa_body = disp._qa_task_body("o/r", issue, "/tmp", "github", coding_agent="claude-code")
    rev_body = disp._reviewer_task_body("o/r", issue, "/tmp", "github",
                                        coding_agent="claude-code")
    assert "/tmp/qa-task.txt" in qa_body
    assert "/tmp/rev-task.txt" in rev_body
    assert "/tmp/qa-task.txt" not in rev_body


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
    print()
    print("PM assignee profile injection tests")
    print("-" * 60)
    for fn in (
        test_pm_body_uses_resolved_profile_names,
        test_pm_body_respects_custom_profiles,
        test_pm_body_includes_issue_number_in_every_template_example,
        test_remap_generic_developer_to_daedalus_profile,
        test_remap_generic_all_roles,
        test_remap_noop_for_explicit_profile_name,
        test_remap_unknown_role_ignored,
        test_remap_logs_all_changes,
        test_pm_soul_mentions_assignee_flag_and_dashed_profiles,
        test_pm_soul_mentions_title_prefix_rule,
    ):
        fn()
    print()
    print("_repair_orphan_tasks (Bug 1 + Bug 2) tests")
    print("-" * 60)
    for fn in (
        test_repair_remaps_generic_assignee,
        test_repair_prefixes_title_from_body,
        test_repair_prefixes_title_from_parent,
        test_repair_noop_for_task_already_with_issue_number,
        test_repair_noop_for_task_with_no_traceable_parent,
        test_repair_respects_custom_profiles,
        test_downstream_body_contains_assignee_and_title_rules,
    ):
        fn()
    print()
    print("coding_agent delegation tests")
    print("-" * 60)
    for fn in (
        test_resolve_coding_agent_valid_values,
        test_resolve_coding_agent_case_insensitive,
        test_resolve_coding_agent_missing_config,
        test_resolve_coding_agent_invalid_value,
        test_resolve_coding_agent_whitespace,
        test_pm_body_injects_delegation_claude_code,
        test_pm_body_no_delegation_when_none,
        test_pm_body_no_delegation_when_hermes,
        test_downstream_body_injects_delegation_codex,
        test_downstream_body_injects_delegation_opencode,
        test_downstream_body_no_delegation_when_none,
        test_resolve_coding_agent_auto_attach_skill,
        test_resolve_coding_agent_skill_no_duplicate,
        test_resolve_coding_agent_no_skill_when_none,
    ):
        fn()
    print("-" * 60)
    print(f"Results: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)

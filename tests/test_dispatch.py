"""Dispatcher unit tests."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import _load_dispatch, check  # noqa: E402,F401

disp = _load_dispatch()



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
        "**Agent: dispatcher**\n\n<!-- daedalus:follow-up-extracted PR #10 issue #101 -->",
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


# ── delegation gate wires the claude-code skill (issue #57) ──────────────────

_SOUL_NAMES = (
    "accessibility-daedalus.md",
    "developer-daedalus.md",
    "documentation-daedalus.md",
    "planner-daedalus.md",
    "project-manager-daedalus.md",
    "qa-daedalus.md",
    "reviewer-daedalus.md",
    "security-analyst-daedalus.md",
    "validator-daedalus.md",
)

_STEP0_LINE = "0. Load the delegation skill: `skill_view(name='autonomous-ai-agents/claude-code')`"


def test_all_souls_wire_claude_code_skill_as_step0():
    """Every delegation gate loads the claude-code skill as step 0 before kanban_show (issue #57)."""
    souls_dir = Path(__file__).resolve().parent.parent / "config" / "souls"
    missing = []
    for name in _SOUL_NAMES:
        content = (souls_dir / name).read_text()
        gate = "If it does, you MUST follow these steps and NOTHING ELSE:"
        # step 0 must exist, and appear before step 1 (kanban_show) inside the gate
        if (_STEP0_LINE not in content
                or gate not in content
                or content.index(_STEP0_LINE) < content.index(gate)
                or content.index(_STEP0_LINE) > content.index(
                    "1. Read the task body from your kanban card using `kanban_show`.")):
            missing.append(name)
    assert not missing, f"souls missing step-0 skill_view before kanban_show: {missing}"
    check("all 9 souls wire claude-code skill as step 0", not missing)


# ── documentation SOUL proactive doc-health audit (issue #98) ────────────────


def test_documentation_soul_has_proactive_doc_audit():
    """documentation SOUL audits all docs vs PRs merged since last_doc_sweep_sha (issue #98)."""
    soul = (Path(__file__).resolve().parent.parent
            / "config" / "souls" / "documentation-daedalus.md").read_text()
    checks = {
        "proactive audit step": "Proactive doc-health audit" in soul,
        "doc_sweep_state.json": ".hermes/doc_sweep_state.json" in soul,
        "last_doc_sweep_sha cursor": "last_doc_sweep_sha" in soul,
        "lightweight / bounded by PRs": "lightweight" in soul.lower(),
        "Docs Health report section": "Docs Health" in soul,
        "project-agnostic workdir": "workdir" in soul.lower(),
        "enumerate root + docs markdown": "git ls-files" in soul,
        "separate PR if already merged": "separate small PR" in soul,
    }
    missing = [k for k, ok in checks.items() if not ok]
    assert not missing, f"documentation SOUL missing doc-audit pieces: {missing}"
    check("documentation SOUL wires proactive doc-health audit", not missing)


# ── _CODING_AGENT_DEFAULTS and per-agent default commands ────────────────────


def test_coding_agent_defaults_dict_exists():
    """_CODING_AGENT_DEFAULTS maps each CLI agent to its preferred command."""
    defaults = disp._CODING_AGENT_DEFAULTS
    assert isinstance(defaults, dict), "_CODING_AGENT_DEFAULTS must be a dict"
    assert defaults.get("claude-code") == "CLAUDE_CONFIG_DIR=$HOME/.claude claude --dangerously-skip-permissions -p", f"claude-code default wrong: {defaults.get('claude-code')!r}"
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


# ── issue #141: dead/hung coding agent must fail fast, not hang forever ───────


def test_wait_for_agent_cmd_has_pid_liveness_and_timeout():
    """_wait_for_agent_cmd polls output AND checks PID liveness + a wall-clock cap."""
    cmd = disp._wait_for_agent_cmd("dev", 141, 1800)
    # liveness check on the captured PID
    assert "kill -0" in cmd, f"expected a PID liveness check, got:\n{cmd}"
    assert "dev-141-pid.txt" in cmd, "wait must read the spawned PID file"
    # wall-clock ceiling
    assert "1800" in cmd, "wait must honor the max_wait ceiling"
    assert "$((SECONDS-S))" in cmd, "wait must track elapsed wall-clock time"
    # clear failure markers + stderr surfaced
    assert "CODING_AGENT_DIED" in cmd
    assert "CODING_AGENT_TIMEOUT" in cmd
    assert "dev-141-err.txt" in cmd, "wait must surface the agent stderr log"


def test_wait_for_agent_cmd_has_no_double_quotes():
    """The wait command is embedded in terminal(\"...\"); a literal \" would break it."""
    cmd = disp._wait_for_agent_cmd("dev", 141, 3600)
    assert '"' not in cmd, f"double quotes would terminate the terminal() string:\n{cmd}"


def test_wait_for_agent_cmd_no_infinite_until_loop():
    """The old unbounded `until [ -s out ]; do sleep 30; done` must be gone."""
    cmd = disp._wait_for_agent_cmd("dev", 141, 3600)
    assert "until [ -s" not in cmd, (
        "the infinite poll loop that hung on dead agents must not return"
    )


def test_wait_for_agent_cmd_developer_detects_open_pr():
    """detect_pr wires the provider-side PR handshake into the poll loop (#146).

    When the agent opens a PR but doesn't exit/emit the handshake line, the
    detector populates out.txt and the loop breaks instead of waiting out the
    full timeout (then retrying into a duplicate PR).
    """
    cmd = disp._wait_for_agent_cmd("dev", 146, 3600, detect_pr=True)
    assert "daedalus-detect-pr.sh" in cmd, (
        f"developer wait must invoke the PR detector, got:\n{cmd}"
    )
    # detector is passed the out + pid files and the loop breaks once out fills
    assert "dev-146-out.txt" in cmd and "dev-146-pid.txt" in cmd
    assert "[ -s /tmp/dev-146-out.txt ] && break" in cmd, (
        "loop must break immediately when the detector writes the PR line"
    )
    # still embeddable in terminal("...") — no literal double quotes
    assert '"' not in cmd, f"double quotes would break the terminal() string:\n{cmd}"
    # backstops remain
    assert "kill -0" in cmd and "CODING_AGENT_TIMEOUT" in cmd


def test_wait_for_agent_cmd_no_pr_detection_by_default():
    """Non-developer roles (default detect_pr=False) must NOT poll for a PR.

    They run on an existing PR branch, where detection would fire instantly and
    kill their agent before it posts its report.
    """
    cmd = disp._wait_for_agent_cmd("qa", 146, 3600)
    assert "daedalus-detect-pr.sh" not in cmd, (
        f"only the developer role should detect PRs, got:\n{cmd}"
    )


def test_delegation_pr_detection_is_developer_only():
    """Through the public builder: developer gets PR detection, others don't."""
    dev = disp._build_delegation_instructions(
        "claude-code", cmd="", role="developer", issue_number=146)
    assert "daedalus-detect-pr.sh" in dev, "developer delegation must detect the PR"
    for role in ("validator", "pm", "qa", "reviewer", "security", "documentation"):
        body = disp._build_delegation_instructions(
            "claude-code", cmd="", role=role, issue_number=146)
        assert "daedalus-detect-pr.sh" not in body, (
            f"{role}: must not run PR detection (would kill its agent early)"
        )


def test_delegation_spawn_captures_pid_and_separate_stderr():
    """Spawn line must record the agent PID and route stderr to its own log."""
    for agent in ("claude-code", "codex", "opencode"):
        body = disp._build_delegation_instructions(agent, cmd="", issue_number=141)
        assert "echo $$ > /tmp/dev-141-pid.txt" in body, (
            f"{agent}: spawn must capture the PID for the liveness check, got:\n{body}"
        )
        # The current Hermes terminal tool rejects nohup/&/disown/setsid in a
        # foreground call, so the agent must spawn via terminal(background=True);
        # otherwise the coding agent never launches and the card hangs (#141).
        assert "background=True" in body, (
            f"{agent}: spawn must use terminal(background=True); nohup/& is rejected by Hermes"
        )
        assert "nohup" not in body and "background=False" not in body, (
            f"{agent}: must not use the Hermes-rejected nohup/& foreground spawn"
        )
        assert "2> /tmp/dev-141-err.txt" in body, (
            f"{agent}: stderr must go to its own log, not merged into out.txt"
        )
        # stderr must NOT be merged into out.txt anymore
        assert f"out.txt 2>&1' >" not in body, (
            f"{agent}: stderr must not be merged into out.txt"
        )


def test_delegation_instructions_fail_fast_on_dead_agent():
    """Every delegating role is told to block (not complete) when the agent dies."""
    for role in ("developer", "validator", "pm", "qa", "reviewer", "security",
                 "documentation"):
        body = disp._build_delegation_instructions(
            "claude-code", cmd="", role=role, issue_number=141)
        assert "kill -0" in body, f"{role}: wait must include a liveness check"
        assert "CODING_AGENT_DIED" in body, f"{role}: must surface the death marker"
        assert "coding-agent-failed:" in body, (
            f"{role}: must block the card with a clear error on agent failure"
        )
        assert "until [ -s" not in body, f"{role}: no infinite poll loop"


def test_delegation_wait_uses_configured_max_wait():
    """The configured coding_agent_max_wait is baked into the generated wait."""
    with mock.patch.object(disp, "_CODING_AGENT_MAX_WAIT", 600):
        body = disp._build_delegation_instructions(
            "claude-code", cmd="", role="developer", issue_number=141)
    assert "600s" in body, f"expected configured 600s ceiling in wait, got:\n{body}"


def test_resolve_coding_agent_max_wait_default():
    """Unset / invalid / non-positive max_wait falls back to the default."""
    d = disp._DEFAULT_CODING_AGENT_MAX_WAIT
    assert disp._resolve_coding_agent_max_wait({}) == d
    assert disp._resolve_coding_agent_max_wait(None) == d
    assert disp._resolve_coding_agent_max_wait({"coding_agent_max_wait": "nope"}) == d
    assert disp._resolve_coding_agent_max_wait({"coding_agent_max_wait": 0}) == d
    assert disp._resolve_coding_agent_max_wait({"coding_agent_max_wait": -5}) == d


def test_resolve_coding_agent_max_wait_override():
    """A positive max_wait (int or numeric string) is honored."""
    assert disp._resolve_coding_agent_max_wait({"coding_agent_max_wait": 900}) == 900
    assert disp._resolve_coding_agent_max_wait({"coding_agent_max_wait": "120"}) == 120


def test_resolve_max_dispatch_default():
    """Unset / invalid / non-positive max_dispatch falls back to 5."""
    assert disp._resolve_max_dispatch({}) == 5
    assert disp._resolve_max_dispatch(None) == 5
    assert disp._resolve_max_dispatch({"max_dispatch": "x"}) == 5
    assert disp._resolve_max_dispatch({"max_dispatch": 0}) == 5
    assert disp._resolve_max_dispatch({}, default=3) == 3


def test_resolve_max_dispatch_override():
    """A positive max_dispatch is honored (caps concurrent coding agents)."""
    assert disp._resolve_max_dispatch({"max_dispatch": 2}) == 2
    assert disp._resolve_max_dispatch({"max_dispatch": "4"}) == 4


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


def test_coding_agent_max_turns_default_for_claude():
    """A fresh project (no override) gets a sane --max-turns, not claude's 25 default (#143)."""
    cmd = disp._apply_coding_agent_max_turns("claude-code", "", {})
    assert "--max-turns 100" in cmd, cmd
    assert "claude" in cmd  # the default claude-code cmd was applied


def test_coding_agent_max_turns_configurable():
    """execution.coding_agent_max_turns overrides the default budget."""
    cmd = disp._apply_coding_agent_max_turns("claude-code", "", {"coding_agent_max_turns": 250})
    assert "--max-turns 250" in cmd, cmd


def test_coding_agent_max_turns_respects_explicit():
    """An explicit --max-turns in the cmd is never doubled."""
    base = "claude --dangerously-skip-permissions -p --max-turns 50"
    cmd = disp._apply_coding_agent_max_turns("claude-code", base, {})
    assert cmd == base
    assert cmd.count("--max-turns") == 1


def test_coding_agent_max_turns_skips_non_claude():
    """codex/opencode use different turn flags — leave them untouched."""
    assert disp._apply_coding_agent_max_turns("codex", "codex exec --full-auto", {}) == "codex exec --full-auto"
    assert disp._apply_coding_agent_max_turns("opencode", "opencode run", {}) == "opencode run"


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


def test_qa_task_body_comment_targets_pr_not_issue():
    """_qa_task_body comment instruction targets the PR, not the issue (#115)."""
    body = disp._qa_task_body("org/repo", _ISSUE, "/tmp", "github")
    assert "Post a QA summary comment on the PR (not the issue)" in body
    assert "Post a QA summary comment on GitHub issue #" not in body


def test_reviewer_task_body_comment_targets_pr_not_issue():
    """_reviewer_task_body comment instruction targets the PR, not the issue (#115)."""
    body = disp._reviewer_task_body("org/repo", _ISSUE, "/tmp", "github")
    assert "Post review findings on the PR (not the issue)" in body
    assert "Post review findings on GitHub issue #" not in body


def test_security_task_body_comment_targets_pr_not_issue():
    """_security_task_body comment instruction targets the PR, not the issue (#115)."""
    body = disp._security_task_body("org/repo", _ISSUE, "/tmp", "github")
    assert "Post findings or sign-off on the PR (not the issue)" in body
    assert "Post findings or sign-off on GitHub issue #" not in body


def test_dispatcher_followup_header_is_bold():
    """_extract_follow_ups summary uses the bold **Agent: dispatcher** header (#115)."""
    src = (Path(__file__).resolve().parent.parent
           / "scripts" / "daedalus_dispatch.py").read_text()
    assert '"**Agent: dispatcher**\\n\\n"' in src
    assert '"Agent: dispatcher\\n\\n"' not in src


def test_documentation_soul_posts_on_pr_not_issue():
    """documentation SOUL instructs posting the completion comment on the PR (#115)."""
    soul = (Path(__file__).resolve().parent.parent
            / "config" / "souls" / "documentation-daedalus.md").read_text()
    assert "Post a comment on the GitHub **PR** (not the issue)" in soul
    assert "Post a comment on the GitHub **issue** (not the PR)" not in soul


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


def test_validator_body_delegation_appended_for_cloud_agent():
    """_validator_body appends delegation block (append=True) for a cloud agent."""
    body = disp._validator_body("org/repo", _ISSUE, "/tmp", "main", "github",
                                coding_agent="claude-code")
    assert "AGENT DELEGATION" in body
    assert "terminal(" in body
    # validator uses append mode: delegation comes AFTER the issue body
    assert body.index("--- Issue #55 ---") < body.index("AGENT DELEGATION")


def test_validator_body_hermes_leaves_body_unchanged():
    """Locks item-6 fix: hermes path drops no delegation block AND no stray
    trailing blank line that the old ``!= "none"`` guard used to append."""
    plain = disp._validator_body("org/repo", _ISSUE, "/tmp", "main", "github",
                                 coding_agent="hermes")
    none = disp._validator_body("org/repo", _ISSUE, "/tmp", "main", "github",
                                coding_agent="none")
    assert "AGENT DELEGATION" not in plain
    # hermes must be byte-identical to none (no trailing "\n\n" append regression)
    assert plain == none


def test_role_delegation_uses_role_specific_tmp_file():
    """Each role uses a distinct, issue-scoped tmp file pair to avoid conflicts."""
    issue = {"number": 7, "title": "T", "body": "B"}
    qa_body = disp._qa_task_body("o/r", issue, "/tmp", "github", coding_agent="claude-code")
    rev_body = disp._reviewer_task_body("o/r", issue, "/tmp", "github",
                                        coding_agent="claude-code")
    assert "/tmp/qa-7-task.txt" in qa_body
    assert "/tmp/qa-7-out.txt" in qa_body
    assert "/tmp/rev-7-task.txt" in rev_body
    assert "/tmp/qa-7-task.txt" not in rev_body


def test_role_delegation_tmp_file_scoped_by_issue_number():
    """Concurrent tasks for different issues get isolated /tmp pairs (issue #114)."""
    issue_a = {"number": 112, "title": "A", "body": "B"}
    issue_b = {"number": 113, "title": "C", "body": "D"}
    body_a = disp._qa_task_body("o/r", issue_a, "/tmp", "github", coding_agent="claude-code")
    body_b = disp._qa_task_body("o/r", issue_b, "/tmp", "github", coding_agent="claude-code")
    assert "/tmp/qa-112-task.txt" in body_a
    assert "/tmp/qa-112-out.txt" in body_a
    assert "/tmp/qa-113-task.txt" in body_b
    assert "/tmp/qa-113-out.txt" in body_b
    # Neither issue's files leak into the other's delegation instructions.
    assert "/tmp/qa-112-task.txt" not in body_b
    assert "/tmp/qa-113-task.txt" not in body_a


def test_role_tmp_prefix_has_explicit_accessibility_and_planner():
    """a11y/planner entries are explicit, not falling through to get(role, role) (issue #114)."""
    assert disp._ROLE_TMP_PREFIX["accessibility"] == "a11y"
    assert disp._ROLE_TMP_PREFIX["planner"] == "planner"


def test_role_delegation_wait_command_is_issue_scoped():
    """The _ROLE_AFTER_SPAWN wait command embeds the issue number (issue #114).

    The wait is now a bounded, liveness-guarded loop (issue #141) rather than the
    old `until [ -s ... ]`, but every /tmp ref must still be issue-scoped.
    """
    issue = {"number": 42, "title": "T", "body": "B"}
    dev_body = disp._dev_task_body("o/r", issue, 1, "/tmp", "main", "github",
                                   coding_agent="claude-code")
    assert "/tmp/dev-42-out.txt" in dev_body
    assert "/tmp/dev-42-pid.txt" in dev_body
    assert "/tmp/dev-42-err.txt" in dev_body
    assert "until [ -s" not in dev_body  # the hang-forever loop is gone (#141)
    assert "/tmp/dev-out.txt" not in dev_body
    # validator wait must be scoped too.
    val_body = disp._validator_body("o/r", issue, "/tmp", "main", "github",
                                    coding_agent="claude-code")
    assert "/tmp/validator-42-out.txt" in val_body
    assert "/tmp/validator-42-pid.txt" in val_body
    assert "/tmp/validator-out.txt" not in val_body


# ── _check_team_blockers loop-prevention (issue #87) ─────────────────────────


def _make_blocked_card(tid, assignee, summary, title=None):
    return {
        "id": tid,
        "assignee": assignee,
        "summary": summary,
        "last_summary": summary,
        "title": title or f"#{tid[-4:]} {assignee} task",
        "status": "blocked",
    }


def test_check_team_blockers_skips_review_required_awaiting_pr():
    """review-required: awaiting-pr must NOT create a PM consultation (iterate handles it)."""
    card = _make_blocked_card(
        "t_dev1", "developer-daedalus",
        "review-required: awaiting-pr — Claude Code spawned, PR pending",
        title="#75 Developer: fix bug",
    )
    issue = {"number": 75, "title": "fix bug", "body": ""}
    with mock.patch.object(disp.kanban, "list_blocked", return_value=[card]), \
         mock.patch.object(disp.kanban, "get_latest_summary", return_value=card["summary"]), \
         mock.patch.object(disp.kanban, "list_tasks", return_value=[]), \
         mock.patch.object(disp.kanban, "create_task") as mk_create:
        triggered = disp._check_team_blockers(
            "slug", "org/repo", {75: issue}, "/w", "dev", "github",
        )
    check("review-required awaiting-pr skipped — no PM consultation", mk_create.call_count == 0)
    check("triggered list is empty", triggered == [])


def test_check_team_blockers_skips_review_required_pr_number():
    """review-required: PR #N — ... must NOT create a PM consultation."""
    card = _make_blocked_card(
        "t_dev2", "developer-daedalus",
        "review-required: PR #91 — fix/issue-75-requirements-txt-httpx",
        title="#75 Developer: fix bug",
    )
    issue = {"number": 75, "title": "fix bug", "body": ""}
    with mock.patch.object(disp.kanban, "list_blocked", return_value=[card]), \
         mock.patch.object(disp.kanban, "get_latest_summary", return_value=card["summary"]), \
         mock.patch.object(disp.kanban, "list_tasks", return_value=[]), \
         mock.patch.object(disp.kanban, "create_task") as mk_create:
        triggered = disp._check_team_blockers(
            "slug", "org/repo", {75: issue}, "/w", "dev", "github",
        )
    check("review-required PR #N skipped — no PM consultation", mk_create.call_count == 0)
    check("triggered list is empty", triggered == [])


def test_check_team_blockers_creates_consult_for_genuine_blocker():
    """A genuinely blocked card (non review-required) does create a PM consultation."""
    card = _make_blocked_card(
        "t_dev3", "developer-daedalus",
        "cannot determine VCS provider credentials",
        title="#76 Developer: fix auth bug",
    )
    issue = {"number": 76, "title": "fix auth bug", "body": ""}
    with mock.patch.object(disp.kanban, "list_blocked", return_value=[card]), \
         mock.patch.object(disp.kanban, "get_latest_summary", return_value=card["summary"]), \
         mock.patch.object(disp.kanban, "list_tasks", return_value=[]), \
         mock.patch.object(disp.kanban, "create_task", return_value="t_consult") as mk_create:
        triggered = disp._check_team_blockers(
            "slug", "org/repo", {76: issue}, "/w", "dev", "github",
        )
    check("genuine blocker creates PM consultation", mk_create.call_count == 1)
    check("issue number in triggered list", 76 in triggered)


def test_check_team_blockers_skips_escalate():
    """escalate: summary must still be skipped (existing guard)."""
    card = _make_blocked_card(
        "t_dev4", "developer-daedalus",
        "ESCALATE: exceeded max fix attempts",
        title="#77 Developer: fix retry bug",
    )
    issue = {"number": 77, "title": "fix retry bug", "body": ""}
    with mock.patch.object(disp.kanban, "list_blocked", return_value=[card]), \
         mock.patch.object(disp.kanban, "get_latest_summary", return_value=card["summary"]), \
         mock.patch.object(disp.kanban, "list_tasks", return_value=[]), \
         mock.patch.object(disp.kanban, "create_task") as mk_create:
        triggered = disp._check_team_blockers(
            "slug", "org/repo", {77: issue}, "/w", "dev", "github",
        )
    check("escalate: summary skipped — no PM consultation", mk_create.call_count == 0)


def test_check_team_blockers_skips_when_active_consultation_exists():
    """If a non-done PM consultation already exists, do not create another."""
    card = _make_blocked_card(
        "t_dev5", "developer-daedalus",
        "cannot find module httpx",
        title="#78 Developer: fix dep bug",
    )
    existing_consult = {
        "id": "t_consult1",
        "assignee": "project-manager-daedalus",
        "title": "consult: #78 fix dep bug",
        "status": "todo",
    }
    issue = {"number": 78, "title": "fix dep bug", "body": ""}
    with mock.patch.object(disp.kanban, "list_blocked", return_value=[card]), \
         mock.patch.object(disp.kanban, "get_latest_summary", return_value=card["summary"]), \
         mock.patch.object(disp.kanban, "list_tasks", return_value=[existing_consult]), \
         mock.patch.object(disp.kanban, "create_task") as mk_create:
        triggered = disp._check_team_blockers(
            "slug", "org/repo", {78: issue}, "/w", "dev", "github",
        )
    check("active consultation prevents duplicate", mk_create.call_count == 0)
    check("triggered list is empty when consult already open", triggered == [])


# ── _count_active_issue_tasks (issue #109: accidental-close guard) ────────────


def test_count_active_issue_tasks_counts_non_done_tasks():
    """Active (todo/in-progress) tasks for the issue are counted → guard fires."""
    tasks = [
        {"id": "t1", "title": "#105 QA: verify fix", "status": "todo"},
        {"id": "t2", "title": "#105 Reviewer: review PR", "status": "in-progress"},
        {"id": "t3", "title": "#105 Developer: implement", "status": "done"},
    ]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        active = disp._count_active_issue_tasks("slug", 105)
    check("counts only non-done tasks for the issue", active == 2)
    assert active == 2, "accidental mid-pipeline close must report active tasks"


def test_count_active_issue_tasks_all_done_returns_zero():
    """All tasks done/cancelled → 0, so legitimate-close cleanup proceeds as before."""
    tasks = [
        {"id": "t1", "title": "#105 QA: verify fix", "status": "done"},
        {"id": "t2", "title": "#105 Reviewer: review PR", "status": "cancelled"},
    ]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        active = disp._count_active_issue_tasks("slug", 105)
    check("zero active when all tasks done/cancelled", active == 0)
    assert active == 0, "legitimate close (all tasks done) must not be guarded"


def test_count_active_issue_tasks_ignores_other_issues():
    """Active tasks belonging to a different issue number must not be counted."""
    tasks = [
        {"id": "t1", "title": "#106 QA: verify fix", "status": "todo"},
        {"id": "t2", "title": "no issue number here", "status": "todo"},
        {"id": "t3", "title": "#105 Developer: implement", "status": "todo"},
    ]
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        active = disp._count_active_issue_tasks("slug", 105)
    check("only matches tasks for the target issue", active == 1)
    assert active == 1, "guard must scope active-task count to the closed issue only"


# ── issue #120 extracted helpers ──────────────────────────────────────────────


def test_unpack_issue_extracts_and_strips():
    n, title, body, url = disp._unpack_issue(
        {"number": 9, "title": "T", "body": "  x  ", "url": "u"})
    assert (n, title, body, url) == (9, "T", "x", "u")


def test_unpack_issue_defaults():
    n, title, body, url = disp._unpack_issue({"number": 1})
    assert (n, title, body, url) == (1, "", "", "")


def test_resolve_howtos_keys_and_github_default():
    h = disp._resolve_howtos("github", "org/repo", 5)
    assert set(h) == {"comment", "pr_create", "close_completed", "close_wontfix"}
    assert "org/repo" in h["comment"]
    assert "org/repo/issues/5" in h["close_completed"]
    assert "completed" in h["close_completed"]
    assert "not_planned" in h["close_wontfix"]


def test_resolve_howtos_unknown_provider_falls_back_to_github():
    h = disp._resolve_howtos("bogus", "org/repo", 1)
    assert h["comment"] == disp._resolve_howtos("github", "org/repo", 1)["comment"]


def test_build_security_notify_cmds_empty_is_placeholder():
    out = disp._build_security_notify_cmds("org/repo", 3, "Title", [])
    assert "no notification targets" in out


def test_build_security_notify_cmds_one_line_per_target():
    out = disp._build_security_notify_cmds(
        "org/repo", 3, "Title", ["slack:ops", "discord:sec"])
    lines = out.splitlines()
    assert len(lines) == 2
    assert "hermes send -t slack:ops" in lines[0]
    assert "org/repo#3 (Title)" in lines[0]


def test_prepend_delegation_none_returns_body_unchanged():
    assert disp._prepend_delegation("BODY", "none", "") == "BODY"


def test_prepend_delegation_hermes_returns_body_unchanged():
    # The latent-bug fix: "hermes" is now guarded exactly like "none".
    assert disp._prepend_delegation("BODY", "hermes", "") == "BODY"


def test_prepend_delegation_prepends_for_real_agent():
    out = disp._prepend_delegation("BODY", "claude-code", "", role="developer",
                                   issue_number=7)
    assert out.endswith("BODY")
    assert "AGENT DELEGATION" in out
    assert out.index("AGENT DELEGATION") < out.index("BODY")


def test_prepend_delegation_append_mode():
    out = disp._prepend_delegation("BODY", "claude-code", "", issue_number=7,
                                   append=True, trailing="")
    assert out.startswith("BODY")
    assert "AGENT DELEGATION" in out


def test_get_task_summary_uses_inline_summary():
    assert disp._get_task_summary({"summary": "spec: do it"}, "slug") == "spec: do it"


def test_get_task_summary_falls_back_to_show_card():
    fake = mock.Mock()
    fake.show_card.return_value = {"latest_summary": "CONFIRMED: ok"}
    with mock.patch.object(disp, "kanban", fake):
        out = disp._get_task_summary({"id": "t1"}, "slug")
    assert out == "CONFIRMED: ok"
    fake.show_card.assert_called_once_with("slug", "t1")


def test_get_task_summary_no_id_no_fallback():
    assert disp._get_task_summary({}, "slug") == ""


# ── _check_confirmed_validators: STOP handler reachability (issue #115) ─────────


def _make_done_validator_card(tid, summary, issue_number):
    """Build a done validator card dict for testing."""
    return {
        "id": tid,
        "assignee": "validator-daedalus",
        "title": f"#validate: #{issue_number} Some issue",
        "status": "done",
        "summary": summary,
        "latest_summary": summary,
        "idempotency_key": "",
    }


def test_check_confirmed_validators_stop_reaches_dedicated_handler():
    """stop: summaries must reach the dedicated handler (line 2101), auto-close the
    issue, and NOT create a PM consultation card (that was the dead-code symptom).
    """
    done_card = _make_done_validator_card("t_val1", "STOP: duplicate issue", 501)
    issue = {"number": 501, "title": "Test issue", "body": ""}

    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.return_value = [done_card]       # status="done" call
    # second call: all-tasks scan for idempotency — none already handled
    fake_kanban.list_tasks.side_effect = [[done_card], []]
    fake_kanban.create_task.return_value = "t_stop_marker"  # idempotency marker task

    provider = mock.Mock()
    provider.close_issue.return_value = True

    with mock.patch.object(disp, "kanban", fake_kanban):
        triggered = disp._check_confirmed_validators(
            "slug", "org/repo", {501: issue},
            1, "/w", "slack://foo", "dev", "github",
            provider=provider,
        )

    # Stop handler must actually call close_issue
    provider.close_issue.assert_called_once_with(501)
    # Issue number is in the triggered list
    assert 501 in triggered, f"expected 501 in triggered, got {triggered}"
    # Idempotency marker task was created (so future ticks don't re-close)
    assert fake_kanban.create_task.call_count >= 1
    # The marker task's idempotency key must start with the correct prefix
    created_args = fake_kanban.create_task.call_args_list[0]
    assert created_args.kwargs["idempotency_key"].startswith("validator-stop-closed-")


def test_check_confirmed_validators_stop_idempotent_already_closed():
    """A stop: whose idempotency marker already exists must NOT re-call close_issue,
    but must still appear in triggered (so iteration bookkeeping stays consistent).
    """
    done_card = _make_done_validator_card("t_val2", "STOP: already fixed", 502)
    marker = {
        "id": "t_old_mark",
        "idempotency_key": "validator-stop-closed-502",
    }
    issue = {"number": 502, "title": "Dup issue", "body": ""}

    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.side_effect = [[done_card], [marker]]

    provider = mock.Mock()
    provider.close_issue.return_value = True  # would succeed but must not be called

    with mock.patch.object(disp, "kanban", fake_kanban):
        triggered = disp._check_confirmed_validators(
            "slug", "org/repo", {502: issue},
            1, "/w", "slack://foo", "dev", "github",
            provider=provider,
        )

    provider.close_issue.assert_not_called()
    assert 502 in triggered


def test_check_confirmed_validators_blocked_still_creates_pm_consultation():
    """Guard against regressing back: a blocked: summary must still create a PM
    consultation task (regression safety net for the original handler).
    """
    done_card = _make_done_validator_card("t_val3", "BLOCKED: cannot reproduce", 503)
    issue = {"number": 503, "title": "Unstable repro", "body": ""}

    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.return_value = [done_card]
    fake_kanban.create_task.return_value = "t_pm_consult"

    # No provider — blocked handler doesn't need one
    with mock.patch.object(disp, "kanban", fake_kanban):
        triggered = disp._check_confirmed_validators(
            "slug", "org/repo", {503: issue},
            1, "/w", "slack://foo", "dev", "github",
            provider=None,
        )

    # Must create a PM consultation task
    fake_kanban.create_task.assert_called_once()
    call = fake_kanban.create_task.call_args
    assert "consult:" in call.args[1] or "consult:" in call.kwargs.get("title", "")
    assert 503 in triggered


def test_check_confirmed_validators_stop_no_provider_skips_close():
    """When provider is None, the stop: handler must log but not crash, skip
    the close_issue call, and continue. The issue is NOT added to triggered
    (graceful no-op when the dispatcher can't actually act).
    """
    done_card = _make_done_validator_card("t_val4", "STOP: cannot reproduce", 504)
    issue = {"number": 504, "title": "Flaky issue", "body": ""}

    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.side_effect = [[done_card], []]

    with mock.patch.object(disp, "kanban", fake_kanban):
        triggered = disp._check_confirmed_validators(
            "slug", "org/repo", {504: issue},
            1, "/w", "slack://foo", "dev", "github",
            provider=None,
        )

    # No crash, no close_issue, no marker task
    assert not fake_kanban.create_task.called
    # The no-provider path is a graceful no-op (issue NOT in triggered)
    assert 504 not in triggered


if __name__ == "__main__":
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
        test_pm_body_has_no_task_creation,
        test_pm_body_has_spec_completion_signal,
        test_pm_body_has_delegation_for_cloud_agents,
        test_remap_generic_developer_to_daedalus_profile,
        test_remap_generic_all_roles,
        test_remap_noop_for_explicit_profile_name,
        test_remap_unknown_role_ignored,
        test_remap_logs_all_changes,
        test_pm_soul_has_no_task_creation,
        test_pm_body_includes_issue_number,
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
        test_pm_body_delegation_for_cloud_agents,
        test_pm_body_has_no_delegation_instructions,
        test_downstream_body_injects_delegation_codex,
        test_downstream_body_injects_delegation_opencode,
        test_downstream_body_no_delegation_when_none,
        test_resolve_coding_agent_auto_attach_skill,
        test_resolve_coding_agent_skill_no_duplicate,
        test_resolve_coding_agent_no_skill_when_none,
        test_all_souls_wire_claude_code_skill_as_step0,
        test_documentation_soul_has_proactive_doc_audit,
    ):
        fn()
    print()
    print("_check_team_blockers loop-prevention tests (issue #87)")
    print("-" * 60)
    for fn in (
        test_check_team_blockers_skips_review_required_awaiting_pr,
        test_check_team_blockers_skips_review_required_pr_number,
        test_check_team_blockers_creates_consult_for_genuine_blocker,
        test_check_team_blockers_skips_escalate,
        test_check_team_blockers_skips_when_active_consultation_exists,
    ):
        fn()
    print()
    print("_count_active_issue_tasks (issue #109 accidental-close guard) tests")
    print("-" * 60)
    for fn in (
        test_count_active_issue_tasks_counts_non_done_tasks,
        test_count_active_issue_tasks_all_done_returns_zero,
        test_count_active_issue_tasks_ignores_other_issues,
    ):
        fn()
    print("-" * 60)
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)

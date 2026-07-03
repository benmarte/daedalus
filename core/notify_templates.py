"""Human-friendly notification templates for Daedalus.

All templates return standard markdown — **bold**, [text](url), ## headers,
- lists, emoji — so they render natively on every Hermes messaging platform:

  - Slack: platform adapter converts [text](url) → <url|text>, **x** → *x*
  - Teams: adapter converts [text](url) → <a href>, **x** → <b>
  - Discord/Telegram/Signal: native markdown

Functions return ``""`` on a silent tick (nothing happened) — callers
must NOT send anything in that case.

Two sub-concerns are handled here:
  1. Notification templates (Slack/Teams/Discord) — dispatch summary, doc
     report envelope, pipeline failure, PR-ready.
  2. GitHub content templates (PR body, doc comment) — constants the task
     instructions embed so agents produce structured, consistent output.
"""
from __future__ import annotations

import re
from typing import Any


# ── low-level helpers ─────────────────────────────────────────────────────────

def _link(text: str, url: str) -> str:
    """``[text](url)`` when url is set; plain ``text`` otherwise."""
    return f"[{text}]({url})" if url else text


def _issue_link(n: int, provider=None) -> str:
    url = provider.issue_url(n) if provider else ""
    return _link(f"#{n}", url)


def _pr_link(n: int, provider=None) -> str:
    url = provider.pr_url(n) if provider else ""
    return _link(f"PR #{n}", url)


def extract_issue_number(text: str) -> int | None:
    """Try to find the first ``Issue #<n>`` or ``#<n>`` reference in text."""
    m = re.search(r"[Ii]ssue\s+#(\d+)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(?<!\w)#(\d+)", text)
    return int(m.group(1)) if m else None


# ── GitHub content templates (embedded in agent task instructions) ─────────────

# The developer agent uses this exact format for the PR body.
PR_BODY_TEMPLATE = """\
Closes #<issue_number>

## Problem

<Describe the bug or feature request being addressed — one short paragraph>

## Fix

<Describe what was changed and why — one short paragraph>

## Files Changed

| File | Change |
|------|--------|
| `path/to/file.py` | Brief description of what changed in this file |

## How to Test

1. <Step-by-step instructions a reviewer can follow to verify the fix>
2. <Include any required setup, env vars, or seed data>

## Manual Testing

- [ ] Tested locally
- [ ] No regressions in adjacent features
- [ ] Edge cases covered\
"""


# The documentation agent uses this exact format for its PR comment.
DOC_COMMENT_TEMPLATE = """\
**Agent: documentation**

## 📋 Documentation Report — Issue #<issue_number> · PR #<pr_number>

**Issue:** [#<issue_number> <issue_title>](<issue_url>)
**PR:** [#<pr_number> <pr_title>](<pr_url>)

---

## Summary

<What was done and why — 2–3 sentences>

## Files Changed

| File | Description |
|------|-------------|
| `path/to/changed/file.py` | What changed in this file |

## Resolution

<How the issue was resolved — root cause found, what the fix does>

## Testing Instructions

1. <Step 1 — include any setup if needed>
2. <Step 2>

Expected result: <what should happen after following the steps>

## Notes

<Caveats, known limitations, or suggested follow-up work. Write "None." if absent.>\
"""


# ── agent comment header ─────────────────────────────────────────────────────

# Consistent with the ``**Agent: documentation**`` sentinel the dispatcher
# already inspects.  Override via ``execution.comment_header_template`` in
# daedalus.yaml.  Supported placeholders: {role}, {profile}, {issue}, {pr}.
DEFAULT_COMMENT_HEADER_TEMPLATE = "**Agent: {role}**"

_ROLE_LABELS: dict[str, str] = {
    "validator": "validator",
    "pm": "project-manager",
    "developer": "developer",
    "reviewer": "reviewer",
    "security": "security-analyst",
    "documentation": "documentation",
    "daedalus": "daedalus",
}


def render_agent_header(
    role: str,
    *,
    profile: str = "",
    issue: int | None = None,
    pr: int | None = None,
    template: str = DEFAULT_COMMENT_HEADER_TEMPLATE,
) -> str:
    """Return a one-line attribution header for an agent comment.

    Prepend this to every comment posted to a VCS issue or PR so it is
    always clear which pipeline role wrote it.  Missing placeholder values
    substitute to empty strings so the template never raises ``KeyError``.
    """
    label = _ROLE_LABELS.get(role, role)
    ctx = {
        "role": label,
        "profile": profile or "",
        "issue": f"#{issue}" if issue else "",
        "pr": f"#{pr}" if pr else "",
    }
    return template.format_map(ctx)


# ── per-issue thread mirroring (issue #121) ───────────────────────────────────

def render_thread_root(name: str, issue_number: int, issue_title: str,
                       issue_url: str = "") -> str:
    """Root (thread-anchor) message posted when an issue is dispatched.

    This is the parent every later agent comment / PR event replies to, so the
    full conversation lives in one platform thread.
    """
    title = (issue_title or "").strip()
    ref = _link(f"#{issue_number}", issue_url)
    head = f"{ref}: {title}" if title else ref
    return (
        f"## 🧵 Daedalus — **{name}**\n\n"
        f"**Issue:** {head}\n\n"
        f"Dispatched to the roster. Agent updates on this issue and its PR will "
        f"appear in this thread."
    )


def render_thread_comment(issue_number: int, pr_number: int | None,
                          comment_body: str, *, issue_url: str = "",
                          pr_url: str = "") -> str:
    """Thread reply wrapping a mirrored agent issue/PR comment verbatim."""
    if pr_number:
        loc = f"on {_link(f'PR #{pr_number}', pr_url)} · issue {_link(f'#{issue_number}', issue_url)}"
    else:
        loc = f"on issue {_link(f'#{issue_number}', issue_url)}"
    return f"💬 New agent comment {loc}:\n\n{comment_body.strip()}"


def render_thread_pr_event(event: str, pr_number: int, pr_title: str = "",
                           pr_url: str = "") -> str:
    """Thread reply for a PR lifecycle transition (opened / merged)."""
    icon = {"opened": "🔍", "merged": "✅", "closed": "🚪"}.get(event, "🔄")
    verb = {"opened": "opened for review",
            "merged": "merged — work complete",
            "closed": "closed"}.get(event, event)
    title = (pr_title or "").strip()
    ref = _link(f"PR #{pr_number}", pr_url)
    head = f"{ref}: {title}" if title else ref
    return f"{icon} {head} {verb}."


# ── dispatch summary ──────────────────────────────────────────────────────────

def render_dispatch_summary(
    name: str,
    summary: dict[str, Any],
    provider=None,
    *,
    dry_run: bool = False,
) -> str:
    """Rich markdown dispatch summary for a single project.

    Returns ``""`` when nothing happened so the caller stays silent.
    Provider is optional — without it, issue/PR references show as plain
    ``#n`` text instead of hyperlinks.
    """
    if summary.get("error"):
        return (
            f"## ❌ Daedalus Error — **{name}**\n\n"
            f"**Error:** {summary['error']}"
        )

    created = summary.get("created") or []
    completed = summary.get("completed") or []
    advance_prs = summary.get("advance_prs") or []
    routed = summary.get("routed_actions") or {}
    reconciled = summary.get("reconciled") or []
    delivered = summary.get("slack_delivered") or []
    spec_created = summary.get("spec_created") or []
    blocked = summary.get("blocked") or []
    blocked_deps = summary.get("blocked_deps") or {}

    if not (created or completed or advance_prs or routed or reconciled
            or delivered or spec_created or blocked or blocked_deps):
        return ""

    mode = summary.get("mode", "?")
    issues_seen = summary.get("issues_seen", 0)
    dry_badge = " _(dry-run)_" if dry_run else ""

    lines: list[str] = [
        f"## 🤖 Daedalus — **{name}**{dry_badge}",
        "",
        f"**Mode:** {mode} | **Issues seen:** {issues_seen}",
    ]

    if spec_created:
        lines += ["", "### 📄 Spec Files Dispatched"]
        for sf in spec_created:
            lines.append(f"- `{sf}`")

    if blocked:
        lines += ["", "### 🛑 Blocked (Security / Escalation)"]
        for n in blocked:
            lines.append(f"- {_issue_link(n, provider)} — requires human review")

    if blocked_deps:
        lines += ["", "### ⛓ Waiting on Dependencies"]
        for n, deps in blocked_deps.items():
            dep_links = ", ".join(_issue_link(d, provider) for d in deps)
            lines.append(f"- {_issue_link(n, provider)} — blocked by {dep_links}")

    if created:
        lines += ["", "### 📋 Dispatched"]
        for n in created:
            lines.append(f"- {_issue_link(n, provider)} — dispatched to roster")

    if completed:
        lines += ["", "### ✅ Completed"]
        for n in completed:
            lines.append(f"- {_issue_link(n, provider)} — work merged and closed")

    if advance_prs:
        lines += ["", "### ⏭ PRs Advanced"]
        for pr in advance_prs:
            lines.append(f"- {_pr_link(pr, provider)} — advanced through review")

    if routed:
        parts = []
        if routed.get("qa_fix"):
            parts.append(f"QA fixes: {routed['qa_fix']}x")
        if routed.get("pm_route"):
            parts.append(f"PM routes: {routed['pm_route']}x")
        if routed.get("escalate"):
            parts.append(f"Escalations: {routed['escalate']}x")
        if parts:
            lines += ["", "### 🔧 Auto-Remediation",
                      "- " + " | ".join(parts)]

    if reconciled:
        lines += ["", "### 🔄 Board Updates"]
        for n, st in reconciled:
            lines.append(f"- {_issue_link(n, provider)} → {st}")

    if delivered:
        lines += ["", "### 📨 Reports Delivered"]
        for pr in delivered:
            lines.append(f"- Doc report for {_pr_link(pr, provider)}")

    return "\n".join(lines)


def render_all_summaries(
    summaries: dict[str, dict[str, Any]],
    providers: dict[str, Any] | None = None,
    *,
    dry_run: bool = False,
) -> str:
    """Render all project summaries into one notification message.

    ``providers`` is an optional ``{project_name: VCSProvider}`` map used to
    build hyperlinks. Returns ``""`` when nothing happened.
    """
    providers = providers or {}
    parts = []
    for name, s in summaries.items():
        msg = render_dispatch_summary(name, s, providers.get(name), dry_run=dry_run)
        if msg:
            parts.append(msg)
    if not parts:
        return ""
    return "\n\n---\n\n".join(parts)


# ── doc report notification envelope ─────────────────────────────────────────

def render_doc_report_notification(
    repo: str,
    pr_number: int,
    pr_url: str,
    report_body: str,
    *,
    issue_number: int | None = None,
    issue_url: str = "",
) -> str:
    """Wrap a documentation agent's PR comment in a rich notification envelope.

    ``report_body`` is the raw ``**Agent: documentation**`` PR comment embedded
    verbatim. Returns the full notification ready to deliver via ``hermes send``.
    """
    pr_link = _link(f"PR #{pr_number}", pr_url)
    issue_ref = ""
    if issue_number:
        issue_ref = f" · Issue {_link(f'#{issue_number}', issue_url)}"

    nav = _link("View PR", pr_url)
    if issue_number and issue_url:
        nav += f" · {_link('View Issue', issue_url)}"

    return (
        f"## 📋 Doc Report — {pr_link}{issue_ref}\n\n"
        f"**Repository:** `{repo}`\n\n"
        f"---\n\n"
        f"{report_body.strip()}\n\n"
        f"---\n\n"
        f"{nav}"
    )


# ── pipeline-failure notification ─────────────────────────────────────────────

def render_pipeline_failure(
    name: str,
    repo: str,
    issue_number: int | None = None,
    pr_number: int | None = None,
    error: str = "",
    provider=None,
) -> str:
    """Notification for a dispatch error or CI pipeline failure."""
    issue_ref = f" — issue {_issue_link(issue_number, provider)}" if issue_number else ""
    pr_ref = f" — {_pr_link(pr_number, provider)}" if pr_number else ""
    details = f"\n\n**Detail:** {error}" if error else ""
    return (
        f"## ❌ Pipeline Failure — **{name}**\n\n"
        f"**Repository:** `{repo}`{issue_ref}{pr_ref}{details}"
    )


# ── PR-ready notification ─────────────────────────────────────────────────────

def render_security_escalation(
    name: str,
    issue_number: int,
    issue_title: str,
    concern: str,
    *,
    issue_url: str = "",
    provider=None,
) -> str:
    """Urgent notification when the validator flags an issue as a potential security threat.

    Sent immediately when the validator detects prompt injection, social
    engineering, credential exfiltration, backdoor requests, or supply-chain
    attack patterns. The pipeline is blocked pending human review.
    """
    issue_ref = _link(f"#{issue_number}: {issue_title}", issue_url) if issue_url else f"#{issue_number}: {issue_title}"
    return (
        f"## 🚨 Security Escalation — **{name}**\n\n"
        f"**Issue:** {issue_ref}\n\n"
        f"**Concern:**\n{concern}\n\n"
        f"---\n"
        f"The Daedalus pipeline has been **blocked** on this issue. "
        f"No code will be written until a human reviews and re-classifies it. "
        f"If the issue is legitimate, close and re-open it with additional context "
        f"that addresses the concern above, then move it back to **Ready**."
    )


def render_pr_ready(
    name: str,
    pr_number: int,
    pr_url: str,
    pr_title: str,
    base_branch: str,
    issue_number: int | None = None,
    issue_url: str = "",
) -> str:
    """Notification when a PR is ready for human review."""
    pr_link = _link(f"PR #{pr_number}: {pr_title}", pr_url)
    issue_ref = ""
    if issue_number:
        issue_ref = f"\n**Issue:** {_link(f'#{issue_number}', issue_url)}"

    return (
        f"## 🔍 PR Ready for Review — **{name}**\n\n"
        f"**PR:** {pr_link}"
        f"{issue_ref}\n"
        f"**Target branch:** `{base_branch}`\n\n"
        f"{_link('Open PR', pr_url)}"
    )

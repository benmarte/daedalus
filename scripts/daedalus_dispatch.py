#!/usr/bin/env python3
"""Deterministic daedalus dispatch — the cron entrypoint (run with --no-agent).

Each tick, for the project whose workdir matches the cwd:
  1. For every in-scope issue, reconcile its GitHub Project status from PR state
     (open PR -> In review, merged -> Done).
  2. For new issues (no PR, no existing kanban task): set the card to In progress
     and create a Hermes-kanban task carrying the issue + lifecycle instructions.
  3. Dispatch the board so Hermes workers execute the tasks — Hermes tracks their
     status/runs/heartbeat live, so tracking is deterministic, not agent-dependent.

The ONLY agent-driven part is the code each kanban worker writes. All board and
status bookkeeping happens here, in code, so it can never be skipped.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make the plugin's modules importable. This script may run in place (plugin/
# scripts/) OR be COPIED into ~/.hermes/scripts/ (Hermes --script rejects symlinks
# that escape that dir), so locate the plugin root robustly by looking for core/.
def _find_plugin_root() -> Path:
    for c in (Path(__file__).resolve().parent.parent,
              Path.home() / ".hermes" / "plugins" / "daedalus"):
        if (c / "core").is_dir():
            return c
    return Path(__file__).resolve().parent.parent


_PLUGIN_ROOT = _find_plugin_root()
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from config import ConfigLoader  # noqa: E402
from core import iterate  # noqa: E402
from core import providers  # noqa: E402
from core import kanban  # noqa: E402
from core import registry  # noqa: E402
from core import source_specs  # noqa: E402

logger = logging.getLogger("daedalus.dispatch")

_LIFECYCLE = ("Triage → Spec → Plan → Build → Test → Review → Code-Simplify → Ship")

# Notification event types a cron.notifications[] entry can subscribe to.
NOTIFY_EVENTS = ("doc-report", "dispatch-summary", "pipeline-failure", "pr-ready")


def _notify_targets(resolved: Dict[str, Any], event: str) -> List[str]:
    """Delivery targets for a notification event.

    ``cron.notifications`` (list of {platform, target, events}) takes
    precedence; entries with no ``events`` list receive every event.
    Falls back to the legacy single ``cron.deliver`` string, which receives
    every event. Targets are ``hermes send`` strings (``slack:C123``,
    ``discord:#general``, ``telegram:-100123``, ``signal:+15551234``, …).
    """
    cron = resolved.get("cron") or {}
    notifications = cron.get("notifications")
    if notifications:
        out: List[str] = []
        for entry in notifications:
            if not isinstance(entry, dict):
                continue
            target = (entry.get("target") or "").strip()
            if not target:
                continue
            events = entry.get("events") or list(NOTIFY_EVENTS)
            if event in events and target not in out:
                out.append(target)
        return out
    deliver = (cron.get("deliver") or "").strip()
    return [deliver] if deliver else []


def _board_slug(repo: str, name: str = "") -> str:
    import re
    slug = repo.replace("/", "-") if repo else name
    return re.sub(r"[^a-zA-Z0-9_-]", "-", slug).strip("-").lower() or name


def _fetch_issues(provider, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Open issues matching the configured label filter (ANY label), deduped."""
    if provider is None:
        return []
    state = filters.get("state", "open")
    limit = int(filters.get("limit", 20))
    labels = [l for l in (filters.get("labels") or []) if l]
    return [i.as_dict() for i in provider.list_issues(state=state, labels=labels, limit=limit)]


# API-based instructions only — no gh/glab/az CLIs are installed for workers.
_PR_COMMENT_HOWTO = {
    "github": "the GitHub API with your GITHUB_TOKEN env var: "
              "POST https://api.github.com/repos/{repo}/issues/<pr>/comments "
              "with JSON body {{\"body\": \"<report markdown>\"}} and header "
              "'Authorization: Bearer $GITHUB_TOKEN' (curl works)",
    "gitlab": "the GitLab API with your GITLAB_TOKEN env var: "
              "POST /api/v4/projects/<project-id>/merge_requests/<mr>/notes "
              "with header 'PRIVATE-TOKEN: $GITLAB_TOKEN'",
    "azuredevops": "the Azure DevOps PR threads API with your AZURE_DEVOPS_PAT "
                   "env var (Basic auth, base64 of ':' + PAT)",
}

_PR_CREATE_HOWTO = {
    "github": "the GitHub API (POST https://api.github.com/repos/{repo}/pulls with "
              "'Authorization: Bearer $GITHUB_TOKEN')",
    "gitlab": "the GitLab API (POST /api/v4/projects/<project-id>/merge_requests with "
              "'PRIVATE-TOKEN: $GITLAB_TOKEN')",
    "azuredevops": "the Azure DevOps API (POST .../pullrequests, Basic auth with "
                   "$AZURE_DEVOPS_PAT)",
}


def _task_body(repo: str, issue: Dict[str, Any], iterations: int, workdir: str,
               notify_target: str = "", base_branch: str = "dev",
               provider_name: str = "github") -> str:
    """Triage body for decompose(): describes the FULL lifecycle so the decomposer
    fans it out across the roster (developer → reviewer → security-analyst →
    documentation). Each role's instructions are spelled out so routing is clean."""
    n = issue.get("number")
    title = issue.get("title", "")
    body = (issue.get("body") or "").strip()
    comment_howto = _PR_COMMENT_HOWTO.get(provider_name,
                                          _PR_COMMENT_HOWTO["github"]).format(repo=repo)
    pr_create_howto = _PR_CREATE_HOWTO.get(provider_name,
                                           _PR_CREATE_HOWTO["github"]).format(repo=repo)
    return (
        f"Deliver issue {repo}#{n}: {title}\n"
        f"Work in the existing git repo at {workdir} (cd there first). Base branch: {base_branch}.\n\n"
        f"Decompose this into the following role tasks and assign each to the right agent:\n\n"
        f"1. DEVELOPER — implement the fix/feature. Follow the agent-skills lifecycle "
        f"({_LIFECYCLE}). Branch fix/issue-{n}-<slug>, write code + tests, iterate up to {iterations}x "
        f"if review fails. Push the branch (git credentials are pre-configured) and open a PR "
        f"into {base_branch} via {pr_create_howto} — no gh/glab/az CLI is installed. "
        f"The PR body MUST include `Closes #{n}` on its own line "
        f"(REQUIRED — links the issue and tracks it to completion), plus Problem, Fix, How to test, "
        f"Manual testing.\n"
        f"2. REVIEWER — review the developer's PR for correctness, quality, and performance; request "
        f"changes or approve.\n"
        f"3. SECURITY-ANALYST — audit the PR diff for vulnerabilities (authz, secrets, injection, "
        f"input validation); flag findings or sign off.\n"
        f"4. DOCUMENTATION — after the PR is open and reviewed, write a detailed completion report and "
        f"post it as a comment on the PR ({comment_howto}). "
        f"Use the PR number from the chain above (developer/reviewer cards carry it). "
        f"The report MUST cover: the issue (#{n} + summary), what was fixed, the files edited (with a "
        f"one-line note per file), how it was resolved, the PR link, and step-by-step instructions to "
        f"test it manually. Use clear Markdown with a heading and a file-change table. "
        f"Prefix the comment with `**Agent: documentation**` so the dispatcher can locate it. "
        f"NOTE: Slack delivery is handled automatically by the dispatcher — do NOT attempt to "
        f"deliver the report yourself.\n\n"
        f"--- Issue #{n} ---\n{body}\n"
    )


def run(resolved: Dict[str, Any], *, assignee: Optional[str] = None, max_dispatch: int = 5,
        dry_run: bool = False, provider=None) -> Dict[str, Any]:
    """Reconcile statuses, create tasks for new issues, and dispatch. Returns a summary.

    When dry_run is True, no board status moves, kanban cards, or dispatches happen —
    every mutating action is logged as "[dry-run] would ..." and reflected in the
    returned summary, so a tick can be safely previewed before scheduling the cron.

    ``provider`` (a core.providers.VCSProvider) is built from the resolved config
    when not injected (tests inject a fake).
    """
    repo = resolved.get("repo", "")
    filters = (resolved.get("issues") or {}).get("filters", {})
    execution = resolved.get("execution") or {}
    iterations = int(execution.get("max_lifecycle_iterations", 3))   # self-improving loop cap (configurable)
    # Worker assignment is handled by decompose() routing on profile descriptions,
    # so no single worker_profile is pinned here.
    workdir = resolved.get("workdir", "")
    # Messaging target the documentation agent's completion report is sent to.
    notify_target = (resolved.get("cron") or {}).get("deliver", "")
    base_branch = (resolved.get("vcs") or {}).get("target_branch", "dev")
    slug = _board_slug(repo, resolved.get("name", ""))
    if provider is None:
        provider = providers.get_provider(resolved)
    board_mode = bool(provider is not None and provider.board_configured())
    if not board_mode:
        logger.warning("dispatch: no VCS board configured — skipping board status moves")

    # Ready-gating: when a board is configured, ONLY issues whose board status is
    # in the configured ready_statuses become new work. PR-state reconciliation
    # (open/merged -> In review) below still runs for every open issue, regardless of status.
    ready: Optional[set] = None
    if board_mode:
        ready_statuses = ((resolved.get("tracking") or {}).get("ready_statuses")
                          or [provider.status_name("ready")])
        ready = provider.board_numbers_with_statuses(ready_statuses)
        logger.info("dispatch: %d issue(s) in %s: %s", len(ready), ready_statuses, sorted(ready))

    kanban.ensure_board(slug)

    # ── spec-file trigger source ─────────────────────────────────────────────
    # When sources.local_specs.enabled, scan <repo>/.hermes/pending/ for *.md
    # files and create a triage card for each (idempotent via file-path key).
    # This runs BEFORE auto-advance + GitHub-issue polling so spec-driven work
    # enters the board regardless of whether GitHub issues are configured.
    spec_sources = resolved.get("sources", {}).get("local_specs", {})
    spec_created = []
    if spec_sources.get("enabled"):
        spec_dir = spec_sources.get("directory", ".hermes/pending/")
        spec_files = source_specs.list_spec_files(workdir, directory=spec_dir)
        for sf in spec_files:
            tid = source_specs.spec_to_triage(
                slug, workdir, sf,
                base_branch=base_branch,
                workspace=f"dir:{workdir}" if workdir else None,
            )
            if tid:
                kanban.decompose(slug, tid)
                spec_created.append(sf.name)
        if spec_created:
            logger.info(
                "dispatch: spec-file source created %d triage card(s): %s",
                len(spec_created), spec_created,
            )

    # ── auto-advance (CI-aware routing + self-healing) ────────────────────────
    # For every blocked card: classify its state (dev+green CI → advance,
    # dev+red CI → fix card, reviewer with findings → PM routing card, etc.) and
    # execute the appropriate action. The self-healing loop creates fix-up tasks
    # for failing CI/review and escalates after MAX_FIX_ATTEMPTS.
    #
    # Also run native diagnostics to surface stuck-in-blocked cards with severity
    # alongside the classify → execute path. Diagnostics degrades gracefully.
    diag = kanban.diagnostics(slug)
    if diag:
        logger.info("dispatch: diagnostics for %s: %d finding(s)", slug, len(diag))
        for d in diag:
            logger.info("dispatch:   [%s] %s — %s",
                        d.get("severity", "?"), d.get("task_id", "?"),
                        d.get("message", ""))
    iterate_counts, advance_prs = iterate.run_iterate(
        slug, repo, resolved=resolved, provider=provider, dry_run=dry_run,
    )
    # Separate advance PR numbers from routed actions (dev_fix / escalate) for
    # the human summary so PR numbers are reported correctly.
    routed_actions = {k: v for k, v in iterate_counts.items()
                      if v > 0 and k not in (iterate.ADVANCE, iterate.APPROVE_ADVANCE)}
    if any(c > 0 for c in iterate_counts.values()) and not dry_run:
        kanban.dispatch(slug, max_spawns=max_dispatch)

    # ── doc-report delivery ──────────────────────────────────────────────────
    # The dispatcher delivers documentation reports (PR comments prefixed
    # `**Agent: documentation**`) to every configured doc-report target,
    # because agents run in isolated profile HOMEs without messaging config.
    # Idempotent via a hidden PR comment sentinel.
    slack_delivered = _deliver_doc_reports(
        slug, provider, _notify_targets(resolved, "doc-report"), dry_run=dry_run,
    )

    created, reconciled, completed = [], [], []
    issues: List[Dict[str, Any]] = []

    if not board_mode:
        # Kanban-only mode: no VCS board, so the kanban board IS the tracker.
        # A human creates a triage card (dashboard / `hermes kanban create
        # --triage`); we fan every triage card out across the roster and
        # dispatch. (auto-advance above already flows review-required handoffs.)
        if dry_run:
            logger.info("[dry-run] kanban-only: would decompose triage cards + dispatch")
        else:
            kanban.decompose_all_triage(slug)
            kanban.dispatch(slug, max_spawns=max_dispatch)
        summary = {"board": slug, "mode": "kanban", "created": created,
                   "reconciled": reconciled, "completed": completed,
                   "advance_prs": advance_prs, "routed_actions": routed_actions,
                   "issues_seen": 0, "spec_created": spec_created,
                   "slack_delivered": slack_delivered}
        logger.info("dispatch summary: %s", summary)
        return summary

    # Board mode: poll Ready issues, reconcile PR state, triage+decompose.
    in_review_name = provider.status_name("in_review")
    existing = kanban.list_issue_numbers(slug)
    issues = _fetch_issues(provider, filters)

    for issue in issues:
        n = issue["number"]
        # Reconciliation acts ONLY on daedalus-managed issues — ones that have a
        # kanban card. Issues the daedalus never dispatched (incl. everything not
        # in "Ready") are left untouched, so a tick never surprises non-Ready issues.
        if n in existing:
            pr = provider.pr_state_for_issue(n)
            if pr == "merged":
                # Merged into dev = work complete. GitHub does NOT auto-close issues
                # on a non-default-branch merge, so we do it: card -> Done + close.
                if dry_run:
                    logger.info("[dry-run] would set #%s -> Done + close issue (PR merged)", n)
                    completed.append(n)
                else:
                    provider.board_set_status(n, provider.status_name("done"))
                    if provider.close_issue(n):
                        completed.append(n)
            elif pr == "open":
                # PR open and awaiting review -> In review.
                if dry_run:
                    logger.info("[dry-run] would set #%s -> %s (PR open)", n, in_review_name)
                    reconciled.append((n, in_review_name))
                elif provider.board_set_status(n, in_review_name):
                    reconciled.append((n, in_review_name))
            # No/closed PR on a managed issue: leave it (worker still in progress).
            continue
        # Unmanaged issue: only "Ready" items become new work.
        if ready is not None and n not in ready:
            continue  # Ready-gating: not in "Ready" -> don't dispatch yet
        if provider.pr_state_for_issue(n):
            # Already has an open/merged PR -> work exists; don't dispatch a
            # duplicate worker. (Checked only for Ready candidates to limit API calls.)
            logger.info("dispatch: #%s is Ready but already has a PR — skipping (no duplicate)", n)
            continue
        if len(created) >= max_dispatch:
            break  # cap new tasks per tick
        # New work (deterministic, code): board status -> In progress, then
        # create a TRIAGE card and decompose it so the roster fans out across
        # developer -> reviewer -> security-analyst -> documentation. Hermes tracks
        # each sub-task live on the board.
        if dry_run:
            logger.info("[dry-run] would dispatch #%s (%s): set In progress + create triage card + decompose",
                        n, issue.get("title", ""))
            created.append(n)
            existing.add(n)
            continue
        provider.board_set_status(n, provider.status_name("in_progress"))
        # Pin the triage to the project checkout; Hermes propagates the workspace to
        # every decomposed child, so no worker can wander into the wrong repo.
        tid = kanban.create_triage(slug, n, issue.get("title", ""),
                                   _task_body(repo, issue, iterations, workdir, notify_target,
                                              base_branch, provider_name=provider.name),
                                   idempotency_key=f"issue-{n}",
                                   workspace=f"dir:{workdir}" if workdir else None)
        if tid:
            kanban.decompose(slug, tid)  # fan out to dev/reviewer/security/documentation
            created.append(n)
            existing.add(n)

    if created and not dry_run:
        kanban.dispatch(slug, max_spawns=max_dispatch)  # nudge (gateway also auto-dispatches)

    # ── cleanup: archive kanban tasks for issues closed directly on VCS ───────
    # Issues closed without a merged PR (won't-fix, duplicate, manual close)
    # never appear in the open-issue fetch, so the reconciliation loop above
    # never sees them. Find managed issue numbers absent from this tick's open
    # fetch, check their VCS state, and complete their kanban tasks.
    seen_open = {i["number"] for i in issues}
    orphaned = existing - seen_open
    for n in sorted(orphaned):
        state = provider.get_issue_state(n)
        if state != "closed":
            continue  # still open (filtered by label/limit) or unknown — leave it
        if dry_run:
            logger.info("[dry-run] #%s closed externally → would archive kanban tasks + Done", n)
            completed.append(n)
            continue
        provider.board_set_status(n, provider.status_name("done"))
        closed_tasks = kanban.close_issue_tasks(slug, n)
        logger.info("dispatch: #%s closed externally → Done (%d task(s) completed: %s)",
                    n, len(closed_tasks), closed_tasks)
        completed.append(n)

    summary = {"board": slug, "mode": provider.name, "created": created, "reconciled": reconciled,
               "completed": completed, "advance_prs": advance_prs,
               "routed_actions": routed_actions, "issues_seen": len(issues),
               "spec_created": spec_created, "slack_delivered": slack_delivered}
    logger.info("dispatch summary: %s", summary)
    return summary


def _human_summary(summaries: Dict[str, Dict[str, Any]], dry_run: bool = False) -> str:
    """Human-readable Slack/cron message — or '' when nothing happened.

    The --no-agent cron delivers stdout verbatim and treats empty stdout as
    SILENT, so a no-op tick produces no message (no spam); only ticks with real
    activity post a readable summary.
    """
    lines = []
    for name, s in summaries.items():
        if s.get("error"):
            lines.append(f"• *{name}* — ⚠️ error: {s['error']}")
            continue
        bits = []
        if s.get("created"):
            bits.append("dispatched " + ", ".join(f"#{n}" for n in s["created"]))
        if s.get("completed"):
            bits.append("✅ closed " + ", ".join(f"#{n}" for n in s["completed"]))
        if s.get("advance_prs"):
            bits.append("⏭️ advanced PR " + ", ".join(f"#{pr}" for pr in s["advance_prs"]))
        ra = s.get("routed_actions")
        if ra:
            parts = []
            if ra.get("dev_fix_ci"):
                parts.append(f"ci-fix:{ra['dev_fix_ci']}")
            if ra.get("pm_route"):
                parts.append(f"pm-route:{ra['pm_route']}")
            if ra.get("escalate"):
                parts.append(f"escalate:{ra['escalate']}")
            if parts:
                bits.append("🔧 " + ", ".join(parts))
        if s.get("reconciled"):
            bits.append("🔄 " + ", ".join(f"#{n}→{st}" for n, st in s["reconciled"]))
        if s.get("slack_delivered"):
            bits.append("📨 delivered " + ", ".join(f"PR #{pr}" for pr in s["slack_delivered"]))
        if bits:
            lines.append(f"• *{name}* ({s.get('mode', '?')}): " + "; ".join(bits))
    if not lines:
        return ""  # nothing happened -> silent
    header = "*🤖 Daedalus dispatch*" + (" _(dry-run)_" if dry_run else "")
    return header + "\n" + "\n".join(lines)


# ── Slack delivery (dispatcher context, NOT agent) ──────────────────────────


def _send_via_hermes(notify_target: str, report_body: str) -> bool:
    """Send a report to Slack via `hermes send` from the dispatcher's root context.

    Runs ``hermes send -t <notify_target> --file <tmpfile>`` via subprocess
    (list-args, no shell). A temporary file is created for the body and cleaned
    up afterwards. Returns True on success; False is logged gracefully.
    """
    import tempfile

    if not notify_target or not report_body.strip():
        return False

    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False,
                                         encoding="utf-8") as tf:
            tf.write(report_body)
            tmp = tf.name
        r = subprocess.run(
            ["hermes", "send", "-t", notify_target, "--file", tmp, "-q"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            logger.warning(
                "dispatch: hermes send to %s failed (rc=%s): %s",
                notify_target, r.returncode, (r.stderr or "").strip(),
            )
            return False
        logger.info("dispatch: delivered doc report to %s", notify_target)
        return True
    except Exception as e:
        logger.warning("dispatch: hermes send to %s raised: %s", notify_target, e)
        return False
    finally:
        if tmp:
            try:
                Path(tmp).unlink()
            except OSError:
                pass


def _deliver_doc_reports(
    slug: str, provider, notify_targets,
    *, dry_run: bool = False,
) -> List[int]:
    """Deliver completed documentation reports to the messaging target(s).

    Scans the board's DONE cards for documentation cards whose linked PR has a
    ``**Agent: documentation**`` comment. For each such PR, fetches the report
    body from the comment and sends it to every target in ``notify_targets``
    (a list of ``hermes send`` target strings; a bare string is accepted for
    backward compatibility).

    Idempotent: posts a hidden sentinel PR comment
    (``<!-- daedalus:slack-delivered -->``) after delivery; skips PRs that already
    have it. The sentinel is posted once ANY target received the report (so a
    flaky secondary channel can't re-spam the channels that already got it);
    failed targets are logged.

    Returns the list of PR numbers that were successfully delivered (for the
    human summary).
    """
    if isinstance(notify_targets, str):
        notify_targets = [notify_targets] if notify_targets else []
    notify_targets = [t for t in (notify_targets or []) if t]
    if not notify_targets or provider is None:
        return []

    delivered: List[int] = []
    doc_cards = kanban.list_tasks(slug, status="done")

    for card in doc_cards:
        assignee = (card.get("assignee") or "").strip()
        if assignee != "documentation-daedalus":
            continue

        # Resolve the PR number: try the card's body/events for a PR reference,
        # then fall back to the issue number on the parent triage card.
        pr_number = _parse_pr_from_card(card)
        if pr_number is None:
            # Try parent → issue number → PR
            pr_number = _resolve_pr_from_parents(slug, provider, card)

        if pr_number is None:
            logger.debug(
                "dispatch: doc card %s has no resolvable PR — skipping Slack delivery",
                card.get("id"),
            )
            continue

        # Idempotence: skip if already delivered
        if provider.pr_has_delivery_marker(pr_number):
            logger.debug(
                "dispatch: PR #%s already has slack-delivered marker — skipping",
                pr_number,
            )
            continue

        # Find the **Agent: documentation** comment on the PR
        report_body = _find_doc_comment(provider, pr_number)
        if not report_body:
            logger.debug(
                "dispatch: PR #%s has no **Agent: documentation** comment yet — skipping",
                pr_number,
            )
            continue

        if dry_run:
            logger.info(
                "[dry-run] would deliver doc report for PR #%s to %s",
                pr_number, ", ".join(notify_targets),
            )
            delivered.append(pr_number)
            continue

        # Deliver via the dispatcher's root context — fan out to every target.
        sent_to = [t for t in notify_targets if _send_via_hermes(t, report_body)]
        if not sent_to:
            # Total send failure → do NOT post the sentinel (retry next tick)
            continue
        if len(sent_to) < len(notify_targets):
            logger.warning(
                "dispatch: doc report for PR #%s reached %d/%d targets (failed: %s)",
                pr_number, len(sent_to), len(notify_targets),
                ", ".join(t for t in notify_targets if t not in sent_to),
            )

        # Post sentinel so we never re-deliver
        if not provider.post_delivery_marker(pr_number, report_body):
            # Sentinel post failure is noisy but not fatal — we delivered.
            # Next tick might re-deliver but the dedup sentinel is best-effort.
            logger.warning(
                "dispatch: delivered PR #%s but sentinel post failed — may re-deliver",
                pr_number,
            )

        delivered.append(pr_number)
        logger.info(
            "dispatch: delivered doc report for PR #%s to %s",
            pr_number, ", ".join(sent_to),
        )

    return delivered


def _parse_pr_from_card(card: dict) -> Optional[int]:
    """Extract a PR number from a card's body + latest summary."""
    body = (card.get("body") or "").strip()
    summary = (card.get("latest_summary") or "").strip()
    text = f"{body}\n{summary}"
    m = re.search(r"PR #(\d+)", text)
    return int(m.group(1)) if m else None


def _summary_events(summary: Dict[str, Any]) -> set:
    """Event types a tick summary triggers (for notifications[] filtering)."""
    events = {"dispatch-summary"}
    if summary.get("error"):
        events.add("pipeline-failure")
    if summary.get("advance_prs") or summary.get("reconciled"):
        events.add("pr-ready")
    return events


def _notify_project_summary(name: str, summary: Dict[str, Any],
                            resolved: Dict[str, Any], *, dry_run: bool = False) -> bool:
    """Self-deliver a project's tick summary to its ``cron.notifications`` targets.

    Returns True when the project uses ``notifications[]`` — the caller must
    then NOT include it in stdout (which the legacy cron ``--deliver`` path
    would deliver a second time). Legacy single-``deliver`` projects return
    False and keep flowing through cron stdout delivery.
    """
    if not ((resolved.get("cron") or {}).get("notifications")):
        return False
    msg = _human_summary({name: summary}, dry_run=dry_run)
    if not msg:
        return True  # silent tick — handled, nothing to send
    targets: List[str] = []
    for event in sorted(_summary_events(summary)):
        for t in _notify_targets(resolved, event):
            if t not in targets:
                targets.append(t)
    for t in targets:
        if dry_run:
            logger.info("[dry-run] would send dispatch summary for %s to %s", name, t)
        else:
            _send_via_hermes(t, msg)
    return True


def _resolve_pr_from_parents(slug: str, provider, card: dict) -> Optional[int]:
    """Walk parent cards to find an issue number, then resolve to a PR."""
    parents = card.get("parents") or []
    for pid in parents:
        parent = kanban.show_card(slug, pid)
        if not parent:
            continue
        # Try to find an issue number in the parent's title
        m = re.search(r"#(\d+)", (parent.get("title") or ""))
        if m:
            issue_num = int(m.group(1))
            pr = provider.pr_number_for_issue(issue_num)
            if pr:
                return pr
    return None


def _find_doc_comment(provider, pr_number: int) -> str:
    """Return the body of the first ``**Agent: documentation**`` PR comment, or ''."""
    for c in provider.list_pr_comments(pr_number):
        body = c.body or ""
        if "**Agent: documentation**" in body:
            return body
    return ""


def main() -> int:
    """Cron / single-repo entrypoint.

    Without --repo: sweeps every repo registered in core.registry, resolves
    each via ConfigLoader().resolve_repo_config(), calls run(), aggregates
    per-repo summaries into a human Slack message.

    With --repo <path>: resolves that single repo and calls run() for it.

    Always returns 0 (errors are logged + summarized, never via exit code).
    """
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Daedalus dispatch — sweep registered repos or run a single one."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Log intended actions without mutating anything.")
    parser.add_argument("--repo", type=str, default=None,
                        help="Run dispatch for a single repo path (skips the registry sweep).")
    args = parser.parse_args()

    dry_run = args.dry_run
    if dry_run:
        logger.info("dispatch: DRY RUN — no GitHub status moves, kanban cards, or dispatches")

    loader = ConfigLoader()
    summaries: Dict[str, Dict[str, Any]] = {}

    # -- single-repo path ----------------------------------------------------
    if args.repo:
        repo_path = str(Path(args.repo).expanduser().resolve())
        try:
            resolved = loader.resolve_repo_config(repo_path)
        except Exception as e:
            logger.warning("dispatch: could not resolve %s: %s", repo_path, e)
            return 0
        name = resolved.get("name", repo_path)
        try:
            summaries[name] = run(resolved, dry_run=dry_run)
        except Exception as e:
            logger.error("dispatch: run failed for %s: %s", name, e)
            summaries[name] = {"error": str(e)}
        if _notify_project_summary(name, summaries[name], resolved, dry_run=dry_run):
            return 0
        msg = _human_summary(summaries, dry_run=dry_run)
        if msg:
            print(msg)
        return 0

    # -- registry sweep ------------------------------------------------------
    repo_paths = registry.list_projects()
    if not repo_paths:
        logger.info("dispatch: registry is empty — nothing to do")
        return 0

    resolved_map: Dict[str, Dict[str, Any]] = {}
    for rp in repo_paths:
        try:
            resolved = loader.resolve_repo_config(rp)
        except FileNotFoundError:
            logger.warning("dispatch: no .hermes/daedalus.yaml in %s — skipping", rp)
            continue
        except Exception as e:
            logger.warning("dispatch: could not resolve %s: %s", rp, e)
            continue
        name = resolved.get("name", rp)
        resolved_map[name] = resolved
        try:
            summaries[name] = run(resolved, dry_run=dry_run)
        except Exception as e:
            logger.error("dispatch: run failed for %s: %s", name, e)
            summaries[name] = {"error": str(e)}

    # Projects with cron.notifications self-deliver their summary (multi-target,
    # any platform); the rest flow through stdout, which the no-agent cron
    # delivers to its legacy --deliver target. stdout stays EMPTY on a no-op
    # tick so the cron is silent (no JSON spam). Full detail still goes to
    # stderr via the per-project logger.info above.
    legacy: Dict[str, Dict[str, Any]] = {}
    for name, s in summaries.items():
        r = resolved_map.get(name)
        if r is None or not _notify_project_summary(name, s, r, dry_run=dry_run):
            legacy[name] = s
    msg = _human_summary(legacy, dry_run=dry_run)
    if msg:
        print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
import os
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
from core import github_project as gp  # noqa: E402
from core import iterate  # noqa: E402
from core import kanban  # noqa: E402
from core import registry  # noqa: E402
from core import source_specs  # noqa: E402

logger = logging.getLogger("daedalus.dispatch")

_LIFECYCLE = ("Triage → Spec → Plan → Build → Test → Review → Code-Simplify → Ship")


def _board_slug(repo: str, name: str = "") -> str:
    import re
    slug = repo.replace("/", "-") if repo else name
    return re.sub(r"[^a-zA-Z0-9_-]", "-", slug).strip("-").lower() or name


def _fetch_issues(repo: str, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Open issues matching the configured label filter (ANY label), deduped."""
    state = filters.get("state", "open")
    limit = int(filters.get("limit", 20))
    labels = [l for l in (filters.get("labels") or []) if l]
    label_sets = [[l] for l in labels] or [[]]
    seen: Dict[int, Dict[str, Any]] = {}
    for ls in label_sets:
        args = ["issue", "list", "--repo", repo, "--state", state, "--limit", str(limit),
                "--json", "number,title,body,labels"]
        for l in ls:
            args += ["--label", l]
        data = gp._gh_json(args)
        for it in (data or []):
            seen.setdefault(it["number"], it)
    return list(seen.values())[:limit]


def _task_body(repo: str, issue: Dict[str, Any], iterations: int, workdir: str,
               notify_target: str = "", base_branch: str = "dev") -> str:
    """Triage body for decompose(): describes the FULL lifecycle so the decomposer
    fans it out across the roster (developer → reviewer → security-analyst →
    documentation). Each role's instructions are spelled out so routing is clean."""
    n = issue.get("number")
    title = issue.get("title", "")
    body = (issue.get("body") or "").strip()
    deliver = (
        f"\\nThe DISPATCHER will deliver the report to {notify_target} automatically "
        f"when the documentation card completes; the agent does NOT need to call `hermes send`."
        if notify_target else ""
    )
    return (
        f"Deliver GitHub issue {repo}#{n}: {title}\n"
        f"Work in the existing git repo at {workdir} (cd there first). Base branch: {base_branch}.\n\n"
        f"Decompose this into the following role tasks and assign each to the right agent:\n\n"
        f"1. DEVELOPER — implement the fix/feature. Follow the agent-skills lifecycle "
        f"({_LIFECYCLE}). Branch fix/issue-{n}-<slug>, write code + tests, iterate up to {iterations}x "
        f"if review fails. Open a PR into {base_branch}. The PR body MUST include `Closes #{n}` on its own line "
        f"(REQUIRED — links the issue and tracks it to completion), plus Problem, Fix, How to test, "
        f"Manual testing.\n"
        f"2. REVIEWER — review the developer's PR for correctness, quality, and performance; request "
        f"changes or approve.\n"
        f"3. SECURITY-ANALYST — audit the PR diff for vulnerabilities (authz, secrets, injection, "
        f"input validation); flag findings or sign off.\n"
        f"4. DOCUMENTATION — after the PR is open and reviewed, write a detailed completion report and "
        f"post it as a comment on the PR using "
        f"`gh pr comment <PR-NUMBER> --repo {repo} --body-file <report.md>`. "
        f"The comment must start with `**Agent: documentation**` so the dispatcher can find it. "
        f"DO NOT attempt `hermes send` to Slack — the DISPATCHER will deliver the report "
        f"to the configured channel automatically.{deliver}\\n"
        f"The report MUST cover: the issue (#{n} + summary), what was fixed, the files edited (with a "
        f"one-line note per file), how it was resolved, the PR link, and step-by-step instructions to "
        f"test it manually. Use clear Markdown with a heading and a file-change table.\n\n"
        f"--- Issue #{n} ---\n{body}\n"
    )


def run(resolved: Dict[str, Any], *, assignee: Optional[str] = None, max_dispatch: int = 5,
        dry_run: bool = False) -> Dict[str, Any]:
    """Reconcile statuses, create tasks for new issues, and dispatch. Returns a summary.

    When dry_run is True, no GitHub status moves, kanban cards, or dispatches happen —
    every mutating action is logged as "[dry-run] would ..." and reflected in the
    returned summary, so a tick can be safely previewed before scheduling the cron.
    """
    repo = resolved.get("repo", "")
    owner = repo.split("/")[0] if "/" in repo else repo
    filters = (resolved.get("issues") or {}).get("filters", {})
    execution = resolved.get("execution") or {}
    iterations = int(execution.get("max_lifecycle_iterations", 3))   # self-improving loop cap (configurable)
    # Worker assignment is handled by decompose() routing on profile descriptions,
    # so no single worker_profile is pinned here.
    workdir = resolved.get("workdir", "")
    # Slack/etc. target the documentation agent posts its completion report to.
    notify_target = (resolved.get("cron") or {}).get("deliver", "")
    base_branch = (resolved.get("vcs") or {}).get("target_branch", "dev")
    slug = _board_slug(repo, resolved.get("name", ""))
    proj_num = (resolved.get("tracking") or {}).get("github_project_number")
    ghproj = gp.GitHubProject(owner, proj_num) if proj_num else None
    if not ghproj:
        logger.warning("dispatch: no tracking.github_project_number set — skipping GitHub status moves")

    # Ready-gating: when a Project board is configured, ONLY issues whose Project
    # status is in the configured ready_statuses become new work. PR-state reconciliation
    # (open/merged -> In review) below still runs for every open issue, regardless of status.
    ready: Optional[set] = None
    if ghproj:
        ready_statuses = (resolved.get("tracking") or {}).get("ready_statuses") or ["Ready"]
        ready = ghproj.numbers_with_statuses(ready_statuses)
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
        slug, repo, resolved=resolved, dry_run=dry_run,
    )
    # Separate advance PR numbers from routed actions (dev_fix / escalate) for
    # the human summary so PR numbers are reported correctly.
    routed_actions = {k: v for k, v in iterate_counts.items()
                      if v > 0 and k not in (iterate.ADVANCE, iterate.APPROVE_ADVANCE)}
    if any(c > 0 for c in iterate_counts.values()) and not dry_run:
        kanban.dispatch(slug, max_spawns=max_dispatch)

    created, reconciled, completed = [], [], []
    issues: List[Dict[str, Any]] = []

    if not ghproj:
        # Kanban-only mode: no GitHub Project board, so the kanban board IS the
        # tracker. A human creates a triage card (dashboard / `hermes kanban
        # create --triage`); we fan every triage card out across the roster and
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
                   "delivered": _deliver_doc_reports(slug, repo, notify_target,
                                                      dry_run=dry_run)}
        logger.info("dispatch summary: %s", summary)
        return summary

    # GitHub-Project mode: poll Ready issues, reconcile PR state, triage+decompose.
    existing = kanban.list_issue_numbers(slug)
    issues = _fetch_issues(repo, filters)

    for issue in issues:
        n = issue["number"]
        # Reconciliation acts ONLY on daedalus-managed issues — ones that have a
        # kanban card. Issues the daedalus never dispatched (incl. everything not
        # in "Ready") are left untouched, so a tick never surprises non-Ready issues.
        if n in existing:
            pr = gp.pr_state_for_issue(repo, n)
            if pr == "merged":
                # Merged into dev = work complete. GitHub does NOT auto-close issues
                # on a non-default-branch merge, so we do it: card -> Done + close.
                if dry_run:
                    logger.info("[dry-run] would set #%s -> Done + close issue (PR merged)", n)
                    completed.append(n)
                else:
                    if ghproj:
                        ghproj.set_status(n, "Done")
                    if gp.close_issue(repo, n):
                        completed.append(n)
            elif pr == "open":
                # PR open and awaiting review -> In review.
                if dry_run:
                    logger.info("[dry-run] would set #%s -> In review (PR open)", n)
                    reconciled.append((n, "In review"))
                elif ghproj and ghproj.set_status(n, "In review"):
                    reconciled.append((n, "In review"))
            # No/closed PR on a managed issue: leave it (worker still in progress).
            continue
        # Unmanaged issue: only "Ready" items become new work.
        if ready is not None and n not in ready:
            continue  # Ready-gating: not in "Ready" -> don't dispatch yet
        if gp.pr_state_for_issue(repo, n):
            # Already has an open/merged PR -> work exists; don't dispatch a
            # duplicate worker. (Checked only for Ready candidates to limit API calls.)
            logger.info("dispatch: #%s is Ready but already has a PR — skipping (no duplicate)", n)
            continue
        if len(created) >= max_dispatch:
            break  # cap new tasks per tick
        # New work (deterministic, code): GitHub overall status -> In progress, then
        # create a TRIAGE card and decompose it so the roster fans out across
        # developer -> reviewer -> security-analyst -> documentation. Hermes tracks
        # each sub-task live on the board.
        if dry_run:
            logger.info("[dry-run] would dispatch #%s (%s): set In progress + create triage card + decompose",
                        n, issue.get("title", ""))
            created.append(n)
            existing.add(n)
            continue
        if ghproj:
            ghproj.set_status(n, "In progress")
        # Pin the triage to the project checkout; Hermes propagates the workspace to
        # every decomposed child, so no worker can wander into the wrong repo.
        tid = kanban.create_triage(slug, n, issue.get("title", ""),
                                   _task_body(repo, issue, iterations, workdir, notify_target, base_branch),
                                   idempotency_key=f"issue-{n}",
                                   workspace=f"dir:{workdir}" if workdir else None)
        if tid:
            kanban.decompose(slug, tid)  # fan out to dev/reviewer/security/documentation
            created.append(n)
            existing.add(n)

    if created and not dry_run:
        kanban.dispatch(slug, max_spawns=max_dispatch)  # nudge (gateway also auto-dispatches)

    summary = {"board": slug, "mode": "github", "created": created, "reconciled": reconciled,
               "completed": completed, "advance_prs": advance_prs,
               "routed_actions": routed_actions, "issues_seen": len(issues),
               "spec_created": spec_created,
               "delivered": _deliver_doc_reports(slug, repo, notify_target,
                                                  dry_run=dry_run)}
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
        if s.get("delivered"):
            bits.append("📨 delivered report(s) for " + ", ".join(f"#{n}" for n in s["delivered"]))
        if bits:
            lines.append(f"• *{name}* ({s.get('mode', '?')}): " + "; ".join(bits))
    if not lines:
        return ""  # nothing happened -> silent
    header = "*🤖 Daedalus dispatch*" + (" _(dry-run)_" if dry_run else "")
    return header + "\n" + "\n".join(lines)


SLACK_DELIVERED_MARKER = "<!-- daedalus:slack-delivered -->"


def _subprocess_run(args: List[str], env: Optional[Dict[str, str]] = None,
                    timeout: int = 30) -> tuple[int, str, str]:
    """Run a subprocess; return (rc, stdout, stderr). Test-patchable."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", str(e)


def _deliver_doc_reports(slug: str, repo: str, notify_target: str,
                         board_cards: Optional[List[Dict[str, Any]]] = None,
                         dry_run: bool = False) -> List[int]:
    """Deliver documentation reports from completed cards to Slack via ``hermes send``.

    For every ``done`` documentation card whose PR has a ``**Agent: documentation**``
    comment, deliver that comment body to ``notify_target`` (the configured cron.deliver
    channel). Dedup via a ``<!-- daedalus:slack-delivered -->`` PR comment so each
    report is delivered at most once.

    Args:
        slug: Kanban board slug.
        repo: GitHub repo (org/name).
        notify_target: The cron.deliver channel (e.g. ``slack:tasks``).
        board_cards: Pre-fetched list of all kanban cards (avoids extra CLI call).
                     If None, list_tasks is called for the board.
        dry_run: If True, log intentions without sending or posting comments.

    Returns:
        List of issue numbers whose reports were delivered (or would be on dry_run).
    """
    delivered: List[int] = []

    if not notify_target or not repo:
        return delivered

    cards = board_cards if board_cards is not None else kanban.list_tasks(slug)
    if not cards:
        return delivered

    for card in cards:
        if (card.get("status") or "").lower() != "done":
            continue
        if (card.get("assignee") or "").strip().lower() != "documentation":
            continue

        title = card.get("title", "")
        # Extract issue number from title (e.g. "#42 Some title")
        m = re.search(r"#(\d+)", title)
        if not m:
            continue
        issue_n = int(m.group(1))

        pr_num = gp.pr_number_for_issue(repo, issue_n)
        if not pr_num:
            continue

        # Dedup: check for the delivered marker on the PR
        if gp.pr_find_comment(repo, pr_num, SLACK_DELIVERED_MARKER):
            logger.debug("dispatch: PR #%s already has slack-delivered marker — skipping", pr_num)
            # Still count as delivered (idempotent) but don't re-send
            delivered.append(issue_n)
            continue

        # Find the documentation comment
        doc_comment = gp.pr_find_comment(repo, pr_num, "**Agent: documentation**")
        if not doc_comment:
            continue

        body = (doc_comment.get("body") or "").strip()
        if not body:
            continue

        if dry_run:
            logger.info(
                "[dry-run] would deliver doc report for issue #%s (PR #%s) to %s",
                issue_n, pr_num, notify_target,
            )
            delivered.append(issue_n)
            continue

        # Deliver via hermes send (dispatcher context, root HOME — works).
        # Write the body to a temp file so large reports don't hit shell limits.
        try:
            import tempfile
            tf = tempfile.NamedTemporaryFile(
                mode="w", suffix=".md", delete=False, encoding="utf-8",
            )
            tf.write(body)
            tmp_path = tf.name
            tf.close()
            # Run hermes send from root context (no profile isolation).
            hermes_env = os.environ.copy()
            hermes_home = str(Path.home() / ".hermes")
            if Path(hermes_home).is_dir():
                hermes_env["HERMES_HOME"] = hermes_home
            rc, out, err = _subprocess_run(
                ["hermes", "send", "-t", notify_target, "--file", tmp_path],
                env=hermes_env,
            )
            if rc != 0:
                logger.warning(
                    "dispatch: hermes send failed for issue #%s (PR #%s) — rc=%s: %s",
                    issue_n, pr_num, rc, (err or out or "").strip(),
                )
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            logger.info(
                "dispatch: delivered doc report for issue #%s (PR #%s) to %s",
                issue_n, pr_num, notify_target,
            )
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass
        except Exception as e:
            logger.warning(
                "dispatch: hermes send exception for issue #%s (PR #%s): %s",
                issue_n, pr_num, e,
            )
            continue

        # Post dedup marker
        gp.pr_add_comment(repo, pr_num, SLACK_DELIVERED_MARKER)
        delivered.append(issue_n)

    return delivered


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
        msg = _human_summary(summaries, dry_run=dry_run)
        if msg:
            print(msg)
        return 0

    # -- registry sweep ------------------------------------------------------
    repo_paths = registry.list_projects()
    if not repo_paths:
        logger.info("dispatch: registry is empty — nothing to do")
        return 0

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
        try:
            summaries[name] = run(resolved, dry_run=dry_run)
        except Exception as e:
            logger.error("dispatch: run failed for %s: %s", name, e)
            summaries[name] = {"error": str(e)}

    # stdout is what the no-agent cron delivers to Slack — human-readable, and
    # EMPTY on a no-op tick so the cron stays silent (no JSON spam). Full detail
    # still goes to stderr via the per-project logger.info above.
    msg = _human_summary(summaries, dry_run=dry_run)
    if msg:
        print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

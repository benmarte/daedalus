"""core.dispatch.delivery — hermes-send wrappers and notification delivery helpers.

Low-fan-in helpers for constructing and dispatching notifications:
  - prompt-body builders (security escalation commands, completion comments)
  - ``hermes send`` subprocess wrapper and bool shim
  - PR / card inspection utilities (task summaries, doc comments)
  - Project-summary event-type classifier

Functions that create closures over ``_hermes_send`` (``_mirror_issue_threads``,
``_notify_project_summary``) STAY in ``scripts/daedalus_dispatch.py`` so
monkeypatching ``disp._hermes_send`` continues to work in existing tests.

Moved from scripts/daedalus_dispatch.py (issue #1153 PR 1/4).
The dispatcher re-exports every symbol so the public surface is unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from core import kanban
from core import notify_templates
from core.util import extract_issue_number
from core.util import extract_pr_number_from_summary

logger = logging.getLogger("daedalus.dispatch")

# Fenced code block matcher (mirrors ``core.iterate.outcomes._FENCED_RE``): the
# full ``` ```json … ``` ``` block is group(0); its interior is group(1). Used by
# ``_hide_outcome_blocks`` to HTML-comment-wrap ``daedalus_outcome`` records when
# mirroring completion summaries into human-facing comments (#1417).
_OUTCOME_FENCED_RE: re.Pattern[str] = re.compile(
    r"```(?:json)?\s*(.*?)\s*```",
    re.DOTALL,
)

# ── Prompt-body helpers ───────────────────────────────────────────────────────


def _build_security_notify_cmds(
    repo: str, n: int, title: str, targets: List[str]
) -> str:
    """Build the ``hermes send`` escalation commands block for a role body.

    Mirrors the inline block shared by ``_task_body`` and ``_validator_body``:
    one ``hermes send`` line per target, or a placeholder when none configured.
    """
    if not targets:
        return "       (no notification targets configured for this project)"
    # The whole message is one shell argument the agent runs verbatim; the
    # untrusted title must not be able to escape it (quote/backtick/$()/;/newline
    # → command injection, issue #1131). shlex.quote guarantees a single safe
    # token. {t} is a config-controlled target, not attacker input.
    return "\n".join(
        "       hermes send -t {t} -q --body {msg}".format(
            t=t,
            msg=shlex.quote(
                f"SECURITY ESCALATION: {repo}#{n} ({title}) blocked for human review."
            ),
        )
        for t in targets
    )


def _validator_summary_burns_cap(summary: str) -> bool:
    """Return True if a done validator summary counts toward the retry cap.

    A validator run only burns a retry when it actually completed and produced a
    real, non-CONFIRMED verdict (STOP/BLOCKED/ESCALATE or any other non-empty
    output). An empty/None summary means the delegated Claude Code agent died or
    timed out before writing a verdict — a *failed delegation*, not a decision —
    which must be retried without counting against the cap (#916). A CONFIRMED
    run is a success and likewise never burns the cap.
    """
    s = (summary or "").strip().lower()
    if not s:
        return False
    return not s.startswith("confirmed")


def _hide_outcome_blocks(summary: str) -> str:
    """Wrap fenced ``daedalus_outcome`` JSON blocks in HTML comments (#1417).

    Agents append a fenced ``daedalus_outcome`` OutcomeRecord to their kanban
    summaries (#1170) so ``core.iterate.outcomes.parse`` and ``classify_blocked``
    can route by ``(role, verdict)``. When those summaries are mirrored into
    human-facing issue/PR comments (#894) the raw fenced block leaks into the
    rendered thread as meaningless JSON.

    Wrap each fenced block that contains ``"daedalus_outcome"`` in an HTML comment
    (``<!-- daedalus:outcome ... -->``). HTML comments are invisible in rendered
    GitHub/GitLab/Azure DevOps markdown, yet the raw comment body still contains
    the fenced block verbatim — so ``outcomes.parse`` and the comment read-back
    paths (``_validator_github_comment_outcome`` in ``checks.py``, spec adoption
    in ``validator_comment.py``) keep working unchanged (they operate on raw
    bodies, not rendered HTML). Blocks without ``"daedalus_outcome"`` (e.g. code
    snippets in prose) are left untouched.
    """

    def _wrap(m: "re.Match[str]") -> str:
        block = m.group(0)
        if '"daedalus_outcome"' not in m.group(1):
            return block
        return f"<!-- daedalus:outcome\n{block}\n-->"

    return _OUTCOME_FENCED_RE.sub(_wrap, summary)


def _format_completion_comment(role: str, title: str, summary: str) -> str:
    """Render a role's kanban completion summary as a GitHub issue comment body.

    Used by ``_post_completion_comments`` (#894). Leads with ``**Agent: <role>**``
    so the issue thread mirrors the prior agent-posted convention. Any fenced
    ``daedalus_outcome`` block in the summary is hidden inside an HTML comment so
    the human thread stays clean while the machine read-back paths still see the
    raw JSON (#1417).
    """
    summary = _hide_outcome_blocks((summary or "").strip())
    lines = [f"**Agent: {role}**", ""]
    if title:
        lines.append(f"**Task:** {title}")
        lines.append("")
    lines.append(summary or "_Completed — no summary was recorded on the kanban card._")
    return "\n".join(lines)


# ── Summary notification helpers ──────────────────────────────────────────────


def _human_summary(
    summaries: Dict[str, Dict[str, Any]],
    dry_run: bool = False,
    provider_map: Optional[Dict[str, Any]] = None,
) -> str:
    """Rich markdown dispatch notification — or '' when nothing happened.

    The --no-agent cron delivers stdout verbatim; empty stdout is SILENT so a
    no-op tick produces no message (no spam). Passes ``provider_map`` through to
    ``notify_templates`` so issue/PR references become hyperlinks where possible.
    """
    return notify_templates.render_all_summaries(
        summaries, provider_map, dry_run=dry_run
    )


def _summary_events(summary: Dict[str, Any]) -> Set[str]:
    """Event types a tick summary triggers (for notifications[] filtering)."""
    events: Set[str] = {"dispatch-summary"}
    if summary.get("error"):
        events.add("pipeline-failure")
    if summary.get("advance_prs") or summary.get("reconciled"):
        events.add("pr-ready")
    if summary.get("blocked"):
        events.add("security-escalation")
    return events


# Sentinel "issue number" under which a project's tick-summary thread is anchored
# in dispatch_state. Real issues start at 1, so 0 never collides — this lets the
# per-project summary reuse the same dedup + threading machinery as per-issue
# comment mirrors (issue #137).
_PROJECT_SUMMARY_ANCHOR = 0


# ── hermes send subprocess wrapper ───────────────────────────────────────────


def _hermes_send(
    notify_target: str,
    report_body: str,
    *,
    thread_id: Optional[str] = None,
    broadcast: Optional[bool] = None,
) -> tuple[bool, Optional[str]]:
    """Send ``report_body`` via ``hermes send`` from the dispatcher's root context.

    Runs ``hermes send -t <target> --file <tmpfile> --json`` (list-args, no
    shell) and parses the JSON result. When *thread_id* is given the message is
    posted as a thread reply (target becomes ``<target>:<thread_id>``).

    When *broadcast* is True and *thread_id* is set, also post the message as a
    root message to the channel feed (Slack reply_broadcast behavior). This is
    handled by making a second call to _hermes_send without thread_id.

    Returns ``(ok, anchor)`` where *anchor* is the posted message's thread anchor
    (Slack ``thread_ts`` / Discord ``message_id``) reported by the platform
    adapter — used to anchor subsequent replies. Failures are logged gracefully
    and return ``(False, None)``.
    """
    import tempfile

    if not notify_target or not report_body.strip():
        return (False, None)

    target = f"{notify_target}:{thread_id}" if thread_id else notify_target
    tmp = None
    broadcast_tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(report_body)
            tmp = tf.name
        r = subprocess.run(
            ["hermes", "send", "-t", target, "--file", tmp, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        # Broadcast: post as root message to channel feed when broadcast=True
        # and this is a thread reply. This makes the reply visible in the
        # channel even if users don't expand the thread (Slack reply_broadcast
        # equivalent).
        if broadcast and thread_id:
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".md", delete=False, encoding="utf-8"
                ) as bf:
                    bf.write(report_body)
                    broadcast_tmp = bf.name
                # Post to channel root (no thread_id)
                channel_target = notify_target
                subprocess.run(
                    [
                        "hermes",
                        "send",
                        "-t",
                        channel_target,
                        "--file",
                        broadcast_tmp,
                        "--json",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except Exception as exc:
                # Broadcast failure is non-fatal
                logger.warning("dispatch: broadcast to channel feed failed: %s", exc)
            finally:
                if broadcast_tmp:
                    try:
                        os.unlink(broadcast_tmp)
                    except OSError:
                        pass

        if r.returncode != 0:
            logger.warning(
                "dispatch: hermes send to %s failed (rc=%s): %s",
                target,
                r.returncode,
                (r.stderr or "").strip(),
            )
            return (False, None)
        anchor: Optional[str] = None
        try:
            payload = json.loads(r.stdout or "{}")
        except (json.JSONDecodeError, ValueError):
            payload = {}
        if isinstance(payload, dict):
            if payload.get("error"):
                logger.warning(
                    "dispatch: hermes send to %s errored: %s",
                    target,
                    payload.get("error"),
                )
                return (False, None)
            raw = payload.get("message_id") or payload.get("ts")
            if raw is not None:
                anchor = str(raw)
        logger.info("dispatch: delivered to %s", target)
        return (True, anchor)
    except Exception as e:
        logger.warning("dispatch: hermes send to %s raised: %s", target, e)
        return (False, None)
    finally:
        if tmp:
            try:
                Path(tmp).unlink()
            except OSError:
                pass


def _send_via_hermes(notify_target: str, report_body: str) -> bool:
    """Backward-compatible bool wrapper around :func:`_hermes_send` (no threading)."""
    ok, _ = _hermes_send(notify_target, report_body)
    return ok


# ── Card inspection utilities ─────────────────────────────────────────────────


def _parse_pr_from_card(card: dict) -> Optional[int]:
    """Extract a PR number from a card's body + latest summary."""
    body = (card.get("body") or "").strip()
    summary = (card.get("latest_summary") or "").strip()
    text = f"{body}\n{summary}"
    return extract_pr_number_from_summary(text)


def _resolve_pr_from_parents(slug: str, provider: Any, card: dict) -> Optional[int]:
    """Walk parent cards to find an issue number, then resolve to a PR."""
    parents = card.get("parents") or []
    for pid in parents:
        parent = kanban.show_card(slug, pid)
        if not parent:
            continue
        # Try to find an issue number in the parent's title
        issue_num = extract_issue_number(parent.get("title") or "")
        if issue_num is not None:
            pr = provider.pr_number_for_issue(issue_num)
            if pr:
                return pr
    return None


def _find_doc_comment(provider: Any, pr_number: int) -> str:
    """Return the body of the first ``**Agent: documentation**`` PR comment, or ''."""
    for c in provider.list_pr_comments(pr_number):
        body = c.body or ""
        if "**Agent: documentation**" in body:
            return body
    return ""

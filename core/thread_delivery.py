"""Per-issue platform thread mirroring for Daedalus (issue #121).

Daedalus agents post their progress, specs, reviews and decisions as GitHub
issue/PR comments.  This module mirrors that conversation into one thread per
configured notification target (Slack, Discord, …) so the whole exchange is
visible without leaving the messaging platform.

Design:

  * **Platform-agnostic** — threading is expressed purely as an opaque *anchor*
    string (Slack ``thread_ts``, Discord ``message_id``, …). The ``send``
    callable injected by the caller is responsible for the platform mechanics
    (``hermes send -t platform:chat_id:thread_id``). This module never imports
    ``subprocess`` / ``hermes``, so it is fully unit-testable with a fake send.

  * **Anchor lifecycle** — the first event for a target posts a *root* message
    (no thread id) and stores the returned anchor. Subsequent events post as
    *replies* (thread id = anchor). If a reply fails — e.g. the anchor message
    was deleted — we fall back to a fresh root and update the stored anchor.

  * **Duplicate suppression** — every event carries a stable ``event_key``;
    once mirrored to a target it is recorded in dispatch state and never resent,
    so consecutive cron ticks don't repost the same comment.

The ``send`` contract::

    send(target: str, body: str, thread_id: Optional[str]) -> Tuple[bool, Optional[str]]

returns ``(ok, anchor)`` — ``anchor`` is the posted message's thread anchor
(only needed for root posts; ignored for replies).
"""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from core import dispatch_state
from core.providers.base import DELIVERY_MARKER

# Sentinel every agent comment carries (see notify_templates.render_agent_header).
AGENT_MARKER = "**Agent:"

# Bookkeeping comments the dispatcher posts for its own idempotency — never
# mirror these into the human-facing thread.
_SKIP_SUBSTRINGS = (
    DELIVERY_MARKER,                       # <!-- daedalus:slack-delivered -->
    "<!-- daedalus:escalation-notified",
    "<!-- daedalus:follow-up-extracted",
)

SendFn = Callable[[str, str, Optional[str]], Tuple[bool, Optional[str]]]


def _is_agent_comment(body: str) -> bool:
    """True when *body* is a mirror-worthy agent comment (not bookkeeping)."""
    if not body or AGENT_MARKER not in body:
        return False
    if body.lstrip().startswith("<!--"):
        return False
    return not any(marker in body for marker in _SKIP_SUBSTRINGS)


def deliver_event(
    workdir: str,
    issue_number: int,
    target: str,
    body: str,
    event_key: str,
    *,
    send: SendFn,
    dry_run: bool = False,
) -> str:
    """Mirror one event (``body``) to *target*'s thread for *issue_number*.

    Returns ``"sent"``, ``"skipped"`` (empty body or already mirrored) or
    ``"failed"`` (delivery failed; left unmarked so a later tick retries).

    Anchor handling:
      * no stored anchor  → post a root, store the returned anchor;
      * stored anchor     → post a reply; on failure fall back to a new root and
                            update the anchor (covers a deleted thread parent).
    The ``event_key`` is recorded only after a successful send.
    """
    if not body or not body.strip():
        return "skipped"
    if dispatch_state.has_thread_event(workdir, issue_number, target, event_key):
        return "skipped"
    if dry_run:
        return "sent"

    anchor = dispatch_state.get_thread_anchor(workdir, issue_number, target)
    if anchor:
        ok, _ = send(target, body, anchor)
        if not ok:
            # Anchor may be stale/deleted — fall back to a fresh root thread.
            ok, new_anchor = send(target, body, None)
            if ok and new_anchor:
                dispatch_state.set_thread_anchor(workdir, issue_number, target, new_anchor)
    else:
        ok, new_anchor = send(target, body, None)
        if ok and new_anchor:
            dispatch_state.set_thread_anchor(workdir, issue_number, target, new_anchor)

    if ok:
        dispatch_state.mark_thread_event(workdir, issue_number, target, event_key)
        return "sent"
    return "failed"


def select_comments(provider, issue_number: int,
                    pr_number: Optional[int] = None) -> List[Tuple[str, str]]:
    """Return ``[(event_key, body)]`` for every agent comment worth mirroring.

    Scans the issue's comments and (when *pr_number* is set) the linked PR's
    comments, keeping only agent-authored comments and skipping the dispatcher's
    own bookkeeping markers. ``event_key`` embeds the comment's stable id so the
    same comment is mirrored at most once per target.
    """
    out: List[Tuple[str, str]] = []
    if provider is None:
        return out

    try:
        issue_comments = provider.get_issue_comments(issue_number) or []
    except Exception:
        issue_comments = []
    for c in issue_comments:
        body = (c.get("body") if isinstance(c, dict) else getattr(c, "body", "")) or ""
        cid = str((c.get("id") if isinstance(c, dict) else getattr(c, "id", "")) or "")
        if cid and _is_agent_comment(body):
            out.append((f"comment:issue:{cid}", body))

    if pr_number:
        try:
            pr_comments = provider.list_pr_comments(pr_number) or []
        except Exception:
            pr_comments = []
        for c in pr_comments:
            body = getattr(c, "body", "") or (c.get("body") if isinstance(c, dict) else "") or ""
            cid = str(getattr(c, "id", "") or (c.get("id") if isinstance(c, dict) else "") or "")
            if cid and _is_agent_comment(body):
                out.append((f"comment:pr:{cid}", body))

    return out

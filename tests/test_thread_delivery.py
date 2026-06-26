"""Unit tests for core.thread_delivery (issue #121).

Covers the platform-agnostic mirroring logic: root-then-reply anchoring,
cross-tick duplicate suppression, deleted-anchor fallback, and agent-comment
selection. A recording fake stands in for the injected ``send`` callable, so
no hermes/subprocess is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import check  # noqa: E402,F401

from core import dispatch_state, thread_delivery  # noqa: E402
from core.providers.base import Comment, DELIVERY_MARKER  # noqa: E402


class FakeSend:
    """Records (target, body, thread_id) calls; returns scripted (ok, anchor)."""

    def __init__(self, *, ok=True, anchor="ts-root", fail_replies=False):
        self.ok = ok
        self.anchor = anchor
        self.fail_replies = fail_replies
        self.calls = []

    def __call__(self, target, body, thread_id):
        self.calls.append((target, body, thread_id))
        if thread_id is not None:
            # A reply.
            if self.fail_replies:
                return (False, None)
            return (self.ok, None)
        # A root post.
        return (self.ok, self.anchor if self.ok else None)


# ── _is_agent_comment ─────────────────────────────────────────────────────────


def test_is_agent_comment_accepts_agent_marker():
    check("agent comment accepted", thread_delivery._is_agent_comment("**Agent: developer**\n\nDone."))


def test_is_agent_comment_rejects_plain():
    check("plain comment rejected", not thread_delivery._is_agent_comment("just a human note"))


def test_is_agent_comment_rejects_delivery_marker():
    body = f"{DELIVERY_MARKER}\n\nDelivered:\n\n**Agent: documentation**\nreport"
    check("delivery-sentinel comment skipped", not thread_delivery._is_agent_comment(body))


def test_is_agent_comment_rejects_html_marker_prefix():
    body = "<!-- daedalus:follow-up-extracted PR #1 issue #2 -->\n\n**Agent: daedalus**"
    check("html-marker comment skipped", not thread_delivery._is_agent_comment(body))


# ── deliver_event: root then reply ──────────────────────────────────────────────


def test_first_event_posts_root_and_stores_anchor(tmp_path):
    wd = str(tmp_path)
    send = FakeSend(anchor="ts-1")
    res = thread_delivery.deliver_event(wd, 1, "slack:C1", "hello", "root", send=send)
    check("returned sent", res == "sent")
    check("posted as root (no thread_id)", send.calls[0][2] is None)
    check("anchor stored", dispatch_state.get_thread_anchor(wd, 1, "slack:C1") == "ts-1")


def test_second_event_posts_reply_to_anchor(tmp_path):
    wd = str(tmp_path)
    send = FakeSend(anchor="ts-1")
    thread_delivery.deliver_event(wd, 1, "slack:C1", "root msg", "root", send=send)
    thread_delivery.deliver_event(wd, 1, "slack:C1", "a comment", "comment:issue:9", send=send)
    check("second call is a reply", send.calls[1][2] == "ts-1")


# ── deliver_event: duplicate suppression ────────────────────────────────────────


def test_duplicate_event_skipped(tmp_path):
    wd = str(tmp_path)
    send = FakeSend()
    thread_delivery.deliver_event(wd, 1, "slack:C1", "x", "root", send=send)
    res = thread_delivery.deliver_event(wd, 1, "slack:C1", "x", "root", send=send)
    check("duplicate returns skipped", res == "skipped")
    check("send not called twice", len(send.calls) == 1)


def test_dedup_is_per_target(tmp_path):
    wd = str(tmp_path)
    send = FakeSend()
    thread_delivery.deliver_event(wd, 1, "slack:C1", "x", "root", send=send)
    res = thread_delivery.deliver_event(wd, 1, "discord:#ops", "x", "root", send=send)
    check("other target still sends", res == "sent")


def test_empty_body_skipped(tmp_path):
    wd = str(tmp_path)
    send = FakeSend()
    res = thread_delivery.deliver_event(wd, 1, "slack:C1", "   ", "root", send=send)
    check("empty body skipped", res == "skipped" and not send.calls)


# ── deliver_event: failure handling ─────────────────────────────────────────────


def test_failed_send_not_marked(tmp_path):
    wd = str(tmp_path)
    send = FakeSend(ok=False)
    res = thread_delivery.deliver_event(wd, 1, "slack:C1", "x", "root", send=send)
    check("failed send reported", res == "failed")
    check("event not marked (retryable)", not dispatch_state.has_thread_event(wd, 1, "slack:C1", "root"))


def test_dry_run_does_not_send_or_mark(tmp_path):
    wd = str(tmp_path)
    send = FakeSend()
    res = thread_delivery.deliver_event(wd, 1, "slack:C1", "x", "root", send=send, dry_run=True)
    check("dry-run reports sent", res == "sent")
    check("dry-run did not call send", not send.calls)
    check("dry-run did not mark", not dispatch_state.has_thread_event(wd, 1, "slack:C1", "root"))


def test_deleted_anchor_falls_back_to_new_root(tmp_path):
    wd = str(tmp_path)
    # Seed a stale anchor; replies will fail (parent deleted), root posts succeed.
    dispatch_state.set_thread_anchor(wd, 1, "slack:C1", "stale-ts")
    send = FakeSend(anchor="ts-new", fail_replies=True)
    res = thread_delivery.deliver_event(wd, 1, "slack:C1", "comment", "comment:issue:1", send=send)
    check("event recovered via fallback", res == "sent")
    check("attempted reply first", send.calls[0][2] == "stale-ts")
    check("fell back to a root", send.calls[1][2] is None)
    check("anchor updated to new root", dispatch_state.get_thread_anchor(wd, 1, "slack:C1") == "ts-new")


# ── select_comments ─────────────────────────────────────────────────────────────


class FakeCommentProvider:
    def __init__(self, issue_comments, pr_comments=None):
        self._issue = issue_comments
        self._pr = pr_comments or []

    def get_issue_comments(self, n):
        return self._issue

    def list_pr_comments(self, pr):
        return self._pr


def test_select_comments_filters_to_agent_comments():
    prov = FakeCommentProvider(
        issue_comments=[
            {"id": 1, "body": "**Agent: validator**\nCONFIRMED"},
            {"id": 2, "body": "human chatter"},
        ],
        pr_comments=[Comment(id="99", body="**Agent: reviewer**\nLGTM")],
    )
    got = thread_delivery.select_comments(prov, 5, pr_number=42)
    keys = [k for k, _ in got]
    check("agent issue comment selected", "comment:issue:1" in keys)
    check("human comment excluded", "comment:issue:2" not in keys)
    check("agent PR comment selected", "comment:pr:99" in keys)


def test_select_comments_no_pr():
    prov = FakeCommentProvider(issue_comments=[{"id": 1, "body": "**Agent: pm**\nspec"}])
    got = thread_delivery.select_comments(prov, 5)
    check("only issue scanned when no PR", [k for k, _ in got] == ["comment:issue:1"])


def test_select_comments_handles_provider_errors():
    class Boom:
        def get_issue_comments(self, n):
            raise RuntimeError("api down")

        def list_pr_comments(self, pr):
            return []

    got = thread_delivery.select_comments(Boom(), 5, pr_number=1)
    check("provider error degrades to empty", got == [])


if __name__ == "__main__":
    import inspect
    import tempfile

    print("thread_delivery tests (issue #121)")
    print("-" * 60)
    for _name, _fn in sorted(
        (n, f) for n, f in globals().items()
        if n.startswith("test_") and callable(f)
    ):
        if "tmp_path" in inspect.signature(_fn).parameters:
            with tempfile.TemporaryDirectory() as d:
                _fn(Path(d))
        else:
            _fn()
    print("-" * 60)
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)

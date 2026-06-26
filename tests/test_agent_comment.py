"""Unit tests for scripts/agent_comment.py shared GitHub comment helper (#120)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_agent_comment():
    p = ROOT / "scripts" / "agent_comment.py"
    spec = importlib.util.spec_from_file_location("agent_comment", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ac = _load_agent_comment()


# ── build_comment_body: header enforcement ───────────────────────────────────


def test_body_always_starts_with_agent_header():
    body = ac.build_comment_body("developer", "Done", "details")
    assert body.startswith("**Agent: developer**")


def test_body_includes_heading_as_h2_and_sections():
    body = ac.build_comment_body("qa", "QA Summary", "### Result\npass")
    assert "## QA Summary" in body
    assert "### Result\npass" in body


def test_body_without_heading_still_has_header():
    body = ac.build_comment_body("reviewer", "", "just the body")
    assert body.startswith("**Agent: reviewer**")
    assert "## " not in body


def test_body_strips_leading_trailing_newlines_in_sections():
    body = ac.build_comment_body("docs", "H", "\n\nmiddle\n\n")
    assert "**Agent: docs**\n\n## H\n\nmiddle\n" == body


# ── post_comment / post_pr_comment: endpoint + enforced header ────────────────


class _Captured:
    def __init__(self):
        self.url = None
        self.payload = None
        self.headers = None


def _patch_urlopen(monkeypatch, captured):
    def fake_urlopen(req):
        captured.url = req.full_url
        captured.payload = json.loads(req.data.decode())
        captured.headers = req.headers

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"id": 1}'

        return _Resp()

    monkeypatch.setattr(ac.urllib.request, "urlopen", fake_urlopen)


def test_post_comment_hits_issue_endpoint_with_header(monkeypatch):
    cap = _Captured()
    _patch_urlopen(monkeypatch, cap)
    out = ac.post_comment("org/repo", 12, "developer", "Done", "body", token="tok")
    assert out == {"id": 1}
    assert cap.url == "https://api.github.com/repos/org/repo/issues/12/comments"
    assert cap.payload["body"].startswith("**Agent: developer**")
    # urllib title-cases header keys.
    assert cap.headers["Authorization"] == "Bearer tok"


def test_post_pr_comment_uses_same_issues_endpoint(monkeypatch):
    cap = _Captured()
    _patch_urlopen(monkeypatch, cap)
    ac.post_pr_comment("org/repo", 88, "reviewer", "Review", "verdict", token="tok")
    assert cap.url == "https://api.github.com/repos/org/repo/issues/88/comments"
    assert cap.payload["body"].startswith("**Agent: reviewer**")

"""Issue #1141 — webhook signature verification is wired into the ready path.

`verify_signature()` used to be dead code: `scripts/daedalus-ready.sh` read the
payload from stdin and dispatched with no signature check, so anyone who could
reach the webhook endpoint could trigger a full pipeline dispatch of agents with
repo write access. The fix extracts the dispatch logic into the importable
`core/webhook_dispatch.py`, reads the raw body ONCE, and verifies the HMAC
signature BEFORE `normalize()`.

Covers PM spec acceptance criteria 1-6:
  1. Bad sig + secret set   → never reaches normalize(), no dispatch.
  2. Missing sig + secret set → rejected (no normalize, no dispatch).
  3. Valid sig              → normalize() + dispatch proceed.
  4. No secret              → one-time warning (marker), dispatch proceeds.
  5. Raw bytes to verify    == exact stdin bytes (cat- byte-mangling regression).
  6. vcs.webhook_secret_env accepted by validation; absence valid; empty rejected.

Dual-mode: runs under pytest and as `python tests/test_issue_1141_webhook_signature.py`.
"""

from __future__ import annotations

import hashlib
import hmac
import sys
import unittest
from pathlib import Path
from unittest import mock

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config import validate_vcs  # noqa: E402
from core import webhook_dispatch as wd  # noqa: E402
from core.webhook_normalizer import ReadyEvent, verify_signature  # noqa: E402

SECRET = "top-secret"
# A github-shaped payload so infer_provider() returns "github". Trailing newline
# is deliberate: the old `payload=$(cat -)` shell path stripped it, mangling any
# HMAC computed over the raw body.
GH_BODY = b'{"projects_v2_item": {"node_id": "PVTI_x"}}\n'


def _gh_sig(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _fake_scope(_ev):
    return "ALL"


class TestSignatureGatesNormalize(unittest.TestCase):
    """AC 1-3: verification decides whether normalize()/dispatch happen."""

    def test_bad_signature_never_reaches_normalize(self):
        """AC1: bad sig + secret set → rejected, normalize() not called."""
        normalize_spy = mock.Mock(name="normalize")
        headers = {"X-Hub-Signature-256": "sha256=deadbeef"}
        decision = wd.handle_ready_event(
            GH_BODY,
            headers,
            secret=SECRET,
            normalize_fn=normalize_spy,
            verify_fn=verify_signature,
            resolve_scope_fn=_fake_scope,
        )
        self.assertEqual(decision["status"], "rejected")
        normalize_spy.assert_not_called()

    def test_missing_signature_header_rejected(self):
        """AC2: missing sig header + secret set → rejected, no normalize."""
        normalize_spy = mock.Mock(name="normalize")
        decision = wd.handle_ready_event(
            GH_BODY,
            {},  # no signature header forwarded
            secret=SECRET,
            normalize_fn=normalize_spy,
            verify_fn=verify_signature,
            resolve_scope_fn=_fake_scope,
        )
        self.assertEqual(decision["status"], "rejected")
        normalize_spy.assert_not_called()

    def test_valid_signature_proceeds_to_dispatch(self):
        """AC3: valid sig → normalize() called + dispatch decision."""
        ev = ReadyEvent(provider="github", repo="o/r", issue_number=1, board_slug="b")
        normalize_spy = mock.Mock(name="normalize", return_value=ev)
        headers = {"X-Hub-Signature-256": _gh_sig(GH_BODY)}
        decision = wd.handle_ready_event(
            GH_BODY,
            headers,
            secret=SECRET,
            normalize_fn=normalize_spy,
            verify_fn=verify_signature,
            resolve_scope_fn=_fake_scope,
        )
        self.assertEqual(decision["status"], "dispatched")
        self.assertEqual(decision["scope"], "ALL")
        normalize_spy.assert_called_once()


class TestNoSecretBackwardCompatible(unittest.TestCase):
    """AC4: no secret → one-time warning, dispatch still proceeds."""

    def test_no_secret_warns_once_and_dispatches(self):
        ev = ReadyEvent(provider="github", repo="o/r", issue_number=1, board_slug="b")
        normalize_spy = mock.Mock(name="normalize", return_value=ev)
        warn_spy = mock.Mock(name="on_missing_secret")
        for _ in range(2):
            decision = wd.handle_ready_event(
                GH_BODY,
                {},
                secret=None,
                normalize_fn=normalize_spy,
                verify_fn=verify_signature,
                resolve_scope_fn=_fake_scope,
                on_missing_secret=warn_spy,
            )
            self.assertEqual(decision["status"], "dispatched")
        # handle_ready_event invokes the hook each time; the marker file dedups.
        self.assertEqual(warn_spy.call_count, 2)
        self.assertEqual(normalize_spy.call_count, 2)

    def test_warn_no_secret_once_uses_marker(self):
        """The marker file makes warn_no_secret_once() fire exactly once."""
        marker = Path(_project_root) / ".tmp_1141_marker"
        if marker.exists():
            marker.unlink()
        try:
            with mock.patch.object(wd.logger, "warning") as warn:
                wd.warn_no_secret_once(marker=marker)
                wd.warn_no_secret_once(marker=marker)
        finally:
            if marker.exists():
                marker.unlink()
        self.assertEqual(warn.call_count, 1)


class TestRawBytesRegression(unittest.TestCase):
    """AC5: verify_signature receives the EXACT stdin bytes (cat- bug)."""

    def test_verify_receives_exact_raw_bytes(self):
        captured = {}

        def capturing_verify(provider, payload_bytes, headers, secret):
            captured["bytes"] = payload_bytes
            return verify_signature(provider, payload_bytes, headers, secret)

        ev = ReadyEvent(provider="github", repo="o/r", issue_number=1, board_slug="b")
        headers = {"X-Hub-Signature-256": _gh_sig(GH_BODY)}
        decision = wd.handle_ready_event(
            GH_BODY,
            headers,
            secret=SECRET,
            normalize_fn=mock.Mock(return_value=ev),
            verify_fn=capturing_verify,
            resolve_scope_fn=_fake_scope,
        )
        # Exact bytes, including the trailing newline the old shell path stripped.
        self.assertEqual(captured["bytes"], GH_BODY)
        # And because the bytes were intact, the real HMAC verified → dispatch.
        self.assertEqual(decision["status"], "dispatched")

    def test_trailing_newline_strip_would_fail_verification(self):
        """Sanity: a signature computed over the stripped body must NOT verify
        against the raw body — proving the regression test has teeth."""
        stripped = GH_BODY.rstrip(b"\n")
        headers = {"X-Hub-Signature-256": _gh_sig(stripped)}  # sig over WRONG bytes
        decision = wd.handle_ready_event(
            GH_BODY,
            headers,
            secret=SECRET,
            normalize_fn=mock.Mock(),
            verify_fn=verify_signature,
            resolve_scope_fn=_fake_scope,
        )
        self.assertEqual(decision["status"], "rejected")


class TestProviderInferenceAndHeaders(unittest.TestCase):
    """Supporting helpers: provider inference and CGI header reconstruction."""

    def test_infer_provider(self):
        self.assertEqual(wd.infer_provider({"projects_v2_item": {}}), "github")
        self.assertEqual(
            wd.infer_provider({"object_kind": "issue", "object_attributes": {}}),
            "gitlab",
        )
        self.assertEqual(wd.infer_provider({"resource": {"workItemId": 1}}), "azure")
        self.assertEqual(wd.infer_provider({"new_status": "Ready"}), "hermes")
        self.assertEqual(wd.infer_provider({"foo": "bar"}), "unknown")

    def test_headers_from_env_reconstructs_signature_header(self):
        environ = {
            "HTTP_X_HUB_SIGNATURE_256": "sha256=abc",
            "HTTP_X_GITLAB_TOKEN": "tok",
            "PATH": "/usr/bin",  # non-HTTP_ var ignored
        }
        headers = wd.headers_from_env(environ)
        self.assertEqual(headers.get("X-Hub-Signature-256"), "sha256=abc")
        self.assertEqual(headers.get("X-Gitlab-Token"), "tok")
        self.assertNotIn("Path", headers)

    def test_unknown_provider_ignored_before_verify(self):
        verify_spy = mock.Mock(name="verify")
        decision = wd.handle_ready_event(
            b'{"foo": "bar"}',
            {},
            secret=SECRET,
            verify_fn=verify_spy,
            normalize_fn=mock.Mock(),
        )
        self.assertEqual(decision["status"], "ignored")
        verify_spy.assert_not_called()

    def test_invalid_json_ignored(self):
        decision = wd.handle_ready_event(
            b"not json", {}, secret=SECRET, normalize_fn=mock.Mock()
        )
        self.assertEqual(decision["status"], "ignored")


class TestConfigValidation(unittest.TestCase):
    """AC6: vcs.webhook_secret_env validation."""

    def test_absent_is_valid(self):
        cfg = {"repo": "o/r", "vcs": {"provider": "github"}}
        self.assertEqual(validate_vcs(cfg), [])

    def test_non_empty_string_is_valid(self):
        cfg = {
            "repo": "o/r",
            "vcs": {
                "provider": "github",
                "webhook_secret_env": "DAEDALUS_WEBHOOK_SECRET",
            },
        }
        self.assertEqual(validate_vcs(cfg), [])

    def test_empty_string_is_rejected(self):
        cfg = {"repo": "o/r", "vcs": {"provider": "github", "webhook_secret_env": "  "}}
        errors = validate_vcs(cfg)
        self.assertTrue(any("webhook_secret_env" in e for e in errors))

    def test_non_string_is_rejected(self):
        cfg = {"repo": "o/r", "vcs": {"provider": "github", "webhook_secret_env": 123}}
        errors = validate_vcs(cfg)
        self.assertTrue(any("webhook_secret_env" in e for e in errors))


if __name__ == "__main__":
    unittest.main()

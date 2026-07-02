"""Webhook ready-event dispatch entry point with HMAC signature verification.

This module is the importable, testable replacement for the inline python that
used to live in ``scripts/daedalus-ready.sh``. It reads the raw webhook body
from stdin exactly ONCE (fixing the ``payload=$(cat -)`` byte-mangling that
would break any HMAC computed over a re-``echo``ed body), verifies the payload
signature BEFORE parsing/normalizing it, and only then resolves the dispatch
scope and fires ``daedalus-cron.sh``.

Decision rule (fail-safe):

  * secret configured + signature invalid/missing  → warn, DO NOT dispatch (exit 0)
  * secret configured + signature valid            → dispatch
  * no secret configured                           → one-time warning, dispatch
    (backward-compatible: existing deployments keep working, operators are told
    once that verification is off)

Verification only fires for receivers that forward CGI-style ``HTTP_*`` headers
(daedalus-owned / reverse-proxied endpoints). The native hermes gateway already
HMAC-verifies fail-closed before it ever reaches this hook and forwards no
headers, so a missing-header event with no configured secret still dispatches.

Secret source: ``vcs.webhook_secret_env`` names an environment variable (same
convention as ``vcs.token_env``); the value is read from ``os.environ``. No raw
secret ever lives in YAML.

Usage (from the thin shell wrapper):
    cd ~/.hermes/plugins/daedalus && python3 -m core.webhook_dispatch
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, Optional

from core.webhook_normalizer import normalize, verify_signature

logger = logging.getLogger("daedalus.webhook_dispatch")

# One-time no-secret warning marker so operators aren't spammed on every event.
_NO_SECRET_MARKER = Path.home() / ".hermes" / "daedalus" / ".webhook_secret_warned"


def infer_provider(payload: dict) -> str:
    """Infer the webhook provider from the parsed payload's structure.

    Mirrors the inference the old inline script did. Returns one of
    'github' | 'gitlab' | 'azure' | 'hermes' | 'unknown'.
    """
    if not isinstance(payload, dict):
        return "unknown"
    if "projects_v2_item" in payload:
        return "github"
    if "object_attributes" in payload and payload.get("object_kind") == "issue":
        return "gitlab"
    if "resource" in payload and "workItemId" in (payload.get("resource") or {}):
        return "azure"
    if "new_status" in payload:
        return "hermes"
    return "unknown"


def headers_from_env(environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Reconstruct HTTP headers from CGI-style ``HTTP_*`` environment variables.

    A reverse proxy / CGI receiver exposes ``X-Hub-Signature-256`` as
    ``HTTP_X_HUB_SIGNATURE_256``. Convert back to the canonical hyphenated,
    title-cased header name that ``verify_signature`` looks up.
    """
    environ = os.environ if environ is None else environ
    headers: Dict[str, str] = {}
    for key, val in environ.items():
        if key.startswith("HTTP_") and len(key) > 5:
            name = "-".join(part.capitalize() for part in key[5:].split("_"))
            headers[name] = val
    return headers


def resolve_webhook_secret(
    config_loader=None,
    list_projects: Optional[Callable[[], list]] = None,
) -> Optional[str]:
    """Resolve the HMAC secret from ``vcs.webhook_secret_env`` across projects.

    The ready hook is a single global subscription, so there is no repo context
    before ``normalize()``. We scan every registered project's resolved config
    for a ``vcs.webhook_secret_env`` env-var name and return the first non-empty
    value found in ``os.environ``. Returns ``None`` when no project configures a
    secret (verification disabled). Never raises.
    """
    if config_loader is None:
        from config import ConfigLoader

        config_loader = ConfigLoader()
    if list_projects is None:
        from core import registry

        list_projects = registry.list_projects

    try:
        projects = list_projects() or []
    except Exception:
        logger.exception("webhook: failed to list projects for secret resolution")
        return None

    for rp in projects:
        try:
            resolved = config_loader.resolve_repo_config(rp)
        except Exception:
            continue
        env_name = ((resolved.get("vcs") or {}).get("webhook_secret_env") or "").strip()
        if env_name:
            val = (os.environ.get(env_name) or "").strip()
            if val:
                return val
    return None


def resolve_scope(
    ev,
    config_loader=None,
    list_projects: Optional[Callable[[], list]] = None,
) -> str:
    """Resolve the local repo path a Ready event scopes to (issue #137).

    Returns the matched project's ``workdir`` (or its registry path), or
    ``"ALL"`` when no registered project matches — the legacy global sweep.
    """
    if config_loader is None:
        from config import ConfigLoader

        config_loader = ConfigLoader()
    if list_projects is None:
        from core import registry

        list_projects = registry.list_projects

    ident = (getattr(ev, "repo", "") or "").strip()
    try:
        projects = list_projects() or []
    except Exception:
        return "ALL"
    for rp in projects:
        try:
            resolved = config_loader.resolve_repo_config(rp)
        except Exception:
            continue
        if ident and (resolved.get("repo") or "").strip() == ident:
            return (resolved.get("workdir") or rp).strip()
    return "ALL"


def warn_no_secret_once(marker: Optional[Path] = None) -> None:
    """Emit a single warning that signature verification is disabled.

    Uses a filesystem marker so the warning fires once per deployment rather
    than on every inbound event. Failures to write the marker are non-fatal.
    """
    marker = _NO_SECRET_MARKER if marker is None else marker
    try:
        if marker.exists():
            return
    except Exception:
        pass
    logger.warning(
        "webhook: no vcs.webhook_secret_env configured — HMAC signature "
        "verification is DISABLED. Any actor who can reach the webhook endpoint "
        "can trigger a pipeline dispatch of agents with repo write access. Set "
        "vcs.webhook_secret_env to the name of an env var holding the shared "
        "secret to enable verification."
    )
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1")
    except Exception:
        pass


def handle_ready_event(
    raw_body: bytes,
    headers: Optional[Dict[str, str]],
    *,
    secret: Optional[str],
    normalize_fn: Callable = normalize,
    verify_fn: Callable = verify_signature,
    resolve_scope_fn: Optional[Callable] = None,
    on_missing_secret: Optional[Callable[[], None]] = None,
) -> dict:
    """Decide what to do with one inbound webhook body. Pure — never dispatches.

    Order of operations (security-critical): infer provider from the parsed
    body, then verify the signature over the RAW bytes BEFORE calling
    ``normalize_fn``. A payload that fails verification never reaches
    ``normalize_fn`` and produces no dispatch.

    Returns a decision dict with a ``status`` of:
      * ``"ignored"``   — unknown provider, unparseable body, or not a Ready event
      * ``"rejected"``  — secret configured but signature invalid/missing
      * ``"dispatched"``— proceed; carries ``scope`` ("ALL" or a repo path)
    """
    try:
        payload = json.loads(raw_body)
    except Exception:
        return {"status": "ignored", "reason": "invalid JSON"}

    provider = infer_provider(payload)
    if provider == "unknown":
        return {"status": "ignored", "reason": "unknown provider"}

    if secret:
        if not verify_fn(provider, raw_body, headers, secret):
            logger.warning(
                "webhook: signature verification failed for %s event — not dispatching",
                provider,
            )
            return {"status": "rejected", "reason": "signature verification failed"}
    else:
        if on_missing_secret is not None:
            on_missing_secret()

    ev = normalize_fn(provider, payload)
    if not ev:
        return {"status": "ignored", "reason": "not a Ready event"}

    scope = resolve_scope_fn(ev) if resolve_scope_fn is not None else resolve_scope(ev)
    return {"status": "dispatched", "scope": scope}


def _run_dispatch(scope: Optional[str]) -> None:
    """Fire daedalus-cron.sh in the background, optionally scoped to a repo."""
    cron = str(Path.home() / ".hermes" / "scripts" / "daedalus-cron.sh")
    cmd = ["bash", cron]
    if scope:
        cmd += ["--repo", scope]
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main(argv=None) -> int:
    """Read the raw webhook body from stdin ONCE, verify, and dispatch."""
    raw_body = sys.stdin.buffer.read()
    headers = headers_from_env()
    secret = resolve_webhook_secret()

    decision = handle_ready_event(
        raw_body,
        headers,
        secret=secret,
        resolve_scope_fn=resolve_scope,
        on_missing_secret=warn_no_secret_once,
    )

    status = decision.get("status")
    if status == "dispatched":
        scope = decision.get("scope") or "ALL"
        if scope == "ALL":
            _run_dispatch(None)
            print(json.dumps({"status": "dispatched", "scope": "all"}))
        else:
            _run_dispatch(scope)
            print(json.dumps({"status": "dispatched", "repo": scope}))
    else:
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

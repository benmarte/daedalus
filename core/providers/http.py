"""Shared HTTP layer for VCS providers.

Thin synchronous wrapper over httpx (a core Hermes dependency) with:
retry/backoff on 429/5xx honouring Retry-After, per-style pagination
(GitHub Link header, GitLab X-Next-Page, Azure continuation token),
HTTPS-only enforcement, optional SSL certificate verification bypass,
and token redaction in every error message.

Provider methods catch :class:`ProviderError` and degrade gracefully —
nothing in this module ever logs or embeds a credential.
"""
from __future__ import annotations

import logging
import os
import time
import warnings
from typing import Any

import httpx

from .base import ProviderConfigError

logger = logging.getLogger("daedalus.providers.http")

DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_BACKOFF = [1.0, 2.0, 4.0]  # seconds; Retry-After header wins when present


class ProviderError(Exception):
    """Any provider HTTP failure. ``status_code`` is None for transport errors."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class HTTPClient:
    """HTTPS-only JSON client. All errors are raised as ProviderError with the
    token redacted; callers (provider methods) catch and return safe defaults."""

    def __init__(self, base_url: str, headers: dict[str, str],
                 token: str = "", timeout: float = DEFAULT_TIMEOUT,
                 verify_ssl: bool = True):
        if not base_url.startswith("https://"):
            raise ProviderError(f"refusing non-HTTPS base URL: {base_url}")
        self._base = base_url.rstrip("/")
        self._headers = dict(headers)
        self._token = token or ""
        self._timeout = timeout
        self._verify_ssl = verify_ssl
        if not verify_ssl:
            if not os.environ.get("DAEDALUS_DEV_MODE"):
                raise ProviderConfigError(
                    "verify_ssl=false is not permitted outside dev mode "
                    "(set DAEDALUS_DEV_MODE env var to override)"
                )
            warnings.filterwarnings("ignore", message="Unverified HTTPS request")
            logger.error(
                "SSL certificate verification disabled (base_url=%s) — DAEDALUS_DEV_MODE active",
                base_url,
            )

    # ── core ─────────────────────────────────────────────────────────────────
    def _redact(self, text: str) -> str:
        if not self._token or not text:
            return text or ""
        from urllib.parse import quote as _quote
        for variant in (self._token, _quote(self._token, safe="")):
            text = text.replace(variant, "<REDACTED>")
        return text

    def request(self, method: str, path: str, *,
                params: dict[str, Any] | None = None,
                json_body: Any = None,
                content_type: str | None = None,
                headers: dict[str, str] | None = None) -> httpx.Response:
        url = path if path.startswith("https://") else f"{self._base}{path}"
        hdrs = dict(self._headers)
        if headers:
            hdrs.update(headers)
        if content_type:
            hdrs["Content-Type"] = content_type
        last_exc: ProviderError | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = httpx.request(method, url, params=params, json=json_body,
                                     headers=hdrs, timeout=self._timeout,
                                     verify=self._verify_ssl)
            except httpx.HTTPError as e:
                last_exc = ProviderError(f"{method} {path}: {self._redact(str(e))}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
                    continue
                raise last_exc
            # GitHub secondary rate-limit returns 403 with a Retry-After header
            # or a body containing "rate limit". Treat these as retryable.
            _is_ratelimit_403 = (
                resp.status_code == 403
                and ("rate limit" in resp.text.lower()
                     or "Retry-After" in resp.headers)
            )
            if (resp.status_code in RETRY_STATUSES or _is_ratelimit_403) and attempt < MAX_RETRIES:
                time.sleep(self._retry_delay(resp, attempt))
                continue
            if resp.status_code >= 400:
                raise ProviderError(
                    f"{method} {path} -> {resp.status_code}: {self._redact(resp.text[:300])}",
                    status_code=resp.status_code,
                )
            return resp
        raise last_exc or ProviderError(f"{method} {path}: retries exhausted")

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After", "")
        try:
            return min(float(retry_after), 30.0)
        except (TypeError, ValueError):
            return RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]

    # ── JSON helpers ─────────────────────────────────────────────────────────
    def get_json(self, path: str, *, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> Any:
        resp = self.request("GET", path, params=params, headers=headers)
        try:
            return resp.json()
        except Exception:
            return None

    def post_json(self, path: str, json_body: Any, *,
                  content_type: str | None = None) -> Any:
        resp = self.request("POST", path, json_body=json_body, content_type=content_type)
        try:
            return resp.json()
        except Exception:
            return None

    def patch_json(self, path: str, json_body: Any, *,
                   content_type: str | None = None) -> Any:
        resp = self.request("PATCH", path, json_body=json_body, content_type=content_type)
        try:
            return resp.json()
        except Exception:
            return None

    def put_json(self, path: str, json_body: Any) -> Any:
        resp = self.request("PUT", path, json_body=json_body)
        try:
            return resp.json()
        except Exception:
            return None

    # ── pagination ───────────────────────────────────────────────────────────
    def get_paginated(self, path: str, *, params: dict[str, Any] | None = None,
                      style: str = "link_header", per_page: int = 100,
                      max_pages: int = 10) -> list[Any]:
        """Collect list results across pages.

        style: ``link_header`` (GitHub), ``x_next_page`` (GitLab),
        ``continuation`` (Azure DevOps — items under ``value``).
        """
        out: list[Any] = []
        params = dict(params or {})
        if style in ("link_header", "x_next_page"):
            params.setdefault("per_page", per_page)
        url = path
        for _ in range(max_pages):
            resp = self.request("GET", url, params=params)
            try:
                data = resp.json()
            except Exception:
                break
            if style == "continuation":
                out.extend((data or {}).get("value") or [])
                token = resp.headers.get("x-ms-continuationtoken")
                if not token:
                    break
                params["continuationToken"] = token
            elif style == "x_next_page":
                if not isinstance(data, list):
                    break
                out.extend(data)
                next_page = resp.headers.get("X-Next-Page", "").strip()
                if not next_page:
                    break
                params["page"] = next_page
            else:  # link_header
                if not isinstance(data, list):
                    break
                out.extend(data)
                next_url = _parse_link_next(resp.headers.get("Link", ""))
                if not next_url:
                    break
                url, params = next_url, {}
        return out


def _parse_link_next(link_header: str) -> str | None:
    """Extract the rel="next" URL from an RFC 5988 Link header, or None."""
    for part in (link_header or "").split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        if 'rel="next"' in section[1]:
            return section[0].strip().strip("<>")
    return None

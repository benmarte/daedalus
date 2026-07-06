"""Tests for core/providers/http.py — retry, pagination, redaction, TLS-only."""
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.providers.base import ProviderConfigError  # noqa: E402
from core.providers.http import (HTTPClient, ProviderError, _parse_link_next)  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self.text = text or ""

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


@pytest.fixture
def no_sleep():
    with mock.patch("core.providers.http.time.sleep") as m:
        yield m


def _client(token="sekret-token-123"):
    return HTTPClient("https://api.example.com", {"Authorization": f"Bearer {token}"},
                      token=token)


def test_https_only():
    with pytest.raises(ProviderError):
        HTTPClient("http://api.example.com", {})


def test_verify_ssl_default_true():
    c = HTTPClient("https://api.example.com", {})
    assert c._verify_ssl is True


def test_verify_ssl_false_sets_verify_false(no_sleep, monkeypatch):
    monkeypatch.setenv("DAEDALUS_DEV_MODE", "1")
    resp = FakeResponse(200, json_data={"ok": True})
    with mock.patch("core.providers.http.httpx.request", return_value=resp) as req:
        c = HTTPClient("https://api.example.com", {}, verify_ssl=False)
        c.get_json("/thing")
    assert req.call_count == 1
    # Verify was passed as False to httpx
    _, kwargs = req.call_args
    assert kwargs["verify"] is False


def test_verify_ssl_false_rejected_in_prod(monkeypatch):
    monkeypatch.delenv("DAEDALUS_DEV_MODE", raising=False)
    with pytest.raises(ProviderConfigError):
        HTTPClient("https://api.example.com", {}, verify_ssl=False)


def test_verify_ssl_false_permitted_in_dev_mode(monkeypatch, caplog):
    monkeypatch.setenv("DAEDALUS_DEV_MODE", "1")
    with caplog.at_level("ERROR", logger="daedalus.providers.http"):
        c = HTTPClient("https://api.example.com", {}, verify_ssl=False)
    assert c._verify_ssl is False
    assert any(
        r.levelname == "ERROR" and "SSL certificate verification disabled" in r.getMessage()
        for r in caplog.records
    )


def test_verify_ssl_true_passes_verify_true(no_sleep):
    resp = FakeResponse(200, json_data={"ok": True})
    with mock.patch("core.providers.http.httpx.request", return_value=resp) as req:
        c = HTTPClient("https://api.example.com", {}, verify_ssl=True)
        c.get_json("/thing")
    _, kwargs = req.call_args
    assert kwargs["verify"] is True


def test_retry_on_429_honours_retry_after(no_sleep):
    responses = [FakeResponse(429, headers={"Retry-After": "2"}),
                 FakeResponse(200, json_data={"ok": True})]
    with mock.patch("core.providers.http.httpx.request", side_effect=responses) as req:
        data = _client().get_json("/thing")
    assert data == {"ok": True}
    assert req.call_count == 2
    no_sleep.assert_called_once_with(2.0)


def test_retry_on_503_then_success(no_sleep):
    responses = [FakeResponse(503), FakeResponse(503), FakeResponse(200, json_data=[1])]
    with mock.patch("core.providers.http.httpx.request", side_effect=responses):
        assert _client().get_json("/x") == [1]


def test_4xx_raises_with_status_and_redacts_token(no_sleep):
    token = "sekret-token-123"
    resp = FakeResponse(401, text=f"bad credentials for {token}")
    with mock.patch("core.providers.http.httpx.request", return_value=resp):
        with pytest.raises(ProviderError) as exc:
            _client(token).get_json("/x")
    assert exc.value.status_code == 401
    assert token not in str(exc.value)
    assert "<REDACTED>" in str(exc.value)


def test_network_error_redacts_token(no_sleep):
    import httpx as _httpx
    token = "sekret-token-123"
    err = _httpx.ConnectError(f"boom {token}")
    with mock.patch("core.providers.http.httpx.request", side_effect=err):
        with pytest.raises(ProviderError) as exc:
            _client(token).get_json("/x")
    assert token not in str(exc.value)


def test_paginate_link_header(no_sleep):
    page1 = FakeResponse(200, json_data=[1, 2],
                         headers={"Link": '<https://api.example.com/x?page=2>; rel="next"'})
    page2 = FakeResponse(200, json_data=[3])
    with mock.patch("core.providers.http.httpx.request", side_effect=[page1, page2]):
        assert _client().get_paginated("/x") == [1, 2, 3]


def test_paginate_x_next_page(no_sleep):
    page1 = FakeResponse(200, json_data=[1], headers={"X-Next-Page": "2"})
    page2 = FakeResponse(200, json_data=[2], headers={"X-Next-Page": ""})
    with mock.patch("core.providers.http.httpx.request", side_effect=[page1, page2]):
        assert _client().get_paginated("/x", style="x_next_page") == [1, 2]


def test_paginate_continuation(no_sleep):
    page1 = FakeResponse(200, json_data={"value": [1]},
                         headers={"x-ms-continuationtoken": "tok"})
    page2 = FakeResponse(200, json_data={"value": [2]})
    with mock.patch("core.providers.http.httpx.request", side_effect=[page1, page2]):
        assert _client().get_paginated("/x", style="continuation") == [1, 2]


def test_parse_link_next():
    hdr = ('<https://api.github.com/x?page=2>; rel="next", '
           '<https://api.github.com/x?page=9>; rel="last"')
    assert _parse_link_next(hdr) == "https://api.github.com/x?page=2"
    assert _parse_link_next('<https://x>; rel="last"') is None
    assert _parse_link_next("") is None

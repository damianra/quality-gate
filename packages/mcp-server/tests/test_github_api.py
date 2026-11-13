"""FR-P3: GitHubClient retries transient faults with bounded backoff."""
from __future__ import annotations

import httpx
import pytest

from quality_gate_mcp import config
from quality_gate_mcp.github_api import GitHubClient


def _client(handler) -> GitHubClient:
    client = GitHubClient(token="t", base_url="https://api.test")
    client._client = httpx.Client(
        base_url="https://api.test",
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("quality_gate_mcp.github_api.time.sleep",
                        lambda _s: None)


def test_retries_429_honoring_retry_after(monkeypatch) -> None:
    calls: list[str] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(
                429, headers={"Retry-After": "7"}, json={})
        return httpx.Response(200, json={"number": 1})

    monkeypatch.setattr(
        "quality_gate_mcp.github_api.time.sleep", delays.append)
    gh = _client(handler)
    assert gh.get_pr("a/b", 1)["number"] == 1
    assert calls == ["/repos/a/b/pulls/1"] * 2
    assert delays == [7.0]           # Retry-After wins over backoff


def test_retries_5xx_then_succeeds() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"ok": True})

    gh = _client(handler)
    assert gh.get_pr("a/b", 1) == {"ok": True}
    assert attempts["n"] == 3        # default RETRY_ATTEMPTS


def test_permanent_4xx_never_retries() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, json={"message": "Not Found"})

    gh = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        gh.get_pr("a/b", 999)
    assert calls["n"] == 1


def test_transport_error_retries_then_raises(monkeypatch) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(config, "RETRY_ATTEMPTS", 2)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("refused", request=request)

    gh = _client(handler)
    with pytest.raises(httpx.ConnectError):
        gh.get_pr("a/b", 1)
    assert calls["n"] == 2


def test_exhausted_retries_raise_last_status() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502, json={})

    gh = _client(handler)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        gh.get_pr("a/b", 1)
    assert ei.value.response.status_code == 502
    assert calls["n"] == config.RETRY_ATTEMPTS

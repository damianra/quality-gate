"""FR-P15/P16: JSON logging, run correlation, /readyz, heartbeat."""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from pr_review_orch import config, ingress, logs, run_queue


def test_json_formatter_includes_run_id() -> None:
    logs.run_id_var.set("pr-review/acme/widgets/7")
    rec = logging.LogRecord(
        "x.y", logging.INFO, __file__, 1, "hello", (), None)
    doc = json.loads(logs.JsonFormatter().format(rec))
    assert doc["run_id"] == "pr-review/acme/widgets/7"
    assert doc["level"] == "INFO" and doc["msg"] == "hello"
    logs.run_id_var.set("")
    rec2 = logging.LogRecord(
        "x.y", logging.INFO, __file__, 1, "bye", (), None)
    assert "run_id" not in json.loads(logs.JsonFormatter().format(rec2))


def test_run_id_var_survives_to_thread() -> None:
    """contextvars copy into asyncio.to_thread — the correlation works
    for the sync clone/graph hops, not just async code."""
    async def drive() -> str:
        logs.run_id_var.set("pr-review/a/b/1")
        return await asyncio.to_thread(logs.run_id_var.get)

    assert asyncio.run(drive()) == "pr-review/a/b/1"


def test_readyz_no_dsn(monkeypatch) -> None:
    async def drive():
        ingress._QUEUE = asyncio.Queue()
        transport = ASGITransport(app=ingress.app)
        async with AsyncClient(
                transport=transport, base_url="http://t") as c:
            return await c.get("/readyz")

    monkeypatch.setattr(config, "DATABASE_URL", "")
    r = asyncio.run(drive())
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "postgres": None,
                        "queue_depth": 0}


def test_readyz_pg_down_is_503(monkeypatch) -> None:
    async def drive():
        ingress._QUEUE = asyncio.Queue()
        ingress._QUEUE.put_nowait({"e": 1})
        transport = ASGITransport(app=ingress.app)
        async with AsyncClient(
                transport=transport, base_url="http://t") as c:
            return await c.get("/readyz")

    monkeypatch.setattr(config, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(run_queue, "reachable", lambda: False)
    r = asyncio.run(drive())
    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "postgres": False,
                        "queue_depth": 1}


def test_readyz_pg_up(monkeypatch) -> None:
    async def drive():
        ingress._QUEUE = asyncio.Queue()
        transport = ASGITransport(app=ingress.app)
        async with AsyncClient(
                transport=transport, base_url="http://t") as c:
            return await c.get("/readyz")

    monkeypatch.setattr(run_queue, "reachable", lambda: True)
    r = asyncio.run(drive())
    assert r.status_code == 200
    assert r.json()["postgres"] is True


def test_heartbeat_disabled_without_url(monkeypatch) -> None:
    monkeypatch.setattr(config, "HEARTBEAT_URL", "")
    asyncio.run(ingress._heartbeat())   # must not raise


def test_heartbeat_pings_url(monkeypatch) -> None:
    hits: list[str] = []
    monkeypatch.setattr(config, "HEARTBEAT_URL", "https://hc.example/p")

    def fake_get(url, timeout):
        hits.append(url)

    monkeypatch.setattr("httpx.get", fake_get)
    asyncio.run(ingress._heartbeat())
    assert hits == ["https://hc.example/p"]


def test_heartbeat_failure_is_swallowed(monkeypatch) -> None:
    monkeypatch.setattr(config, "HEARTBEAT_URL", "https://hc.example/p")

    def boom(url, timeout):
        raise RuntimeError("down")

    monkeypatch.setattr("httpx.get", boom)
    asyncio.run(ingress._heartbeat())   # must not raise


def test_run_once_pings_heartbeat(monkeypatch, tmp_path) -> None:
    """Every completed run — success or failure — pings the monitor."""
    pings: list[str] = []

    async def fake_heartbeat() -> None:
        pings.append("x")

    async def fake_status(*a, **k) -> None:
        return None

    monkeypatch.setattr(ingress, "_heartbeat", fake_heartbeat)
    monkeypatch.setattr(ingress, "_post_status", fake_status)
    monkeypatch.setattr(ingress.fetch, "clone_pr",
                        lambda *a, **k: tmp_path / "wt")
    (tmp_path / "wt").mkdir()
    monkeypatch.setattr(ingress.graph, "run_pr_review",
                        lambda *a, **k: {"verdict": {"approve": True}})
    monkeypatch.setattr(ingress.run_queue, "mark_running", lambda *a: None)
    monkeypatch.setattr(ingress.run_queue, "mark_done", lambda *a: None)
    monkeypatch.setattr(ingress.run_queue, "mark_failed",
                        lambda *a, **k: None)
    event = {"repo": "a/b", "pr_number": 1, "clone_url": "u",
             "head_sha": "s", "delivery_id": "d"}
    asyncio.run(ingress._run_once(event))
    assert pings == ["x"]
    assert not (tmp_path / "wt").exists()

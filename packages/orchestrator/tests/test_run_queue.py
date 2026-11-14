"""Durable run-queue tests (FR-P4) — fake psycopg, no live DB needed."""

from __future__ import annotations

import pytest

from pr_review_orch import run_queue


class _FakeCursor:
    def __init__(self, fetch_rows=(), rowcount=1):
        self.calls: list = []
        self._fetch_rows = list(fetch_rows)
        self.rowcount = rowcount

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def execute(self, sql, params=None) -> None:
        self.calls.append((sql, params))

    def fetchall(self):
        return self._fetch_rows


class _FakeConn:
    """Models psycopg3 semantics: the `with` block commits on clean
    exit and closes — a commit() after it would raise."""

    def __init__(self, cur) -> None:
        self.cur = cur
        self.committed = False
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        if args[0] is None:
            self.committed = True
        self.closed = True
        return False

    def cursor(self):
        return self.cur

    def commit(self) -> None:
        if self.closed:
            raise run_queue.psycopg.OperationalError(
                "the connection is closed")
        self.committed = True


@pytest.fixture
def _dsn(monkeypatch):
    monkeypatch.setattr(run_queue.config, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(run_queue, "_SCHEMA_READY", True)


def test_enabled_reflects_dsn(monkeypatch) -> None:
    monkeypatch.setattr(run_queue.config, "DATABASE_URL", "")
    assert not run_queue.enabled()
    monkeypatch.setattr(run_queue.config, "DATABASE_URL", "postgres://x")
    assert run_queue.enabled()


def test_enqueue_inserts_and_dedupes(_dsn, monkeypatch) -> None:
    """delivery_id PK makes dedupe restart-proof (ON CONFLICT)."""
    new = _FakeConn(_FakeCursor(rowcount=1))
    dup = _FakeConn(_FakeCursor(rowcount=0))
    conns = iter((new, dup))
    monkeypatch.setattr(
        run_queue.psycopg, "connect", lambda *a, **k: next(conns))
    event = {
        "delivery_id": "d-1", "repo": "a/b", "pr_number": 3,
        "head_sha": "abc", "clone_url": "u",
    }
    assert run_queue.enqueue(dict(event)) is True
    assert "ON CONFLICT" in new.cur.calls[0][0]
    assert new.cur.calls[0][1][0] == "d-1"
    assert run_queue.enqueue(dict(event)) is False   # duplicate
    assert new.committed and dup.committed


def test_recover_maps_unfinished_rows(_dsn, monkeypatch) -> None:
    """queued/running rows from a crashed process become events again."""
    cur = _FakeCursor(fetch_rows=[
        ("d-1", "a/b", 3, "sha1", "u1"),
        ("d-2", "a/c", 9, "sha2", "u2"),
    ])
    monkeypatch.setattr(
        run_queue.psycopg, "connect", lambda *a, **k: _FakeConn(cur))
    events = run_queue.recover()
    assert events == [
        {"delivery_id": "d-1", "repo": "a/b", "pr_number": 3,
         "head_sha": "sha1", "clone_url": "u1"},
        {"delivery_id": "d-2", "repo": "a/c", "pr_number": 9,
         "head_sha": "sha2", "clone_url": "u2"},
    ]
    assert "queued" in cur.calls[0][0] and "running" in cur.calls[0][0]


def test_recover_noop_without_dsn(monkeypatch) -> None:
    monkeypatch.setattr(run_queue.config, "DATABASE_URL", "")
    assert run_queue.recover() == []


def test_marks_noop_without_dsn(monkeypatch) -> None:
    """Local dev (no DB): marks are no-ops, never reach psycopg."""
    monkeypatch.setattr(run_queue.config, "DATABASE_URL", "")
    called = []
    monkeypatch.setattr(
        run_queue.psycopg, "connect", lambda *a, **k: called.append(1))
    run_queue.mark_running("d-1")
    run_queue.mark_done("d-1")
    run_queue.mark_failed("d-1", "boom")
    assert called == []


def test_mark_failed_binds_error_then_id(_dsn, monkeypatch) -> None:
    cur = _FakeCursor()
    monkeypatch.setattr(
        run_queue.psycopg, "connect", lambda *a, **k: _FakeConn(cur))
    run_queue.mark_failed("d-1", "RuntimeError: exploded")
    sql, params = cur.calls[0]
    assert "error = %s" in sql and "delivery_id = %s" in sql
    assert params == ("RuntimeError: exploded", "d-1")


def test_mark_running_increments_attempts(_dsn, monkeypatch) -> None:
    cur = _FakeCursor()
    monkeypatch.setattr(
        run_queue.psycopg, "connect", lambda *a, **k: _FakeConn(cur))
    run_queue.mark_running("d-1")
    sql, params = cur.calls[0]
    assert "attempts + 1" in sql and params == ("d-1",)


def test_mark_swallows_db_errors(_dsn, monkeypatch) -> None:
    """Queue bookkeeping must never kill a review run."""
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(run_queue.psycopg, "connect", boom)
    run_queue.mark_done("d-1")   # logs, never raises


def test_setup_runs_ddl_once(monkeypatch) -> None:
    monkeypatch.setattr(run_queue.config, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(run_queue, "_SCHEMA_READY", False)
    cur = _FakeCursor()
    monkeypatch.setattr(
        run_queue.psycopg, "connect", lambda *a, **k: _FakeConn(cur))
    run_queue.setup()
    run_queue.setup()
    creates = [s for s, _ in cur.calls if "CREATE TABLE" in s]
    assert len(creates) == 1   # memoized: second call is a no-op

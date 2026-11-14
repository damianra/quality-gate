"""Durable run queue (FR-P4): ``pending_runs`` is the source of truth.

Accepted webhook events are persisted BEFORE the ``202`` response, so a
process restart cannot silently drop a review GitHub believes was
queued. The in-process asyncio worker still dispatches and coalesces
exactly as before — the table adds durability (startup recovery of
``queued``/``running`` rows) and restart-surviving delivery dedupe
(``delivery_id`` primary key). No broker: the scale is tens of PRs/day.

When ``DATABASE_URL`` is unset (local dev) every function is a no-op
and the ingress falls back to the in-memory path — same FR-O21
semantics, just not durable.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

from . import config

logger = logging.getLogger(__name__)

_SCHEMA_READY = False

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS pending_runs (
        delivery_id TEXT PRIMARY KEY,
        repo TEXT NOT NULL,
        pr_number INT NOT NULL,
        head_sha TEXT NOT NULL,
        clone_url TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        attempts INT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        error TEXT
    )
    """,
)


def enabled() -> bool:
    """True when a DSN is configured — the durable path is active."""
    return bool(config.DATABASE_URL)


def reachable() -> bool | None:
    """Postgres liveness probe for ``/readyz`` (FR-P16).

    ``None`` when no DSN is configured (Postgres is optional — local
    in-memory mode), else a bounded ``SELECT 1``.
    """
    if not enabled():
        return None
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:  # noqa: BLE001 - probe must not raise
        return False


def _connect():
    return psycopg.connect(config.DATABASE_URL, connect_timeout=5)


def setup() -> None:
    """Bootstrap the table once per process (memoized DDL, FR-O18
    pattern). Best-effort: a failed DDL retries on the next call."""
    global _SCHEMA_READY
    if not enabled() or _SCHEMA_READY:
        return
    try:
        # psycopg3: the `with` block commits on clean exit and then
        # closes the connection — a commit() after it raises
        # OperationalError("the connection is closed").
        with _connect() as conn, conn.cursor() as cur:
            for ddl in _DDL:
                cur.execute(ddl)
        _SCHEMA_READY = True
    except Exception:  # noqa: BLE001 - retried on next call
        logger.warning("pending_runs schema bootstrap failed", exc_info=True)


def enqueue(event: dict[str, Any]) -> bool:
    """Persist an accepted event; ``False`` when ``delivery_id`` exists.

    ``delivery_id`` (``X-GitHub-Delivery``) is the dedupe key — it
    survives restarts, unlike the old bounded in-memory set. Raises on
    DB failure: the caller answers ``500`` instead of acknowledging a
    delivery it could not durably accept.
    """
    setup()
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pending_runs (
                delivery_id, repo, pr_number, head_sha, clone_url
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (delivery_id) DO NOTHING
            """,
            (
                event["delivery_id"], event["repo"], event["pr_number"],
                event["head_sha"], event["clone_url"],
            ),
        )
        inserted = cur.rowcount > 0
    return inserted


def recover() -> list[dict[str, Any]]:
    """Requeue rows left ``queued``/``running`` by a crashed process."""
    if not enabled():
        return []
    setup()
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT delivery_id, repo, pr_number, head_sha, clone_url
                FROM pending_runs
                WHERE status IN ('queued', 'running')
                ORDER BY created_at
                """
            )
            rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - recovery is best-effort
        logger.warning("pending_runs recovery failed", exc_info=True)
        return []
    events = [
        {
            "delivery_id": r[0], "repo": r[1], "pr_number": r[2],
            "head_sha": r[3], "clone_url": r[4],
        }
        for r in rows
    ]
    if events:
        logger.info("recovering %d pending run(s) from a previous process",
                    len(events))
    return events


def _mark(delivery_id: str, sql: str, params: tuple) -> None:
    """One status transition; best-effort — queue bookkeeping never
    kills a review run. ``delivery_id`` binds the trailing ``WHERE``."""
    if not enabled() or not delivery_id:
        return
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (*params, delivery_id))
    except Exception:  # noqa: BLE001
        logger.warning("pending_runs mark failed for %s",
                       delivery_id, exc_info=True)


def mark_running(delivery_id: str) -> None:
    """queued -> running: start timestamp + attempt counter."""
    _mark(
        delivery_id,
        """UPDATE pending_runs
           SET status = 'running', started_at = now(),
               attempts = attempts + 1
           WHERE delivery_id = %s""",
        (),
    )


def mark_done(delivery_id: str) -> None:
    """running -> done."""
    _mark(
        delivery_id,
        """UPDATE pending_runs
           SET status = 'done', finished_at = now()
           WHERE delivery_id = %s""",
        (),
    )


def mark_failed(delivery_id: str, error: str) -> None:
    """running -> failed with the exception summary (FR-P2 trace)."""
    _mark(
        delivery_id,
        """UPDATE pending_runs
           SET status = 'failed', finished_at = now(), error = %s
           WHERE delivery_id = %s""",
        (error[:500],),
    )

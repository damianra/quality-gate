"""Run observability: per-node durations, LLM usage and process timings.

Metrics are plain dicts appended to ``PRReviewState`` lists — the
add-reducers merge contributions from parallel specialist branches.
``summarize`` folds everything into the report the synthesizer embeds in
the verdict comment and the CLI prints, so the user can see where time and
tokens went.

``persist_run`` (FR-O18) additionally writes the finished run's telemetry
to Postgres — one ``run_metrics`` row per task plus one
``llm_call_metrics`` row per model call — so usage, latency and cost can
be measured per PR and per model. Persistence is best-effort: it never
breaks a review run.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import config

logger = logging.getLogger("pr-review-orch.metrics")


def node_duration(node: str, started: float) -> dict[str, Any]:
    """Duration entry for one graph node."""
    return {"node": node, "duration_s": round(time.monotonic() - started, 3)}


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    """Aggregate node/LLM/process metrics into a compact report."""
    llm_calls = state.get("llm_calls", [])
    processes = state.get("processes", [])
    nodes = state.get("node_durations", [])
    started_at = state.get("started_at")

    prompt = sum(c.get("usage", {}).get("prompt_tokens", 0) for c in llm_calls)
    completion = sum(c.get("usage", {}).get("completion_tokens", 0) for c in llm_calls)
    total_s = round(time.monotonic() - started_at, 3) if started_at else None

    return {
        "total_s": total_s,
        "nodes": sorted(nodes, key=lambda n: n["duration_s"], reverse=True),
        "llm": {
            "calls": len(llm_calls),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "detail": llm_calls,
        },
        "processes": {"count": len(processes), "detail": processes},
    }


def summary_line(metrics: dict[str, Any]) -> str:
    """Compact one-liner embedded in the verdict comment."""
    parts = [f"{metrics['total_s']}s total"]
    if metrics["nodes"]:
        slowest = metrics["nodes"][0]
        parts.append(f"slowest node: {slowest['node']} ({slowest['duration_s']}s)")
    llm = metrics["llm"]
    if llm["calls"]:
        parts.append(f"{llm['calls']} LLM call(s), {llm['total_tokens']} tokens")
    if metrics["processes"]["count"]:
        parts.append(f"{metrics['processes']['count']} process(es)")
    return "; ".join(parts)


# --------------------------------------------------------------------------- #
# Postgres persistence (FR-O18)
# --------------------------------------------------------------------------- #

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS run_metrics (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id TEXT NOT NULL,
        repo TEXT NOT NULL,
        pr_number INT NOT NULL,
        head_sha TEXT,
        dry_run BOOLEAN NOT NULL DEFAULT FALSE,
        duration_s DOUBLE PRECISION,
        verdict_approve BOOLEAN,
        blockers INT,
        warnings INT,
        infos INT,
        llm_calls_count INT,
        prompt_tokens INT,
        completion_tokens INT,
        total_tokens INT,
        nodes JSONB,
        processes JSONB,
        metrics JSONB,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_call_metrics (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id TEXT NOT NULL,
        repo TEXT NOT NULL,
        pr_number INT NOT NULL,
        head_sha TEXT,
        role TEXT,
        node TEXT,
        provider TEXT,
        model TEXT,
        prompt_tokens INT,
        completion_tokens INT,
        total_tokens INT,
        cost_usd NUMERIC(12,6),
        duration_s DOUBLE PRECISION,
        finish_reason TEXT,
        truncated BOOLEAN,
        status TEXT NOT NULL DEFAULT 'ok',
        error TEXT,
        raw JSONB,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
)

_RUN_INSERT = """
    INSERT INTO run_metrics (
        run_id, repo, pr_number, head_sha, dry_run, duration_s,
        verdict_approve, blockers, warnings, infos,
        llm_calls_count, prompt_tokens, completion_tokens, total_tokens,
        nodes, processes, metrics
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s)
"""

_CALL_INSERT = """
    INSERT INTO llm_call_metrics (
        run_id, repo, pr_number, head_sha, role, node, provider, model,
        prompt_tokens, completion_tokens, total_tokens, cost_usd,
        duration_s, finish_reason, truncated, status, error, raw
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s)
"""


def _run_row(state: dict[str, Any], run_id: str) -> tuple[Any, ...]:
    """Column tuple for one ``run_metrics`` row. Pure for tests."""
    report = state.get("metrics") or summarize(state)
    counts = {"blocker": 0, "warning": 0, "info": 0}
    for f in state.get("findings", []):
        if f.get("severity") in counts:
            counts[f["severity"]] += 1
    llm = report["llm"]
    return (
        run_id,
        state.get("repo_path", ""),
        int(state.get("pr_number", 0)),
        state.get("head_sha", ""),
        bool(state.get("dry_run", False)),
        report["total_s"],
        (state.get("verdict") or {}).get("approve"),
        counts["blocker"],
        counts["warning"],
        counts["info"],
        llm["calls"],
        llm["prompt_tokens"],
        llm["completion_tokens"],
        llm["total_tokens"],
        Jsonb(report["nodes"]),
        Jsonb(report["processes"]["detail"]),
        Jsonb(report),
    )


def _call_rows(state: dict[str, Any], run_id: str) -> list[tuple[Any, ...]]:
    """One column tuple per ``llm_calls`` entry (FR-O16 shape, tolerant)."""
    rows = []
    for call in state.get("llm_calls", []):
        usage = call.get("usage") or {}
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        cost = call.get("cost_usd", usage.get("cost", usage.get("cost_usd")))
        rows.append((
            run_id,
            state.get("repo_path", ""),
            int(state.get("pr_number", 0)),
            state.get("head_sha", ""),
            call.get("role"),
            call.get("node"),
            call.get("provider"),
            call.get("model"),
            prompt,
            completion,
            prompt + completion,
            cost,
            call.get("duration_s"),
            call.get("finish_reason"),
            bool(call.get("truncated", False)),
            call.get("status", "ok"),
            call.get("error"),
            Jsonb(call),
        ))
    return rows


def persist_run(
    state: dict[str, Any], run_id: str, dsn: str | None = None
) -> None:
    """Write task + per-model-call telemetry to Postgres (FR-O18).

    No-op when ``dsn``/``DATABASE_URL`` is unset. ``run_id`` is the
    checkpointer thread id (``pr-review/{slug}/{pr}``) so metrics join
    back to the task. Write failures are logged and swallowed —
    telemetry must never break a review run.
    """
    dsn = config.DATABASE_URL if dsn is None else dsn
    if not dsn:
        return
    try:
        # Bounded connect: a hung Postgres must not stall the run either.
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                for ddl in _DDL:
                    cur.execute(ddl)
                cur.execute(_RUN_INSERT, _run_row(state, run_id))
                cur.executemany(_CALL_INSERT, _call_rows(state, run_id))
            conn.commit()
    except Exception:  # noqa: BLE001 - telemetry must never kill a run
        logger.warning("run metrics persistence failed", exc_info=True)

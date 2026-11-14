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
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import config

logger = logging.getLogger("pr-review-orch.metrics")

#: Schema bootstrap runs once per process; a failed DDL leaves it False
#: so the next run retries (FR-O18 bootstrap-on-write).
_SCHEMA_READY = False


def node_duration(node: str, started: float) -> dict[str, Any]:
    """Duration entry for one graph node."""
    return {"node": node, "duration_s": round(time.monotonic() - started, 3)}


def _attempt_calls(state: dict[str, Any]) -> list[dict[str, Any]]:
    """``llm_calls`` belonging to this run attempt only.

    ``llm_calls`` is an add-reducer field, so a checkpoint-restored
    thread (same ``thread_id`` per PR) accumulates calls across webhook
    events. Entries are stamped with the attempt's worktree basename:
    a new event clones a fresh ``qg-*`` worktree (new attempt), while a
    mid-graph resume reuses the worktree and keeps its calls. Untagged
    entries count as current for backwards compatibility.
    """
    attempt = Path(state.get("repo_path") or "").name
    calls = state.get("llm_calls") or []
    if not attempt:
        return calls
    return [c for c in calls
            if not isinstance(c, dict) or c.get("attempt") in (None, attempt)]


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    """Aggregate node/LLM/process metrics into a compact report."""
    llm_calls = _attempt_calls(state)
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
            "by_model": _by_model(llm_calls),
            "detail": llm_calls,
        },
        "processes": {"count": len(processes), "detail": processes},
    }


def _by_model(llm_calls: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-model totals across calls (FR-T3).

    Missing telemetry fields stay absent — a provider that reports only
    ``usage`` still contributes calls/tokens/duration, never fake zeros
    for perf or memory.
    """
    out: dict[str, dict[str, Any]] = {}
    for c in llm_calls:
        model = c.get("model") or "(unknown)"
        agg = out.setdefault(model, {
            "provider": c.get("provider"),
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "duration_s": 0.0,
            "tps": [],
            "rss_delta_mb": [],
            "model_vram_mb": [],
        })
        usage = c.get("usage") or {}
        agg["calls"] += 1
        agg["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
        agg["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
        agg["duration_s"] = round(
            agg["duration_s"] + float(c.get("duration_s") or 0.0), 3)
        tps = (c.get("perf") or {}).get("completion_tps")
        if tps is not None:
            agg["tps"].append(tps)
        delta = (c.get("memory") or {}).get("rss_delta_mb")
        if delta is not None:
            agg["rss_delta_mb"].append(delta)
        vram = (c.get("provider_stats") or {}).get("model_vram_mb")
        if vram is not None:
            agg["model_vram_mb"].append(vram)
    for agg in out.values():
        agg["total_tokens"] = agg["prompt_tokens"] + agg["completion_tokens"]
        agg["mean_completion_tps"] = (
            round(sum(agg["tps"]) / len(agg["tps"]), 1) if agg["tps"] else None
        )
        agg["max_rss_delta_mb"] = (
            max(agg["rss_delta_mb"]) if agg["rss_delta_mb"] else None
        )
        agg["model_vram_mb"] = (
            max(agg["model_vram_mb"]) if agg["model_vram_mb"] else None
        )
        del agg["tps"]
        del agg["rss_delta_mb"]
    return out


def summary_line(metrics: dict[str, Any]) -> str:
    """Compact one-liner embedded in the verdict comment."""
    parts = [f"{metrics['total_s']}s total"]
    if metrics["nodes"]:
        slowest = metrics["nodes"][0]
        parts.append(f"slowest node: {slowest['node']} ({slowest['duration_s']}s)")
    llm = metrics["llm"]
    if llm["calls"]:
        per_model = ", ".join(
            f"{m}: {a['total_tokens']}" for m, a in llm["by_model"].items()
        )
        parts.append(
            f"{llm['calls']} LLM call(s), {llm['total_tokens']} tokens"
            + (f" [{per_model}]" if per_model else "")
        )
    if metrics["processes"]["count"]:
        parts.append(f"{metrics['processes']['count']} process(es)")
    return "; ".join(parts)


def detail_block(metrics: dict[str, Any]) -> str:
    """Markdown table of per-model LLM telemetry (FR-T3).

    Rendered inside the verdict's collapsible ``<details>`` block.
    Providers that report only ``usage`` still get a row with their
    counts — missing perf/memory cells render as ``—``. Empty string
    when no LLM calls ran.
    """
    by_model = metrics.get("llm", {}).get("by_model") or {}
    if not by_model:
        return ""
    rows = [
        "| model | calls | prompt+completion | total s | tok/s | "
        "ΔRSS MB | model VRAM MB |",
        "|---|---|---|---|---|---|---|",
    ]
    for model, a in sorted(by_model.items()):
        def _v(value: Any, suffix: str = "") -> str:
            return f"{value}{suffix}" if value is not None else "—"
        rows.append(
            f"| {model} | {a['calls']} | "
            f"{a['prompt_tokens']}+{a['completion_tokens']} | "
            f"{a['duration_s']} | {_v(a['mean_completion_tps'])} | "
            f"{_v(a['max_rss_delta_mb'])} | {_v(a['model_vram_mb'])} |"
        )
    return "\n".join(rows)


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
    # FR-T4: idempotent column adds — no migration tooling needed.
    "ALTER TABLE llm_call_metrics "
    "ADD COLUMN IF NOT EXISTS completion_tps DOUBLE PRECISION",
    "ALTER TABLE llm_call_metrics "
    "ADD COLUMN IF NOT EXISTS rss_delta_mb DOUBLE PRECISION",
    "ALTER TABLE llm_call_metrics "
    "ADD COLUMN IF NOT EXISTS model_size_mb DOUBLE PRECISION",
    "ALTER TABLE llm_call_metrics "
    "ADD COLUMN IF NOT EXISTS model_vram_mb DOUBLE PRECISION",
    # FR-P2: failed runs are visible in run_metrics, not just in logs.
    "ALTER TABLE run_metrics "
    "ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ok'",
    "ALTER TABLE run_metrics "
    "ADD COLUMN IF NOT EXISTS error TEXT",
)

_RUN_INSERT = """
    INSERT INTO run_metrics (
        run_id, repo, pr_number, head_sha, dry_run, duration_s,
        verdict_approve, blockers, warnings, infos,
        llm_calls_count, prompt_tokens, completion_tokens, total_tokens,
        nodes, processes, metrics, status, error
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s)
"""

_CALL_INSERT = """
    INSERT INTO llm_call_metrics (
        run_id, repo, pr_number, head_sha, role, node, provider, model,
        prompt_tokens, completion_tokens, total_tokens, cost_usd,
        duration_s, finish_reason, truncated, status, error,
        completion_tps, rss_delta_mb, model_size_mb, model_vram_mb, raw
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _run_row(
    state: dict[str, Any], run_id: str,
    status: str = "ok", error: str | None = None,
) -> tuple[Any, ...]:
    """Column tuple for one ``run_metrics`` row. Pure for tests."""
    report = state.get("metrics") or summarize(state)
    counts = {"blocker": 0, "warning": 0, "info": 0}
    for f in state.get("findings", []):
        if f.get("severity") in counts:
            counts[f["severity"]] += 1
    llm = report["llm"]
    return (
        run_id,
        # FR-P1: repo identity is the owner/repo slug, never the
        # (possibly ephemeral) worktree path.
        state.get("repo_slug") or state.get("repo_path", ""),
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
        status,
        error,
    )


def _call_rows(state: dict[str, Any], run_id: str) -> list[tuple[Any, ...]]:
    """One column tuple per ``llm_calls`` entry (FR-O16 shape, tolerant).

    Each entry is built in isolation: a malformed call dict is skipped
    and logged instead of aborting persistence for the whole run.
    """
    rows = []
    for i, call in enumerate(_attempt_calls(state)):
        try:
            usage = call.get("usage") or {}
            prompt = int(usage.get("prompt_tokens", 0) or 0)
            completion = int(usage.get("completion_tokens", 0) or 0)
            cost = call.get(
                "cost_usd", usage.get("cost", usage.get("cost_usd"))
            )
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
                (call.get("perf") or {}).get("completion_tps"),
                (call.get("memory") or {}).get("rss_delta_mb"),
                (call.get("provider_stats") or {}).get("model_ram_mb"),
                (call.get("provider_stats") or {}).get("model_vram_mb"),
                Jsonb(call),
            ))
        except Exception:  # noqa: BLE001 - skip the bad row, keep the rest
            logger.warning("skipping malformed llm_calls entry %d", i,
                           exc_info=True)
    return rows


def persist_run(
    state: dict[str, Any], run_id: str, dsn: str | None = None,
    status: str = "ok", error: str | None = None,
) -> None:
    """Write task + per-model-call telemetry to Postgres (FR-O18).

    No-op when ``dsn``/``DATABASE_URL`` is unset. ``run_id`` is the
    checkpointer thread id (``pr-review/{slug}/{pr}``) so metrics join
    back to the task. ``status``/``error`` (FR-P2) mark the run
    ``ok|error|timeout`` so crashed runs are queryable, not invisible.
    Write failures are logged and swallowed — telemetry must never
    break a review run.
    """
    dsn = config.DATABASE_URL if dsn is None else dsn
    if not dsn:
        return
    global _SCHEMA_READY
    try:
        # Bounded connect: a hung Postgres must not stall the run either.
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                if not _SCHEMA_READY:
                    for ddl in _DDL:
                        cur.execute(ddl)
                    _SCHEMA_READY = True
                cur.execute(
                    _RUN_INSERT, _run_row(state, run_id, status, error))
                for row in _call_rows(state, run_id):
                    cur.execute(_CALL_INSERT, row)
            conn.commit()
    except Exception:  # noqa: BLE001 - telemetry must never kill a run
        logger.warning("run metrics persistence failed", exc_info=True)

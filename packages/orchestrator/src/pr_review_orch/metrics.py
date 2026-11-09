"""Run observability: per-node durations, LLM usage and process timings.

Metrics are plain dicts appended to ``PRReviewState`` lists — the
add-reducers merge contributions from parallel specialist branches.
``summarize`` folds everything into the report the synthesizer embeds in
the verdict comment and the CLI prints, so the user can see where time and
tokens went.
"""

from __future__ import annotations

import time
from typing import Any


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

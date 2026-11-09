"""Unit tests for run observability (metrics.py)."""

from __future__ import annotations

import time

from pr_review_orch import metrics


def test_node_duration_records_elapsed() -> None:
    started = time.monotonic() - 0.5
    entry = metrics.node_duration("router", started)
    assert entry["node"] == "router"
    assert entry["duration_s"] >= 0.5


def _state(**kw) -> dict:
    base = {
        "started_at": time.monotonic() - 10,
        "node_durations": [
            {"node": "qa", "duration_s": 5.0},
            {"node": "router", "duration_s": 1.0},
        ],
        "llm_calls": [
            {"node": "synthesizer", "duration_s": 2.0,
             "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
            {"node": "router", "duration_s": 1.0,
             "usage": {"prompt_tokens": 50, "completion_tokens": 10}},
        ],
        "processes": [
            {"agent": "qa", "command": "tsc --noEmit", "duration_s": 3.0,
             "exit_code": 0, "timed_out": False},
        ],
    }
    base.update(kw)
    return base


def test_summarize_aggregates() -> None:
    report = metrics.summarize(_state())
    assert report["total_s"] >= 10
    assert report["nodes"][0]["node"] == "qa"  # sorted slowest-first
    assert report["llm"]["calls"] == 2
    assert report["llm"]["prompt_tokens"] == 150
    assert report["llm"]["total_tokens"] == 180
    assert report["processes"]["count"] == 1


def test_summary_line_mentions_time_and_tokens() -> None:
    line = metrics.summary_line(metrics.summarize(_state()))
    assert "total" in line and "qa" in line
    assert "180 tokens" in line and "1 process(es)" in line


def test_summary_line_minimal_run() -> None:
    line = metrics.summary_line(metrics.summarize(
        {"started_at": time.monotonic(), "node_durations": [],
         "llm_calls": [], "processes": []}))
    assert "total" in line and "LLM" not in line

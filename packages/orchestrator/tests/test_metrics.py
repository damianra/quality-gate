"""Unit tests for run observability (metrics.py)."""

from __future__ import annotations

import time

import pytest

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


# --------------------------------------------------------------------------- #
# FR-T3 per-model aggregation + telemetry detail block
# --------------------------------------------------------------------------- #


def _telemetry_state() -> dict:
    return _state(llm_calls=[
        {"role": "review", "node": "review", "provider": "ollama",
         "model": "qwen3:8b", "duration_s": 40.0,
         "usage": {"prompt_tokens": 3000, "completion_tokens": 400},
         "perf": {"completion_tps": 10.0},
         "memory": {"rss_delta_mb": 40.0},
         "provider_stats": {"model_vram_mb": 5200.0}},
        {"role": "harness", "node": "synthesizer", "provider": "ollama",
         "model": "qwen3:8b", "duration_s": 10.0,
         "usage": {"prompt_tokens": 500, "completion_tokens": 100},
         "perf": {"completion_tps": 10.0},
         "memory": {"rss_delta_mb": 5.0},
         "provider_stats": {"model_vram_mb": 5200.0}},
        {"role": "security", "node": "security", "provider": "devin-cli",
         "model": "swe-2-high", "duration_s": 30.0,
         "usage": {"prompt_tokens": 0, "completion_tokens": 0}},
    ])


def test_by_model_aggregates_tokens_and_telemetry() -> None:
    report = metrics.summarize(_telemetry_state())
    qwen = report["llm"]["by_model"]["qwen3:8b"]
    assert qwen["calls"] == 2
    assert qwen["total_tokens"] == 4000
    assert qwen["duration_s"] == 50.0
    assert qwen["mean_completion_tps"] == 10.0
    assert qwen["max_rss_delta_mb"] == 40.0
    assert qwen["model_vram_mb"] == 5200.0
    cli = report["llm"]["by_model"]["swe-2-high"]
    assert cli["calls"] == 1 and cli["mean_completion_tps"] is None


def test_summary_line_splits_tokens_per_model() -> None:
    line = metrics.summary_line(metrics.summarize(_telemetry_state()))
    assert "3 LLM call(s), 4000 tokens" in line
    assert "[qwen3:8b: 4000, swe-2-high: 0]" in line


def test_detail_block_renders_table_with_missing_cells() -> None:
    block = metrics.detail_block(metrics.summarize(_telemetry_state()))
    assert "| model | calls |" in block
    assert "| qwen3:8b | 2 | 3500+500 | 50.0 | 10.0 | 40.0 | 5200.0 |" in block
    assert "| swe-2-high | 1 | 0+0 | 30.0 | — | — | — |" in block


def test_detail_block_empty_without_calls() -> None:
    assert metrics.detail_block(metrics.summarize(
        {"started_at": time.monotonic(), "node_durations": [],
         "llm_calls": [], "processes": []})) == ""


# --------------------------------------------------------------------------- #
# FR-O18 persistence
# --------------------------------------------------------------------------- #


def _full_state() -> dict:
    return {
        "repo_path": "/var/quality-gate/worktrees/qg-abc123",
        "repo_slug": "acme/widgets",
        "pr_number": 7,
        "head_sha": "abc1234",
        "dry_run": True,
        "verdict": {"approve": True},
        "findings": [
            {"severity": "warning"},
            {"severity": "info"},
            {"severity": "info"},
        ],
        "llm_calls": [
            {"role": "review", "node": "review", "provider": "openrouter",
             "model": "nvidia/nemotron-3.5-lightning:free",
             "duration_s": 1.25,
             "usage": {"prompt_tokens": 900, "completion_tokens": 120},
             "finish_reason": "stop", "truncated": False,
             "cost_usd": 0.0},
            {"role": "security", "provider": "devin-cli",
             "model": "swe-2-high", "duration_s": 42.0,
             "usage": {"prompt_tokens": 0, "completion_tokens": 0},
             "finish_reason": None, "truncated": False, "pgid": 1234},
        ],
        "started_at": time.monotonic() - 5,
        "node_durations": [{"node": "router", "duration_s": 1.0}],
        "processes": [
            {"agent": "qa", "command": ["tsc"], "duration_s": 0.5,
             "exit_code": 0},
        ],
    }


def test_run_row_maps_task_fields() -> None:
    row = metrics._run_row(_full_state(), "pr-review/quality-gate/7")
    (run_id, repo, pr, sha, dry, _dur, approve, blockers, warnings, infos,
     calls, prompt, completion, total, nodes, procs, report,
     status, error) = row
    assert run_id == "pr-review/quality-gate/7"
    assert repo == "acme/widgets" and pr == 7 and sha == "abc1234"
    assert dry is True and approve is True
    assert (blockers, warnings, infos) == (0, 1, 2)
    assert calls == 2 and prompt == 900 and completion == 120
    assert total == 1020
    assert isinstance(nodes, metrics.Jsonb)
    assert isinstance(procs, metrics.Jsonb)
    assert isinstance(report, metrics.Jsonb)
    assert status == "ok" and error is None   # FR-P2 defaults


def test_call_rows_map_per_call_fields() -> None:
    rows = metrics._call_rows(_full_state(), "run-1")
    assert len(rows) == 2
    api_call = rows[0]
    assert api_call[4] == "review" and api_call[5] == "review"
    assert api_call[6] == "openrouter"
    assert api_call[7] == "nvidia/nemotron-3.5-lightning:free"
    assert api_call[8:11] == (900, 120, 1020)
    assert api_call[11] == 0.0          # cost_usd from the call dict
    assert api_call[13] == "stop"       # finish_reason
    cli_call = rows[1]
    assert cli_call[6] == "devin-cli" and cli_call[7] == "swe-2-high"
    assert cli_call[10] == 0            # subprocess usage is zeroed
    assert cli_call[15] == "ok"         # status default
    assert cli_call[16] is None         # no error
    assert cli_call[17:21] == (None, None, None, None)  # no telemetry
    assert isinstance(cli_call[21], metrics.Jsonb)


def test_call_rows_maps_telemetry_columns() -> None:
    state = _full_state()
    state["llm_calls"] = [{
        "role": "qa", "node": "qa", "provider": "ollama",
        "model": "qwen3:8b", "duration_s": 40.0,
        "usage": {"prompt_tokens": 3000, "completion_tokens": 400},
        "perf": {"completion_tps": 10.0},
        "memory": {"rss_before_mb": 100.0, "rss_after_mb": 140.0,
                   "rss_delta_mb": 40.0},
        "provider_stats": {"model_ram_mb": 5600.0, "model_vram_mb": 5200.0},
    }]
    rows = metrics._call_rows(state, "run-1")
    assert len(rows) == 1
    assert rows[0][17] == 10.0          # completion_tps
    assert rows[0][18] == 40.0          # rss_delta_mb
    assert rows[0][19] == 5600.0        # model_size_mb
    assert rows[0][20] == 5200.0        # model_vram_mb


class _FakeCursor:
    def __init__(self) -> None:
        self.calls: list = []

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def execute(self, sql, params=None) -> None:
        self.calls.append(("execute", sql, params))

    def executemany(self, sql, seq) -> None:
        self.calls.append(("executemany", sql, list(seq)))


class _FakeConn:
    def __init__(self) -> None:
        self.cur = _FakeCursor()
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def cursor(self):
        return self.cur

    def commit(self) -> None:
        self.committed = True


def test_persist_run_noop_without_dsn(monkeypatch) -> None:
    monkeypatch.setattr(metrics.config, "DATABASE_URL", "")
    called: list = []
    monkeypatch.setattr(metrics.psycopg, "connect",
                        lambda *a, **kw: called.append(a[0]))
    metrics.persist_run(_full_state(), "run-1")           # falls back to env
    metrics.persist_run(_full_state(), "run-1", dsn="")   # explicit empty
    assert called == []


def test_persist_run_writes_rows(monkeypatch) -> None:
    monkeypatch.setattr(metrics, "_SCHEMA_READY", False)
    conn = _FakeConn()
    monkeypatch.setattr(metrics.psycopg, "connect",
                        lambda *a, **kw: conn)
    metrics.persist_run(_full_state(), "run-1", dsn="postgres://x")

    kinds = [k for k, *_ in conn.cur.calls]
    # 2 CREATE + 6 ALTER DDL (first call in process) + 1 run + 2 calls
    assert kinds.count("execute") == 11

    run_insert = conn.cur.calls[8]
    assert run_insert[2][0] == "run-1"      # first param is run_id
    # FR-P1: repo column is the slug, not the ephemeral worktree path.
    assert run_insert[2][1] == "acme/widgets"
    assert run_insert[2][17] == "ok"        # FR-P2 default status
    assert run_insert[2][18] is None        # no error on success
    first_call_insert = conn.cur.calls[9][2]
    assert first_call_insert[6] == "openrouter"
    assert conn.committed


def test_persist_run_marks_error_status(monkeypatch) -> None:
    """FR-P2: a failed run persists status/error instead of vanishing."""
    monkeypatch.setattr(metrics, "_SCHEMA_READY", True)
    conn = _FakeConn()
    monkeypatch.setattr(metrics.psycopg, "connect",
                        lambda *a, **kw: conn)
    metrics.persist_run(
        _full_state(), "run-1", dsn="postgres://x",
        status="error", error="RuntimeError: node exploded")
    run_insert = conn.cur.calls[0]
    assert run_insert[2][17] == "error"
    assert run_insert[2][18] == "RuntimeError: node exploded"


def test_run_pr_review_persists_error_on_crash(monkeypatch, tmp_path) -> None:
    """FR-P2: the finally path leaves a run_metrics row for a crashed
    run — the exception propagates but the failure is queryable."""
    from pr_review_orch import graph
    monkeypatch.setattr(graph.config, "DATABASE_URL", "")
    monkeypatch.setattr(
        graph.git_ops, "get_remote_slug", lambda *_a: "acme/widgets")

    def boom(*_a, **_k):
        raise RuntimeError("node exploded")

    monkeypatch.setattr(graph, "build_graph", boom)
    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        graph.metrics, "persist_run",
        lambda state, run_id, **kw: recorded.append((run_id, kw)))
    with pytest.raises(RuntimeError, match="node exploded"):
        graph.run_pr_review(str(tmp_path), 7)
    assert recorded == [("pr-review/acme/widgets/7", {
        "status": "error", "error": "RuntimeError: node exploded"})]


def test_persist_run_runs_ddl_once_per_process(monkeypatch) -> None:
    monkeypatch.setattr(metrics, "_SCHEMA_READY", False)
    conn = _FakeConn()
    monkeypatch.setattr(metrics.psycopg, "connect",
                        lambda *a, **kw: conn)
    metrics.persist_run(_full_state(), "run-1", dsn="postgres://x")
    metrics.persist_run(_full_state(), "run-1", dsn="postgres://x")

    ddl_count = sum(
        1 for _k, sql, _p in conn.cur.calls if "CREATE TABLE" in sql
    )
    alter_count = sum(
        1 for _k, sql, _p in conn.cur.calls if "ADD COLUMN" in sql
    )
    assert ddl_count == 2   # only the first persist_run ran the DDLs
    assert alter_count == 6  # FR-T4 + FR-P2 columns, once per process


def test_call_rows_skips_malformed_entry() -> None:
    state = _full_state()
    state["llm_calls"] = [
        {"role": "review", "provider": "openrouter", "model": "m",
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        "not-a-dict",  # malformed entry
        {"role": "qa", "provider": "ollama", "model": "qwen3:8b",
         "usage": "also-not-a-dict"},
    ]
    rows = metrics._call_rows(state, "run-1")
    assert len(rows) == 1                  # only the healthy entry
    assert rows[0][6] == "openrouter"
    assert rows[0][8:11] == (10, 5, 15)


def test_persist_run_swallows_db_errors(monkeypatch) -> None:
    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(metrics.psycopg, "connect", boom)
    metrics.persist_run(_full_state(), "run-1", dsn="postgres://x")

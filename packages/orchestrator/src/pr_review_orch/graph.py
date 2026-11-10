"""LangGraph wiring: router -> [specialists] -> synthesizer -> cleanup -> END.

Guarantees:
- ``cleanup`` is the only node reaching END; every path converges on it.
- ``run_pr_review`` wraps invocation in try/finally: even on node failure
  or cancellation, any pgid recorded in the checkpointer state is killed.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Any

from langgraph.graph import END, START, StateGraph

from quality_gate_mcp import tools as mcp_tools

from . import agents, cleanup, config, metrics, scope
from .state import PRReviewState

logger = logging.getLogger("pr-review-orch")

SPECIALISTS = {"review": "review", "security": "security", "qa": "qa"}

# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #


def router_node(state: PRReviewState) -> dict[str, Any]:
    """Profile the PR and decide which specialists to activate."""
    repo_path = state["repo_path"]
    pr_number = state["pr_number"]

    signals = mcp_tools.analyze_pr_signals(repo_path, pr_number)

    files: list[dict[str, Any]] = []
    for page in range(1, config.MAX_FILES_PAGES + 1):
        chunk = mcp_tools.get_pr_diff(repo_path, pr_number, page=page)["files"]
        files.extend(chunk)
        if len(chunk) < 30:
            break

    # Blast-radius scope: reviewers/sub-agents consume this bounded
    # context instead of scanning the whole repo (token/time guardrail).
    pr_context = scope.build_pr_context(
        repo_path, files, signals.get("diff_text", "")
    )

    return {
        "pr_title": signals.get("title", ""),
        "pr_body": signals.get("pr_body", ""),
        "head_sha": signals.get("head_sha", ""),
        "base_ref": signals.get("base_ref", ""),
        "files": files,
        "signals": signals,
        "pr_context": pr_context,
        "agents_to_run": agents.choose_agents(signals, files),
    }


def route_specialists(state: PRReviewState) -> list[str]:
    """Fan-out: return the node names selected by the router."""
    return [SPECIALISTS[a] for a in state.get("agents_to_run", ["review"])
            if a in SPECIALISTS] or ["review"]


def review_node(state: PRReviewState) -> dict[str, Any]:
    try:
        return agents.review_specialist(state)
    except Exception as exc:  # noqa: BLE001 - specialists must not kill the run
        return {"errors": [f"review: {exc}"]}


def security_node(state: PRReviewState) -> dict[str, Any]:
    try:
        return agents.security_specialist(state)
    except Exception as exc:  # noqa: BLE001
        return {"errors": [f"security: {exc}"]}


def qa_node(state: PRReviewState) -> dict[str, Any]:
    try:
        return agents.qa_specialist(state)
    except Exception as exc:  # noqa: BLE001
        return {"errors": [f"qa: {exc}"]}


def synthesizer_node(state: PRReviewState) -> dict[str, Any]:
    """Consolidate findings into a verdict and publish it (unless dry-run)."""
    findings = state.get("findings", [])
    blockers = [f for f in findings if f["severity"] == "blocker"]
    approve = not blockers

    checks = [
        {
            "name": f["title"],
            "severity": f["severity"],
            "passed": f["severity"] != "blocker",
            "detail": f"[{f['agent']}] {f['detail']}",
        }
        for f in findings
    ]
    if state.get("errors"):
        checks.append({
            "name": "Internal errors",
            "severity": "blocker",
            "passed": False,
            "detail": "; ".join(state["errors"]),
        })
        approve = False

    summary = (
        f"{len(findings)} finding(s): {len(blockers)} blocker(s), "
        f"{sum(1 for f in findings if f['severity'] == 'warning')} warning(s), "
        f"{sum(1 for f in findings if f['severity'] == 'info')} info."
    )
    # Optional orchestrator (harness role) summary — recorded in
    # llm_calls BEFORE the report is built so it persists in metrics.
    harness = agents.harness_summarize(state, findings, approve)
    merged = {
        **state,
        "llm_calls": state.get("llm_calls", []) + harness["llm_calls"],
    }
    report = metrics.summarize(merged)
    verdict = {
        "approve": approve,
        "checks": checks,
        "summary": summary,
        "metrics": report,
    }
    if harness["text"]:
        verdict["llm_summary"] = harness["text"]
        summary = f"{summary} {harness['text']}"

    update: dict[str, Any] = {
        "verdict": verdict,
        "metrics": report,
        "llm_calls": harness["llm_calls"],
        "active_pgids": harness["active_pgids"],
    }
    if state.get("dry_run"):
        update["posted"] = {"action": "dry_run", "url": "", "head_sha": state.get("head_sha", "")[:7]}
    else:
        posted = mcp_tools.post_verdict(
            state["repo_path"], state["pr_number"], approve, checks,
            result=summary,
            note=f"PR #{state['pr_number']} reviewed automatically by quality-gate.",
            metrics_line=metrics.summary_line(report),
        )
        update["posted"] = posted
    return update


def cleanup_node(state: PRReviewState) -> dict[str, Any]:
    """MANDATORY terminal node: kill every registered process group."""
    report = cleanup.terminate_pgids(state.get("active_pgids", []))
    logger.info("cleanup report: %s", report)
    return {"cleanup_report": report}


# --------------------------------------------------------------------------- #
# Graph construction
# --------------------------------------------------------------------------- #


def _timed(name: str, fn: Any) -> Any:
    """Wrap a node so its wall-clock duration lands in metrics state."""

    @functools.wraps(fn)
    def wrapper(state: PRReviewState) -> dict[str, Any]:
        started = time.monotonic()
        update = fn(state) or {}
        durations = list(update.get("node_durations") or [])
        durations.append(metrics.node_duration(name, started))
        update["node_durations"] = durations
        return update

    return wrapper


def build_graph(checkpointer: Any | None = None) -> Any:
    g = StateGraph(PRReviewState)

    g.add_node("router", _timed("router", router_node))
    g.add_node("review", _timed("review", review_node))
    g.add_node("security", _timed("security", security_node))
    g.add_node("qa", _timed("qa", qa_node))
    g.add_node("synthesizer", _timed("synthesizer", synthesizer_node))
    g.add_node("cleanup", _timed("cleanup", cleanup_node))

    g.add_edge(START, "router")
    g.add_conditional_edges("router", route_specialists, list(SPECIALISTS.values()))
    for name in SPECIALISTS.values():
        g.add_edge(name, "synthesizer")
    g.add_edge("synthesizer", "cleanup")
    g.add_edge("cleanup", END)

    return g.compile(checkpointer=checkpointer)


# --------------------------------------------------------------------------- #
# Runner (resumable via Postgres checkpointer when DATABASE_URL is set)
# --------------------------------------------------------------------------- #


def _thread_id(repo_path: str, pr_number: int) -> str:
    slug = repo_path.rstrip("/").rsplit("/", 1)[-1]
    return f"pr-review/{slug}/{pr_number}"


def _invoke(graph: Any, state: PRReviewState, run_config: dict[str, Any]) -> dict[str, Any]:
    try:
        return graph.invoke(state, config=run_config)
    finally:
        # Safety net: even if a node raised mid-run, terminate whatever
        # pgids reached the checkpointed state.
        try:
            snapshot = graph.get_state(run_config)
            pgids = snapshot.values.get("active_pgids", []) if snapshot else []
            if pgids:
                report = cleanup.terminate_pgids(pgids)
                logger.info("finally-cleanup report: %s", report)
        except Exception:  # noqa: BLE001 - cleanup must never mask the real error
            logger.exception("finally-cleanup failed")


def run_pr_review(repo_path: str, pr_number: int, dry_run: bool = False) -> dict[str, Any]:
    """Execute the review graph for one PR and return the final state."""
    initial: PRReviewState = {
        "repo_path": repo_path,
        "pr_number": pr_number,
        "dry_run": dry_run,
        "findings": [],
        "active_pgids": [],
        "errors": [],
        "started_at": time.monotonic(),
        "node_durations": [],
        "llm_calls": [],
        "processes": [],
    }
    run_config = {"configurable": {"thread_id": _thread_id(repo_path, pr_number)}}

    if config.DATABASE_URL:
        from langgraph.checkpoint.postgres import PostgresSaver

        with PostgresSaver.from_conn_string(config.DATABASE_URL) as checkpointer:
            checkpointer.setup()
            final = _invoke(build_graph(checkpointer), initial, run_config)
    else:
        from langgraph.checkpoint.memory import InMemorySaver

        final = _invoke(build_graph(InMemorySaver()), initial, run_config)

    # FR-O18: persist task telemetry; best-effort, never raises.
    metrics.persist_run(
        final, run_id=run_config["configurable"]["thread_id"]
    )
    return final

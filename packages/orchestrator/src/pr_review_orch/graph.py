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

from quality_gate_mcp import git_ops, tools as mcp_tools

from . import agents, cleanup, config, logs, metrics, scope
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


def _dedup_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse identical (agent, title, file) findings into one (FR-C10).

    Keeps the highest severity across duplicates and appends the
    occurrence count so repeated signal hits (e.g. six identical
    secret-candidate warnings on one test file) render once.
    """
    rank = {"info": 0, "warning": 1, "blocker": 2}
    merged: dict[tuple[str, str, Any], dict[str, Any]] = {}
    order: list[tuple[str, str, Any]] = []
    for f in findings:
        key = (f.get("agent", ""), f.get("title", ""), f.get("file"))
        if key not in merged:
            merged[key] = {**f, "_count": 1}
            order.append(key)
            continue
        cur = merged[key]
        cur["_count"] += 1
        if rank.get(f.get("severity"), 0) > rank.get(cur.get("severity"), 0):
            cur["severity"] = f["severity"]
            cur["detail"] = f["detail"]
    out = []
    for key in order:
        f = merged[key]
        n = f.pop("_count")
        if n > 1:
            f = {**f, "detail": f"{f['detail']} — {n} similar findings"}
        out.append(f)
    return out


def synthesizer_node(state: PRReviewState) -> dict[str, Any]:
    """Consolidate findings into a verdict and publish it (unless dry-run)."""
    findings = _dedup_findings(state.get("findings", []))
    blockers = [f for f in findings if f["severity"] == "blocker"]
    approve = not blockers

    checks = [
        {
            "name": f["title"],
            "severity": f.get("severity"),
            "passed": f.get("severity") != "blocker",
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
            metrics_detail=metrics.detail_block(report),
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


def _thread_id(repo_slug: str, pr_number: int) -> str:
    """Checkpoint thread id keyed by repo identity, not by directory.

    FR-P1: in service mode ``repo_path`` is an ephemeral ``qg-*`` worktree,
    so deriving the slug from the path made every run a new thread and
    broke resumability (NFR-3). The slug travels explicitly instead.
    """
    return f"pr-review/{repo_slug}/{pr_number}"


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


def run_pr_review(repo_path: str, pr_number: int, dry_run: bool = False,
                  repo_slug: str | None = None) -> dict[str, Any]:
    """Execute the review graph for one PR and return the final state.

    ``repo_slug`` is the ``owner/repo`` identity used for the checkpoint
    thread id and run metrics; when omitted it is resolved from the
    checkout's ``origin`` remote (CLI/Action path). The ingress passes
    the slug from the webhook payload so ephemeral worktrees keep a
    stable identity (FR-P1).
    """
    slug = repo_slug or git_ops.get_remote_slug(repo_path)
    initial: PRReviewState = {
        "repo_path": repo_path,
        "repo_slug": slug,
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
    run_config = {"configurable": {"thread_id": _thread_id(slug, pr_number)}}
    run_id = run_config["configurable"]["thread_id"]
    # FR-P15: correlate run logs with the run_metrics key (contextvar
    # already set by the ingress drain — this also covers CLI/Action).
    logs.run_id_var.set(run_id)

    try:
        if config.DATABASE_URL:
            from langgraph.checkpoint.postgres import PostgresSaver

            with PostgresSaver.from_conn_string(
                    config.DATABASE_URL) as checkpointer:
                checkpointer.setup()
                final = _invoke(build_graph(checkpointer), initial, run_config)
        else:
            from langgraph.checkpoint.memory import InMemorySaver

            final = _invoke(build_graph(InMemorySaver()), initial, run_config)

        # FR-O18: persist task telemetry; best-effort, never raises.
        metrics.persist_run(final, run_id=run_id)
    except Exception as exc:
        # FR-P2: a crashed run still leaves a run_metrics row —
        # status=error|timeout plus the exception summary, so "the
        # review never ran" is a query, not a mystery.
        metrics.persist_run(
            initial, run_id=run_id,
            status="timeout" if isinstance(exc, TimeoutError) else "error",
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
        raise
    return final

"""LangGraph state schema.

Fields merged across parallel specialist nodes (``findings``,
``active_pgids``, ``errors``) use an ``operator.add`` reducer so fan-out
branches can contribute without clobbering each other.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class PRReviewState(TypedDict, total=False):
    # Run inputs, provided by the CLI entry point (env vars / args)
    repo_path: str
    repo_slug: str  # owner/repo identity: thread id + run metrics (FR-P1)
    pr_number: int
    dry_run: bool

    # Populated by router_node
    pr_title: str
    pr_body: str
    head_sha: str
    base_ref: str
    files: list[dict[str, Any]]
    signals: dict[str, Any]
    agents_to_run: list[str]
    # Scoped blast-radius context: diff + changed files + direct
    # dependents; sub-agents must review this, not the whole repo.
    pr_context: dict[str, Any]

    # Written by specialist nodes; add-reducer merges parallel branches
    findings: Annotated[list[dict[str, Any]], operator.add]
    active_pgids: Annotated[list[int], operator.add]
    errors: Annotated[list[str], operator.add]

    # Observability: timing/usage entries merged across parallel branches
    started_at: float
    node_durations: Annotated[list[dict[str, Any]], operator.add]
    llm_calls: Annotated[list[dict[str, Any]], operator.add]
    processes: Annotated[list[dict[str, Any]], operator.add]
    metrics: dict[str, Any]  # aggregate report set by synthesizer_node

    # Populated by synthesizer_node
    verdict: dict[str, Any]
    posted: dict[str, Any]

    # Populated by cleanup_node
    cleanup_report: dict[str, Any]

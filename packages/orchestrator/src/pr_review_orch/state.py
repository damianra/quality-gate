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

    # Written by specialist nodes; add-reducer merges parallel branches
    findings: Annotated[list[dict[str, Any]], operator.add]
    active_pgids: Annotated[list[int], operator.add]
    errors: Annotated[list[str], operator.add]

    # Populated by synthesizer_node
    verdict: dict[str, Any]
    posted: dict[str, Any]

    # Populated by cleanup_node
    cleanup_report: dict[str, Any]

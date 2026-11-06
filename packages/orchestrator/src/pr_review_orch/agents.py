"""Specialist agents: prompts + deterministic analysis.

Each specialist consumes ``signals`` (from ``analyze_pr_signals``) and the
PR file list, emits structured ``findings``, and may spawn validation
processes whose pgids are appended to ``active_pgids`` for the cleanup node.

Findings schema::

    {"agent": "review|security|qa",
     "severity": "info|warning|blocker",
     "title": str, "detail": str, "file": str | None}
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from quality_gate_mcp import tools as mcp_tools

from . import config

# --------------------------------------------------------------------------- #
# Prompts (document the specialist contract; usable by an LLM enrichment later)
# --------------------------------------------------------------------------- #

REVIEW_PROMPT = """You are ReviewAgent. Evaluate the PR against the repo's
AGENTS.md rules, detect vibecoding (whitespace noise, commented-out blocks,
TODOs, oversized diffs) and verify scope fidelity (files changed vs. files
claimed in the PR body). Report findings only — never modify code."""

SECURITY_PROMPT = """You are SecurityAgent. Hunt for hardcoded secrets,
suspicious new dependencies, and churn on sensitive configuration files
(CI workflows, env files, build config). Any leaked credential is a blocker."""

QA_PROMPT = """You are QAAgent. Validate structural soundness: TypeScript
typecheck (tsc --noEmit) or syntax check (node --check) when applicable.
Every spawned process MUST register its pgid in active_pgids for cleanup."""

# --------------------------------------------------------------------------- #
# Router helpers
# --------------------------------------------------------------------------- #


def choose_agents(signals: dict[str, Any], files: list[dict[str, Any]]) -> list[str]:
    """Decide which specialists the router activates. Review always runs."""
    agents = ["review"]
    if (
        signals.get("secret_candidates")
        or signals.get("new_dependencies")
        or signals.get("config_churn")
    ):
        agents.append("security")
    code_exts = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
    if any(Path(f["filename"]).suffix in code_exts for f in files):
        agents.append("qa")
    return agents


def _finding(agent: str, severity: str, title: str, detail: str,
             file: str | None = None) -> dict[str, Any]:
    return {"agent": agent, "severity": severity, "title": title,
            "detail": detail, "file": file}


# --------------------------------------------------------------------------- #
# ReviewAgent — AGENTS.md rules, vibecoding, scope fidelity
# --------------------------------------------------------------------------- #


def review_specialist(state: dict[str, Any]) -> dict[str, Any]:
    s = state.get("signals", {})
    findings: list[dict[str, Any]] = []

    # AGENTS.md presence (rules are advisory; we just note it)
    agents_md = Path(state["repo_path"]) / "AGENTS.md"
    if not agents_md.exists():
        findings.append(_finding(
            "review", "info", "AGENTS.md missing",
            "Repository has no AGENTS.md; only generic review rules applied."))

    wr = s.get("whitespace_ratio", 0.0)
    if wr >= 0.6 and s.get("lines_changed", 0) > 20:
        findings.append(_finding(
            "review", "warning", "Whitespace-dominated diff",
            f"{wr:.0%} of changed lines are whitespace-only; likely editor "
            "reformatting noise, not intentional changes."))
    elif wr >= 0.3 and s.get("lines_changed", 0) > 50:
        findings.append(_finding(
            "review", "info", "Notable whitespace churn",
            f"{wr:.0%} of the diff is whitespace-only edits."))

    if s.get("large_diff"):
        findings.append(_finding(
            "review", "warning", "Oversized PR",
            f"{s.get('files_changed')} files / {s.get('lines_changed')} lines "
            "changed. Consider splitting into focused PRs."))

    for block in s.get("commented_blocks", []):
        findings.append(_finding(
            "review", "warning", "Commented-out code block",
            f"{block['lines']} consecutive comment-only lines added "
            f"(e.g. {block['sample']!r}). Dead code should be deleted, not commented.",
            file=block["file"]))

    for todo in s.get("todos_added", []):
        findings.append(_finding(
            "review", "info", "TODO/FIXME added", todo[:120]))

    for f in s.get("files_outside_hint", []):
        findings.append(_finding(
            "review", "warning", "File outside declared scope",
            f"{f} is not mentioned by any path hinted in the PR body.",
            file=f))

    if s.get("debug_leftovers"):
        findings.append(_finding(
            "review", "warning", "Debug leftovers",
            f"{len(s['debug_leftovers'])} console.log/debugger statement(s) added."))

    return {"findings": findings}


# --------------------------------------------------------------------------- #
# SecurityAgent — secrets, dependencies, sensitive config churn
# --------------------------------------------------------------------------- #


def security_specialist(state: dict[str, Any]) -> dict[str, Any]:
    s = state.get("signals", {})
    findings: list[dict[str, Any]] = []

    for line in s.get("secret_candidates", []):
        findings.append(_finding(
            "security", "blocker", "Possible hardcoded secret",
            f"Added line matches secret pattern: {line[:120]}"))

    for dep in s.get("new_dependencies", []):
        findings.append(_finding(
            "security", "warning", "New dependency",
            f"{dep} added to package.json — verify provenance and license.",
            file="package.json"))

    for f in s.get("config_churn", []):
        findings.append(_finding(
            "security", "warning", "Sensitive config modified",
            f"{f} is on the sensitive-config watchlist.", file=f))

    return {"findings": findings}


# --------------------------------------------------------------------------- #
# QAAgent — typecheck / syntax validation via spawn_process
# --------------------------------------------------------------------------- #


def _qa_commands(repo: Path, files: list[str]) -> list[list[str]]:
    """Pick the cheapest available validation commands for this repo."""
    cmds: list[list[str]] = []
    local_tsc = repo / "node_modules" / ".bin" / "tsc"
    if local_tsc.exists() and (repo / "tsconfig.json").exists():
        cmds.append([str(local_tsc), "--noEmit"])
    elif shutil.which("node"):
        js_files = [f for f in files if f.endswith((".js", ".jsx", ".mjs", ".cjs"))][:20]
        for f in js_files:
            if (repo / f).exists():
                cmds.append(["node", "--check", f])
    return cmds


def qa_specialist(state: dict[str, Any]) -> dict[str, Any]:
    repo = Path(state["repo_path"])
    files = [f["filename"] for f in state.get("files", [])]
    findings: list[dict[str, Any]] = []
    pgids: list[int] = []

    commands = _qa_commands(repo, files)
    if not commands:
        findings.append(_finding(
            "qa", "info", "No validators available",
            "Neither a local tsc nor node is available; structural checks skipped."))
        return {"findings": findings, "active_pgids": pgids}

    failures = 0
    for cmd in commands:
        # spawn_process starts a NEW process group; the pgid is registered
        # unconditionally so the cleanup node can terminate stragglers.
        res = mcp_tools.spawn_process(
            str(repo), cmd[0], cmd[1:], timeout=config.QA_PROCESS_TIMEOUT
        )
        if res["pgid"] > 0:
            pgids.append(res["pgid"])
        if res["timed_out"]:
            failures += 1
            findings.append(_finding(
                "qa", "warning", "Validation timed out",
                f"{' '.join(cmd)} exceeded {config.QA_PROCESS_TIMEOUT}s."))
        elif res["exit_code"] == 127:
            findings.append(_finding(
                "qa", "info", "Validator missing",
                f"Command not found: {cmd[0]}"))
            break
        elif res["exit_code"] != 0:
            failures += 1
            output = (res["stderr"] or res["stdout"]).strip().splitlines()
            findings.append(_finding(
                "qa", "blocker", "Validation failed",
                f"`{' '.join(cmd)}` exited {res['exit_code']}: "
                f"{output[0][:200] if output else 'no output'}"))

    if not failures:
        findings.append(_finding(
            "qa", "info", "Validation passed",
            f"{len(commands)} check(s) completed cleanly "
            f"({', '.join(c[0] for c in commands)})."))

    return {"findings": findings, "active_pgids": pgids}

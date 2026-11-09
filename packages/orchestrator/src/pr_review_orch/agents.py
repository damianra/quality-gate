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

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from quality_gate_mcp import tools as mcp_tools

from . import config, llm

logger = logging.getLogger("pr-review-orch")

# --------------------------------------------------------------------------- #
# Prompts — REVIEW/SECURITY double as system prompts for the LLM
# enrichment (FR-O16); QA_PROMPT documents the deterministic contract.
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
# LLM enrichment (FR-O16)
#
# Optional second pass over the bounded ``pr_context``. The deterministic
# pass always runs; the LLM pass is additive: each specialist calls its own
# role config (``QG_LLM_*_<ROLE>`` with base fallback), merges the model's
# structured findings, and records the call in ``llm_calls``. Provider
# errors, token-cap truncation and unparseable output degrade to an info
# note — LLM failure alone never rejects a PR.
# --------------------------------------------------------------------------- #

_LLM_OUTPUT_CONTRACT = (
    "Respond with ONLY a JSON array of findings — no prose, no markdown "
    "fences: "
    '[{"severity": "info|warning|blocker", "title": str, "detail": str, '
    '"file": str|null}]. At most 10 findings; "blocker" means must-fix '
    "before merge. Judge only the supplied context."
)

_LLM_SEVERITIES = {"info", "warning", "blocker"}
_LLM_MAX_FINDINGS = 10


def _llm_user_message(state: dict[str, Any]) -> str:
    """User message for specialist LLM calls: signals + bounded diff."""
    ctx = state.get("pr_context") or {}
    signals = state.get("signals") or {}
    signal_keys = (
        "files_changed", "lines_changed", "whitespace_ratio", "large_diff",
        "todos_added", "debug_leftovers", "secret_candidates",
        "new_dependencies", "config_churn",
    )
    parts = [
        f"PR #{state.get('pr_number')}: {state.get('pr_title', '')}",
        f"base: {state.get('base_ref', '')} "
        f"head: {str(state.get('head_sha', ''))[:7]}",
        "",
        "## Deterministic signals",
        json.dumps({k: signals[k] for k in signal_keys if signals.get(k)}),
        "",
        "## Changed files",
        "\n".join(ctx.get("changed_files") or []),
        "",
        ("## Diff (truncated to scope budget)"
         if ctx.get("truncated") else "## Diff"),
        ctx.get("diff_text") or "(empty)",
    ]
    return "\n".join(parts)


def _llm_findings(agent: str, text: str) -> list[dict[str, Any]]:
    """Parse the model's JSON findings into the findings schema.

    Tolerant extraction: the first ``[`` .. last ``]`` slice is parsed, so
    a chatty preamble does not sink the whole response. Severities outside
    ``info|warning|blocker`` are clamped to ``info``.
    """
    try:
        start = text.index("[")
        end = text.rindex("]") + 1
        data = json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        return []
    out: list[dict[str, Any]] = []
    for item in (data if isinstance(data, list) else [])[:_LLM_MAX_FINDINGS]:
        if not isinstance(item, dict):
            continue
        sev = str(item.get("severity", "info")).lower()
        title = str(item.get("title") or "LLM finding").strip() or "LLM finding"
        out.append(_finding(
            agent,
            sev if sev in _LLM_SEVERITIES else "info",
            f"LLM: {title[:80]}",
            str(item.get("detail", ""))[:500],
            file=item["file"] if isinstance(item.get("file"), str) else None,
        ))
    return out


def _call_record(
    cfg: llm.LLMConfig, node: str, status: str,
    meta: dict[str, Any] | None = None, error: str | None = None,
) -> dict[str, Any]:
    """Build one ``llm_calls`` entry (shape consumed by metrics._call_rows)."""
    meta = meta or {}
    rec: dict[str, Any] = {
        "role": cfg.role or node,
        "node": node,
        "provider": cfg.provider,
        "model": cfg.model,
        "duration_s": meta.get("duration_s", 0.0),
        "usage": meta.get(
            "usage", {"prompt_tokens": 0, "completion_tokens": 0}
        ),
        "finish_reason": meta.get("finish_reason"),
        "truncated": bool(meta.get("truncated")),
        "status": status,
    }
    if error:
        rec["error"] = error[:300]
    if meta.get("text"):
        rec["raw"] = {"text": meta["text"][:1000]}
    return rec


def _llm_enrich(
    agent: str, system_prompt: str, state: dict[str, Any]
) -> dict[str, Any]:
    """Optional LLM pass for one specialist (FR-O16).

    Returns ``{"findings", "llm_calls", "active_pgids"}`` — all
    add-reducer fields. Unconfigured/disabled roles are a no-op so the
    specialist stays deterministic; any call failure degrades to an info
    note instead of a rejection.
    """
    out: dict[str, Any] = {"findings": [], "llm_calls": [], "active_pgids": []}
    try:
        cfg = llm.LLMConfig.for_role(agent)
    except llm.LLMConfigError as exc:
        out["findings"] = [_finding(
            agent, "info", "LLM enrichment unavailable",
            f"role config: {exc}")]
        return out
    if not cfg.enabled:
        return out

    try:
        meta = cfg.complete_with_meta(
            f"{system_prompt}\n\n{_LLM_OUTPUT_CONTRACT}",
            _llm_user_message(state),
            cwd=state.get("repo_path"),
        )
    except Exception as exc:  # noqa: BLE001 - LLM failure must not reject
        logger.warning("%s LLM call failed: %s", agent, exc)
        out["findings"] = [_finding(
            agent, "info", "LLM enrichment unavailable",
            f"{cfg.provider}/{cfg.model} call failed; "
            "deterministic findings only.")]
        out["llm_calls"] = [
            _call_record(cfg, agent, "error", error=str(exc))
        ]
        return out

    pgids = [meta["pgid"]] if meta.get("pgid") else []
    if meta.get("truncated"):
        out["findings"] = [_finding(
            agent, "info", "LLM enrichment truncated",
            f"{cfg.provider}/{cfg.model} hit the token cap; "
            "deterministic findings only.")]
        out["llm_calls"] = [_call_record(cfg, agent, "truncated", meta)]
        out["active_pgids"] = pgids
        return out

    found = _llm_findings(agent, meta.get("text") or "")
    if not found:
        out["findings"] = [_finding(
            agent, "info", "LLM enrichment unparseable",
            "Model output was not a valid findings JSON array.")]
        out["llm_calls"] = [_call_record(cfg, agent, "unparseable", meta)]
        out["active_pgids"] = pgids
        return out

    out["findings"] = found
    out["llm_calls"] = [_call_record(cfg, agent, "ok", meta)]
    out["active_pgids"] = pgids
    return out


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

    # FR-O16: optional LLM pass with the review role config; degrades to
    # info notes on any failure — never removes deterministic findings.
    enrich = _llm_enrich("review", REVIEW_PROMPT, state)
    findings += enrich["findings"]
    return {
        "findings": findings,
        "llm_calls": enrich["llm_calls"],
        "active_pgids": enrich["active_pgids"],
    }


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

    # FR-O16: optional LLM pass with the security role config; degrades to
    # info notes on any failure — never removes deterministic findings.
    enrich = _llm_enrich("security", SECURITY_PROMPT, state)
    findings += enrich["findings"]
    return {
        "findings": findings,
        "llm_calls": enrich["llm_calls"],
        "active_pgids": enrich["active_pgids"],
    }


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
    proc_metrics: list[dict[str, Any]] = []

    commands = _qa_commands(repo, files)
    if not commands:
        findings.append(_finding(
            "qa", "info", "No validators available",
            "Neither a local tsc nor node is available; structural checks skipped."))
        return {"findings": findings, "active_pgids": pgids, "processes": proc_metrics}

    failures = 0
    for cmd in commands:
        # spawn_process starts a NEW process group; the pgid is registered
        # unconditionally so the cleanup node can terminate stragglers.
        res = mcp_tools.spawn_process(
            str(repo), cmd[0], cmd[1:], timeout=config.QA_PROCESS_TIMEOUT
        )
        if res["pgid"] > 0:
            pgids.append(res["pgid"])
        proc_metrics.append({
            "agent": "qa",
            "command": " ".join(cmd),
            "duration_s": res.get("duration_s", 0.0),
            "exit_code": res["exit_code"],
            "timed_out": res["timed_out"],
        })
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

    return {"findings": findings, "active_pgids": pgids, "processes": proc_metrics}

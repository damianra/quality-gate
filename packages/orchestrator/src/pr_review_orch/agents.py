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
import re
import shutil
from pathlib import Path
from typing import Any

from quality_gate_mcp import tools as mcp_tools

from . import config, llm

logger = logging.getLogger("pr-review-orch")

# --------------------------------------------------------------------------- #
# Prompts — REVIEW/SECURITY double as system prompts for the LLM
# enrichment (FR-O16); all three feed _llm_enrich's system prompt.
# --------------------------------------------------------------------------- #

REVIEW_PROMPT = """You are ReviewAgent. Evaluate the PR against the repo's
AGENTS.md rules, detect vibecoding (whitespace noise, commented-out blocks,
TODOs, oversized diffs) and verify scope fidelity (files changed vs. files
claimed in the PR body). Report findings only — never modify code."""

SECURITY_PROMPT = """You are SecurityAgent. Hunt for hardcoded secrets,
suspicious new dependencies, and churn on sensitive configuration files
(CI workflows, env files, build config). Any leaked credential is a blocker."""

QA_PROMPT = """You are QAAgent. Analyze the diff for validation gaps:
missing or broken tests, untested edge cases, assertions that no longer
match the changed behavior, and error paths nobody exercises. Report
findings only — never modify code."""

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
    '"file": str|null}]. At most 10 findings; "blocker" means a real '
    "defect that must be fixed before merge (correctness, security, "
    "broken behavior) — missing tests, coverage gaps, style and "
    "suggestions are at most \"warning\". Judge only the supplied "
    "context."
)

_LLM_SEVERITIES = {"info", "warning", "blocker"}
_LLM_MAX_FINDINGS = 10


def _llm_user_message(state: dict[str, Any]) -> str:
    """User message for specialist LLM calls: signals + bounded evidence.

    FR-O23: besides the diff, ships the full contents of changed files
    and the dependent names gathered by ``build_pr_context``. Every
    evidence block has its own char cap (``QG_PROMPT_MAX_DIFF_CHARS``,
    ``QG_PROMPT_MAX_FILES_CHARS``) so oversized PRs still fit the
    deployed model context; truncated sections are flagged.
    """
    ctx = state.get("pr_context") or {}
    signals = state.get("signals") or {}
    signal_keys = (
        "files_changed", "lines_changed", "whitespace_ratio", "large_diff",
        "todos_added", "debug_leftovers", "secret_candidates",
        "new_dependencies", "config_churn",
    )

    raw_diff = ctx.get("diff_text") or "(empty)"
    diff = raw_diff[: config.PROMPT_MAX_DIFF_CHARS]
    diff_cut = ctx.get("truncated") or len(diff) < len(raw_diff)

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
        ("## Diff (truncated to scope/prompt budget)"
         if diff_cut else "## Diff"),
        diff,
    ]

    contents = ctx.get("file_contents") or {}
    if contents:
        parts += ["", "## File contents"]
        budget = config.PROMPT_MAX_FILES_CHARS
        omitted = 0
        files_cut = False
        for rel, text in contents.items():
            if budget <= 0:
                omitted += 1
                continue
            if len(text) > budget:
                text = text[:budget]
                files_cut = True
            budget -= len(text)
            parts += [f"### {rel}", "```", text, "```"]
        if omitted or files_cut:
            parts.append(
                f"(file contents truncated to prompt budget"
                + (f"; {omitted} file(s) omitted)" if omitted else ")"))

    related = ctx.get("related_files") or []
    if related:
        parts += [
            "", "## Related files (dependents, not shown)",
            "\n".join(related[:50]),
        ]

    return "\n".join(parts)


def _llm_findings(agent: str, text: str) -> list[dict[str, Any]]:
    """Parse the model's JSON findings into the findings schema.

    Tolerant extraction: a strict whole-body parse first (json-mode
    output may be a bare array, a ``{"findings": [...]}`` wrapper or a
    single finding dict), then the ``[`` .. ``]`` slice for prose with
    an embedded array. Severities outside ``info|warning|blocker`` are
    clamped to ``info``.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if data is None:
        try:
            start = text.index("[")
            end = text.rindex("]") + 1
            data = json.loads(text[start:end])
        except (ValueError, json.JSONDecodeError):
            return []
    if isinstance(data, dict):
        # Wrapper object -> explicit "findings" list; a bare finding
        # dict ({severity, title, ...}) wraps itself; last resort is
        # the first list value for other wrapper key names.
        if isinstance(data.get("findings"), list):
            data = data["findings"]
        elif "severity" in data or "title" in data:
            data = [data]
        else:
            data = next(
                (v for v in data.values() if isinstance(v, list)), []
            )
    out: list[dict[str, Any]] = []
    for item in (data if isinstance(data, list) else []):
        if len(out) >= _LLM_MAX_FINDINGS:
            break
        if not isinstance(item, dict):
            continue
        # A finding must carry content. Foreign schemas (e.g. the model
        # echoing {"code_changes": [{"file": ..., "changes": [...}]} for
        # a code-task PR) reach here via the "first list value" wrapper
        # fallback and would render as empty "LLM finding" noise —
        # skipping them lets the caller degrade to "unparseable" instead.
        title = str(
            item.get("title") or item.get("summary") or item.get("issue") or ""
        ).strip()
        detail = str(
            item.get("detail") or item.get("description")
            or item.get("message") or item.get("finding") or ""
        ).strip()
        if not title and not detail:
            continue
        sev = str(item.get("severity", "info")).lower()
        out.append(_finding(
            agent,
            sev if sev in _LLM_SEVERITIES else "info",
            f"LLM: {(title or detail)[:80]}",
            detail[:500],
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
    # FR-T2 telemetry: perf/RSS/provider footprint — absent keys mean
    # "provider did not report", never zero-cost.
    for key in ("perf", "memory", "provider_stats"):
        if meta.get(key) is not None:
            rec[key] = meta[key]
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

    found = _llm_findings(agent, _strip_think(meta.get("text") or ""))
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
# Harness (orchestrator) summarization
# --------------------------------------------------------------------------- #

_HARNESS_PROMPT = """You are the quality-gate orchestrator. Based on the
specialist findings below, write a short executive summary of this PR
review verdict in English: overall risk, the main themes behind
blockers/warnings, and the recommended action. Maximum 3 sentences,
plain text, no markdown, no thinking tags."""

_HARNESS_MAX_FINDINGS = 8
_HARNESS_DETAIL_CHARS = 240

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_TAG_RE = re.compile(r"</?think>", re.IGNORECASE)


def _strip_think(text: str) -> str:
    """Remove reasoning blocks (``<think>…</think>``) from model output.

    Qwen-class reasoning models emit their chain-of-thought inside
    ``<think>`` tags; an unclosed tag (truncated output) is also dropped
    up to the first paragraph break so the summary stays clean prose.
    """
    text = _THINK_RE.sub("", text)
    if _THINK_TAG_RE.search(text):
        # Unclosed <think>: keep only what follows the reasoning section.
        tail = _THINK_TAG_RE.split(text)[-1]
        text = tail.split("\n\n", 1)[-1] if "\n\n" in tail else tail
    return "\n".join(
        line for line in text.splitlines() if not _THINK_TAG_RE.search(line)
    ).strip()


def _harness_user_message(
    state: dict[str, Any], findings: list[dict[str, Any]], approve: bool
) -> str:
    """Bounded digest of the verdict for the harness summary prompt."""
    rank = {"blocker": 0, "warning": 1, "info": 2}
    top = sorted(findings, key=lambda f: rank.get(f["severity"], 3))
    top = top[:_HARNESS_MAX_FINDINGS]
    counts = {s: sum(1 for f in findings if f["severity"] == s)
              for s in rank}
    lines = [
        f"PR: {state.get('pr_title', '(untitled)')}",
        f"Verdict: {'approved' if approve else 'changes required'}",
        f"Findings: {counts['blocker']} blocker, {counts['warning']} "
        f"warning, {counts['info']} info",
        "",
        "Top findings (specialist analysis):",
    ]
    for f in top:
        detail = (f.get("detail") or "").replace("\n", " ")
        lines.append(
            f"- [{f['severity']}][{f['agent']}] {f['title']}"
            + (f" — {detail[:_HARNESS_DETAIL_CHARS]}" if detail else "")
        )
    return "\n".join(lines)


def harness_summarize(
    state: dict[str, Any], findings: list[dict[str, Any]], approve: bool
) -> dict[str, Any]:
    """Optional orchestrator-role (harness) summary of the verdict.

    Returns ``{"text", "llm_calls", "active_pgids"}``. Unlike specialist
    enrichment this never emits findings — a failed call only records an
    ``error`` row so the attempt is still visible in metrics.
    """
    out: dict[str, Any] = {"text": "", "llm_calls": [], "active_pgids": []}
    try:
        cfg = llm.LLMConfig.for_role("harness")
    except llm.LLMConfigError:
        return out
    if not cfg.enabled:
        return out

    try:
        meta = cfg.complete_with_meta(
            _HARNESS_PROMPT,
            _harness_user_message(state, findings, approve),
            cwd=state.get("repo_path"),
        )
    except Exception as exc:  # noqa: BLE001 - harness failure is cosmetic
        logger.warning("harness LLM call failed: %s", exc)
        out["llm_calls"] = [
            _call_record(cfg, "synthesizer", "error", error=str(exc))
        ]
        return out

    if meta.get("pgid"):
        out["active_pgids"] = [meta["pgid"]]
    text = _strip_think(meta.get("text") or "")
    status = "truncated" if meta.get("truncated") else "ok"
    if not text:
        status = "unparseable"
    else:
        out["text"] = text
    out["llm_calls"] = [_call_record(cfg, "synthesizer", status, meta)]
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

#: Manifest/lockfile basenames whose diff triggers the FR-O24 audit.
_DEP_MANIFESTS = {
    "package.json", "package-lock.json", "npm-shrinkwrap.json",
    "yarn.lock", "pnpm-lock.yaml",
    "pyproject.toml", "uv.lock", "pipfile", "pipfile.lock",
    "go.mod", "go.sum", "cargo.toml", "cargo.lock",
    "gemfile", "gemfile.lock", "composer.json", "composer.lock",
}
_NPM_MANIFESTS = {
    "package.json", "package-lock.json", "npm-shrinkwrap.json",
    "yarn.lock", "pnpm-lock.yaml",
}
_PY_MANIFESTS = {"pyproject.toml", "uv.lock", "pipfile", "pipfile.lock"}

_MAX_AUDIT_FINDINGS = 10
_AUDIT_SEVERITIES = {"critical": "blocker", "high": "warning"}


def _is_dep_manifest(path: str) -> bool:
    name = Path(path).name.lower()
    return name in _DEP_MANIFESTS or (
        name.startswith("requirements") and name.endswith(".txt"))


def _audit_command(
    manifests: list[str], repo: Path
) -> tuple[list[str], Path] | None:
    """Pick the best available scanner + cwd for the changed manifests.

    npm/pip-audit audit the *PR's* dependencies, not the host's ambient
    environment: npm runs in the changed manifest's directory (a root
    run without a root lockfile exits with an error JSON that must not
    read as "clean"), pip-audit gets `-r` on each changed requirements
    file (it cannot audit pyproject/uv.lock manifests directly).
    """
    if shutil.which("osv-scanner"):
        return (["osv-scanner", "scan", "source", "-r",
                 "--format", "json", "."], repo)
    names = {Path(m).name.lower() for m in manifests}
    if names & _NPM_MANIFESTS and shutil.which("npm"):
        npm_manifest = next(
            m for m in manifests
            if Path(m).name.lower() in _NPM_MANIFESTS)
        return (["npm", "audit", "--omit=dev", "--json"],
                repo / Path(npm_manifest).parent)
    reqs = [
        m for m in manifests
        if Path(m).name.lower().startswith("requirements")
        and m.lower().endswith(".txt")]
    if reqs and shutil.which("pip-audit"):
        return (["pip-audit", "--format", "json",
                 *[f"-r{r}" for r in reqs]], repo)
    return None


def _parse_osv(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten osv-scanner results into {id, package, version, severity,
    fixed, summary}."""
    out: list[dict[str, Any]] = []
    for res in data.get("results") or []:
        for pkg in res.get("packages") or []:
            info = pkg.get("package") or {}
            for v in pkg.get("vulnerabilities") or []:
                sev = str(
                    (v.get("database_specific") or {}).get("severity", "")
                ).lower()
                fixed = ""
                for aff in v.get("affected") or []:
                    for rng in aff.get("ranges") or []:
                        for ev in rng.get("events") or []:
                            if ev.get("fixed"):
                                fixed = ev["fixed"]
                                break
                        if fixed:
                            break
                    if fixed:
                        break
                out.append({
                    "id": str(v.get("id", "")),
                    "package": str(info.get("name", "")),
                    "version": str(info.get("version", "")),
                    "severity": sev,
                    "fixed": fixed,
                    "summary": str(v.get("summary", "")).strip(),
                })
    return out


def _parse_npm_audit(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten `npm audit --json` (npm v7+) vulnerabilities."""
    out: list[dict[str, Any]] = []
    for name, v in (data.get("vulnerabilities") or {}).items():
        via = next(
            (x.get("title") for x in v.get("via") or []
             if isinstance(x, dict) and x.get("title")), "")
        fix = v.get("fixAvailable")
        fixed = fix.get("version", "") if isinstance(fix, dict) else ""
        out.append({
            "id": str(via.split(" ", 1)[0]) if via else "",
            "package": name,
            "version": str(v.get("range", "")),
            "severity": str(v.get("severity", "")).lower(),
            "fixed": fixed,
            "summary": str(via),
        })
    return out


def _parse_pip_audit(data: Any) -> list[dict[str, Any]]:
    """Flatten `pip-audit --format json` (list of dependency objects)."""
    out: list[dict[str, Any]] = []
    for dep in data if isinstance(data, list) else []:
        for v in dep.get("vulns") or []:
            fix = v.get("fix_versions") or []
            out.append({
                "id": str(v.get("id", "")),
                "package": str(dep.get("name", "")),
                "version": str(dep.get("version", "")),
                "severity": "",
                "fixed": fix[0] if fix else "",
                "summary": str(v.get("description", "")).strip()[:200],
            })
    return out


def _dependency_audit(repo: Path, changed: list[str]) -> dict[str, Any]:
    """Run a vuln scanner over the worktree (FR-O24).

    Deterministic, best-effort: absent/timeout/unparseable degrade to an
    info note — the audit never rejects a PR by itself beyond real
    critical CVEs (blocker) or high ones (warning).
    """
    out: dict[str, Any] = {"findings": [], "pgids": [], "processes": []}
    if not config.DEP_AUDIT_ENABLED:
        return out
    manifests = [f for f in changed if _is_dep_manifest(f)]
    if not manifests:
        return out
    sel = _audit_command(manifests, repo)
    if sel is None:
        out["findings"].append(_finding(
            "security", "info", "Dependency audit skipped",
            "Dependency manifest changed but no supported scanner on the "
            "host (osv-scanner / npm / pip-audit)."))
        return out
    cmd, cwd = sel

    res = mcp_tools.spawn_process(
        str(cwd), cmd[0], cmd[1:], timeout=config.AUDIT_PROCESS_TIMEOUT)
    if res["pgid"] > 0:
        out["pgids"].append(res["pgid"])
    out["processes"].append({
        "agent": "security",
        "command": " ".join(cmd),
        "duration_s": res.get("duration_s", 0.0),
        "exit_code": res["exit_code"],
        "timed_out": res["timed_out"],
    })
    if res["timed_out"]:
        out["findings"].append(_finding(
            "security", "info", "Dependency audit timed out",
            f"`{' '.join(cmd)}` exceeded "
            f"{config.AUDIT_PROCESS_TIMEOUT}s."))
        return out
    try:
        data = json.loads(res["stdout"] or "{}")
    except ValueError:
        out["findings"].append(_finding(
            "security", "info", "Dependency audit output unparseable",
            f"`{' '.join(cmd)}` exited {res['exit_code']} with non-JSON "
            "output."))
        return out
    if isinstance(data, dict) and data.get("error"):
        # e.g. npm audit against a dir without a lockfile returns
        # {"error": {...}} — a failure, NOT a clean bill.
        err = data["error"]
        out["findings"].append(_finding(
            "security", "info", "Dependency audit failed",
            f"`{' '.join(cmd)}`: {err.get('summary') or err.get('detail') or err}"))

    vulns = (
        _parse_osv(data) if cmd[0] == "osv-scanner"
        else _parse_npm_audit(data) if cmd[0] == "npm"
        else _parse_pip_audit(data)
    )
    if not vulns:
        out["findings"].append(_finding(
            "security", "info", "Dependency audit clean",
            f"`{cmd[0]}` reported no known vulnerabilities."))
        return out
    for v in vulns[:_MAX_AUDIT_FINDINGS]:
        sev = _AUDIT_SEVERITIES.get(v["severity"], "info")
        label = v["package"] + (
            f"@{v['version']}" if v["version"] else "")
        detail = (
            f"{v['id'] + ' — ' if v['id'] else ''}"
            f"{v['severity'] or 'unknown'} severity"
            + (f"; fixed in {v['fixed']}" if v["fixed"] else "")
            + (f". {v['summary'][:150]}" if v["summary"] else ""))
        out["findings"].append(_finding(
            "security", sev, f"Vulnerable dependency: {label}", detail))
    extra = len(vulns) - _MAX_AUDIT_FINDINGS
    if extra > 0:
        out["findings"].append(_finding(
            "security", "info", "More vulnerable dependencies",
            f"{extra} additional vulnerable package(s) omitted; "
            "run the scanner locally for the full list."))
    return out


# Test/fixture paths: secret-pattern hits here are almost always fixtures
# (e.g. `token: 't'`, `'fake-token'`), so they downgrade to warning.
_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|__mocks__|fixtures?|mocks?)(?:/|$)"
    r"|\.(?:test|spec)\.[a-z0-9]+$"
    r"|(?:^|/)test_[a-z0-9_]+\.[a-z0-9]+$",
    re.IGNORECASE,
)


def security_specialist(state: dict[str, Any]) -> dict[str, Any]:
    s = state.get("signals", {})
    findings: list[dict[str, Any]] = []

    for cand in s.get("secret_candidates", []):
        cand_file = cand.get("file")
        cand_line = cand.get("line", "")
        in_test = bool(cand_file) and _TEST_PATH_RE.search(cand_file)
        findings.append(_finding(
            "security",
            "warning" if in_test else "blocker",
            "Possible hardcoded secret" + (" (test file)" if in_test else ""),
            f"Added line matches secret pattern: {cand_line[:120]}",
            file=cand_file))

    for dep in s.get("new_dependencies", []):
        findings.append(_finding(
            "security", "warning", "New dependency",
            f"{dep} added to package.json — verify provenance and license.",
            file="package.json"))

    for f in s.get("config_churn", []):
        findings.append(_finding(
            "security", "warning", "Sensitive config modified",
            f"{f} is on the sensitive-config watchlist.", file=f))

    # FR-O24: deterministic vuln scan over the worktree when the diff
    # touched a dependency manifest/lockfile. Best-effort — degrades to
    # info notes when no scanner is installed or the output is unusable.
    audit = _dependency_audit(
        Path(state["repo_path"]),
        [f["filename"] for f in state.get("files", [])],
    )
    findings += audit["findings"]

    # FR-O16: optional LLM pass with the security role config; degrades to
    # info notes on any failure — never removes deterministic findings.
    enrich = _llm_enrich("security", SECURITY_PROMPT, state)
    findings += enrich["findings"]
    return {
        "findings": findings,
        "llm_calls": enrich["llm_calls"],
        "active_pgids": audit["pgids"] + enrich["active_pgids"],
        "processes": audit["processes"],
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
        # FR-O16: the LLM pass still runs — validation-gap analysis does
        # not depend on local validators being installed.
        enrich = _llm_enrich("qa", QA_PROMPT, state)
        return {
            "findings": findings + enrich["findings"],
            "active_pgids": pgids + enrich["active_pgids"],
            "processes": proc_metrics,
            "llm_calls": enrich["llm_calls"],
        }

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

    # FR-O16: optional LLM pass with the qa role config; degrades to
    # info notes on any failure — never removes deterministic findings.
    enrich = _llm_enrich("qa", QA_PROMPT, state)
    findings += enrich["findings"]
    pgids += enrich["active_pgids"]

    return {
        "findings": findings,
        "active_pgids": pgids,
        "processes": proc_metrics,
        "llm_calls": enrich["llm_calls"],
    }

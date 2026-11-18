"""FR-O28 functional stage — build/test/smoke steps inside the sandbox.

Step contract: ``quality-gate.yaml`` at the worktree root::

    steps:
      - name: unit-tests
        argv: [uv, run, pytest, packages, -q]
        timeout: 300        # optional, capped by QG_FUNC_TIMEOUT
        required: true      # failure -> blocker (default warning)
        net: true           # optional egress opt-in for this step

With no contract the stage autodetects per ecosystem (mirroring the
``_qa_commands`` detector pattern). Steps run inside the FR-O28 sandbox;
with no usable sandbox they fall back to the host only when PR-code
execution is allowed (``QG_EXEC_VALIDATORS``, FR-P12), otherwise the
stage degrades to an info note — false positives never hard-block.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from quality_gate_mcp import tools as mcp_tools

from . import config, sandbox

logger = logging.getLogger("pr-review-orch")

CONTRACT_NAME = "quality-gate.yaml"

#: Ecosystem autodetect — manifest basename -> test command.
_AUTODETECT = [
    ("pyproject.toml", ["uv", "run", "pytest"]),
    ("package.json", ["npm", "test"]),
    ("go.mod", ["go", "test", "./..."]),
    ("Cargo.toml", ["cargo", "test"]),
]


def _validate_step(raw: Any, index: int) -> dict[str, Any]:
    """Normalize one contract step; raises ValueError on malformed input."""
    if not isinstance(raw, dict):
        raise ValueError(f"step {index}: expected a mapping")
    argv = raw.get("argv")
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(a, str) and a for a in argv)):
        raise ValueError(f"step {index}: 'argv' must be a non-empty "
                         "list of strings")
    timeout = raw.get("timeout", config.FUNC_TIMEOUT)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError(f"step {index}: 'timeout' must be positive")
    return {
        "name": str(raw.get("name") or f"step-{index}"),
        "argv": [str(a) for a in argv],
        "timeout": min(int(timeout), config.FUNC_TIMEOUT),
        "required": bool(raw.get("required", False)),
        "net": bool(raw.get("net", False)),
    }


def load_contract(repo: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Read ``quality-gate.yaml``. Returns (steps, error) — error set when
    the file exists but is malformed (caller falls back to autodetect)."""
    path = repo / CONTRACT_NAME
    if not path.exists():
        return [], None
    try:
        data = yaml.safe_load(path.read_text()) or {}
        steps = data.get("steps")
        if not isinstance(steps, list):
            raise ValueError("'steps' must be a list")
        return ([_validate_step(s, i)
                 for i, s in enumerate(steps[:config.FUNC_MAX_STEPS], 1)],
                None)
    except (yaml.YAMLError, ValueError, OSError) as exc:
        return [], str(exc)


def autodetect(repo: Path) -> list[dict[str, Any]]:
    """Default test steps per ecosystem manifest at the repo root."""
    steps = []
    for manifest, argv in _AUTODETECT:
        if (repo / manifest).exists():
            steps.append({
                "name": f"test-{Path(manifest).stem}",
                "argv": list(argv),
                "timeout": config.FUNC_TIMEOUT,
                "required": False,
                "net": False,
            })
        if len(steps) >= config.FUNC_MAX_STEPS:
            break
    return steps


def _finding(severity: str, title: str, detail: str) -> dict[str, Any]:
    return {"agent": "functional", "severity": severity,
            "title": title, "detail": detail, "file": None}


def run_functional(repo: Path) -> dict[str, Any]:
    """Execute the functional stage. Returns findings, pgids and process
    metrics — the same dict shape the specialists emit."""
    out: dict[str, Any] = {"findings": [], "pgids": [], "processes": []}
    steps, err = load_contract(repo)
    if err:
        out["findings"].append(_finding(
            "warning", "Functional contract malformed",
            f"{CONTRACT_NAME} ignored ({err}); falling back to "
            "ecosystem autodetect."))
    if not steps:
        steps = autodetect(repo)
    if not steps:
        out["findings"].append(_finding(
            "info", "Functional validation skipped",
            "No quality-gate.yaml contract and no recognized ecosystem "
            "manifest at the repo root."))
        return out

    use_sandbox = sandbox.available()
    if not use_sandbox and not config.EXEC_VALIDATORS:
        out["findings"].append(_finding(
            "info", "Functional validation skipped",
            f"{len(steps)} step(s) resolvable but no sandbox runtime "
            "available and PR-code execution is disabled on the host "
            "(QG_EXEC_VALIDATORS=0)."))
        return out
    if not use_sandbox:
        out["findings"].append(_finding(
            "info", "Functional validation on host",
            "No sandbox runtime/image available — steps ran via host "
            "spawn_process (QG_SANDBOX=auto fallback)."))

    for step in steps:
        cmdline = " ".join(step["argv"])
        # PR-declared egress is gated on the global opt-in: a hostile
        # contract cannot grant itself network access by writing
        # `net: true` (the FR-O24 audit's egress is service-level and
        # unaffected).
        step_net = step["net"] and config.SANDBOX_ALLOW_NET
        if step["net"] and not step_net:
            out["findings"].append(_finding(
                "info", f"Step egress denied: {step['name']}",
                f"`{cmdline}` declares net: true but "
                "QG_SANDBOX_ALLOW_NET=0 — running without egress."))
        if use_sandbox:
            res = sandbox.run(repo, step["argv"], step["timeout"],
                              net=step_net)
        else:
            res = mcp_tools.spawn_process(
                str(repo), step["argv"][0], step["argv"][1:],
                timeout=step["timeout"])
        if res["pgid"] > 0:
            out["pgids"].append(res["pgid"])
        out["processes"].append({
            "agent": "functional",
            "name": step["name"],
            "command": cmdline,
            "duration_s": res.get("duration_s", 0.0),
            "exit_code": res["exit_code"],
            "timed_out": res["timed_out"],
            "sandboxed": use_sandbox,
        })
        where = "sandbox" if use_sandbox else "host"
        if res["timed_out"]:
            out["findings"].append(_finding(
                "warning", f"Functional step timed out: {step['name']}",
                f"`{cmdline}` exceeded {step['timeout']}s ({where})."))
        elif res["exit_code"] != 0:
            tail = (res["stderr"] or res["stdout"]).strip().splitlines()
            sev = "blocker" if step["required"] else "warning"
            out["findings"].append(_finding(
                sev, f"Functional step failed: {step['name']}",
                f"`{cmdline}` exited {res['exit_code']} ({where}): "
                f"{tail[-1][:200] if tail else 'no output'}"))
    return out

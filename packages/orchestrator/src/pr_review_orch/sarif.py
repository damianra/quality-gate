"""FR-O32: findings → SARIF 2.1.0.

The deduped finding list converts to one SARIF ``run`` per ``agent``
value — the §6.3 schema carries no finer tool discriminator and scanner
findings already encode it in ``title`` (e.g. ``semgrep:<id>``), which
becomes the ``ruleId``. Emission is deterministic and additive: it runs
after dedup so the artifact mirrors the posted verdict comment, and a
write failure degrades to an error entry, never a run failure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import config

#: §6.3 severity → SARIF level.
_LEVELS = {"info": "note", "warning": "warning", "blocker": "error"}

#: Emission is disabled by these SARIF_PATH values.
_DISABLED = {"", "0", "off", "false", "none"}


def findings_to_sarif(findings: list[dict[str, Any]],
                      version: str = "") -> dict[str, Any]:
    """Map the deduped finding list to a SARIF 2.1.0 log.

    One ``run`` per ``agent`` value present in the findings; with no
    findings a single empty ``quality-gate`` run is emitted so the
    artifact still uploads cleanly. ``file`` becomes
    ``artifactLocation.uri`` (repo-relative POSIX path, already
    normalized by producers; ``.`` when absent — GitHub code scanning
    requires every result to carry a location) and ``line`` becomes
    ``region.startLine``, omitted when absent.
    """
    by_agent: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        agent = str(f.get("agent") or "review")
        by_agent.setdefault(agent, []).append(f)
    if not by_agent:
        by_agent = {"": []}
    runs: list[dict[str, Any]] = []
    for agent in sorted(by_agent):
        rules: dict[str, dict[str, Any]] = {}
        results: list[dict[str, Any]] = []
        for f in by_agent[agent]:
            rid = str(f.get("title") or "finding")
            rules.setdefault(rid, {
                "id": rid,
                "shortDescription": {"text": rid[:200]},
            })
            result: dict[str, Any] = {
                "ruleId": rid,
                "level": _LEVELS.get(
                    str(f.get("severity") or ""), "note"),
                "message": {
                    "text": str(f.get("detail") or rid)[:1000]},
            }
            # GitHub code scanning rejects results with no ``locations``
            # at all (``locationFromSarifResult``), so run-level
            # findings anchor to ``.`` — the worktree root directory,
            # a valid relative artifact URI — instead of dropping them.
            file = f.get("file")
            phys: dict[str, Any] = {
                "artifactLocation": {
                    "uri": file.replace(os.sep, "/")
                    if isinstance(file, str) and file else "."},
            }
            line = f.get("line")
            if type(line) is int and line > 0:
                phys["region"] = {"startLine": line}
            result["locations"] = [{"physicalLocation": phys}]
            results.append(result)
        driver: dict[str, Any] = {
            "name": f"quality-gate-{agent}".rstrip("-"),
            "rules": list(rules.values()),
        }
        if version:
            driver["version"] = version
        runs.append({
            "tool": {"driver": driver},
            "columnKind": "utf16CodeUnits",
            "results": results,
        })
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": runs,
    }


def emit(findings: list[dict[str, Any]], repo_path: str,
         version: str = "") -> Path | None:
    """Write the SARIF log to ``config.SARIF_PATH``.

    Relative paths resolve against ``repo_path`` (the worktree root).
    Returns the written path, or ``None`` when emission is disabled.
    """
    raw = config.SARIF_PATH.strip()
    if raw.lower() in _DISABLED:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(repo_path) / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(findings_to_sarif(findings, version), indent=2)
        + "\n")
    return path

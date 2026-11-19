"""FR-O29 security scanning toolchain.

Deterministic scanners vendored into the ``qg-sandbox`` image —
``semgrep`` (SAST), ``gitleaks`` (secrets), ``trivy`` (IaC config) —
run inside the FR-O28 sandbox as *evidence generators* for the
SecurityAgent. Everything here is additive: a missing binary, bad
JSON, timeout or empty result degrades to an info note, never blocks
the run and never removes other findings. Scanner output is untrusted
data (FR-O25): parsers are defensive and matched secret text is never
emitted into findings.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from . import config, sandbox

#: The FR-O28 runner injected by ``agents.security_specialist`` —
#: ``(repo, argv, timeout) -> spawn result dict``.
Runner = Callable[[Path, list[str], int], dict[str, Any]]

#: Ruleset location inside the sandbox image; the host fallback reads
#: the same file shipped inside this package.
_IMAGE_RULES = "/opt/qg-rules"
_PKG_RULES = Path(__file__).resolve().parent / "qg-rules"

#: IaC/config subset that triggers ``trivy config`` — the IaC half of
#: the sensitive-config watchlist plus deploy trees and Terraform.
_IAC_RE = re.compile(
    r"(?:^|/)(?:Dockerfile[^/]*|docker-compose[^/]*\.ya?ml|compose\.ya?ml)$"
    r"|(^|/)\.github/workflows/[^/]+\.ya?ml$"
    r"|(^|/)deploy/"
    r"|\.tf$",
    re.IGNORECASE,
)

#: Scanner-suppression directives a PR could add to mute the toolchain.
#: The PR controls its own tree, so these files/inline markers always
#: earn a warning — scanners themselves may silently honor them.
_SCANNER_IGNORE_FILES = frozenset(
    {".gitleaksignore", ".semgrepignore", ".trivyignore"})
_SUPPRESS_RE = re.compile(
    r"nosemgrep|nosem\b|tfsec:ignore|trivy:ignore|checkov:skip",
    re.IGNORECASE)

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

#: Same fixture-path policy as the SecurityAgent secret signal.
_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|__mocks__|fixtures?|mocks?)(?:/|$)"
    r"|\.(?:test|spec)\.[a-z0-9]+$"
    r"|(?:^|/)test_[a-z0-9_]+\.[a-z0-9]+$",
    re.IGNORECASE,
)

#: Path prefixes a scanner may report that need stripping back to
#: repo-relative form (gitleaks/trivy report the scanned target root).
_TARGET_PREFIXES = ("./", "/src/", "/")


def _finding(severity: str, title: str, detail: str,
             file: str | None = None) -> dict[str, Any]:
    return {"agent": "security", "severity": severity, "title": title,
            "detail": detail, "file": file}


def added_line_ranges(diff_text: str) -> dict[str, set[int]]:
    """Map added new-side line numbers per file from a unified diff.

    Gitleaks reports whole-file hits; only findings whose start line
    sits on an added line survive — pre-existing leaks in untouched
    code must not fire on this PR.
    """
    ranges: dict[str, set[int]] = {}
    current: str | None = None
    new_line = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++"):
            target = raw[4:].strip()
            current = target[2:] if target.startswith("b/") else None
        elif raw.startswith("@@"):
            m = _HUNK_RE.match(raw)
            new_line = int(m.group(1)) if m else 0
        elif raw.startswith("+"):
            if current is not None:
                ranges.setdefault(current, set()).add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            pass  # deleted line: consumes no new-side number
        else:
            new_line += 1  # context line
    return ranges


def _repo_path(scanned: str) -> str:
    """Normalize a scanner-reported path back to repo-relative."""
    p = scanned
    for pre in _TARGET_PREFIXES:
        if p.startswith(pre):
            p = p[len(pre):]
            break
    return p


def _record_process(out: dict[str, Any], argv: list[str],
                    res: dict[str, Any]) -> None:
    if res.get("pgid", -1) > 0:
        out["pgids"].append(res["pgid"])
    out["processes"].append({
        "agent": "security",
        "command": " ".join(argv)[:200],
        "duration_s": res.get("duration_s", 0.0),
        "exit_code": res.get("exit_code", -1),
        "timed_out": res.get("timed_out", False),
    })


def _tool_missing(name: str, res: dict[str, Any]) -> bool:
    """Binary absent either on host (127) or in the sandbox image."""
    if res.get("exit_code") == 127:
        return True
    blob = f"{res.get('stderr', '')}{res.get('stdout', '')}".lower()
    return "no such file" in blob or "command not found" in blob


def _cap(findings: list[dict[str, Any]], tool: str) -> list[dict[str, Any]]:
    if len(findings) <= config.SEC_MAX_FINDINGS:
        return findings
    findings = findings[:config.SEC_MAX_FINDINGS]
    findings.append(_finding(
        "info", f"{tool} findings truncated",
        f"{tool} produced more than {config.SEC_MAX_FINDINGS} hits; "
        "run the scanner locally for the full list."))
    return findings


def _run_tool(name: str, repo: Path, argv: list[str],
              run: Runner, out: dict[str, Any]) -> dict[str, Any] | None:
    """Spawn one scanner; ``None`` signals a degraded run (already
    reported as an info finding + process entry)."""
    res = run(repo, argv, config.SEC_TOOL_TIMEOUT)
    _record_process(out, argv, res)
    if res.get("timed_out"):
        out["findings"].append(_finding(
            "info", f"{name} timed out",
            f"`{' '.join(argv[:6])}` exceeded "
            f"{config.SEC_TOOL_TIMEOUT}s."))
        return None
    if _tool_missing(name, res):
        out["findings"].append(_finding(
            "info", f"{name} unavailable",
            f"{name} is not on PATH in this environment; skipped."))
        return None
    return res


def _semgrep(repo: Path, files: list[str],
             run: Runner, out: dict[str, Any]) -> None:
    rules = _IMAGE_RULES if sandbox.available() else str(_PKG_RULES)
    argv = ["semgrep", "scan", "--config", rules, "--json",
            "--metrics", "off", "--disable-nosem", "--quiet",
            *files[:64]]
    res = _run_tool("semgrep", repo, argv, run, out)
    if res is None:
        return
    try:
        doc = json.loads(res.get("stdout") or "{}")
    except (TypeError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    results = doc.get("results")
    if not isinstance(results, list):
        out["findings"].append(_finding(
            "info", "semgrep output unusable",
            f"semgrep exited {res.get('exit_code')} with no parseable "
            "JSON."))
        return
    findings: list[dict[str, Any]] = []
    for hit in results:
        if not isinstance(hit, dict):
            continue
        extra = hit.get("extra") or {}
        sev = "warning" if str(
            extra.get("severity", "")).upper() == "ERROR" else "info"
        start = (hit.get("start") or {}).get("line", "?")
        msg = str(extra.get("message") or hit.get("check_id", ""))[:160]
        findings.append(_finding(
            sev, f"semgrep:{hit.get('check_id', 'unknown')}",
            f"line {start}: {msg}",
            file=_repo_path(str(hit.get("path", ""))) or None))
    out["findings"] += _cap(findings, "semgrep")


def _gitleaks(repo: Path, changed: set[str],
              ranges: dict[str, set[int]],
              run: Runner, out: dict[str, Any]) -> None:
    # `gitleaks dir` takes a single target; scan the tree once and
    # post-filter to findings on changed files' added lines — equivalent
    # to scanning the changed set, without per-file spawns. The report
    # goes to /dev/stdout so nothing secret touches the container fs.
    argv = ["gitleaks", "dir", "--no-banner", "--exit-code", "0",
            "--report-format", "json", "--report-path", "/dev/stdout", "."]
    res = _run_tool("gitleaks", repo, argv, run, out)
    if res is None:
        return
    try:
        entries = json.loads(res.get("stdout") or "[]")
    except (TypeError, ValueError):
        out["findings"].append(_finding(
            "info", "gitleaks output unusable",
            f"gitleaks exited {res.get('exit_code')} with no parseable "
            "JSON."))
        return
    if not isinstance(entries, list):
        entries = []
    findings: list[dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        f = _repo_path(str(e.get("File", "")))
        try:
            line = int(e.get("StartLine", 0))
        except (TypeError, ValueError):
            line = 0
        if f not in changed or line not in ranges.get(f, set()):
            continue
        rule = str(e.get("RuleID", "unknown"))
        secret = str(e.get("Secret") or "")
        hint = f" ({secret[:4]}…)" if len(secret) > 4 else ""
        in_test = bool(_TEST_PATH_RE.search(f))
        findings.append(_finding(
            "warning" if in_test else "blocker",
            "Committed secret" + (" (test file)" if in_test else ""),
            f"gitleaks:{rule} matched an added line {line} — verify "
            f"and rotate if real{hint}.",
            file=f))
    out["findings"] += _cap(findings, "gitleaks")


def _trivy(repo: Path, iac_files: list[str],
           run: Runner, out: dict[str, Any]) -> None:
    # `trivy config` takes one target — spawn per changed IaC file
    # (bounded) rather than scanning the tree and guessing Target paths.
    findings: list[dict[str, Any]] = []
    for target in iac_files[:8]:
        argv = ["trivy", "config", "--format", "json",
                "--exit-code", "0", target]
        res = _run_tool("trivy", repo, argv, run, out)
        if res is None:
            return  # degraded run already noted; no point retrying
        try:
            doc = json.loads(res.get("stdout") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        for block in doc.get("Results") or []:
            for mc in block.get("Misconfigurations") or []:
                if not isinstance(mc, dict):
                    continue
                sev = str(mc.get("Severity", "")).upper()
                mapped = "warning" if sev in {"CRITICAL", "HIGH"} else "info"
                line = (mc.get("CauseMetadata") or {}).get("StartLine")
                loc = f"line {line}: " if line else ""
                findings.append(_finding(
                    mapped,
                    f"trivy:{mc.get('ID') or mc.get('AVDID') or 'misconfig'}",
                    f"{loc}{str(mc.get('Title') or '')[:140]}",
                    file=target))
    out["findings"] += _cap(findings, "trivy")


def _suppression_findings(changed: set[str],
                          diff_text: str) -> list[dict[str, Any]]:
    """Warn when the PR arms scanner-muting controls the tools would
    silently honor — the PR controls its own ignore files."""
    out: list[dict[str, Any]] = []
    for f in changed:
        if Path(f).name in _SCANNER_IGNORE_FILES:
            out.append(_finding(
                "warning", "Scanner ignore file added",
                f"`{f}` can suppress gitleaks/semgrep/trivy hits — "
                "review its contents.", file=f))
    if _SUPPRESS_RE.search(diff_text):
        out.append(_finding(
            "warning", "Scanner suppression directive added",
            "The diff adds a nosem/tfsec/trivy/checkov suppression "
            "marker — verify it is justified."))
    return out


def run_security_tools(repo: Path, files: list[str], diff_text: str,
                       run: Runner) -> dict[str, Any]:
    """FR-O29 entry point — run the enabled vendored scanners.

    ``files`` is the PR's changed-file list; scanners run only when
    their trigger surface is present. Returns ``{findings, pgids,
    processes}`` for the agent to merge additively.
    """
    out: dict[str, Any] = {"findings": [], "pgids": [], "processes": []}
    enabled = config.SEC_TOOLS
    # Only files that exist at the PR head are scannable.
    changed = {f for f in files if f and (repo / f).is_file()}
    if not enabled or not changed:
        return out

    out["findings"] += _suppression_findings(changed, diff_text)
    ranges = added_line_ranges(diff_text)

    code_files = [f for f in sorted(changed)
                  if Path(f).suffix.lower() in config.QA_CODE_EXTS]
    if "semgrep" in enabled and code_files:
        _semgrep(repo, code_files, run, out)

    if "gitleaks" in enabled:
        _gitleaks(repo, changed, ranges, run, out)

    iac = [f for f in sorted(changed) if _IAC_RE.search(f)]
    if "trivy" in enabled and iac:
        _trivy(repo, iac, run, out)

    return out

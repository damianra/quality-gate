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

import hashlib
import json
import re
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
#: `.gitleaks.toml` and `trivy.yaml` are auto-loaded configs (allowlist /
#: severity overrides), not just ignore files.
_SCANNER_IGNORE_FILES = frozenset(
    {".gitleaksignore", ".gitleaks.toml", ".semgrepignore",
     ".trivyignore", "trivy.yaml"})
#: Inline suppression directives, scoped to their real comment syntaxes
#: (per-language: #, //, /* */ and <!-- -->) — a bare `nosem` would
#: over-match text like our own `--disable-nosem` argv.
_SUPPRESS_RE = re.compile(
    r"nosemgrep|(?:#|//|/\*|<!--)\s*nosem\b"
    r"|gitleaks:(?:allow|ignore)"
    r"|tfsec:ignore|trivy:ignore|checkov:skip",
    re.IGNORECASE)

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

#: Non-code extensions the `generic` semgrep rules still cover.
_GENERIC_EXTS = frozenset({".sh", ".bash", ".zsh"})

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
            "detail": detail,
            "file": file[:200] if isinstance(file, str) else file}


def added_line_ranges(diff_text: str) -> dict[str, set[int]]:
    """Map added new-side line numbers per file from a unified diff.

    Gitleaks reports whole-file hits; only findings whose start line
    sits on an added line survive — pre-existing leaks in untouched
    code must not fire on this PR.

    ``---``/``+++`` are file headers ONLY between ``diff --git`` and the
    first ``@@`` — inside a hunk an added line whose content starts with
    ``++`` (``+++foo``) or a deleted line starting ``-- `` is payload,
    not a header. ``\\ No newline at end of file`` markers consume no
    new-side line number.
    """
    ranges: dict[str, set[int]] = {}
    current: str | None = None
    in_header = False
    binary = False  # inside a GIT binary patch — payload is base85
    new_line = 0
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            current, in_header, binary = None, True, False
        elif binary:
            pass
        elif in_header and raw.startswith("--- "):
            pass
        elif in_header and raw.startswith("+++ "):
            target = raw[4:].strip()
            current = target[2:] if target.startswith("b/") else None
        elif raw.startswith(("GIT binary patch", "Binary files ")):
            binary = True
        elif raw.startswith("@@"):
            in_header = False
            m = _HUNK_RE.match(raw)
            new_line = int(m.group(1)) if m else 0
        elif in_header:
            pass  # index/mode lines between diff --git and @@
        elif raw.startswith("\\"):
            pass  # "\ No newline at end of file" — no line consumed
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
    """Binary absent either on host (127) or in the sandbox image.

    Only stderr is checked — stdout is the scanner's report (untrusted
    PR-influenced data) and grepping it would let a leak/semgrep line
    containing "no such file" silently disable the whole tool.
    """
    if res.get("exit_code") == 127:
        return True
    blob = str(res.get("stderr", "")).lower()
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
    try:
        res = run(repo, argv, config.SEC_TOOL_TIMEOUT)
    except OSError:  # binary absent on the host-fallback path
        out["findings"].append(_finding(
            "info", f"{name} unavailable",
            f"{name} is not on PATH in this environment; skipped."))
        # Pseudo-record so callers can tell missing-binary (exit -1)
        # apart from a timeout when deciding whether to keep going.
        out["processes"].append({
            "agent": "security", "command": " ".join(argv)[:200],
            "duration_s": 0.0, "exit_code": -1, "timed_out": False})
        return None
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
    targets, skipped = files[:64], len(files) - 64
    if skipped > 0:
        out["findings"].append(_finding(
            "info", "semgrep target list truncated",
            f"{skipped} changed file(s) skipped by the target cap; "
            "scan locally for full coverage."))
    # "./" prefix: a PR-controlled filename starting with '-' cannot
    # inject tool options.
    argv = ["semgrep", "scan", "--config", rules, "--json",
            "--metrics", "off", "--disable-nosem", "--quiet",
            *[f"./{f}" for f in targets]]
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
    errors = doc.get("errors")
    if isinstance(errors, list) and errors:
        out["findings"].append(_finding(
            "info", "semgrep reported errors",
            f"{len(errors)} rule/parse error(s) — affected rules may "
            "be silently skipped; check the scan output."))
    findings: list[dict[str, Any]] = []
    for hit in results:
        if not isinstance(hit, dict):
            continue
        extra = hit.get("extra")
        start = hit.get("start")
        extra = extra if isinstance(extra, dict) else {}
        start = start if isinstance(start, dict) else {}
        sev = "warning" if str(
            extra.get("severity", "")).upper() == "ERROR" else "info"
        msg = str(extra.get("message") or hit.get("check_id", ""))[:160]
        findings.append(_finding(
            sev, f"semgrep:{hit.get('check_id', 'unknown')}",
            f"line {start.get('line', '?')}: {msg}",
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
        # Never emit matched secret text — a non-reversible fingerprint
        # still lets maintainers confirm which value was flagged.
        secret = str(e.get("Secret") or "")
        # Shrink the plaintext's lifetime in memory: drop it from the
        # entry once the fingerprint is taken (the raw report sits in
        # res["stdout"], which is never persisted into process records).
        e.pop("Secret", None)
        e.pop("Match", None)
        fp = hashlib.sha256(secret.encode()).hexdigest()[:8]
        hint = f" (sha256:{fp}…)" if secret else ""
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
    if len(iac_files) > 8:
        out["findings"].append(_finding(
            "info", "trivy target list truncated",
            f"{len(iac_files) - 8} IaC file(s) skipped by the target "
            "cap; scan locally for full coverage."))
    findings: list[dict[str, Any]] = []
    for target in iac_files[:8]:
        argv = ["trivy", "config", "--format", "json",
                "--exit-code", "0", f"./{target}"]
        res = _run_tool("trivy", repo, argv, run, out)
        if res is None:
            # A missing binary aborts the whole loop; a timeout on one
            # file must not forfeit coverage of the remaining targets.
            if out["processes"] and \
                    out["processes"][-1].get("timed_out"):
                continue
            return
        try:
            doc = json.loads(res.get("stdout") or "{}")
        except (TypeError, ValueError):
            doc = None
        if not isinstance(doc, dict):
            out["findings"].append(_finding(
                "info", "trivy output unusable",
                f"trivy on `{target}` exited {res.get('exit_code')} "
                "with no parseable JSON."))
            continue
        for block in doc.get("Results") or []:
            if not isinstance(block, dict):
                continue
            for mc in block.get("Misconfigurations") or []:
                if not isinstance(mc, dict):
                    continue
                sev = str(mc.get("Severity", "")).upper()
                mapped = "warning" if sev in {"CRITICAL", "HIGH"} else "info"
                meta = mc.get("CauseMetadata")
                line = meta.get("StartLine") if isinstance(meta, dict) \
                    else None
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
    # Added lines only — removing or merely touching a suppression
    # marker in context must not warn. Only the '+++ ' file header is
    # excluded: '+++x' inside a hunk is an added payload line.
    if any(raw.startswith("+") and not raw.startswith("+++ ")
           and _SUPPRESS_RE.search(raw) for raw in
           diff_text.splitlines()):
        out.append(_finding(
            "warning", "Scanner suppression directive added",
            "The diff adds a nosem/tfsec/trivy/checkov suppression "
            "marker — verify it is justified."))
    return out


_KNOWN_TOOLS = frozenset({"semgrep", "gitleaks", "trivy"})


def triggered(files: list[str]) -> bool:
    """FR-O29 routing surface — True when any enabled tool would fire
    on the PR's changed-file list. ``gitleaks`` scans every changed
    file, so its presence activates the stage unconditionally.
    """
    enabled = config.SEC_TOOLS & _KNOWN_TOOLS
    if not enabled or not files:
        return False
    if "gitleaks" in enabled:
        return True
    names = [Path(f).name.lower() for f in files]
    if "semgrep" in enabled and (
            any(Path(f).suffix.lower()
                in (config.QA_CODE_EXTS | _GENERIC_EXTS) for f in files)
            or any(_IAC_RE.search(f) for f in files)):
        return True
    if "trivy" in enabled and any(_IAC_RE.search(f) for f in files):
        return True
    if any(n in _SCANNER_IGNORE_FILES for n in names):
        return True
    return False


def run_security_tools(repo: Path, files: list[str], diff_text: str,
                       run: Runner) -> dict[str, Any]:
    """FR-O29 entry point — run the enabled vendored scanners.

    ``files`` is the PR's changed-file list; scanners run only when
    their trigger surface is present. Returns ``{findings, pgids,
    processes}`` for the agent to merge additively.
    """
    out: dict[str, Any] = {"findings": [], "pgids": [], "processes": []}
    unknown = config.SEC_TOOLS - _KNOWN_TOOLS
    if unknown:
        out["findings"].append(_finding(
            "info", "Unknown QG_SEC_TOOLS entries ignored",
            f"{', '.join(sorted(unknown))} not in "
            f"{', '.join(sorted(_KNOWN_TOOLS))}."))
    enabled = config.SEC_TOOLS & _KNOWN_TOOLS
    # Only files that exist at the PR head are scannable.
    changed = {f for f in files if f and (repo / f).is_file()}
    if not enabled or not changed:
        return out

    out["findings"] += _suppression_findings(changed, diff_text)
    ranges = added_line_ranges(diff_text)

    iac = [f for f in sorted(changed) if _IAC_RE.search(f)]

    # Code files drive the language rules; shell scripts and the IaC
    # surface are included so the `generic` rules (curl|sh, private-key
    # blocks) can fire on them.
    sem_targets = {
        f for f in changed
        if Path(f).suffix.lower() in config.QA_CODE_EXTS
        or Path(f).suffix.lower() in _GENERIC_EXTS
        or f in iac
    }
    if "semgrep" in enabled and sem_targets:
        _semgrep(repo, sorted(sem_targets), run, out)

    if "gitleaks" in enabled:
        _gitleaks(repo, changed, ranges, run, out)

    if "trivy" in enabled and iac:
        _trivy(repo, iac, run, out)

    return out

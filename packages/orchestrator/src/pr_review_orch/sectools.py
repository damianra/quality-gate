"""FR-O29 security scanning toolchain, hardened per FR-O30.

Deterministic scanners vendored into the ``qg-sandbox`` image —
``semgrep`` (SAST), ``gitleaks`` (secrets), ``trivy`` (IaC config) —
run inside the FR-O28 sandbox as *evidence generators* for the
SecurityAgent. Everything here is additive: a missing binary, bad
JSON, timeout or empty result degrades to an info note, never blocks
the run and never removes other findings. Scanner output is untrusted
data (FR-O25): parsers are defensive and matched secret text is never
emitted into findings.

FR-O30 pins every repo-controlled input the tools would auto-load:
gitleaks/trivy spawn with cwd OUTSIDE the worktree (both resolve
``.gitleaksignore``/``trivy.yaml`` against cwd — verified against the
pinned binaries, the ``-c`` flag alone does not override them),
``--ignore-gitleaks-allow`` + vendored ``-c`` config pin gitleaks,
``--ignorefile ""`` pins trivy's ignore files, and a deterministic
counter-scan enumerates the suppression surfaces that remain live.
Enabled tools run concurrently and merge deterministically.
"""

from __future__ import annotations

import ast
import concurrent.futures
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from . import config, sandbox

#: The FR-O28 runner injected by ``agents.security_specialist`` —
#: ``(repo, argv, timeout, *, outside=False) -> spawn result dict``.
Runner = Callable[..., dict[str, Any]]

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
#: `.gitignore`/`.ignore` are on the list too: semgrep honors them
#: (we pass --no-git-ignore so they cannot mute a scan, but a PR that
#: edits them can still hide files from other tools/reviewers).
_SCANNER_IGNORE_FILES = frozenset(
    {".gitleaksignore", ".gitleaks.toml", ".semgrepignore",
     ".trivyignore", ".trivyignore.yaml", "trivy.yaml",
     ".gitignore", ".ignore"})
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
            # git C-quotes paths with non-ASCII/control chars
            # (core.quotePath): '+++ "b/fi\303\251le.py"'. Unquote via
            # literal_eval and re-decode the escaped bytes as UTF-8 —
            # without this the b/ match fails and the file's added-line
            # ranges are silently dropped (fail-open for gitleaks).
            if len(target) >= 2 and target.startswith('"') \
                    and target.endswith('"'):
                try:
                    unq = ast.literal_eval(target)
                    if isinstance(unq, str):
                        target = unq.encode(
                            "latin-1", "replace").decode(
                            "utf-8", "replace")
                except (ValueError, SyntaxError):
                    pass
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


def _repo_path(scanned: str, target: str = "") -> str:
    """Normalize a scanner-reported path back to repo-relative.

    ``target`` is the absolute scan root — gitleaks reports
    ``<target>/<file>``, so the host fallback's real worktree path
    strips cleanly too."""
    p = scanned
    if target and p.startswith(target.rstrip("/") + "/"):
        return p[len(target.rstrip("/")) + 1:]
    for pre in _TARGET_PREFIXES:
        if p.startswith(pre):
            p = p[len(pre):]
            break
    return p


def _repo_target(repo: Path) -> str:
    """Absolute in-environment repo path: ``/src`` inside the sandbox,
    the resolved worktree path on the host fallback."""
    return "/src" if sandbox.available() else str(repo.resolve())


def _rules_path(name: str) -> str:
    """A vendored file under ``qg-rules`` — image or package path."""
    if sandbox.available():
        return f"{_IMAGE_RULES}/{name}"
    return str(_PKG_RULES / name)


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

    Only the exit code decides: 127 is the shell/container exec
    "command not found". Stderr is deliberately NOT consulted — it is
    untrusted tool output, and generic fs-error text ('stat x: no such
    file') on a genuine scan failure would be misdiagnosed as a missing
    binary and, in trivy's per-file loop, abort coverage of every
    remaining target.
    """
    return res.get("exit_code") == 127


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
              run: Runner, out: dict[str, Any], *,
              outside: bool = False,
              mask: list[str] | None = None
              ) -> dict[str, Any] | None:
    """Spawn one scanner; ``None`` signals a degraded run (already
    reported as an info finding + process entry). ``outside`` runs
    with cwd outside the worktree and ``mask`` overlays repo files
    with /dev/null (FR-O30 config pinning)."""
    try:
        res = run(repo, argv, config.SEC_TOOL_TIMEOUT, outside=outside,
                  mask=mask)
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
    # --no-git-ignore: the PR controls .gitignore/.ignore — honoring
    # them would let a diff hide whole paths from the scan.
    # --jobs bounded to the sandbox CPU grant (FR-O30).
    try:
        jobs = max(1, int(float(config.SANDBOX_CPUS)))
    except (TypeError, ValueError):
        jobs = 1
    argv = ["semgrep", "scan", "--config", rules, "--json",
            "--metrics", "off", "--disable-nosem", "--no-git-ignore",
            "--jobs", str(jobs), "--quiet",
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
        check_id = str(hit.get("check_id", "unknown"))[:80]
        findings.append(_finding(
            sev, f"semgrep:{check_id}",
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
    #
    # FR-O30 pinning (all verified against the pinned binary):
    # - mask=[".gitleaksignore"]: the file resolves against the scan
    #   TARGET root (not cwd — re-verified with a valid fingerprint
    #   after the original cwd-pinning claim proved wrong) and `-i` is
    #   additive-only, so a /dev/null overlay is the only control
    # - `--ignore-gitleaks-allow`: inline `gitleaks:allow` comments off
    # - `-c` vendored `[extend] useDefault` config: a repo
    #   `.gitleaks.toml` auto-loads OVER the defaults, so pinning `-c`
    #   blocks a ruleset swap (an empty -c loads ZERO rules).
    target = _repo_target(repo)
    argv = ["gitleaks", "dir", "--no-banner", "--exit-code", "0",
            "--report-format", "json", "--report-path", "/dev/stdout",
            "--ignore-gitleaks-allow",
            "-c", _rules_path("gitleaks.toml"), target]
    res = _run_tool("gitleaks", repo, argv, run, out, outside=True,
                    mask=[".gitleaksignore"])
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
    finally:
        # The raw report is the only place matched secret text exists —
        # shrink its lifetime once parsing is done (FR-O30).
        res["stdout"] = ""
    if not isinstance(entries, list):
        out["findings"].append(_finding(
            "info", "gitleaks output unusable",
            "gitleaks produced valid JSON of an unexpected shape."))
        entries = []
    findings: list[dict[str, Any]] = []
    dropped_no_ranges = 0  # hits on changed files the diff did not map
    for e in entries:
        if not isinstance(e, dict):
            continue
        f = _repo_path(str(e.get("File", "")), target)
        try:
            line = int(e.get("StartLine", 0))
        except (TypeError, ValueError):
            line = 0
        if f not in changed:
            continue  # pre-existing file — not this PR's concern
        if f not in ranges:
            # Changed file whose patch the API elided (large/generated)
            # — dropping silently would be indistinguishable from
            # "no secrets". Surface the coverage gap.
            dropped_no_ranges += 1
            continue
        if line not in ranges[f]:
            continue  # hit on an untouched line — pre-existing leak
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
    if dropped_no_ranges:
        out["findings"].append(_finding(
            "info", "gitleaks coverage gap",
            f"{dropped_no_ranges} gitleaks hit(s) on changed file(s) "
            "could not be verified against added lines — the API "
            "elided those file patches; inspect the files directly."))
    out["findings"] += _cap(findings, "gitleaks")


def _trivy(repo: Path, iac_files: set[str],
           run: Runner, out: dict[str, Any]) -> None:
    # FR-O30: ONE spawn over the repo root, Results[].Target post-
    # filtered to the changed IaC set — one container spawn and one
    # timeout bound instead of one per file. outside=True pins the
    # cwd-resolved `trivy.yaml` config (the -c flag does NOT override
    # it — verified); `--ignorefile ""` disables .trivyignore and
    # .trivyignore.yaml.
    target = _repo_target(repo)
    argv = ["trivy", "config", "--format", "json", "--exit-code", "0",
            "--ignorefile", "", target]
    res = _run_tool("trivy", repo, argv, run, out, outside=True)
    if res is None:
        return
    try:
        doc = json.loads(res.get("stdout") or "{}")
    except (TypeError, ValueError):
        doc = None
    if not isinstance(doc, dict):
        out["findings"].append(_finding(
            "info", "trivy output unusable",
            f"trivy exited {res.get('exit_code')} with no parseable "
            "JSON."))
        return
    findings: list[dict[str, Any]] = []
    for block in doc.get("Results") or []:
        if not isinstance(block, dict):
            continue
        rel = _repo_path(str(block.get("Target", "")), target)
        # Some config types report a directory-level Target (Helm
        # charts, kustomize roots, TF module dirs) — accept a Target
        # that is a changed file or an ancestor of one.
        if rel not in iac_files and not any(
                p.startswith(rel + "/") for p in iac_files):
            continue  # pre-existing misconfig outside this PR's files
        for mc in block.get("Misconfigurations") or []:
            if not isinstance(mc, dict):
                continue
            sev = str(mc.get("Severity", "")).upper()
            mapped = "warning" if sev in {"CRITICAL", "HIGH"} else "info"
            meta = mc.get("CauseMetadata")
            line = meta.get("StartLine") if isinstance(meta, dict) \
                else None
            loc = f"line {line}: " if line else ""
            mc_id = str(mc.get("ID") or mc.get("AVDID")
                        or "misconfig")[:80]
            findings.append(_finding(
                mapped,
                f"trivy:{mc_id}",
                f"{loc}{str(mc.get('Title') or '')[:140]}",
                file=rel))
    out["findings"] += _cap(findings, "trivy")


def _suppression_findings(changed: set[str],
                          diff_text: str) -> list[dict[str, Any]]:
    """Warn when the PR arms scanner-muting controls the tools would
    silently honor — the PR controls its own ignore files."""
    out: list[dict[str, Any]] = []
    for f in changed:
        if Path(f).name.lower() in _SCANNER_IGNORE_FILES:
            out.append(_finding(
                "warning", "Scanner ignore file added",
                f"`{f}` can suppress gitleaks/semgrep/trivy hits — "
                "review its contents.", file=f))
    # Added lines only — removing or merely touching a suppression
    # marker in context must not warn. Only true file headers
    # ('+++ b/…', '+++ /dev/null' and the C-quoted '+++ "b/…"') are
    # excluded; a payload line like '+++ # nosem' still counts.
    header = re.compile(r'\+\+\+ (?:b/|/dev/null|")')
    if any(raw.startswith("+") and not header.match(raw)
           and _SUPPRESS_RE.search(raw) for raw in
           diff_text.splitlines()):
        out.append(_finding(
            "warning", "Scanner suppression directive added",
            "The diff adds a nosem/tfsec/trivy/checkov suppression "
            "marker — verify it is justified."))
    return out


#: rg output line: <path>:<lineno>:<text> — non-greedy path, digits,
#: then the matched payload.
_RG_LINE_RE = re.compile(r"^(.*?):(\d+):(.*)$")

#: Same directive set as _SUPPRESS_RE — Rust regex syntax overlaps
#: (non-capturing groups, \b, \s, literal `<!--` all valid in rg).
_RG_SUPPRESS = _SUPPRESS_RE.pattern


def _counter_scan(repo: Path, pr_files: set[str],
                  ranges: dict[str, set[int]],
                  run: Runner, out: dict[str, Any]) -> None:
    """FR-O30 suppression counter-scan: enumerate the surfaces that are
    live in the tree regardless of which PR introduced them — info
    findings, so muted coverage is never invisible. Surfaces the PR
    itself added or modified are already FR-O29 warnings and are not
    duplicated (ignore files by changed-path, directives by
    added-line)."""
    # Ignore/config half — a plain directory walk (the scan must find
    # files that contain no directive text). .git is skipped: object
    # names never hold scanner configs.
    found: list[str] = []
    for base, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d != ".git"]
        for n in names:
            if n.lower() in _SCANNER_IGNORE_FILES:
                rel = str((Path(base) / n).relative_to(repo))
                if rel not in pr_files:
                    found.append(rel)
    for rel in sorted(found)[:20]:
        out["findings"].append(_finding(
            "info", "Scanner ignore/config file present",
            f"`{rel}` is on the suppression watchlist — under unpinned "
            "defaults a scanner honors it; review its contents.",
            file=rel))
    if len(found) > 20:
        out["findings"].append(_finding(
            "info", "counter-scan file list truncated",
            f"{len(found) - 20} more ignore/config file(s) skipped; "
            "inspect the tree for the full list."))

    # Inline-directive half — `rg --hidden --no-ignore`: dotfiles and
    # gitignored paths are exactly what this scan exists to find. .git
    # is excluded like the walk half — COMMIT_EDITMSG/hooks may contain
    # directive-shaped text without suppressing anything.
    target = _repo_target(repo)
    argv = ["rg", "--hidden", "--no-ignore", "-n", "-g", "!.git",
            "-e", _RG_SUPPRESS, target]
    res = _run_tool("counter-scan", repo, argv, run, out)
    if res is None:
        return
    if res.get("exit_code", 0) >= 2:
        # rg: 0 hits found, 1 no matches, >=2 fatal error — an errored
        # counter-scan must not look identical to a clean tree.
        out["findings"].append(_finding(
            "info", "counter-scan output unusable",
            f"rg exited {res.get('exit_code')} — suppression surfaces "
            "could not be enumerated."))
        return
    directives: list[dict[str, Any]] = []
    for raw_line in str(res.get("stdout") or "").splitlines():
        m = _RG_LINE_RE.match(raw_line)
        if not m:
            continue
        rel = _repo_path(m.group(1), target)
        try:
            lineno = int(m.group(2))
        except (TypeError, ValueError):
            continue
        # Directives the PR itself added on new lines already warned
        # under FR-O29 — enumerate only the pre-existing ones.
        if lineno in ranges.get(rel, set()):
            continue
        directives.append(_finding(
            "info", "Suppression directive present",
            f"`{rel}:{lineno}` carries a scanner-suppression marker "
            "pre-existing in the tree — findings it mutes are "
            "invisible.", file=rel))
    out["findings"] += _cap(directives, "counter-scan")


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
    # Only real files at the PR head are scannable — symlinks are
    # excluded (is_file() follows them; a link could point outside
    # the worktree).
    changed = {f for f in files
               if f and (repo / f).is_file()
               and not (repo / f).is_symlink()}
    if not enabled or not changed:
        return out

    # The suppression warning uses the unfiltered changed list —
    # symlinked ignore files still warn (they cannot be scanned, but a
    # PR adding one is worth flagging).
    warn_set = {f for f in files if f and (repo / f).is_file()}
    out["findings"] += _suppression_findings(warn_set, diff_text)
    ranges = added_line_ranges(diff_text)
    _counter_scan(repo, set(files), ranges, run, out)

    iac = {f for f in changed if _IAC_RE.search(f)}

    # Code files drive the language rules; shell scripts and the IaC
    # surface are included so the `generic` rules (curl|sh, private-key
    # blocks) can fire on them.
    sem_targets = {
        f for f in changed
        if Path(f).suffix.lower() in config.QA_CODE_EXTS
        or Path(f).suffix.lower() in _GENERIC_EXTS
        or f in iac
    }

    # FR-O30: enabled tools run concurrently — each writes into its own
    # accumulator and they merge in tool order, so the wall time is the
    # slowest tool while findings stay deterministic.
    def acc() -> dict[str, Any]:
        return {"findings": [], "pgids": [], "processes": []}

    jobs: list[tuple[Any, dict[str, Any]]] = []
    if "semgrep" in enabled and sem_targets:
        jobs.append((lambda o: _semgrep(
            repo, sorted(sem_targets), run, o), acc()))
    if "gitleaks" in enabled:
        jobs.append((lambda o: _gitleaks(
            repo, changed, ranges, run, o), acc()))
    if "trivy" in enabled and iac:
        jobs.append((lambda o: _trivy(repo, iac, run, o), acc()))

    def guarded(fn: Any, acc: dict[str, Any]) -> None:
        # A job that raises (runner contract bump, sandbox RuntimeError)
        # must degrade to info like any other tool failure — never kill
        # the whole stage or lose the other tools' evidence.
        try:
            fn(acc)
        except Exception as exc:  # noqa: BLE001 — degrade boundary
            acc["findings"].append(_finding(
                "info", "scanner stage degraded",
                f"a scanner job failed unexpectedly: "
                f"{type(exc).__name__}: {str(exc)[:120]}"))

    if len(jobs) == 1:
        guarded(*jobs[0])
    elif jobs:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(jobs)) as pool:
            futures = [pool.submit(guarded, fn, o) for fn, o in jobs]
            for f in futures:
                f.result()
    for _, a in jobs:
        out["findings"] += a["findings"]
        out["pgids"] += a["pgids"]
        out["processes"] += a["processes"]
    return out

#!/usr/bin/env python3
"""FR-O31 golden-set benchmark harness.

Runs the gate's *deterministic* review stages — ``analyze_diff`` signals
plus the real ``review`` / ``security`` / ``qa`` specialist nodes —
against a corpus of synthetic PRs (``bench/cases/*.yaml``) and reports
detection rate and false positives. No GitHub API, no LLM by default.

Case schema (YAML):

    id: secret-in-new-file          # required, unique
    title: human label              # required
    tags: [security, gitleaks]      # optional, for per-tag metrics
    requires: sandbox               # optional — skip when podman/docker
                                    #   + qg-sandbox image unavailable
    pr_body: "..."                  # optional, fed to analyze_diff
    files:                          # required — repo tree at PR head;
      app/config.py: |              #   these are the "changed" files
        API_KEY = "..."
    extra_files:                    # optional — materialized on disk
      .gitleaksignore: |            #   but NOT in the changed list /
        app/config.py               #   synthesized diff (pre-existing
                                    #   tree content, e.g. suppression
                                    #   surfaces the counter-scan finds)
    diff_text: |                    # optional — synthesized as "all
      ...                           #   lines added" when omitted
    expect:
      signals: [secret_candidates]  # analyze_diff keys that must be
                                    #   truthy
      findings:                     # produced findings that must exist
        - agent: security           #   (each field is AND-matched;
          severity: blocker         #    `match` is a case-insensitive
          match: secret             #    substring over title+detail+file)
      no_unexpected_above: warning  # optional — fail when a produced
                                    #   finding not matched by any
                                    #   expectation reaches this level
                                    #   (info|warning|blocker)

LLM enrichment is off by default (the harness scrubs ``QG_LLM_*``);
``--include-llm`` leaves the env untouched so a configured provider runs
— those runs are flagged ``llm: true`` in the report and their findings
are not used for pass/fail (non-deterministic).

Usage:  uv run python bench/run.py [--json out.json] [--md out.md]
          [--case ID] [--include-llm]
Exit code is non-zero when any executed case fails.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "packages" / "orchestrator" / "src"))
sys.path.insert(0, str(_REPO / "packages" / "mcp-server" / "src"))

from pr_review_orch import agents, sandbox  # noqa: E402
from quality_gate_mcp import tools as mcp_tools  # noqa: E402

_SEV = {"info": 0, "warning": 1, "blocker": 2}
_CASES_DIR = Path(__file__).resolve().parent / "cases"


def synth_diff(files: dict[str, str]) -> str:
    """Synthesize a new-file unified diff marking every line as added.

    Keeps ``files`` (the materialized fixture) consistent with the
    added-line ranges gitleaks filters on — authored cases only need
    the file contents.
    """
    hunks = []
    for name, content in files.items():
        lines = content.splitlines()
        body = "\n".join("+" + ln for ln in lines)
        hunks.append(
            f"diff --git a/{name} b/{name}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{name}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n"
            f"{body}"
        )
    return "\n".join(hunks) + "\n"


def _finding_text(f: dict[str, Any]) -> str:
    return " ".join(str(f.get(k, "")) for k in
                    ("title", "detail", "file")).lower()


def _matches(expected: dict[str, Any],
             finding: dict[str, Any]) -> bool:
    for key in ("agent", "severity"):
        if expected.get(key) and finding.get(key) != expected[key]:
            return False
    m = expected.get("match")
    return m is None or str(m).lower() in _finding_text(finding)


@contextlib.contextmanager
def _scrub_llm_env():
    """Remove QG_LLM_* for deterministic runs; restore afterwards."""
    saved = {k: os.environ.pop(k) for k in list(os.environ)
             if k.startswith("QG_LLM_")}
    try:
        yield
    finally:
        os.environ.update(saved)


def _specialists(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Run the real specialist nodes; a crashing node degrades to an
    error note instead of killing the case (same additive contract)."""
    findings: list[dict[str, Any]] = []
    for name in ("review", "security", "qa"):
        try:
            fn = getattr(agents, f"{name}_specialist")
            findings += fn(state).get("findings", [])
        except Exception as exc:  # noqa: BLE001 - report, don't die
            findings.append({"agent": name, "severity": "blocker",
                             "title": f"Specialist crashed: {exc!r}",
                             "detail": "", "file": ""})
    return findings


def evaluate_case(case: dict[str, Any], *, include_llm: bool,
                  sandbox_ok: bool) -> dict[str, Any]:
    """Run one case; returns its result record (never raises for
    assertion-style failures)."""
    cid = case.get("id", "?")
    result: dict[str, Any] = {
        "id": cid, "title": case.get("title", ""),
        "tags": case.get("tags", []), "status": "fail",
        "missing": [], "unexpected": [], "missing_signals": [],
        "findings": [], "llm": include_llm, "duration_s": 0.0,
    }
    if case.get("requires") == "sandbox" and not sandbox_ok:
        result["status"] = "skipped"
        result["reason"] = "sandbox unavailable"
        return result

    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"qg-bench-{cid}-") as td:
        # TemporaryDirectory is 0700 — the sandbox's non-root user could
        # not traverse the mount; real worktrees are 0755.
        os.chmod(td, 0o755)
        repo = Path(td)
        files = case.get("files") or {}
        for name, content in {**files,
                              **(case.get("extra_files") or {})}.items():
            p = repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        diff_text = case.get("diff_text") or synth_diff(files)
        pr_body = case.get("pr_body", "")

        run = _specialists
        try:
            signals = mcp_tools.analyze_diff(diff_text, pr_body, str(repo))
            signals["diff_text"] = diff_text
            state = {
                "repo_path": str(repo),
                "files": [{"filename": f} for f in files],
                "signals": signals,
            }
            findings = run(state)
        except Exception as exc:  # noqa: BLE001
            result["missing"].append(f"harness error: {exc!r}")
            result["duration_s"] = time.monotonic() - t0
            return result

    result["duration_s"] = time.monotonic() - t0
    result["findings"] = [
        {k: f.get(k) for k in ("agent", "severity", "title", "file")}
        for f in findings]

    expect = case.get("expect") or {}
    for sig in expect.get("signals", []):
        if not signals.get(sig):
            result["missing_signals"].append(sig)

    matched: set[int] = set()
    for exp in expect.get("findings", []):
        hit = next((i for i, f in enumerate(findings)
                    if i not in matched and _matches(exp, f)), None)
        if hit is None:
            result["missing"].append(exp)
        else:
            matched.add(hit)

    cap = expect.get("no_unexpected_above")
    if cap in _SEV:
        for i, f in enumerate(findings):
            if i in matched:
                continue
            sev = str(f.get("severity", "info")).lower()
            if _SEV.get(sev, 0) >= _SEV[cap]:
                result["unexpected"].append({
                    "agent": f.get("agent"), "severity": sev,
                    "title": f.get("title"), "file": f.get("file")})

    if not (result["missing"] or result["unexpected"]
            or result["missing_signals"]):
        result["status"] = "pass"
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    ran = [r for r in results if r["status"] != "skipped"]
    per_tag: dict[str, dict[str, int]] = {}
    for r in ran:
        for tag in r.get("tags") or ["untagged"]:
            s = per_tag.setdefault(tag, {"pass": 0, "fail": 0})
            s["pass" if r["status"] == "pass" else "fail"] += 1
    return {
        "cases": len(results), "ran": len(ran),
        "passed": sum(r["status"] == "pass" for r in ran),
        "failed": sum(r["status"] == "fail" for r in ran),
        "skipped": sum(r["status"] == "skipped" for r in results),
        "unexpected_findings": sum(len(r["unexpected"]) for r in ran),
        "per_tag": per_tag,
    }


def render_md(results: list[dict[str, Any]],
              summary: dict[str, Any]) -> str:
    lines = [
        "# quality-gate benchmark report", "",
        f"- cases: **{summary['ran']}** ran "
        f"({summary['skipped']} skipped)",
        f"- pass: **{summary['passed']}** / fail: "
        f"**{summary['failed']}**",
        f"- unexpected findings: **{summary['unexpected_findings']}**",
        "", "| case | status | missing | unexpected | s |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['id']} | {r['status']} | {len(r['missing'])} |"
            f" {len(r['unexpected'])} | {r['duration_s']:.1f} |")
    lines += ["", "## per-tag detection", ""]
    for tag, s in sorted(summary["per_tag"].items()):
        lines.append(f"- `{tag}`: {s['pass']} pass / {s['fail']} fail")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--case", help="run only this case id")
    ap.add_argument("--json", metavar="PATH", help="write JSON report")
    ap.add_argument("--md", metavar="PATH", help="write markdown report")
    ap.add_argument("--include-llm", action="store_true",
                    help="leave QG_LLM_* env in place so a configured "
                         "provider enriches findings (non-deterministic)")
    ap.add_argument("-j", "--jobs", type=int, default=4,
                    help="cases to evaluate in parallel (default 4); "
                         "each case owns its tmpdir and pgids")
    args = ap.parse_args(argv)

    cases = sorted(_CASES_DIR.glob("*.yaml"))
    if not cases:
        print(f"no cases under {_CASES_DIR}", file=sys.stderr)
        return 2
    sandbox_ok = sandbox.available()
    selected = [
        yaml.safe_load(p.read_text(encoding="utf-8")) for p in cases]
    if args.case:
        selected = [c for c in selected if c.get("id") == args.case]
    # Scrub once for the whole run — parallel cases make per-case
    # os.environ mutation racy.
    ctx = contextlib.nullcontext() if args.include_llm \
        else _scrub_llm_env()
    with ctx, concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, args.jobs)) as ex:
        results = list(ex.map(
            lambda c: evaluate_case(
                c, include_llm=args.include_llm, sandbox_ok=sandbox_ok),
            selected))
    if not results:
        print(f"no case matched {args.case!r}", file=sys.stderr)
        return 2

    summary = summarize(results)
    report = {"summary": summary, "sandbox": sandbox_ok,
              "include_llm": args.include_llm, "results": results}
    md = render_md(results, summary)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n",
                                   encoding="utf-8")
    if args.md:
        Path(args.md).write_text(md, encoding="utf-8")
    print(md)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

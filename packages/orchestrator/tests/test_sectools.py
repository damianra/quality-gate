"""FR-O29 security scanning toolchain — trigger gating, defensive JSON
parsing, severity mapping, added-line filtering, redaction, finding
caps and degrade paths. The runner is injected so no real process is
ever spawned.
"""

from __future__ import annotations

import json

import pytest

from pr_review_orch import config, sectools


def _res(stdout="", exit_code=0, timed_out=False, stderr="", pgid=-1):
    return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code,
            "timed_out": timed_out, "pgid": pgid, "duration_s": 0.1}


def _runner(*responses):
    """Dispatch canned results by argv[0]; each response may be a dict
    or a callable(argv)->dict."""
    calls = []

    def run(repo, argv, timeout):
        calls.append(list(argv))
        tool = argv[0]
        for i, spec in enumerate(responses):
            match = spec.get("_argv") if isinstance(spec, dict) else None
            key = spec.get("tool") if isinstance(spec, dict) else None
            if (match is None and key == tool) or (
                    match is not None and match(argv)):
                return spec["res"] if isinstance(spec, dict) else spec(argv)
        return _res(exit_code=127, stderr=f"{tool}: command not found")
    run.calls = calls
    return run


def _touch(repo, *names):
    for n in names:
        p = repo / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x = 1\n")


DIFF = (
    "diff --git a/app.py b/app.py\n"
    "index 000..111 100644\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,3 @@\n"
    " ctx\n"
    "+added line five\n"
    "+added line six\n"
    " ctx2\n"
)


def test_added_line_ranges():
    diff = DIFF + (
        "diff --git a/del.py b/del.py\n"
        "--- a/del.py\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-gone\n"
        "diff --git a/b.py b/b.py\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        "@@ -10,0 +11,2 @@\n"
        "+b11\n"
        "+b12\n"
    )
    r = sectools.added_line_ranges(diff)
    assert r["app.py"] == {2, 3}
    assert r["b.py"] == {11, 12}
    assert "del.py" not in r or not r.get("del.py")


def test_added_line_ranges_payload_lookalikes():
    # Inside a hunk, '+++foo' is an ADDED line, '-- x' a DELETED one —
    # never file headers. '\ No newline' markers consume no line.
    diff = (
        "diff --git a/c.py b/c.py\n"
        "--- a/c.py\n"
        "+++ b/c.py\n"
        "@@ -1,3 +1,3 @@\n"
        " ctx\n"
        "+++payload line is added\n"
        "-rm\n"
        "-- deleted-looking content\n"
        "\\ No newline at end of file\n"
        "+after\n"
    )
    r = sectools.added_line_ranges(diff)
    # +++payload consumes line 2; deletions and the \ marker consume
    # nothing, so +after lands on line 3.
    assert r["c.py"] == {2, 3}


def test_triggers_only_with_matching_files(tmp_path):
    _touch(tmp_path, "app.py", "Dockerfile", "readme.md")
    calls = []

    def run(repo, argv, timeout):
        calls.append(argv[0])
        return _res(stdout="{}" if argv[0] == "semgrep" else "[]")
    sectools.run_security_tools(
        tmp_path, ["app.py", "Dockerfile", "readme.md"], DIFF, run)
    assert "semgrep" in calls and "gitleaks" in calls
    assert "trivy" in calls  # Dockerfile is IaC

    calls.clear()
    _touch(tmp_path, "docs")  # not used; keep dir real
    sectools.run_security_tools(tmp_path, ["readme.md"], "", run)
    assert "semgrep" not in calls and "trivy" not in calls
    assert "gitleaks" in calls  # any existing changed file triggers


def test_disabled_and_empty(tmp_path):
    _touch(tmp_path, "app.py")
    out = sectools.run_security_tools(tmp_path, [], DIFF, _runner())
    assert out == {"findings": [], "pgids": [], "processes": []}
    out = sectools.run_security_tools(
        tmp_path, ["app.py"], DIFF, _runner())
    assert any(p["command"].startswith("semgrep")
               for p in out["processes"])

    import pr_review_orch.config as cfg
    orig = cfg.SEC_TOOLS
    cfg.SEC_TOOLS = frozenset()
    try:
        out = sectools.run_security_tools(
            tmp_path, ["app.py"], DIFF, _runner())
        assert out["processes"] == []
    finally:
        cfg.SEC_TOOLS = orig


def test_missing_tool_degrades_to_info(tmp_path):
    _touch(tmp_path, "app.py")
    out = sectools.run_security_tools(
        tmp_path, ["app.py"], DIFF,
        _runner({"tool": "semgrep",
                 "res": _res(exit_code=127,
                             stderr="semgrep: command not found")}))
    assert any(f["severity"] == "info" and "unavailable" in f["title"]
               for f in out["findings"])


def test_timeout_is_info(tmp_path):
    _touch(tmp_path, "app.py")
    out = sectools.run_security_tools(
        tmp_path, ["app.py"], DIFF,
        _runner({"tool": "semgrep", "res": _res(timed_out=True)}))
    assert any("timed out" in f["title"] for f in out["findings"])
    assert all(f["severity"] == "info" for f in out["findings"])


def test_semgrep_severity_mapping(tmp_path):
    _touch(tmp_path, "app.py")
    payload = {"results": [
        {"check_id": "python-eval-exec", "path": "app.py",
         "start": {"line": 2},
         "extra": {"severity": "ERROR", "message": "eval boom"}},
        {"check_id": "python-weak-hash", "path": "app.py",
         "start": {"line": 9},
         "extra": {"severity": "WARNING", "message": "md5"}},
    ]}
    out = sectools.run_security_tools(
        tmp_path, ["app.py"], DIFF,
        _runner({"tool": "semgrep", "res": _res(json.dumps(payload))}))
    sg = {f["title"]: f["severity"] for f in out["findings"]
          if f["title"].startswith("semgrep:")}
    assert sg["semgrep:python-eval-exec"] == "warning"
    assert sg["semgrep:python-weak-hash"] == "info"


def test_gitleaks_added_line_filter_and_redaction(tmp_path):
    _touch(tmp_path, "app.py")
    entries = [
        # On an added line (2) -> blocker, secret text never emitted.
        {"RuleID": "aws-key", "File": "app.py", "StartLine": 2,
         "Secret": "AKIAFAKESECRET12345"},
        # Same file but untouched line -> dropped.
        {"RuleID": "old-key", "File": "app.py", "StartLine": 500,
         "Secret": "OLDACTIONS"},
        # File not in the diff -> dropped.
        {"RuleID": "other", "File": "other.py", "StartLine": 2,
         "Secret": "XYZ123"},
    ]
    out = sectools.run_security_tools(
        tmp_path, ["app.py"], DIFF,
        _runner({"tool": "gitleaks", "res": _res(json.dumps(entries))}))
    blockers = [f for f in out["findings"] if f["severity"] == "blocker"]
    assert len(blockers) == 1
    assert "gitleaks:aws-key" in blockers[0]["detail"]
    # Matched secret text never emitted — only a non-reversible
    # fingerprint for rotation checks.
    assert "AKIAFAKESECRET12345" not in blockers[0]["detail"]
    assert "AKIA" not in blockers[0]["detail"]
    import hashlib
    fp = hashlib.sha256(b"AKIAFAKESECRET12345").hexdigest()[:8]
    assert fp in blockers[0]["detail"]


def test_gitleaks_test_path_downgrades(tmp_path):
    _touch(tmp_path, "tests/test_app.py")
    diff = (
        "diff --git a/tests/test_app.py b/tests/test_app.py\n"
        "--- a/tests/test_app.py\n"
        "+++ b/tests/test_app.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+a\n"
        "+b\n"
    )
    entries = [{"RuleID": "tok", "File": "tests/test_app.py",
                "StartLine": 2, "Secret": "fixture-secret-value"}]
    out = sectools.run_security_tools(
        tmp_path, ["tests/test_app.py"], diff,
        _runner({"tool": "gitleaks", "res": _res(json.dumps(entries))}))
    hits = [f for f in out["findings"] if "Committed secret" in f["title"]]
    assert hits and hits[0]["severity"] == "warning"
    assert "test file" in hits[0]["title"]


def test_trivy_severity_and_per_file(tmp_path):
    _touch(tmp_path, "Dockerfile", "deploy/app.yaml", "src/a.py")
    calls = []
    doc = {"Results": [{"Target": "x", "Misconfigurations": [
        {"ID": "DS001", "Severity": "HIGH", "Title": "root user",
         "CauseMetadata": {"StartLine": 3}},
        {"ID": "DS099", "Severity": "LOW", "Title": "minor"},
    ]}]}

    def run(repo, argv, timeout):
        if argv[0] == "trivy":
            calls.append(argv[-1])
            return _res(json.dumps(doc))
        return _res("{}" if argv[0] == "semgrep" else "[]")
    out = sectools.run_security_tools(
        tmp_path, ["Dockerfile", "deploy/app.yaml", "src/a.py"], DIFF, run)
    assert set(calls) == {"./Dockerfile", "./deploy/app.yaml"}
    tv = [f for f in out["findings"] if f["title"].startswith("trivy:")]
    assert any(f["severity"] == "warning" for f in tv)
    assert any(f["severity"] == "info" for f in tv)


def test_max_findings_cap(tmp_path, monkeypatch):
    _touch(tmp_path, "app.py")
    monkeypatch.setattr(config, "SEC_MAX_FINDINGS", 2)
    payload = {"results": [
        {"check_id": f"r{i}", "path": "app.py", "start": {"line": i},
         "extra": {"severity": "ERROR", "message": "m"}}
        for i in range(5)]}
    out = sectools.run_security_tools(
        tmp_path, ["app.py"], DIFF,
        _runner({"tool": "semgrep", "res": _res(json.dumps(payload))}))
    sg = [f for f in out["findings"] if f["title"].startswith("semgrep:r")]
    assert len(sg) == 2
    assert any("truncated" in f["title"] for f in out["findings"])


def test_suppression_directives_warn(tmp_path):
    _touch(tmp_path, ".gitleaksignore", "app.py")
    diff = DIFF + "+# nosemgrep: python-eval-exec\n"
    out = sectools.run_security_tools(
        tmp_path, [".gitleaksignore", "app.py"], diff,
        _runner({"tool": "gitleaks", "res": _res("[]")}))
    warns = [f["title"] for f in out["findings"]
             if f["severity"] == "warning"]
    assert "Scanner ignore file added" in warns
    assert "Scanner suppression directive added" in warns


def test_pgids_and_processes_recorded(tmp_path):
    _touch(tmp_path, "app.py")
    out = sectools.run_security_tools(
        tmp_path, ["app.py"], DIFF,
        _runner({"tool": "semgrep",
                 "res": _res(stdout="{}", pgid=4242)}))
    assert 4242 in out["pgids"]
    assert any(p["command"].startswith("semgrep")
               for p in out["processes"])


def test_security_specialist_merges_scans(monkeypatch, tmp_path):
    from pr_review_orch import agents
    canned = {"findings": [{"agent": "security", "severity": "warning",
                            "title": "semgrep:x", "detail": "d",
                            "file": "app.py"}],
              "pgids": [99],
              "processes": [{"agent": "security", "command": "semgrep",
                             "duration_s": 1.0, "exit_code": 0,
                             "timed_out": False}]}
    seen = {}

    def fake(repo, files, diff_text, run):
        seen["files"] = files
        seen["diff"] = diff_text
        return canned
    monkeypatch.setattr(agents.sectools, "run_security_tools", fake)
    monkeypatch.setattr(agents, "_dependency_audit",
                        lambda *a, **k: {"findings": [], "pgids": [],
                                         "processes": []})
    monkeypatch.setattr(agents, "_llm_enrich",
                        lambda *a, **k: {"findings": [],
                                         "llm_calls": [],
                                         "active_pgids": []})
    state = {"repo_path": str(tmp_path), "signals": {"diff_text": DIFF},
             "files": [{"filename": "app.py"}]}
    res = agents.security_specialist(state)
    assert seen["files"] == ["app.py"] and seen["diff"] == DIFF
    assert any(f["title"] == "semgrep:x" for f in res["findings"])
    assert 99 in res["active_pgids"]
    assert any(p["command"] == "semgrep" for p in res["processes"])

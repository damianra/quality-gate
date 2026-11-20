"""FR-O29 security scanning toolchain — trigger gating, defensive JSON
parsing, severity mapping, added-line filtering, redaction, finding
caps and degrade paths. FR-O30: config pinning, suppression
counter-scan, single-spawn trivy and concurrent execution. The runner
is injected so no real process is ever spawned.
"""

from __future__ import annotations

import json

import pytest

from pr_review_orch import config, sectools


def _res(stdout="", exit_code=0, timed_out=False, stderr="", pgid=-1):
    return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code,
            "timed_out": timed_out, "pgid": pgid, "duration_s": 0.1}


def _runner(*responses):
    """Dispatch canned results by argv[0]; each response is a dict
    {"tool": name, "res": result} or {"_argv": fn, "res": result}."""
    calls = []
    outsides = []
    masks = []

    def run(repo, argv, timeout, *, outside=False, mask=None):
        calls.append(list(argv))
        outsides.append(outside)
        masks.append(mask)
        tool = argv[0]
        for i, spec in enumerate(responses):
            match = spec.get("_argv") if isinstance(spec, dict) else None
            key = spec.get("tool") if isinstance(spec, dict) else None
            if (match is None and key == tool) or (
                    match is not None and match(argv)):
                return spec["res"] if isinstance(spec, dict) else spec(argv)
        return _res(exit_code=127, stderr=f"{tool}: command not found")
    run.calls = calls
    run.outsides = outsides
    run.masks = masks
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


def test_added_line_ranges_quoted_path():
    # git C-quotes non-ASCII paths (core.quotePath): the header must be
    # unquoted or the file's ranges silently drop (fail-open).
    diff = (
        'diff --git "a/fi\\303\\251le.py" "b/fi\\303\\251le.py"\n'
        '--- "a/fi\\303\\251le.py"\n'
        '+++ "b/fi\\303\\251le.py"\n'
        "@@ -0,0 +1,1 @@\n"
        "+secret\n"
    )
    r = sectools.added_line_ranges(diff)
    assert r == {"fiéle.py": {1}}


def test_triggers_only_with_matching_files(tmp_path):
    _touch(tmp_path, "app.py", "Dockerfile", "readme.md")
    calls = []

    def run(repo, argv, timeout, *, outside=False, mask=None):
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
    # FR-O32: the matched line rides the finding's `line` field, not
    # the detail text.
    assert blockers[0]["file"] == "app.py" and blockers[0]["line"] == 2


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


def test_trivy_single_spawn_target_filter(tmp_path):
    # FR-O30: one trivy spawn over the repo root; Results[].Target
    # post-filtered to the changed IaC set. Findings on other files are
    # pre-existing noise and dropped.
    _touch(tmp_path, "Dockerfile", "deploy/app.yaml", "src/a.py")
    calls = []
    doc = {"Results": [
        {"Target": "Dockerfile", "Misconfigurations": [
            {"ID": "DS001", "Severity": "HIGH", "Title": "root user",
             "CauseMetadata": {"StartLine": 3}},
            {"ID": "DS099", "Severity": "LOW", "Title": "minor"},
        ]},
        {"Target": "other/base.tf", "Misconfigurations": [
            {"ID": "DS777", "Severity": "CRITICAL", "Title": "not ours"},
        ]},
    ]}

    def run(repo, argv, timeout, *, outside=False, mask=None):
        if argv[0] == "trivy":
            calls.append(argv)
            return _res(json.dumps(doc))
        return _res("{}" if argv[0] == "semgrep" else "[]")
    out = sectools.run_security_tools(
        tmp_path, ["Dockerfile", "deploy/app.yaml", "src/a.py"], DIFF, run)
    assert len(calls) == 1
    argv = calls[0]
    assert "--ignorefile" in argv and "" in argv
    assert argv[-1] == sectools._repo_target(tmp_path)
    tv = [f for f in out["findings"] if f["title"].startswith("trivy:")]
    assert {f["title"] for f in tv} == {"trivy:DS001", "trivy:DS099"}
    assert all(f["file"] == "Dockerfile" for f in tv)
    assert any(f["severity"] == "warning" for f in tv)
    assert any(f["severity"] == "info" for f in tv)


def test_scanners_pinned_against_repo_configs(tmp_path):
    # FR-O30 config pinning: gitleaks/trivy spawn outside the worktree
    # (cwd-resolved trivy.yaml never loads; gitleaks' .gitleaksignore
    # is target-resolved so it is /dev/null-masked instead); gitleaks
    # gets --ignore-gitleaks-allow + vendored -c; semgrep gets
    # --no-git-ignore + --jobs.
    _touch(tmp_path, "Dockerfile", "app.py")
    run = _runner({"tool": "gitleaks", "res": _res("[]")},
                  {"tool": "trivy", "res": _res("{}")},
                  {"tool": "semgrep", "res": _res("{}")},
                  {"tool": "rg", "res": _res("")})
    sectools.run_security_tools(
        tmp_path, ["Dockerfile", "app.py"], DIFF, run)
    by_tool = {c[0]: (c, o)
               for c, o in zip(run.calls, run.outsides)}
    gl_argv, gl_out = by_tool["gitleaks"]
    tv_argv, tv_out = by_tool["trivy"]
    sg_argv, _ = by_tool["semgrep"]
    assert gl_out is True and tv_out is True
    assert "--ignore-gitleaks-allow" in gl_argv
    assert "-c" in gl_argv and \
        gl_argv[gl_argv.index("-c") + 1].endswith("gitleaks.toml")
    # `.gitleaksignore` resolves against the scan target and -i is
    # additive-only: a /dev/null mount is the only effective pin.
    gl_mask = run.masks[run.calls.index(gl_argv)]
    assert gl_mask == [".gitleaksignore"]
    assert "--no-git-ignore" in sg_argv and "--jobs" in sg_argv


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


def test_stdout_content_never_marks_tool_missing(tmp_path):
    # Untrusted report text containing 'no such file' must not be
    # read as a missing binary — that would silently disable the tool.
    _touch(tmp_path, "app.py")
    entries = [{"RuleID": "x", "File": "app.py", "StartLine": 2,
                "Match": "no such file", "Secret": "s3cr3t-value-123"}]
    out = sectools.run_security_tools(
        tmp_path, ["app.py"], DIFF,
        _runner({"tool": "gitleaks", "res": _res(json.dumps(entries))}))
    assert any("Committed secret" in f["title"] for f in out["findings"])
    assert not any("gitleaks unavailable" in f["title"]
                   for f in out["findings"])


def test_gitleaks_allow_suppression_warns(tmp_path):
    _touch(tmp_path, "app.py")
    diff = DIFF + "+token = 'x'  # gitleaks:allow\n"
    out = sectools.run_security_tools(
        tmp_path, ["app.py"], diff,
        _runner({"tool": "gitleaks", "res": _res("[]")}))
    assert any(f["title"] == "Scanner suppression directive added"
               for f in out["findings"])


def test_disable_nosem_argv_is_not_a_directive(tmp_path):
    _touch(tmp_path, "app.py")
    diff = DIFF + '+    "--disable-nosem", "--quiet"\n'
    out = sectools.run_security_tools(
        tmp_path, ["app.py"], diff, _runner())
    assert not any(f["title"] == "Scanner suppression directive added"
                   for f in out["findings"])


def test_counter_scan_enumerates_preexisting(tmp_path):
    # FR-O30: a pre-existing ignore file and a pre-existing inline
    # directive surface as info findings; surfaces the PR itself
    # touched are already warnings and not duplicated.
    _touch(tmp_path, "app.py", "sub/.trivyignore")
    rg_out = (
        f"{tmp_path.resolve()}/app.py:5:# nosem  (pre-existing)\n"
        f"{tmp_path.resolve()}/app.py:2:# nosem  (added line)\n"
    )
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,1 +1,2 @@\n"
        " ctx\n"
        "+# nosem\n"
    )
    out = sectools.run_security_tools(
        tmp_path, ["app.py"], diff,
        _runner({"tool": "rg", "res": _res(rg_out)},
                {"tool": "gitleaks", "res": _res("[]")}))
    infos = {f["title"] for f in out["findings"]
             if f["severity"] == "info"}
    assert "Scanner ignore/config file present" in infos
    d = [f for f in out["findings"]
         if f["title"] == "Suppression directive present"]
    assert (len(d) == 1 and d[0]["file"] == "app.py"
            and d[0]["line"] == 5)
    # The PR-added .trivyignore-less file: sub/.trivyignore is not in
    # changed -> info. app.py:2 directive landed on an added line ->
    # deduped; FR-O29 warning fires instead.
    assert any(f["title"] == "Scanner suppression directive added"
               for f in out["findings"])
    assert not any(f["file"] == "sub/.trivyignore"
                   and f["severity"] == "warning"
                   for f in out["findings"])


def test_symlinks_excluded_from_targets(tmp_path):
    _touch(tmp_path, "app.py")
    (tmp_path / "evil.py").symlink_to("/etc/hostname")
    calls = []
    diff = DIFF

    def run(repo, argv, timeout, *, outside=False, mask=None):
        calls.append(argv)
        return _res("{}" if argv[0] == "semgrep" else "[]")
    sectools.run_security_tools(
        tmp_path, ["app.py", "evil.py"], diff, run)
    sem = next(c for c in calls if c[0] == "semgrep")
    assert not any("evil.py" in a for a in sem)


def test_gitleaks_stdout_scrubbed(tmp_path):
    # The raw report is the only place secret text exists — it must be
    # blanked after parsing.
    _touch(tmp_path, "app.py")
    entries = [{"RuleID": "x", "File": "app.py", "StartLine": 2,
                "Secret": "scrub-me-now"}]
    res = _res(json.dumps(entries))

    def run(repo, argv, timeout, *, outside=False, mask=None):
        if argv[0] == "gitleaks":
            return res
        return _res("[]")
    sectools.run_security_tools(tmp_path, ["app.py"], DIFF, run)
    assert res["stdout"] == ""


def test_pgids_and_processes_recorded(tmp_path):
    _touch(tmp_path, "app.py")
    out = sectools.run_security_tools(
        tmp_path, ["app.py"], DIFF,
        _runner({"tool": "semgrep",
                 "res": _res(stdout="{}", pgid=4242)}))
    assert 4242 in out["pgids"]
    assert any(p["command"].startswith("semgrep")
               for p in out["processes"])


def test_triggered_routing_surface():
    # gitleaks (default-enabled) scans any changed file.
    assert sectools.triggered(["README.md"])
    assert not sectools.triggered([])
    orig = config.SEC_TOOLS
    try:
        config.SEC_TOOLS = frozenset({"semgrep"})
        assert sectools.triggered(["a.py"])
        assert sectools.triggered(["Dockerfile"])  # generic/IaC surface
        assert not sectools.triggered(["README.md"])
        config.SEC_TOOLS = frozenset({"trivy"})
        assert sectools.triggered(["deploy/app.yaml"])
        assert not sectools.triggered(["a.py"])
        config.SEC_TOOLS = frozenset()
        assert not sectools.triggered(["a.py"])
    finally:
        config.SEC_TOOLS = orig


def test_choose_agents_routes_security_on_scanner_surface():
    from pr_review_orch import agents
    # No signals at all — FR-O29 triggers on files alone.
    res = agents.choose_agents({}, [{"filename": "app.py"}])
    assert "security" in res
    assert agents.choose_agents({}, []) == ["review"]


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

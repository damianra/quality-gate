"""FR-O32: findings → SARIF 2.1.0 conversion and QG_SARIF_PATH emission.

One run per agent value, `info|warning|blocker` → `note|warning|error`,
`file`/`line` → `artifactLocation.uri`/`region.startLine` (both omitted
when absent), disabled-path no-op and relative-path resolution.
"""

from __future__ import annotations

import json

from pr_review_orch import config, sarif


def _f(agent="security", severity="warning", title="t",
       detail="d", file=None, line=None):
    return {"agent": agent, "severity": severity, "title": title,
            "detail": detail, "file": file, "line": line}


def test_runs_grouped_per_agent():
    log = sarif.findings_to_sarif([
        _f(agent="security", title="semgrep:x"),
        _f(agent="security", title="trivy:y"),
        _f(agent="review", title="Oversized PR"),
    ])
    assert log["version"] == "2.1.0"
    assert log["$schema"].endswith("sarif-2.1.0.json")
    drivers = {r["tool"]["driver"]["name"] for r in log["runs"]}
    assert drivers == {"quality-gate-review", "quality-gate-security"}
    sec = next(r for r in log["runs"]
               if r["tool"]["driver"]["name"] == "quality-gate-security")
    assert {r["ruleId"] for r in sec["results"]} == {
        "semgrep:x", "trivy:y"}
    assert {r["id"] for r in sec["tool"]["driver"]["rules"]} == {
        "semgrep:x", "trivy:y"}


def test_severity_level_map():
    log = sarif.findings_to_sarif([
        _f(severity="info"), _f(severity="warning"),
        _f(severity="blocker"), _f(severity="bogus"),
    ])
    levels = [r["level"] for r in log["runs"][0]["results"]]
    assert levels == ["note", "warning", "error", "note"]


def test_file_and_line_become_location():
    log = sarif.findings_to_sarif([
        _f(file="src/app.py", line=42),
        _f(file="src/only_file.py"),
        _f(),
        _f(file="x.py", line=0),      # non-positive drops the region
        _f(file="y.py", line="7"),    # non-int drops the region
    ])
    results = log["runs"][0]["results"]
    loc = results[0]["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "src/app.py"
    assert loc["region"]["startLine"] == 42
    loc = results[1]["locations"][0]["physicalLocation"]
    assert "region" not in loc
    assert "locations" not in results[2]
    assert "region" not in results[3]["locations"][0]["physicalLocation"]
    assert "region" not in results[4]["locations"][0]["physicalLocation"]


def test_empty_findings_still_emits_a_run():
    log = sarif.findings_to_sarif([])
    assert len(log["runs"]) == 1
    assert log["runs"][0]["tool"]["driver"]["name"] == "quality-gate"
    assert log["runs"][0]["results"] == []


def test_column_kind_and_version():
    log = sarif.findings_to_sarif([_f()], version="1.2.3")
    run = log["runs"][0]
    assert run["columnKind"] == "utf16CodeUnits"
    assert run["tool"]["driver"]["version"] == "1.2.3"


def test_emit_disabled(monkeypatch, tmp_path):
    for v in ("", "0", "off", "false", "NONE"):
        monkeypatch.setattr(config, "SARIF_PATH", v)
        assert sarif.emit([_f()], str(tmp_path)) is None
    assert not list(tmp_path.iterdir())


def test_emit_relative_resolves_against_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SARIF_PATH", "out/results.sarif")
    written = sarif.emit([_f(file="a.py", line=1)], str(tmp_path))
    assert written == tmp_path / "out/results.sarif"
    log = json.loads(written.read_text())
    assert log["runs"][0]["results"][0]["ruleId"] == "t"


def test_emit_absolute_path(monkeypatch, tmp_path):
    target = tmp_path / "abs.sarif"
    monkeypatch.setattr(config, "SARIF_PATH", str(target))
    assert sarif.emit([], str(tmp_path)) == target
    assert target.exists()

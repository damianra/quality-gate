"""FR-O28 functional stage: contract parsing, autodetect, severity
mapping and the sandbox/host fallback policy. Process execution is
stubbed — no real commands or containers run."""

from pathlib import Path

import pytest

from pr_review_orch import config, functional, sandbox


def _spawn(exit_code=0, timed_out=False, pgid=111):
    return {"pgid": pgid, "exit_code": exit_code, "stdout": "",
            "stderr": "boom\n" if exit_code else "", "timed_out": timed_out,
            "command": [], "duration_s": 0.5}


def _no_sandbox(monkeypatch):
    monkeypatch.setattr(functional.sandbox, "available", lambda: False)


# -- contract loading --------------------------------------------------------- #

def test_contract_missing_falls_to_autodetect(tmp_path):
    steps, err = functional.load_contract(tmp_path)
    assert steps == [] and err is None


def test_contract_valid_steps(tmp_path):
    (tmp_path / "quality-gate.yaml").write_text(
        "steps:\n"
        "  - name: tests\n"
        "    argv: [uv, run, pytest]\n"
        "    required: true\n"
        "    net: true\n"
        "  - name: lint\n"
        "    argv: [ruff, check]\n")
    steps, err = functional.load_contract(tmp_path)
    assert err is None
    assert steps[0] == {"name": "tests", "argv": ["uv", "run", "pytest"],
                        "timeout": config.FUNC_TIMEOUT,
                        "required": True, "net": True}
    assert steps[1]["required"] is False


def test_contract_malformed_reports_error(tmp_path):
    (tmp_path / "quality-gate.yaml").write_text("steps: not-a-list\n")
    steps, err = functional.load_contract(tmp_path)
    assert steps == [] and err
    (tmp_path / "quality-gate.yaml").write_text(
        "steps:\n  - name: bad\n    argv: []\n")
    steps, err = functional.load_contract(tmp_path)
    assert steps == [] and err


def test_contract_step_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "FUNC_MAX_STEPS", 1)
    (tmp_path / "quality-gate.yaml").write_text(
        "steps:\n"
        "  - {name: a, argv: ['true']}\n"
        "  - {name: b, argv: ['true']}\n")
    steps, _ = functional.load_contract(tmp_path)
    assert [s["name"] for s in steps] == ["a"]


def test_autodetect_ecosystems(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "package.json").write_text("{}")
    steps = functional.autodetect(tmp_path)
    names = [s["name"] for s in steps]
    assert "test-pyproject" in names and "test-package" in names
    assert all(s["required"] is False for s in steps)


# -- run_functional ------------------------------------------------------------ #

def test_functional_no_steps_info(tmp_path, monkeypatch):
    _no_sandbox(monkeypatch)
    out = functional.run_functional(tmp_path)
    assert out["findings"][0]["severity"] == "info"
    assert "skipped" in out["findings"][0]["title"].lower()


def test_functional_skips_when_no_sandbox_and_exec_disabled(
        tmp_path, monkeypatch):
    _no_sandbox(monkeypatch)
    monkeypatch.setattr(config, "EXEC_VALIDATORS", False)
    (tmp_path / "pyproject.toml").write_text("")
    called = []
    monkeypatch.setattr(functional.mcp_tools, "spawn_process",
                        lambda *a, **k: called.append(a) or _spawn())
    out = functional.run_functional(tmp_path)
    assert called == []
    f = out["findings"][0]
    assert f["severity"] == "info" and "sandbox" in f["detail"]


def test_functional_host_fallback_marks_info(tmp_path, monkeypatch):
    _no_sandbox(monkeypatch)
    monkeypatch.setattr(config, "EXEC_VALIDATORS", True)
    (tmp_path / "quality-gate.yaml").write_text(
        "steps:\n  - {name: t, argv: ['true'], required: true}\n")
    monkeypatch.setattr(functional.mcp_tools, "spawn_process",
                        lambda *a, **k: _spawn())
    out = functional.run_functional(tmp_path)
    titles = [f["title"] for f in out["findings"]]
    assert "Functional validation on host" in titles
    proc = out["processes"][0]
    assert proc["sandboxed"] is False and proc["name"] == "t"


def test_functional_required_failure_is_blocker(tmp_path, monkeypatch):
    _no_sandbox(monkeypatch)
    monkeypatch.setattr(config, "EXEC_VALIDATORS", True)
    (tmp_path / "quality-gate.yaml").write_text(
        "steps:\n  - {name: t, argv: ['false'], required: true}\n")
    monkeypatch.setattr(functional.mcp_tools, "spawn_process",
                        lambda *a, **k: _spawn(exit_code=1))
    out = functional.run_functional(tmp_path)
    sev = [f["severity"] for f in out["findings"]
           if f["title"].startswith("Functional step failed")]
    assert sev == ["blocker"]


def test_functional_optional_failure_and_timeout_are_warnings(
        tmp_path, monkeypatch):
    _no_sandbox(monkeypatch)
    monkeypatch.setattr(config, "EXEC_VALIDATORS", True)
    (tmp_path / "quality-gate.yaml").write_text(
        "steps:\n"
        "  - {name: a, argv: ['false']}\n"
        "  - {name: b, argv: ['slow']}\n")
    results = iter([_spawn(exit_code=2), _spawn(timed_out=True)])
    monkeypatch.setattr(functional.mcp_tools, "spawn_process",
                        lambda *a, **k: next(results))
    out = functional.run_functional(tmp_path)
    sevs = {(f["severity"], f["title"]) for f in out["findings"]}
    assert ("warning", "Functional step failed: a") in sevs
    assert ("warning", "Functional step timed out: b") in sevs


def test_functional_sandbox_path(tmp_path, monkeypatch):
    monkeypatch.setattr(functional.sandbox, "available", lambda: True)
    seen = []
    monkeypatch.setattr(
        functional.sandbox, "run",
        lambda repo, argv, timeout, *, net=False:
            seen.append((argv, net)) or
            {**_spawn(), "sandboxed": True, "container": "qg-1-1"})
    (tmp_path / "quality-gate.yaml").write_text(
        "steps:\n  - {name: t, argv: ['pytest'], net: true}\n")
    out = functional.run_functional(tmp_path)
    assert seen == [(["pytest"], True)]
    assert out["processes"][0]["sandboxed"] is True
    assert out["findings"] == []


def test_functional_malformed_contract_warns_and_autodetects(
        tmp_path, monkeypatch):
    _no_sandbox(monkeypatch)
    monkeypatch.setattr(config, "EXEC_VALIDATORS", True)
    (tmp_path / "quality-gate.yaml").write_text("steps: oops\n")
    (tmp_path / "pyproject.toml").write_text("")
    monkeypatch.setattr(functional.mcp_tools, "spawn_process",
                        lambda *a, **k: _spawn())
    out = functional.run_functional(tmp_path)
    titles = [f["title"] for f in out["findings"]]
    assert "Functional contract malformed" in titles
    assert out["processes"][0]["name"] == "test-pyproject"

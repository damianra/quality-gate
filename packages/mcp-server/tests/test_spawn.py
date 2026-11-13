"""spawn_process: process-group tracking and timeout kill behavior."""

import os
import signal

import pytest

from quality_gate_mcp.tools import search_code, spawn_process


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def test_spawn_returns_pgid_and_output(tmp_path):
    res = spawn_process(str(tmp_path), "echo", ["hello"])
    assert res["exit_code"] == 0
    assert res["stdout"].strip() == "hello"
    assert res["pgid"] > 0
    assert not res["timed_out"]


def test_spawn_uses_own_process_group(tmp_path):
    res = spawn_process(str(tmp_path), "echo", ["x"])
    # pgid must differ from our own group -> safe killpg target
    assert res["pgid"] != os.getpgrp()


def test_spawn_timeout_kills_group(tmp_path):
    res = spawn_process(str(tmp_path), "sleep", ["30"], timeout=1)
    assert res["timed_out"]
    assert res["exit_code"] != 0
    assert not _group_alive(res["pgid"])


def test_spawn_command_not_found(tmp_path):
    res = spawn_process(str(tmp_path), "definitely-not-a-real-cmd-xyz", [])
    assert res["exit_code"] == 127
    assert "not found" in res["stderr"]


def test_spawn_never_uses_shell(tmp_path):
    # argv is literal: "echo a; touch pwned" must NOT create a file
    res = spawn_process(str(tmp_path), "echo", ["a; touch pwned"])
    assert res["exit_code"] == 0
    assert not (tmp_path / "pwned").exists()


def test_spawn_env_is_scrubbed(tmp_path, monkeypatch):
    """FR-P11: spawned children see an allowlist env, never secrets.

    ``/proc/<pid>/environ`` invariant: a validator running PR-controlled
    code must not find GITHUB_TOKEN / DATABASE_URL / webhook secrets in
    its environment; PATH/HOME still resolve.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "secret-tok")
    monkeypatch.setenv("DATABASE_URL", "postgres://x")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "whsec")
    res = spawn_process(
        str(tmp_path), "python3",
        ["-c",
         "import os\n"
         "for k in ('GITHUB_TOKEN', 'DATABASE_URL',"
         " 'GITHUB_WEBHOOK_SECRET', 'PATH', 'HOME'):\n"
         "    print(k, 'set' if os.environ.get(k) else 'unset')"])
    env_lines = dict(l.split() for l in res["stdout"].splitlines())
    assert env_lines["GITHUB_TOKEN"] == "unset"
    assert env_lines["DATABASE_URL"] == "unset"
    assert env_lines["GITHUB_WEBHOOK_SECRET"] == "unset"
    assert env_lines["PATH"] == "set"
    assert env_lines["HOME"] == "set"


def test_spawn_explicit_env_passthrough(tmp_path, monkeypatch):
    """FR-P11: an explicit env (git auth headers) bypasses scrubbing."""
    from quality_gate_mcp import git_ops
    res = git_ops.spawn_tracked(
        str(tmp_path), "python3",
        ["-c", "import os; print(os.environ.get('MARKER', 'unset'))"],
        env={"PATH": os.environ["PATH"], "MARKER": "yes"})
    assert res["stdout"].strip() == "yes"


def test_search_code_rg(tmp_path):
    (tmp_path / "a.ts").write_text("const needle = 1;\nconst hay = 2;\n")
    pytest.importorskip("subprocess")
    res = spawn_process(str(tmp_path), "rg", ["--version"])
    if res["exit_code"] == 127:
        pytest.skip("ripgrep not installed")
    matches = search_code(str(tmp_path), "needle")
    assert matches == [{"file": "a.ts", "line": 1, "text": "const needle = 1;"}]

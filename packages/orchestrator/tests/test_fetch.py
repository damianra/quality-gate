"""Ephemeral-worktree tests — git spawns are faked; no network."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pr_review_orch import config, fetch

_OK = {
    "pgid": 1,
    "exit_code": 0,
    "stdout": "",
    "stderr": "",
    "timed_out": False,
}


@pytest.fixture
def spawn_calls(monkeypatch, tmp_path):
    """Fake spawn_tracked that records argv/env; all steps succeed."""
    monkeypatch.setattr(config, "WORKTREE_ROOT", str(tmp_path))
    calls: list[dict] = []

    def fake(repo_path, command, args, timeout=None, env=None):
        calls.append({
            "cwd": repo_path, "command": command, "args": list(args),
            "timeout": timeout, "env": env,
        })
        return dict(_OK)

    monkeypatch.setattr(fetch, "spawn_tracked", fake)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    return calls


def test_clone_uses_env_header_not_argv(spawn_calls) -> None:
    worktree = fetch.clone_pr(
        "https://github.com/acme/repo.git", "deadbeef" * 5
    )
    # 3 git steps: clone (blob:none, no-checkout) + fetch sha + checkout
    assert len(spawn_calls) == 3
    clone_args = spawn_calls[0]["args"]
    assert clone_args[0] == "clone"
    assert "--filter=blob:none" in clone_args
    assert spawn_calls[1]["args"][:2] == ["fetch", "--depth"]
    assert spawn_calls[2]["args"] == ["checkout", "FETCH_HEAD"]
    # INV-3: token travels via GIT_CONFIG env header, never in argv.
    for call in spawn_calls:
        assert all("secret-token" not in str(a) for a in call["args"])
        assert call["env"]["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert "Basic" in spawn_calls[0]["env"]["GIT_CONFIG_VALUE_0"]
    assert worktree.exists()


def test_clone_removes_worktree_on_failure(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(config, "WORKTREE_ROOT", str(tmp_path))

    def boom(*a, **kw):
        return {**_OK, "exit_code": 128}

    monkeypatch.setattr(fetch, "spawn_tracked", boom)
    with pytest.raises(fetch.FetchError):
        fetch.clone_pr("https://github.com/acme/repo.git", "deadbeef")
    assert list(tmp_path.iterdir()) == []  # no qg-* left behind


def test_clone_rejects_oversized_worktree(
    spawn_calls, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(fetch, "_dir_size", lambda _p: 10 * 1024**3)
    monkeypatch.setattr(config, "MAX_WORKTREE_BYTES", 1024)
    with pytest.raises(fetch.FetchError, match="exceeds cap"):
        fetch.clone_pr("https://github.com/acme/repo.git", "deadbeef")
    assert list(tmp_path.iterdir()) == []


def test_clone_retries_transient_fault(tmp_path, monkeypatch) -> None:
    """FR-P3: a transient git failure retries; success on 2nd attempt."""
    monkeypatch.setattr(config, "WORKTREE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(config, "RETRY_BACKOFF_S", 0)
    attempts = {"n": 0}

    def flaky(url, sha):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise fetch.FetchError("git clone failed (exit=128)")
        return tmp_path / "qg-ok"

    monkeypatch.setattr(fetch, "_clone_once", flaky)
    assert fetch.clone_pr("u", "s") == tmp_path / "qg-ok"
    assert attempts["n"] == 2


def test_clone_does_not_retry_timeout_or_cap(
    tmp_path, monkeypatch
) -> None:
    """FR-P3: timeouts and size-cap failures are permanent."""
    monkeypatch.setattr(config, "WORKTREE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(config, "RETRY_BACKOFF_S", 0)
    for msg in ("git clone failed (exit=None, timed_out=True)",
                "worktree exceeds cap: 99 > 1 bytes"):
        attempts = {"n": 0}

        def fail(url, sha, _msg=msg):
            attempts["n"] += 1
            raise fetch.FetchError(_msg)

        monkeypatch.setattr(fetch, "_clone_once", fail)
        with pytest.raises(fetch.FetchError):
            fetch.clone_pr("u", "s")
        assert attempts["n"] == 1


def test_clone_exhausts_attempts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "WORKTREE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(config, "RETRY_BACKOFF_S", 0)

    def fail(url, sha):
        raise fetch.FetchError("git clone failed (exit=128)")

    monkeypatch.setattr(fetch, "_clone_once", fail)
    with pytest.raises(fetch.FetchError, match="exit=128"):
        fetch.clone_pr("u", "s")


def test_sweep_stale_worktrees(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "WORKTREE_ROOT", str(tmp_path))
    monkeypatch.setattr(fetch, "_kill_orphans", lambda: 0)
    (tmp_path / "qg-stale1").mkdir()
    (tmp_path / "qg-stale2").mkdir()
    (tmp_path / "keep-me").mkdir()
    assert fetch.sweep_stale_worktrees() == 2
    assert (tmp_path / "keep-me").exists()
    assert not (tmp_path / "qg-stale1").exists()


def test_sweep_kills_orphan_process_groups(tmp_path, monkeypatch) -> None:
    """FR-P7: pgrep hits get killpg'd before the dir sweep runs."""
    monkeypatch.setattr(config, "WORKTREE_ROOT", str(tmp_path))
    (tmp_path / "qg-stale").mkdir()

    class _Out:
        stdout = "4242\nnot-a-pid\n4343\n"

    monkeypatch.setattr(
        fetch.subprocess, "run", lambda *a, **k: _Out())
    monkeypatch.setattr(fetch, "_owns", lambda pid, root: True)
    pgids = {4242: 4242, 4343: 4343}
    monkeypatch.setattr(
        fetch.os, "getpgid", lambda pid: pgids[pid])
    killed: list[int] = []
    monkeypatch.setattr(
        fetch.os, "killpg", lambda pgid, sig: killed.append(pgid))
    assert fetch.sweep_stale_worktrees() == 1
    assert killed == [4242, 4343]       # bad line skipped
    assert not (tmp_path / "qg-stale").exists()


def test_sweep_never_kills_decoy_process(tmp_path, monkeypatch) -> None:
    """FR-P20: a signature-matching process NOT attributable to a
    worktree (e.g. a dev's own cargo check) is never killed."""
    monkeypatch.setattr(config, "WORKTREE_ROOT", str(tmp_path))

    class _Out:
        stdout = "4242\n4343\n"

    monkeypatch.setattr(
        fetch.subprocess, "run", lambda *a, **k: _Out())
    # 4242 runs inside a qg-* worktree; 4343 lives in the user's repo.
    cwds = {4242: tmp_path / "qg-dead" / "pkg",
            4343: Path("/home/dev/their-repo")}
    monkeypatch.setattr(fetch, "_proc_cwd", lambda pid: cwds[pid])
    monkeypatch.setattr(fetch, "_proc_argv",
                        lambda pid: ["cargo", "check"])
    monkeypatch.setattr(fetch.os, "getpgid", lambda pid: pid)
    killed: list[int] = []
    monkeypatch.setattr(
        fetch.os, "killpg", lambda pgid, sig: killed.append(pgid))
    fetch.sweep_stale_worktrees()
    assert killed == [4242]            # decoy 4343 survived


def test_owns_via_argv_when_cwd_unreadable(tmp_path, monkeypatch) -> None:
    """FR-P20: argv attribution when /proc cwd can't be read."""
    root = tmp_path.resolve()
    argv = {1111: ["tsc", "--noEmit", "-p", str(root / "qg-x")],
            2222: ["tsc", "--noEmit"]}
    monkeypatch.setattr(fetch, "_proc_cwd", lambda pid: None)
    monkeypatch.setattr(fetch, "_proc_argv", lambda pid: argv[pid])
    assert fetch._owns(1111, root)
    assert not fetch._owns(2222, root)


def test_owns_unverifiable_is_not_ours(tmp_path, monkeypatch) -> None:
    """FR-P20: when neither cwd nor argv can be read, we don't kill."""
    monkeypatch.setattr(fetch, "_proc_cwd", lambda pid: None)
    monkeypatch.setattr(fetch, "_proc_argv", lambda pid: [])
    assert not fetch._owns(9999, tmp_path.resolve())


def test_sweep_survives_missing_pgrep(tmp_path, monkeypatch) -> None:
    """FR-P7: no procps -> dirs are still swept, no exception."""
    monkeypatch.setattr(config, "WORKTREE_ROOT", str(tmp_path))
    (tmp_path / "qg-stale").mkdir()

    def boom(*a, **k):
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr(fetch.subprocess, "run", boom)
    assert fetch.sweep_stale_worktrees() == 1
    assert not (tmp_path / "qg-stale").exists()


def test_doctor_report_maps_tool_resolution(monkeypatch) -> None:
    """FR-P13: startup report maps each tool to its path or None."""
    monkeypatch.setattr(
        fetch.shutil, "which",
        lambda t: f"/usr/bin/{t}" if t in {"rg", "node"} else None)
    report = fetch.doctor_report()
    assert report["rg"] == "/usr/bin/rg"
    assert report["osv-scanner"] is None
    assert set(report) == set(fetch._VALIDATOR_TOOLS)


def test_git_env_disables_prompts() -> None:
    env = fetch._git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_dir_size_sums_files(tmp_path) -> None:
    (tmp_path / "a").write_bytes(b"x" * 10)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b").write_bytes(b"y" * 5)
    assert fetch._dir_size(tmp_path) == 15

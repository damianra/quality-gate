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
    pgids = {4242: 4242, 4343: 4343}
    monkeypatch.setattr(
        fetch.os, "getpgid", lambda pid: pgids[pid])
    killed: list[int] = []
    monkeypatch.setattr(
        fetch.os, "killpg", lambda pgid, sig: killed.append(pgid))
    assert fetch.sweep_stale_worktrees() == 1
    assert killed == [4242, 4343]       # bad line skipped
    assert not (tmp_path / "qg-stale").exists()


def test_sweep_survives_missing_pgrep(tmp_path, monkeypatch) -> None:
    """FR-P7: no procps -> dirs are still swept, no exception."""
    monkeypatch.setattr(config, "WORKTREE_ROOT", str(tmp_path))
    (tmp_path / "qg-stale").mkdir()

    def boom(*a, **k):
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr(fetch.subprocess, "run", boom)
    assert fetch.sweep_stale_worktrees() == 1
    assert not (tmp_path / "qg-stale").exists()


def test_git_env_disables_prompts() -> None:
    env = fetch._git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_dir_size_sums_files(tmp_path) -> None:
    (tmp_path / "a").write_bytes(b"x" * 10)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b").write_bytes(b"y" * 5)
    assert fetch._dir_size(tmp_path) == 15

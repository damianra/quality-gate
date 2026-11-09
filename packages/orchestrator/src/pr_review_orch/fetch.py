"""Ephemeral PR worktrees for service mode (FR-O20).

Each accepted webhook event clones the repo at the PR head SHA into a
fresh ``mkdtemp`` directory under ``QG_WORKTREE_ROOT``; the worktree is
the run's ``REPO_PATH`` and is removed by the caller when the run ends.

Hard rules (mirrors INV-1/INV-3):
- ``shell=True`` is never used; clones go through
  ``quality_gate_mcp.git_ops.spawn_tracked`` so every git process lives in
  its own process group and is ``killpg``-terminated on
  ``QG_CLONE_TIMEOUT``.
- ``GITHUB_TOKEN`` is passed to git via the ``GIT_CONFIG_*`` environment
  (``http.extraHeader``) — never in the clone URL, argv, or logs.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import tempfile
from pathlib import Path

from quality_gate_mcp import config as mcp_config
from quality_gate_mcp.git_ops import spawn_tracked

from . import config

logger = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """The ephemeral worktree could not be produced (clone/fetch/size)."""


def _worktree_root() -> Path:
    root = Path(config.WORKTREE_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _git_env() -> dict[str, str]:
    """Git env with the token as an auth header — never in argv."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    token = mcp_config.github_token()
    if token:
        cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {cred}"
    return env


def _dir_size(path: Path) -> int:
    """Total file size under ``path`` (bytes); unreadable entries skipped."""
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
    return total


def _git(cwd: Path, args: list[str], step: str) -> None:
    """Run one git step; raise FetchError on timeout or non-zero exit."""
    res = spawn_tracked(
        str(cwd), "git", args, timeout=config.CLONE_TIMEOUT, env=_git_env()
    )
    if res["timed_out"] or res["exit_code"] != 0:
        raise FetchError(
            f"git {step} failed "
            f"(exit={res['exit_code']}, timed_out={res['timed_out']})"
        )


def clone_pr(clone_url: str, head_sha: str) -> Path:
    """Clone ``clone_url`` at ``head_sha`` into a fresh ``qg-*`` worktree.

    The clone is blob-filtered, fetched by SHA (works for forks too), and
    checked against ``QG_MAX_WORKTREE_BYTES``. Any failure removes the
    worktree before raising FetchError — the worktree never leaks.
    """
    worktree = Path(
        tempfile.mkdtemp(prefix="qg-", dir=str(_worktree_root()))
    )
    try:
        env = _git_env()
        res = spawn_tracked(
            str(worktree.parent),
            "git",
            ["clone", "--filter=blob:none", "--no-checkout",
             clone_url, str(worktree)],
            timeout=config.CLONE_TIMEOUT,
            env=env,
        )
        if res["timed_out"] or res["exit_code"] != 0:
            raise FetchError(
                f"git clone failed "
                f"(exit={res['exit_code']}, timed_out={res['timed_out']})"
            )
        # Fetch the PR head commit explicitly: head.sha may be stale on
        # synchronize deliveries and for forks it lives under refs/pull.
        _git(worktree, ["fetch", "--depth", "1", "origin", head_sha],
             "fetch")
        _git(worktree, ["checkout", "FETCH_HEAD"], "checkout")
        size = _dir_size(worktree)
        if size > config.MAX_WORKTREE_BYTES:
            raise FetchError(
                f"worktree exceeds cap: {size} > "
                f"{config.MAX_WORKTREE_BYTES} bytes"
            )
        return worktree
    except FetchError:
        shutil.rmtree(worktree, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(worktree, ignore_errors=True)
        raise FetchError(f"unexpected clone failure: {exc}")


def sweep_stale_worktrees() -> int:
    """Remove leftover ``qg-*`` dirs (crashed runs); return count removed."""
    removed = 0
    root = _worktree_root()
    for entry in root.iterdir():
        if entry.is_dir() and entry.name.startswith("qg-"):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("swept %d stale worktree(s) from %s", removed, root)
    return removed

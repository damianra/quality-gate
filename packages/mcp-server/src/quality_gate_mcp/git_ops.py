"""Git and subprocess helpers.

Hard rules implemented here:
- ``shell=True`` is never used. Commands are always ``[argv...]`` lists.
- Spawned processes run in a NEW process group (``start_new_session=True``)
  so the orchestrator can terminate the whole group via ``os.killpg``.
- Every long-running spawn returns its ``pgid`` so callers can register it
  in graph state (``active_pgids``) for the mandatory cleanup pass.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

from . import config

# --------------------------------------------------------------------------- #
# Remote parsing
# --------------------------------------------------------------------------- #

_REMOTE_RE = re.compile(
    r"^(?:"
    r"git@(?P<ssh_host>[^:/]+):(?P<ssh_path>.+)"          # git@github.com:owner/repo(.git)
    r"|ssh://git@(?P<sshurl_host>[^/]+)/(?P<sshurl_path>.+)"  # ssh://git@github.com/owner/repo
    r"|https?://(?P<http_host>[^/]+)/(?P<http_path>.+)"   # https://github.com/owner/repo(.git)
    r")$"
)


def parse_remote(url: str) -> str:
    """Parse a git remote URL into ``owner/repo``.

    Supports SSH (``git@host:owner/repo.git``), ``ssh://`` and HTTP(S).
    Raises ``ValueError`` for anything else (including non-GitHub hosts is
    fine — we return owner/repo regardless of host, the API base URL is
    configured separately).
    """
    url = url.strip()
    m = _REMOTE_RE.match(url)
    if not m:
        raise ValueError(f"unrecognized git remote URL: {url!r}")
    path = m.group("ssh_path") or m.group("sshurl_path") or m.group("http_path")
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = path.split("/")
    if len(parts) < 2 or not parts[-1] or not parts[-2]:
        raise ValueError(f"cannot extract owner/repo from remote URL: {url!r}")
    return f"{parts[-2]}/{parts[-1]}"


# --------------------------------------------------------------------------- #
# Subprocess primitives
# --------------------------------------------------------------------------- #


def _validate_repo(repo_path: str) -> Path:
    p = Path(repo_path).resolve()
    if not p.is_dir():
        raise ValueError(f"repo_path is not a directory: {repo_path}")
    return p


def run_subprocess(
    repo_path: str,
    argv: Sequence[str],
    timeout: int | None = None,
) -> dict[str, Any]:
    """Run a short-lived command in ``repo_path``. No shell. Always reaped.

    Intended for quick commands (git rev-parse, ripgrep). For potentially
    long-running commands use :func:`spawn_tracked` which exposes the pgid.
    """
    cwd = _validate_repo(repo_path)
    timeout = timeout if timeout is not None else config.GIT_TIMEOUT
    try:
        proc = subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"timed out after {timeout}s",
            "timed_out": True,
        }
    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "timed_out": False,
    }


def run_git(repo_path: str, args: Sequence[str], timeout: int | None = None) -> dict[str, Any]:
    """Run ``git <args>`` inside ``repo_path``."""
    return run_subprocess(repo_path, ["git", *args], timeout=timeout)


def _kill_group(pgid: int, grace: float = config.KILL_GRACE_SECONDS) -> None:
    """SIGTERM a process group, escalate to SIGKILL after ``grace`` seconds."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def spawn_tracked(
    repo_path: str,
    command: str,
    args: Sequence[str],
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Spawn ``command args`` in ``repo_path`` in a new process group.

    Returns a dict with ``pgid`` (always present, even on failure to launch),
    ``exit_code``, ``stdout``, ``stderr`` and ``timed_out``. On timeout the
    whole process group is terminated before returning, so no orphan can
    escape; the pgid is still returned so callers may double-check via
    ``os.killpg(pgid, 0)``.
    """
    cwd = _validate_repo(repo_path)
    timeout = timeout if timeout is not None else config.PROCESS_TIMEOUT
    argv = [command, *args]
    pgid = -1
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            start_new_session=True,  # -> own process group, killable via killpg
            env=env,
        )
        pgid = os.getpgid(proc.pid)
    except FileNotFoundError:
        return {
            "pgid": pgid,
            "exit_code": 127,
            "stdout": "",
            "stderr": f"command not found: {command}",
            "timed_out": False,
            "command": argv,
            "duration_s": round(time.monotonic() - started, 3),
        }
    except OSError as exc:
        return {
            "pgid": pgid,
            "exit_code": 126,
            "stdout": "",
            "stderr": f"failed to spawn {command}: {exc}",
            "timed_out": False,
            "command": argv,
            "duration_s": round(time.monotonic() - started, 3),
        }

    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(pgid)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", "process did not die after SIGKILL"
        if proc.returncode is None:
            proc.returncode = -9

    return {
        "pgid": pgid,
        "exit_code": proc.returncode,
        "stdout": stdout or "",
        "stderr": stderr or "",
        "timed_out": timed_out,
        "command": argv,
        "duration_s": round(time.monotonic() - started, 3),
    }


# --------------------------------------------------------------------------- #
# Repo queries
# --------------------------------------------------------------------------- #


def get_remote_url(repo_path: str, remote: str = "origin") -> str:
    res = run_git(repo_path, ["remote", "get-url", remote])
    if res["exit_code"] != 0:
        raise RuntimeError(f"git remote get-url failed: {res['stderr'].strip()}")
    return res["stdout"].strip()


def get_remote_slug(repo_path: str, remote: str = "origin") -> str:
    """Return ``owner/repo`` for the repo at ``repo_path``."""
    return parse_remote(get_remote_url(repo_path, remote))


def get_head_sha(repo_path: str, ref: str = "HEAD") -> str:
    res = run_git(repo_path, ["rev-parse", ref])
    if res["exit_code"] != 0:
        raise RuntimeError(f"git rev-parse {ref} failed: {res['stderr'].strip()}")
    return res["stdout"].strip()

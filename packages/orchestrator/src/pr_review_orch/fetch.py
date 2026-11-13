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
import signal
import subprocess
import tempfile
import time
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

    Retries transient git faults with exponential backoff (FR-P3):
    timeouts and worktree-size-cap failures are permanent for the
    delivery and never retry. Each attempt gets a fresh ``qg-*`` dir —
    the failed one is removed before the next.
    """
    last_exc: FetchError | None = None
    for attempt in range(config.RETRY_ATTEMPTS):
        try:
            return _clone_once(clone_url, head_sha)
        except FetchError as exc:
            last_exc = exc
            msg = str(exc)
            if "timed_out=True" in msg or "exceeds cap" in msg:
                raise  # permanent for this delivery — don't burn budget
            if attempt + 1 < config.RETRY_ATTEMPTS:
                logger.warning(
                    "clone attempt %d/%d failed (%s); retrying",
                    attempt + 1, config.RETRY_ATTEMPTS, exc)
                time.sleep(config.RETRY_BACKOFF_S * (2 ** attempt))
    raise last_exc  # type: ignore[misc]


def _clone_once(clone_url: str, head_sha: str) -> Path:
    """Single clone attempt — blob-filtered, fetched by SHA (works for
    forks too), checked against ``QG_MAX_WORKTREE_BYTES``. Any failure
    removes the worktree before raising FetchError — it never leaks.
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


#: Process signatures a crashed run may have leaked — same set the CI
#: orphan assertion pgrep's for (FR-P7).
_ORPHAN_PATTERN = (
    "tsc --noEmit|node --check|compile\\(fh\\.read|go vet|"
    "cargo check|rg --line-number"
)


def _proc_cwd(pid: int) -> Path | None:
    """The process's cwd via /proc, ``None`` when unreadable.

    A ``(deleted)`` suffix (worktree already swept) is stripped so the
    comparison still attributes the process correctly.
    """
    try:
        link = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None
    return Path(link.removesuffix(" (deleted)"))


def _proc_argv(pid: int) -> list[str]:
    """The process's argv via /proc, ``[]`` when unreadable."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def _owns(pid: int, root: Path) -> bool:
    """FR-P20: True iff the process is attributable to a ``qg-*``
    worktree — cwd inside ``QG_WORKTREE_ROOT``, or an argv referencing
    one. Unverifiable processes are NOT killed: the sweep must never
    hit a developer's own ``cargo check``/``tsc`` on the shared host.
    """
    cwd = _proc_cwd(pid)
    if cwd is not None and (cwd == root or root in cwd.parents):
        return True
    return any(
        str(root) in arg or arg.startswith("qg-")
        for arg in _proc_argv(pid)
    )


def _kill_orphans() -> int:
    """SIGKILL process groups matching validator/scanner signatures —
    but only those attributable to a ``qg-*`` worktree (FR-P20).

    Runs at ingress startup, before the worktree sweep: a ``kill -9``'d
    worker must not leak ``tsc``/``cargo``/``osv-scanner`` children
    (FR-P7). Spawned groups die whole via their pgid — matching the
    cleanup contract's killpg semantics.
    """
    try:
        out = subprocess.run(
            ["pgrep", "-f", _ORPHAN_PATTERN],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0  # no procps on this host — sweep dirs anyway
    root = _worktree_root().resolve()
    killed = 0
    for line in out.stdout.split():
        if not line.strip().isdigit():
            continue
        pid = int(line)
        if not _owns(pid, root):
            continue  # FR-P20: not ours — never kill it
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            killed += 1
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return killed


#: Binaries the graph may spawn — resolved at startup so a missing
#: toolchain is a logged gap, not a silent skip mid-run (FR-P13).
_VALIDATOR_TOOLS = (
    "rg", "tsc", "node", "go", "cargo",
    "osv-scanner", "npm", "pip-audit",
)


def doctor_report() -> dict[str, str | None]:
    """Log which validator/scanner binaries this image provides.

    Runs at ingress startup: the FR-O24 audit and QA validators skip
    gracefully when a tool is absent, so the report is the operator's
    way to know the deployment actually covers its repos.
    """
    report = {tool: shutil.which(tool) for tool in _VALIDATOR_TOOLS}
    missing = [t for t, p in report.items() if p is None]
    logger.info(
        "doctor: tools resolved=%s missing=%s",
        {t: p for t, p in report.items() if p}, missing or "none",
    )
    return report


def sweep_stale_worktrees() -> int:
    """Kill orphan validators + remove leftover ``qg-*`` dirs (FR-P7).

    Returns the count of directories removed (crashed runs).
    """
    killed = _kill_orphans()
    if killed:
        logger.warning(
            "killed %d orphan validator/scanner group(s)", killed)
    removed = 0
    root = _worktree_root()
    for entry in root.iterdir():
        if entry.is_dir() and entry.name.startswith("qg-"):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("swept %d stale worktree(s) from %s", removed, root)
    return removed

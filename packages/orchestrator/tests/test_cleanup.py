"""Cleanup node: every registered pgid must be terminated — no orphans."""

import os
import signal
import subprocess
import time

import pytest

from pr_review_orch.cleanup import terminate_pgids
from pr_review_orch.graph import cleanup_node


def _spawn_sleep_group() -> tuple[subprocess.Popen, int]:
    """Spawn `sleep` in its own process group; return (proc, pgid)."""
    proc = subprocess.Popen(
        ["sleep", "60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc, os.getpgid(proc.pid)


def _spawn_group_with_child() -> tuple[subprocess.Popen, int]:
    """Spawn a shell that forks a background child in the SAME group."""
    proc = subprocess.Popen(
        ["sh", "-c", "sleep 60 & sleep 60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc, os.getpgid(proc.pid)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def test_terminate_kills_simple_group():
    proc, pgid = _spawn_sleep_group()
    assert _group_alive(pgid)
    report = terminate_pgids([pgid], grace_seconds=2)
    proc.wait(timeout=5)
    assert not _group_alive(pgid)
    assert pgid in report["terminated"] + report["killed"]


def test_terminate_kills_group_with_background_children():
    """The whole GROUP dies — a `sleep 60 &` grandchild can't escape."""
    proc, pgid = _spawn_group_with_child()
    time.sleep(0.3)  # let the background child spawn
    report = terminate_pgids([pgid], grace_seconds=2)
    proc.wait(timeout=5)
    # poll for full group teardown (SIGKILL delivery can lag slightly)
    for _ in range(40):
        if not _group_alive(pgid):
            break
        time.sleep(0.05)
    assert not _group_alive(pgid)
    assert pgid in report["terminated"] + report["killed"]


def test_terminate_dead_group_is_safe():
    proc, pgid = _spawn_sleep_group()
    proc.terminate()
    proc.wait(timeout=5)
    for _ in range(40):
        if not _group_alive(pgid):
            break
        time.sleep(0.05)
    report = terminate_pgids([pgid], grace_seconds=1)
    assert pgid in report["already_dead"] + report["terminated"]


def test_terminate_empty_and_invalid_pgids():
    report = terminate_pgids([], grace_seconds=1)
    assert report == {"terminated": [], "killed": [], "already_dead": [], "failed": []}
    report = terminate_pgids([-1, 0], grace_seconds=1)
    assert sorted(report["failed"]) == [-1, 0]


def test_cleanup_node_terminates_state_pgids():
    proc, pgid = _spawn_sleep_group()
    try:
        result = cleanup_node({"active_pgids": [pgid]})
        proc.wait(timeout=5)
        assert not _group_alive(pgid)
        assert pgid in (
            result["cleanup_report"]["terminated"]
            + result["cleanup_report"]["killed"]
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_cleanup_node_empty_state():
    result = cleanup_node({"active_pgids": []})
    assert result["cleanup_report"]["terminated"] == []


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="posix only")
def test_sigterm_ignored_then_sigkill():
    """A group that ignores SIGTERM must still die via SIGKILL."""
    proc = subprocess.Popen(
        ["sh", "-c", "trap '' TERM; sleep 60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)
    time.sleep(0.3)  # ensure trap is installed
    report = terminate_pgids([pgid], grace_seconds=0.5)
    proc.wait(timeout=5)
    for _ in range(40):
        if not _group_alive(pgid):
            break
        time.sleep(0.05)
    assert not _group_alive(pgid)
    assert pgid in report["killed"] + report["terminated"]

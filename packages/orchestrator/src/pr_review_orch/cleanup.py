"""Forced process-group termination.

The non-negotiable invariant of this system: *no spawned process may
outlive the graph run*. Every pgid registered in ``state["active_pgids"]``
is terminated here — SIGTERM first, SIGKILL after a grace period.
"""

from __future__ import annotations

import os
import signal
import time

from . import config


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours; nothing we can do
    return True


def terminate_pgids(
    pgids: list[int], grace_seconds: float = config.CLEANUP_GRACE_SECONDS
) -> dict[str, list[int]]:
    """Terminate every process group in ``pgids``.

    Returns a report: ``terminated`` (SIGTERM sufficed), ``killed`` (needed
    SIGKILL), ``already_dead`` and ``failed``. Safe to call on dead groups,
    duplicates, or an empty list.
    """
    report: dict[str, list[int]] = {
        "terminated": [],
        "killed": [],
        "already_dead": [],
        "failed": [],
    }
    pending: list[int] = []
    for pgid in dict.fromkeys(pgids):  # dedup, preserve order
        if pgid <= 0:
            report["failed"].append(pgid)
            continue
        try:
            os.killpg(pgid, signal.SIGTERM)
            pending.append(pgid)
        except ProcessLookupError:
            report["already_dead"].append(pgid)
        except PermissionError:
            report["failed"].append(pgid)

    deadline = time.monotonic() + grace_seconds
    while pending and time.monotonic() < deadline:
        still = [p for p in pending if _group_alive(p)]
        if not still:
            break
        time.sleep(0.05)
        pending = still

    for pgid in pending:
        if not _group_alive(pgid):
            report["terminated"].append(pgid)
            continue
        try:
            os.killpg(pgid, signal.SIGKILL)
            report["killed"].append(pgid)
        except ProcessLookupError:
            report["terminated"].append(pgid)
        except PermissionError:
            report["failed"].append(pgid)

    return report

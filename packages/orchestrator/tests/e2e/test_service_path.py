"""FR-P21: failure-matrix end-to-end coverage of the service path.

Marked ``e2e`` (select with ``pytest -m e2e``, skip with ``-m "not e2e"``).
The full ingress pipeline runs for real — HMAC, action/draft filtering,
allowlist, delivery dedupe, durable-queue transitions, per-PR worker
serialization, commit statuses, failure marks, worktree cleanup — while
only the leaf boundaries are stubbed (git clone produces a real temp
worktree; the graph returns a canned verdict; commit statuses are
recorded). A faithful in-memory stand-in mirrors ``run_queue``'s
semantics so tests stay hermetic; the real Postgres round-trip is
available behind ``QG_E2E_DSN``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pr_review_orch import config, fetch, ingress, run_queue

SECRET = "e2e-secret"

pytestmark = pytest.mark.e2e


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(
        SECRET.encode(), body, hashlib.sha256).hexdigest()


def _payload(action="opened", pr=7, repo="acme/widgets", sha="abc123"):
    return {
        "action": action,
        "pull_request": {"number": pr, "head": {"sha": sha}},
        "repository": {
            "full_name": repo,
            "clone_url": f"https://github.com/{repo}.git",
        },
    }


def _wait(pred, timeout=10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


class FakeQueue:
    """In-memory stand-in mirroring run_queue's semantics."""

    def __init__(self, fail_on_enqueue: bool = False):
        self.rows: dict[str, dict] = {}
        self.fail = fail_on_enqueue

    def enabled(self) -> bool:
        return True

    def enqueue(self, event: dict) -> bool:
        if self.fail:
            raise RuntimeError("postgres unreachable")
        if event["delivery_id"] in self.rows:
            return False
        self.rows[event["delivery_id"]] = {
            **event, "status": "queued", "error": None}
        return True

    def recover(self) -> list[dict]:
        return [dict(r) for r in self.rows.values()
                if r["status"] in ("queued", "running")]

    def mark_running(self, delivery_id: str) -> None:
        if delivery_id in self.rows:
            self.rows[delivery_id]["status"] = "running"

    def mark_done(self, delivery_id: str) -> None:
        if delivery_id in self.rows:
            self.rows[delivery_id]["status"] = "done"

    def mark_failed(self, delivery_id: str, error: str) -> None:
        if delivery_id in self.rows:
            self.rows[delivery_id].update(
                status="failed", error=error)


@pytest.fixture
def service(monkeypatch, tmp_path):
    """A running ingress with real composition and stubbed leaves.

    Yields the TestClient, the FakeQueue and the list of posted commit
    statuses. The client fixture enters the lifespan, so the startup
    sweep/recovery runs exactly as in production.
    """
    fake = FakeQueue()
    statuses: list[tuple] = []

    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "ALLOWED_REPOS", ["acme/*"])
    monkeypatch.setattr(config, "WORKTREE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "HEARTBEAT_URL", "")
    monkeypatch.setattr(ingress.run_queue, "enabled", fake.enabled)
    monkeypatch.setattr(ingress.run_queue, "enqueue", fake.enqueue)
    monkeypatch.setattr(ingress.run_queue, "recover", fake.recover)
    monkeypatch.setattr(
        ingress.run_queue, "mark_running", fake.mark_running)
    monkeypatch.setattr(ingress.run_queue, "mark_done", fake.mark_done)
    monkeypatch.setattr(
        ingress.run_queue, "mark_failed", fake.mark_failed)
    monkeypatch.setattr(fetch, "sweep_stale_worktrees", lambda: 0)
    monkeypatch.setattr(fetch, "doctor_report", lambda: {})
    monkeypatch.setattr(
        ingress.mcp_tools, "post_commit_status",
        lambda repo, sha, state, desc="", ctx="":
            statuses.append((repo, sha, state)),
    )

    def fake_clone(url, sha):
        wt = tmp_path / f"qg-{sha[:6]}"
        wt.mkdir()
        return wt

    monkeypatch.setattr(fetch, "clone_pr", fake_clone)
    ingress._SEEN_DELIVERIES.clear()
    ingress._PENDING.clear()
    ingress._ACTIVE.clear()
    with TestClient(ingress.app) as c:
        yield {"client": c, "queue": fake, "statuses": statuses,
               "worktrees": tmp_path}


def _post(svc, payload: dict, delivery="del-1"):
    body = json.dumps(payload).encode()
    return svc["client"].post(
        "/github/webhook", content=body, headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": _sig(body),
        })


def test_happy_path_composes(monkeypatch, service) -> None:
    """Webhook -> row -> pending -> run -> success -> done; worktree gone."""
    monkeypatch.setattr(
        ingress.graph, "run_pr_review",
        lambda *a, **k: {"verdict": {"approve": True}})
    r = _post(service, _payload())
    assert r.status_code == 202
    assert _wait(lambda: service["queue"].rows["del-1"]["status"] == "done")
    states = [s[2] for s in service["statuses"]]
    assert states == ["pending", "success"]
    assert service["statuses"][0][1] == "abc123"   # delivery head sha
    assert not any(
        p.name.startswith("qg-") for p in service["worktrees"].iterdir())


def test_rejecting_verdict_posts_failure(monkeypatch, service) -> None:
    monkeypatch.setattr(
        ingress.graph, "run_pr_review",
        lambda *a, **k: {"verdict": {"approve": False}})
    _post(service, _payload())
    assert _wait(lambda: service["queue"].rows["del-1"]["status"] == "done")
    assert [s[2] for s in service["statuses"]] == ["pending", "failure"]


def test_persist_failure_returns_500_and_queues_nothing(
        monkeypatch, service) -> None:
    """Postgres down at enqueue -> 500 so GitHub retries (NFR-P2)."""
    service["queue"].fail = True
    r = _post(service, _payload())
    assert r.status_code == 500
    assert service["queue"].rows == {}
    assert service["statuses"] == []


def test_recovery_reruns_crashed_row(monkeypatch, tmp_path) -> None:
    """A row left 'running' by a crashed process is re-dispatched on
    startup — the restart loses zero accepted work (NFR-P2)."""
    fake = FakeQueue()
    fake.rows["del-crash"] = {
        "delivery_id": "del-crash", "repo": "acme/widgets",
        "pr_number": 9, "head_sha": "dead99",
        "clone_url": "https://github.com/acme/widgets.git",
        "status": "running", "error": None,
    }
    statuses: list[tuple] = []
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "ALLOWED_REPOS", ["acme/*"])
    monkeypatch.setattr(config, "WORKTREE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "HEARTBEAT_URL", "")
    for name in ("enabled", "enqueue", "recover", "mark_running",
                 "mark_done", "mark_failed"):
        monkeypatch.setattr(
            ingress.run_queue, name, getattr(fake, name))
    monkeypatch.setattr(fetch, "sweep_stale_worktrees", lambda: 0)
    monkeypatch.setattr(fetch, "doctor_report", lambda: {})
    monkeypatch.setattr(
        ingress.mcp_tools, "post_commit_status",
        lambda repo, sha, state, desc="", ctx="":
            statuses.append((repo, sha, state)))
    monkeypatch.setattr(
        fetch, "clone_pr", lambda *a, **k: (tmp_path / "qg-x").mkdir() or
        (tmp_path / "qg-x"))
    monkeypatch.setattr(
        ingress.graph, "run_pr_review",
        lambda *a, **k: {"verdict": {"approve": True}})
    ingress._SEEN_DELIVERIES.clear()
    ingress._PENDING.clear()
    ingress._ACTIVE.clear()
    with TestClient(ingress.app):
        assert _wait(
            lambda: fake.rows["del-crash"]["status"] == "done")
    assert statuses[-1][2] == "success"


def test_run_crash_posts_error_and_fails_row(monkeypatch, service) -> None:
    """Graph crash -> status error + row failed + worktree removed."""
    def boom(*a, **k):
        raise TimeoutError("graph wedged")

    monkeypatch.setattr(ingress.graph, "run_pr_review", boom)
    _post(service, _payload())
    assert _wait(
        lambda: service["queue"].rows["del-1"]["status"] == "failed")
    assert "TimeoutError" in service["queue"].rows["del-1"]["error"]
    assert [s[2] for s in service["statuses"]] == ["pending", "error"]
    assert not any(
        p.name.startswith("qg-") for p in service["worktrees"].iterdir())


def test_real_processes_attributed_and_swept(
        tmp_path, monkeypatch) -> None:
    """FR-P20 e2e: a real spawned 'validator' inside a qg-* worktree is
    killed; a same-signature decoy outside survives — real pgrep,
    real /proc, real killpg."""
    if not shutil.which("pgrep"):
        pytest.skip("procps not installed")
    root = tmp_path.resolve()
    monkeypatch.setattr(config, "WORKTREE_ROOT", str(root))
    worktree = root / "qg-real"
    worktree.mkdir()

    def fake_validator(argv0: str, cwd: Path) -> subprocess.Popen:
        return subprocess.Popen(
            ["bash", "-c", f'exec -a "{argv0}" sleep 30'],
            cwd=cwd, start_new_session=True,
        )

    orphan = fake_validator("tsc --noEmit", worktree)
    decoy = fake_validator("cargo check", Path.home())
    try:
        # Both processes are alive and match the signature before sweep.
        assert orphan.poll() is None and decoy.poll() is None
        fetch.sweep_stale_worktrees()
        orphan.wait(timeout=5)
        assert orphan.returncode is not None       # killed
        assert decoy.poll() is None                # decoy survives
        assert not worktree.exists()               # dir swept too
    finally:
        for p in (orphan, decoy):
            if p.poll() is None:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)


@pytest.mark.skipif(not os.environ.get("QG_E2E_DSN"),
                    reason="needs a real Postgres: set QG_E2E_DSN")
def test_pending_runs_round_trip_on_real_postgres(
        monkeypatch, tmp_path) -> None:
    """Real-table variant: enqueue persists, restart recovers.

    Run with e.g.
    ``QG_E2E_DSN=postgresql://qualitygate:qualitygate@localhost:5432/qg``
    against the compose postgres. Asserts durable semantics the
    FakeQueue mirrors: the row exists before dispatch and ends 'done'.
    """
    dsn = os.environ["QG_E2E_DSN"]
    monkeypatch.setattr(config, "DATABASE_URL", dsn)
    import psycopg
    statuses: list[tuple] = []
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "ALLOWED_REPOS", ["acme/*"])
    monkeypatch.setattr(config, "WORKTREE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "HEARTBEAT_URL", "")
    monkeypatch.setattr(fetch, "sweep_stale_worktrees", lambda: 0)
    monkeypatch.setattr(fetch, "doctor_report", lambda: {})
    monkeypatch.setattr(
        ingress.mcp_tools, "post_commit_status",
        lambda repo, sha, state, desc="", ctx="":
            statuses.append((repo, sha, state)))
    monkeypatch.setattr(
        fetch, "clone_pr", lambda *a, **k:
        (tmp_path / "qg-x").mkdir() or (tmp_path / "qg-x"))
    monkeypatch.setattr(
        ingress.graph, "run_pr_review",
        lambda *a, **k: {"verdict": {"approve": True}})
    run_queue._SCHEMA_READY = False
    run_queue.setup()                # creates pending_runs if absent
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM pending_runs "
                    "WHERE delivery_id = 'del-real'")
        conn.commit()
    ingress._SEEN_DELIVERIES.clear()
    ingress._PENDING.clear()
    ingress._ACTIVE.clear()
    with TestClient(ingress.app) as c:
        body = json.dumps(_payload()).encode()
        r = c.post("/github/webhook", content=body, headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "del-real",
            "X-Hub-Signature-256": _sig(body)})
        assert r.status_code == 202
        assert _wait(lambda: statuses and statuses[-1][2] == "success")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM pending_runs WHERE delivery_id='del-real'")
        assert cur.fetchone()[0] == "done"

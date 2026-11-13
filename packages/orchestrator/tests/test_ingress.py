"""Ingress tests — signature, allowlist, filters, dedupe, run queue.

The drain task is stubbed so the worker runs without cloning; git/API
paths are covered in test_fetch.py and the graph tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from pr_review_orch import config, ingress

SECRET = "test-secret"


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(
        SECRET.encode(), body, hashlib.sha256
    ).hexdigest()


def _payload(action="opened", pr=7, repo="acme/widgets", sha="abc123"):
    return {
        "action": action,
        "pull_request": {"number": pr, "head": {"sha": sha}},
        "repository": {
            "full_name": repo,
            "clone_url": f"https://github.com/{repo}.git",
        },
    }


def _wait(pred, timeout=5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def client(monkeypatch):
    """TestClient with worker draining into a stub (no clone/run)."""
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "ALLOWED_REPOS", ["acme/*"])
    monkeypatch.setattr(
        ingress.fetch, "sweep_stale_worktrees", lambda: 0
    )
    recorded: list[tuple[str, dict]] = []

    async def fake_drain(sem, key, event):
        recorded.append((key, event))

    monkeypatch.setattr(ingress, "_drain_guarded", fake_drain)
    ingress._SEEN_DELIVERIES.clear()
    ingress._PENDING.clear()
    ingress._ACTIVE.clear()
    with TestClient(ingress.app) as c:
        yield {"client": c, "recorded": recorded}


def _post(client, payload: dict, event="pull_request",
          delivery="d-1", sign=True):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
    if sign:
        headers["X-Hub-Signature-256"] = _sig(body)
    return client["client"].post(
        "/github/webhook", content=body, headers=headers
    )


# --- pure helpers ---------------------------------------------------------- #

def test_verify_signature_fails_closed(monkeypatch) -> None:
    body = b'{"a":1}'
    assert not ingress.verify_signature(body, "")
    assert not ingress.verify_signature(body, "md5=deadbeef")
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", "")
    assert not ingress.verify_signature(body, _sig(body))
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", SECRET)
    assert ingress.verify_signature(body, _sig(body))


def test_repo_allowed_rules(monkeypatch) -> None:
    monkeypatch.setattr(config, "ALLOWED_REPOS",
                        ["acme/widgets", "other/*"])
    assert ingress.repo_allowed("acme/widgets")
    assert ingress.repo_allowed("other/anything")
    assert not ingress.repo_allowed("acme/other-repo")
    assert not ingress.repo_allowed("elsewhere/repo")
    monkeypatch.setattr(config, "ALLOWED_REPOS", [])
    assert not ingress.repo_allowed("acme/widgets")  # deny-by-default
    monkeypatch.setattr(config, "ALLOWED_REPOS", ["*"])
    assert ingress.repo_allowed("anything/goes")


def test_extract_event_filters_non_review() -> None:
    assert ingress._extract_event(_payload(action="closed")) is None
    assert ingress._extract_event(_payload(action="opened")) == {
        "repo": "acme/widgets",
        "clone_url": "https://github.com/acme/widgets.git",
        "pr_number": 7,
        "head_sha": "abc123",
    }


# --- endpoint -------------------------------------------------------------- #

def test_bad_signature_rejected(client) -> None:
    payload = _payload()
    assert _post(client, payload, sign=False).status_code == 401
    r = client["client"].post(
        "/github/webhook",
        content=b"{}",
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
    )
    assert r.status_code == 401


def test_non_pull_request_event_ignored(client) -> None:
    assert _post(
        client, _payload(), event="push"
    ).status_code == 202


def test_non_review_action_ignored(client) -> None:
    assert _post(client, _payload(action="closed")).status_code == 202


def test_invalid_json_rejected(client) -> None:
    body = b"not-json"
    r = client["client"].post(
        "/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sig(body),
        },
    )
    assert r.status_code == 400


def test_denied_repo_dropped(client) -> None:
    r = _post(client, _payload(repo="evil/repo"))
    assert r.status_code == 403


def test_accepted_delivery_enqueues_run(client) -> None:
    assert _post(client, _payload()).status_code == 202
    assert _wait(lambda: client["recorded"])
    key, event = client["recorded"][0]
    assert key == "acme/widgets#7"
    assert event["head_sha"] == "abc123"


def test_duplicate_delivery_deduped(client) -> None:
    assert _post(client, _payload(), delivery="same-id").status_code == 202
    assert _post(client, _payload(), delivery="same-id").status_code == 200


def test_healthz(client) -> None:
    r = client["client"].get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


# --- run queue ------------------------------------------------------------- #

@pytest.fixture
def _stub_status(monkeypatch):
    """FR-P8: record commit statuses instead of hitting the API."""
    posted: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        ingress.mcp_tools, "post_commit_status",
        lambda repo, sha, state, desc="", ctx="":
            posted.append((repo, sha, state)),
    )
    return posted


def test_run_once_removes_worktree(monkeypatch, tmp_path, _stub_status) -> None:
    worktree = tmp_path / "qg-run"
    worktree.mkdir()
    monkeypatch.setattr(
        ingress.fetch, "clone_pr", lambda *_a: worktree
    )
    seen: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        ingress.graph, "run_pr_review",
        lambda path, pr, repo_slug=None: seen.append((path, pr, repo_slug)),
    )
    event = {"repo": "a/b", "pr_number": 3,
             "clone_url": "u", "head_sha": "s"}
    asyncio.run(ingress._run_once(event))
    # FR-P1: the run carries the payload's owner/repo identity, not the
    # ephemeral worktree dir — checkpoints/metrics stay stable per PR.
    assert seen == [(str(worktree), 3, "a/b")]
    assert not worktree.exists()  # INV-2: dir cleaned like pgids


def test_run_once_removes_worktree_on_failure(
    monkeypatch, tmp_path, _stub_status
) -> None:
    worktree = tmp_path / "qg-run"
    worktree.mkdir()
    monkeypatch.setattr(
        ingress.fetch, "clone_pr", lambda *_a: worktree
    )

    def boom(*_a):
        raise RuntimeError("graph failed")

    monkeypatch.setattr(ingress.graph, "run_pr_review", boom)
    event = {"repo": "a/b", "pr_number": 3,
             "clone_url": "u", "head_sha": "s"}
    asyncio.run(ingress._run_once(event))  # logs, never propagates
    assert not worktree.exists()


def test_run_once_posts_pending_and_success(
    monkeypatch, tmp_path, _stub_status
) -> None:
    """FR-P8: pending -> success on the delivery sha gates the commit."""
    worktree = tmp_path / "qg-run"
    worktree.mkdir()
    monkeypatch.setattr(
        ingress.fetch, "clone_pr", lambda *_a: worktree)
    monkeypatch.setattr(
        ingress.graph, "run_pr_review",
        lambda *_a, **_k: {"verdict": {"approve": True}})
    event = {"repo": "a/b", "pr_number": 3,
             "clone_url": "u", "head_sha": "deadbeef"}
    asyncio.run(ingress._run_once(event))
    assert _stub_status == [
        ("a/b", "deadbeef", "pending"),
        ("a/b", "deadbeef", "success"),
    ]


def test_run_once_posts_failure_on_reject(
    monkeypatch, tmp_path, _stub_status
) -> None:
    """FR-P8: a rejected verdict posts ``failure`` — the gate blocks."""
    worktree = tmp_path / "qg-run"
    worktree.mkdir()
    monkeypatch.setattr(
        ingress.fetch, "clone_pr", lambda *_a: worktree)
    monkeypatch.setattr(
        ingress.graph, "run_pr_review",
        lambda *_a, **_k: {"verdict": {"approve": False}})
    event = {"repo": "a/b", "pr_number": 3,
             "clone_url": "u", "head_sha": "deadbeef"}
    asyncio.run(ingress._run_once(event))
    assert _stub_status[-1] == ("a/b", "deadbeef", "failure")


def test_run_once_posts_error_on_crash(
    monkeypatch, tmp_path, _stub_status
) -> None:
    """FR-P8: a crashed run posts ``error`` — never silently pending."""
    monkeypatch.setattr(
        ingress.fetch, "clone_pr",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("clone failed")))

    def boom(*_a, **_k):
        raise AssertionError("should not reach the graph")

    monkeypatch.setattr(ingress.graph, "run_pr_review", boom)
    event = {"repo": "a/b", "pr_number": 3,
             "clone_url": "u", "head_sha": "deadbeef"}
    asyncio.run(ingress._run_once(event))
    assert _stub_status == [
        ("a/b", "deadbeef", "pending"),
        ("a/b", "deadbeef", "error"),
    ]


def test_run_once_survives_status_failure(
    monkeypatch, tmp_path
) -> None:
    """FR-P8: a status-post failure degrades to a log — run completes."""
    worktree = tmp_path / "qg-run"
    worktree.mkdir()
    monkeypatch.setattr(
        ingress.fetch, "clone_pr", lambda *_a: worktree)
    monkeypatch.setattr(
        ingress.mcp_tools, "post_commit_status",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("api down")))
    ran: list[bool] = []
    monkeypatch.setattr(
        ingress.graph, "run_pr_review",
        lambda *_a, **_k: ran.append(True)
        or {"verdict": {"approve": True}})
    event = {"repo": "a/b", "pr_number": 3,
             "clone_url": "u", "head_sha": "deadbeef"}
    asyncio.run(ingress._run_once(event))
    assert ran == [True]
    assert not worktree.exists()


def test_enqueue_persists_before_dispatch(client, monkeypatch) -> None:
    """FR-P4: with a DSN the delivery is durable BEFORE the 202."""
    monkeypatch.setattr(ingress.run_queue, "enabled", lambda: True)
    inserted: list[str] = []
    monkeypatch.setattr(
        ingress.run_queue, "enqueue",
        lambda ev: inserted.append(ev["delivery_id"]) or True)
    assert _post(client, _payload(), delivery="d-9").status_code == 202
    assert inserted == ["d-9"]
    assert _wait(lambda: client["recorded"])
    assert client["recorded"][0][1]["delivery_id"] == "d-9"


def test_enqueue_db_duplicate_skipped(client, monkeypatch) -> None:
    """FR-P4: PK conflict -> 200, never reaches the dispatch queue."""
    monkeypatch.setattr(ingress.run_queue, "enabled", lambda: True)
    monkeypatch.setattr(
        ingress.run_queue, "enqueue", lambda ev: False)
    assert _post(client, _payload()).status_code == 200
    assert client["recorded"] == []


def test_enqueue_db_error_returns_500(client, monkeypatch) -> None:
    """FR-P4: a delivery we cannot durably accept is not acknowledged —
    GitHub retries the 500 instead of us dropping it silently."""
    monkeypatch.setattr(ingress.run_queue, "enabled", lambda: True)

    def boom(ev):
        raise RuntimeError("db down")

    monkeypatch.setattr(ingress.run_queue, "enqueue", boom)
    assert _post(client, _payload()).status_code == 500
    assert client["recorded"] == []


def test_startup_requeues_unfinished_deliveries(monkeypatch) -> None:
    """FR-P4: queued/running rows from a crashed process re-dispatch."""
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "ALLOWED_REPOS", ["acme/*"])
    monkeypatch.setattr(
        ingress.fetch, "sweep_stale_worktrees", lambda: 0)
    recovered = {
        "delivery_id": "old-1", "repo": "acme/widgets",
        "pr_number": 5, "head_sha": "s", "clone_url": "u",
    }
    monkeypatch.setattr(
        ingress.run_queue, "recover", lambda: [recovered])
    recorded: list[tuple[str, dict]] = []

    async def fake_drain(sem, key, event):
        recorded.append((key, event))

    monkeypatch.setattr(ingress, "_drain_guarded", fake_drain)
    ingress._ACTIVE.clear()
    with TestClient(ingress.app):
        assert _wait(lambda: recorded)
    assert recorded == [("acme/widgets#5", recovered)]


def test_run_once_marks_running_then_done(
    monkeypatch, tmp_path, _stub_status
) -> None:
    """FR-P4: pending_runs sees queued -> running -> done."""
    marks: list[tuple] = []
    monkeypatch.setattr(
        ingress.run_queue, "mark_running",
        lambda d: marks.append(("running", d)))
    monkeypatch.setattr(
        ingress.run_queue, "mark_done",
        lambda d: marks.append(("done", d)))
    monkeypatch.setattr(
        ingress.run_queue, "mark_failed",
        lambda d, e: marks.append(("failed", d, e)))
    worktree = tmp_path / "qg-run"
    worktree.mkdir()
    monkeypatch.setattr(
        ingress.fetch, "clone_pr", lambda *_a: worktree)
    monkeypatch.setattr(
        ingress.graph, "run_pr_review",
        lambda *_a, **_k: {"verdict": {"approve": True}})
    event = {"repo": "a/b", "pr_number": 3, "clone_url": "u",
             "head_sha": "s", "delivery_id": "d-1"}
    asyncio.run(ingress._run_once(event))
    assert marks == [("running", "d-1"), ("done", "d-1")]


def test_run_once_marks_failed_on_crash(
    monkeypatch, tmp_path, _stub_status
) -> None:
    """FR-P4/FR-P2: a crashed run leaves status=failed + error."""
    marks: list[tuple] = []
    monkeypatch.setattr(
        ingress.run_queue, "mark_running",
        lambda d: marks.append(("running", d)))
    monkeypatch.setattr(
        ingress.run_queue, "mark_done",
        lambda d: marks.append(("done", d)))
    monkeypatch.setattr(
        ingress.run_queue, "mark_failed",
        lambda d, e: marks.append(("failed", d, e)))
    monkeypatch.setattr(
        ingress.fetch, "clone_pr",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("clone failed")))
    event = {"repo": "a/b", "pr_number": 3, "clone_url": "u",
             "head_sha": "s", "delivery_id": "d-1"}
    asyncio.run(ingress._run_once(event))
    assert marks[0] == ("running", "d-1")
    assert marks[1][0:2] == ("failed", "d-1")
    assert "clone failed" in marks[1][2]


def test_drain_coalesces_pending(monkeypatch) -> None:
    """A burst on one PR coalesces: only the latest pending re-runs."""
    runs: list[dict] = []
    key = "a/b#9"
    second = {"repo": "a/b", "pr_number": 9,
              "clone_url": "u", "head_sha": "new"}

    async def fake_once(event):
        runs.append(event["head_sha"])
        if len(runs) == 1:
            ingress._PENDING[key] = second  # arrives mid-run

    monkeypatch.setattr(ingress, "_run_once", fake_once)
    first = {"repo": "a/b", "pr_number": 9,
             "clone_url": "u", "head_sha": "old"}
    asyncio.run(ingress._drain(key, first))
    assert runs == ["old", "new"]
    assert key not in ingress._ACTIVE


def test_thread_id_uses_repo_slug_not_worktree() -> None:
    """FR-P1: ephemeral worktree dirs must not leak into the thread id —
    two runs of the same PR share a checkpoint thread (NFR-3)."""
    tid = ingress.graph._thread_id("acme/widgets", 7)
    assert tid == "pr-review/acme/widgets/7"
    assert "qg-" not in tid

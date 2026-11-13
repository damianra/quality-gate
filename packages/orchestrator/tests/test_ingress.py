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

def test_run_once_removes_worktree(monkeypatch, tmp_path) -> None:
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
    monkeypatch, tmp_path
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

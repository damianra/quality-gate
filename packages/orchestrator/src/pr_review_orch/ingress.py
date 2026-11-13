"""Webhook ingress for service mode (FR-O19) with an in-process run queue
(FR-O21) on top of the ephemeral fetcher (FR-O20).

Flow per accepted delivery: HMAC verify -> pull_request action filter ->
repo allowlist -> delivery dedupe -> enqueue -> worker clones an ephemeral
worktree, runs ``run_pr_review`` on it, then removes the worktree (INV-2
extension: directories are cleaned like pgids).

Security contract (INV-6/FR-O22): signature is verified on the raw body
before JSON parsing; unsigned/mismatched payloads get 401 with no side
effects; the repo allowlist is deny-by-default; only event metadata is
logged — never signatures, token, or body.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import shutil
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response

from quality_gate_mcp import tools as mcp_tools

from . import config, fetch, graph, logs, run_queue

logger = logging.getLogger(__name__)

_REVIEW_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
_MAX_DELIVERY_IDS = 10_000

#: Created per lifespan: queue waiters bind to the running event loop,
#: so a module-level instance would leak stale futures across loops.
_QUEUE: asyncio.Queue[dict[str, Any]] | None = None
#: Delivery IDs already enqueued/processed (X-GitHub-Delivery dedupe).
_SEEN_DELIVERIES: set[str] = set()
#: thread_id -> latest enqueued payload (coalesces bursts on one PR).
_PENDING: dict[str, dict[str, Any]] = {}
#: thread_ids with an active drain task (per-thread serialization).
_ACTIVE: set[str] = set()
#: In-flight drain tasks — tracked (not fire-and-forget) so shutdown
#: can wait for them instead of abandoning runs mid-flight (FR-P5).
_DRAINING: set[asyncio.Task] = set()


def verify_signature(body: bytes, signature: str) -> bool:
    """True iff ``signature`` is the valid ``sha256=<hmac>`` for ``body``.

    Constant-time compare; fails closed when no secret is configured or
    the header is missing/malformed (INV-6).
    """
    secret = config.GITHUB_WEBHOOK_SECRET
    if not secret or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature[len("sha256="):])


def repo_allowed(full_name: str) -> bool:
    """Match ``owner/repo`` against ``QG_ALLOWED_REPOS`` (deny-by-default)."""
    for entry in config.ALLOWED_REPOS:
        if entry == "*":
            return True
        if entry.endswith("/*"):
            if full_name.startswith(entry[:-1]):
                return True
        elif full_name == entry:
            return True
    return False


def _extract_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Pull repo/PR/sha out of a reviewable pull_request payload.

    FR-P6: ``draft: true`` payloads are dropped — drafts are skipped on
    open and on synchronize — while ``ready_for_review`` is accepted so
    a PR marked ready gets its review.
    """
    action = payload.get("action")
    pr = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}
    if action not in _REVIEW_ACTIONS or not pr.get("number"):
        return None
    if pr.get("draft"):
        return None
    return {
        "repo": repo.get("full_name", ""),
        "clone_url": repo.get("clone_url", ""),
        "pr_number": int(pr["number"]),
        "head_sha": (pr.get("head") or {}).get("sha", ""),
    }


def _enqueue(delivery: str, event: dict[str, Any]) -> str:
    """Dedupe + queue a reviewable event.

    FR-P4: with ``DATABASE_URL`` set, the event is persisted to
    ``pending_runs`` before dispatch — ``delivery_id`` is the dedupe
    primary key, so it survives restarts. A persist failure returns
    ``"error"`` -> 500 rather than acknowledging a delivery we could
    not durably accept. Without a DSN the in-memory dedupe path keeps
    the FR-O21 semantics (not durable — local dev only).
    """
    import uuid
    # GitHub always sends X-GitHub-Delivery; synthesize one when absent
    # so pending_runs still gets a stable primary key.
    event["delivery_id"] = delivery or f"syn-{uuid.uuid4()}"
    if run_queue.enabled():
        try:
            if not run_queue.enqueue(event):
                return "duplicate"
        except Exception:
            logger.exception("pending_runs persist failed")
            return "error"
    else:
        if delivery and delivery in _SEEN_DELIVERIES:
            return "duplicate"
        if delivery:
            _SEEN_DELIVERIES.add(delivery)
            if len(_SEEN_DELIVERIES) > _MAX_DELIVERY_IDS:
                _SEEN_DELIVERIES.pop()  # bounded; eviction arbitrary
    assert _QUEUE is not None, "queue exists once the app is running"
    _QUEUE.put_nowait(event)
    return "enqueued"


async def _post_status(event: dict[str, Any], state: str,
                       description: str) -> None:
    """Best-effort commit status (FR-P8): failures log, never raise.

    The status lands on the *delivery* head sha — the exact commit the
    run reviewed — not whatever head the PR may have moved to mid-run.
    """
    try:
        await asyncio.to_thread(
            mcp_tools.post_commit_status,
            event["repo"], event["head_sha"], state,
            description, config.STATUS_CONTEXT,
        )
    except Exception:
        logger.warning(
            "commit status %s failed for %s@%s",
            state, event["repo"], event["head_sha"][:7], exc_info=True,
        )


async def _heartbeat() -> None:
    """Dead-man ping (FR-P16): GET QG_HEARTBEAT_URL after each run.

    Silence past the monitor's interval means the service died; the
    ping itself is best-effort — a heartbeat outage must never break a
    review run.
    """
    url = config.HEARTBEAT_URL
    if not url:
        return
    try:
        import httpx
        await asyncio.to_thread(httpx.get, url, timeout=10)
    except Exception:  # noqa: BLE001
        logger.warning("heartbeat ping failed", exc_info=True)


async def _run_once(event: dict[str, Any]) -> None:
    """Clone -> review -> rmtree for one event (INV-2 directory cleanup)."""
    worktree = None
    delivery_id = event.get("delivery_id", "")
    # FR-P15: correlate every log record of this run with the thread id
    # that also keys run_metrics — contextvars propagate to to_thread.
    logs.run_id_var.set(f"pr-review/{event['repo']}/{event['pr_number']}")
    try:
        await asyncio.to_thread(run_queue.mark_running, delivery_id)
        # FR-P8: gate the delivery sha — pending up front so the PR shows
        # the required check while the review runs.
        await _post_status(event, "pending", "review in progress")
        worktree = await asyncio.to_thread(
            fetch.clone_pr, event["clone_url"], event["head_sha"]
        )
        final = await asyncio.to_thread(
            graph.run_pr_review, str(worktree), event["pr_number"],
            repo_slug=event["repo"],
        )
        approve = bool((final or {}).get("verdict", {}).get("approve"))
        await _post_status(
            event,
            "success" if approve else "failure",
            "review approved" if approve else "changes required",
        )
        await asyncio.to_thread(run_queue.mark_done, delivery_id)
    except Exception as exc:
        logger.exception(
            "run failed for %s#%s", event["repo"], event["pr_number"]
        )
        await asyncio.to_thread(
            run_queue.mark_failed, delivery_id,
            f"{type(exc).__name__}: {exc}")
        await _post_status(event, "error", "review failed — see logs")
    finally:
        if worktree is not None:
            shutil.rmtree(worktree, ignore_errors=True)
        await _heartbeat()  # FR-P16: success or failure both count


async def _drain(key: str, first: dict[str, Any]) -> None:
    """Process queued events for one thread_id, coalescing bursts."""
    event = first
    while True:
        await _run_once(event)
        nxt = _PENDING.pop(key, None)
        if nxt is None:
            _ACTIVE.discard(key)
            return
        event = nxt


async def _worker(queue: asyncio.Queue) -> None:
    """Dispatch queue items; serialize per thread_id, bound parallelism."""
    sem = asyncio.Semaphore(config.MAX_CONCURRENT_RUNS)
    while True:
        event = await queue.get()
        key = f"{event['repo']}#{event['pr_number']}"
        if key in _ACTIVE:
            _PENDING[key] = event  # coalesce: only the latest matters
            continue
        _ACTIVE.add(key)
        task = asyncio.create_task(_drain_guarded(sem, key, event))
        _DRAINING.add(task)
        task.add_done_callback(_DRAINING.discard)  # FR-P5: tracked


async def _drain_guarded(sem: asyncio.Semaphore, key: str,
                         event: dict[str, Any]) -> None:
    async with sem:
        await _drain(key, event)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _QUEUE
    _QUEUE = asyncio.Queue()
    fetch.sweep_stale_worktrees()
    # FR-P4: requeue deliveries a previous process accepted but never
    # finished (queued) or died inside (running).
    for event in await asyncio.to_thread(run_queue.recover):
        _QUEUE.put_nowait(event)
    worker = asyncio.create_task(_worker(_QUEUE))
    try:
        yield
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        # FR-P5 graceful shutdown: give in-flight runs up to
        # QG_SHUTDOWN_GRACE_S to finish before cancelling their drains
        # — a cancelled drain still rmtrees its worktree in the
        # _run_once finally, so abandoning mid-clone is the last resort.
        if _DRAINING:
            logger.info(
                "waiting up to %ss for %d in-flight run(s)",
                config.SHUTDOWN_GRACE_S, len(_DRAINING))
            _done, pending = await asyncio.wait(
                _DRAINING, timeout=config.SHUTDOWN_GRACE_S)
            for task in pending:
                task.cancel()


app = FastAPI(title="quality-gate ingress", lifespan=_lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> Response:
    """Readiness (FR-P16): Postgres reachability + in-memory queue depth.

    Unlike ``/healthz`` (process alive), this answers "can the service
    accept and persist work right now": 503 when Postgres is configured
    but unreachable, 200 otherwise. ``postgres`` is ``null`` when no DSN
    is set (local in-memory mode — nothing to probe).
    """
    pg = await asyncio.to_thread(run_queue.reachable)
    depth = _QUEUE.qsize() if _QUEUE is not None else 0
    ready = pg is not False
    body = {
        "status": "ready" if ready else "degraded",
        "postgres": pg,
        "queue_depth": depth,
    }
    return Response(
        json.dumps(body),
        status_code=200 if ready else 503,
        media_type="application/json",
    )


@app.post("/github/webhook")
async def github_webhook(request: Request) -> Response:
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(body, signature):
        return Response(status_code=401)
    if request.headers.get("X-GitHub-Event", "") != "pull_request":
        return Response(status_code=202)
    try:
        payload = json.loads(body)
    except ValueError:
        return Response(status_code=400)
    event = _extract_event(payload)
    if event is None:
        return Response(status_code=202)
    logger.info(
        "webhook %s %s#%s %s", payload.get("action"), event["repo"],
        event["pr_number"], event["head_sha"][:7],
    )
    if not repo_allowed(event["repo"]):
        logger.info("repo not allowed, dropped: %s", event["repo"])
        return Response(status_code=403)
    status = _enqueue(request.headers.get("X-GitHub-Delivery", ""), event)
    # "error" -> 500: GitHub retries; we never 202 a delivery that could
    # not be durably accepted (FR-P4).
    code = {"enqueued": 202, "duplicate": 200, "error": 500}[status]
    return Response(status_code=code)


def main() -> None:
    """Run the ingress on loopback — nginx owns the public port."""
    import uvicorn

    # FR-P15: JSON logs at QG_LOG_LEVEL — without this the WARNING
    # default drops every informational run log.
    logs.setup_logging(config.LOG_LEVEL)
    if not config.GITHUB_WEBHOOK_SECRET:
        logger.warning(
            "GITHUB_WEBHOOK_SECRET unset — every payload will be rejected"
        )
    uvicorn.run(app, host=config.INGRESS_HOST, port=config.INGRESS_PORT)


if __name__ == "__main__":
    main()

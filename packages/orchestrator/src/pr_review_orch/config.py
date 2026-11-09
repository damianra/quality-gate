"""Orchestrator configuration (env-only; secrets never leave env vars)."""

from __future__ import annotations

import os
import tempfile

#: Postgres DSN for langgraph-checkpoint-postgres. Optional: when unset an
#: in-memory checkpointer is used (no resumability across restarts).
DATABASE_URL = os.environ.get("DATABASE_URL", "")

#: Grace period between SIGTERM and SIGKILL in the cleanup node.
CLEANUP_GRACE_SECONDS = float(os.environ.get("QG_CLEANUP_GRACE_SECONDS", "3"))

#: Cap on files fetched when profiling a PR.
MAX_FILES_PAGES = int(os.environ.get("QG_MAX_FILES_PAGES", "5"))

#: Timeout for QA validation processes (tsc, node --check).
QA_PROCESS_TIMEOUT = int(os.environ.get("QG_QA_PROCESS_TIMEOUT", "120"))

#: Hard caps on the scoped review context (blast radius). Bound token
#: spend when sub-agents/LLMs consume pr_context.
SCOPE_MAX_FILES = int(os.environ.get("QG_SCOPE_MAX_FILES", "25"))
SCOPE_MAX_CHARS = int(os.environ.get("QG_SCOPE_MAX_CHARS", "200000"))

# --- Service mode (FR-O19..O22): webhook ingress + ephemeral fetch ------- #

#: HMAC secret for X-Hub-Signature-256 verification. REQUIRED in service
#: mode — when unset every webhook payload is rejected.
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

#: Repos the service may review. Comma-separated ``owner/repo`` or
#: ``owner/*`` entries; empty = deny all (deny-by-default, INV-6).
ALLOWED_REPOS = [
    r.strip()
    for r in os.environ.get("QG_ALLOWED_REPOS", "").split(",")
    if r.strip()
]

#: Interface the ingress uvicorn binds. Loopback by default (bare host);
#: compose sets 0.0.0.0 so nginx reaches it over the internal network.
INGRESS_HOST = os.environ.get("QG_INGRESS_HOST", "127.0.0.1")

#: Loopback port the ingress uvicorn binds; nginx owns the public port.
INGRESS_PORT = int(os.environ.get("QG_INGRESS_PORT", "8088"))

#: Directory where ephemeral PR worktrees (``qg-*``) are created.
WORKTREE_ROOT = os.environ.get("QG_WORKTREE_ROOT", tempfile.gettempdir())

#: Timeout in seconds for each git step of the ephemeral clone.
CLONE_TIMEOUT = int(os.environ.get("QG_CLONE_TIMEOUT", "300"))

#: Disk cap in bytes per worktree; oversized clones are removed and dropped.
MAX_WORKTREE_BYTES = int(
    os.environ.get("QG_MAX_WORKTREE_BYTES", str(2 * 1024**3))
)

#: Max review runs executing in parallel across distinct PR threads.
MAX_CONCURRENT_RUNS = int(os.environ.get("QG_MAX_CONCURRENT_RUNS", "2"))

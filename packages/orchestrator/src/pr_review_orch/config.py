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

#: Timeout for QA validation processes (tsc, node --check, python
#: compile() check, go vet, cargo check).
QA_PROCESS_TIMEOUT = int(os.environ.get("QG_QA_PROCESS_TIMEOUT", "120"))

#: FR-P12 validator execution policy: validators that run PR-controlled
#: code (``cargo check`` compiles build.rs/proc-macros, ``go vet`` may
#: build cgo) are gated behind this flag. Service deployments set
#: ``QG_EXEC_VALIDATORS=0`` (untrusted checkouts); CLI/Action runs keep
#: the default 1 — the operator chose the checkout.
EXEC_VALIDATORS = os.environ.get(
    "QG_EXEC_VALIDATORS", "1").lower() not in ("0", "false", "no")

#: FR-L1 file extensions that activate the QA specialist — multi-language
#: code set; docs/config/data files never trigger QA on their own.
#: Comma/space-separated override via ``QG_QA_CODE_EXTS``, leading dot
#: optional.
QA_CODE_EXTS = frozenset(
    "." + e.strip().lstrip(".").lower()
    for e in os.environ.get(
        "QG_QA_CODE_EXTS",
        ".ts,.tsx,.js,.jsx,.mjs,.cjs,.py,.go,.rs,.java,.kt,.rb,.php,"
        ".cs,.swift,.c,.cc,.cpp,.h,.hpp",
    ).replace(",", " ").split()
    if e.strip()
)

#: Hard caps on the scoped review context (blast radius). Bound token
#: spend when sub-agents/LLMs consume pr_context.
SCOPE_MAX_FILES = int(os.environ.get("QG_SCOPE_MAX_FILES", "25"))
SCOPE_MAX_CHARS = int(os.environ.get("QG_SCOPE_MAX_CHARS", "200000"))

#: Per-section char caps for the specialist LLM prompt (FR-O23): the
#: diff keeps priority; full file contents fill the remaining budget so
#: the prompt still fits the deployed model context (num_ctx).
PROMPT_MAX_DIFF_CHARS = int(os.environ.get("QG_PROMPT_MAX_DIFF_CHARS", "16000"))
PROMPT_MAX_FILES_CHARS = int(os.environ.get("QG_PROMPT_MAX_FILES_CHARS", "12000"))
#: FR-C1/FR-C2: caps for the PR body and the target repo's rules doc
#: (AGENTS.md/CLAUDE.md) shipped as specialist evidence.
PROMPT_MAX_BODY_CHARS = int(os.environ.get("QG_PROMPT_MAX_BODY_CHARS", "4000"))
PROMPT_MAX_AGENTS_CHARS = int(os.environ.get("QG_PROMPT_MAX_AGENTS_CHARS", "4000"))
#: FR-C3: head excerpts of direct dependents (names-only beyond this).
RELATED_HEAD_FILES = int(os.environ.get("QG_RELATED_HEAD_FILES", "5"))
RELATED_HEAD_CHARS = int(os.environ.get("QG_RELATED_HEAD_CHARS", "1500"))
#: FR-C9: max inspection-checklist items per role.
CHECKLIST_MAX_ITEMS = int(os.environ.get("QG_CHECKLIST_MAX_ITEMS", "12"))

#: FR-O24 dependency audit (SecurityAgent): scanner runs over the
#: worktree when the diff touches a dependency manifest/lockfile.
#: QG_DEP_AUDIT=0 disables it entirely.
DEP_AUDIT_ENABLED = os.environ.get("QG_DEP_AUDIT", "1") not in ("0", "false", "")
AUDIT_PROCESS_TIMEOUT = int(os.environ.get("QG_AUDIT_PROCESS_TIMEOUT", "120"))

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

#: FR-P8: commit-status context posted on the PR head sha so repos can
#: require the review via branch protection ("require status checks").
STATUS_CONTEXT = os.environ.get("QG_STATUS_CONTEXT", "quality-gate")

#: FR-P5: seconds the worker waits for in-flight runs on shutdown
#: before cancelling drain tasks.
SHUTDOWN_GRACE_S = float(os.environ.get("QG_SHUTDOWN_GRACE_S", "120"))

#: FR-P3: attempts for transient clone faults (network hiccups on
#: git clone/fetch). Timeouts and size-cap failures never retry.
RETRY_ATTEMPTS = int(os.environ.get("QG_RETRY_ATTEMPTS", "3"))
#: FR-P3: backoff base seconds between clone attempts.
RETRY_BACKOFF_S = float(os.environ.get("QG_RETRY_BACKOFF_S", "2"))

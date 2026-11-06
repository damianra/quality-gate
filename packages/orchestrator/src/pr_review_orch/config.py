"""Orchestrator configuration (env-only; secrets never leave env vars)."""

from __future__ import annotations

import os

#: Postgres DSN for langgraph-checkpoint-postgres. Optional: when unset an
#: in-memory checkpointer is used (no resumability across restarts).
DATABASE_URL = os.environ.get("DATABASE_URL", "")

#: Grace period between SIGTERM and SIGKILL in the cleanup node.
CLEANUP_GRACE_SECONDS = float(os.environ.get("QG_CLEANUP_GRACE_SECONDS", "3"))

#: Cap on files fetched when profiling a PR.
MAX_FILES_PAGES = int(os.environ.get("QG_MAX_FILES_PAGES", "5"))

#: Timeout for QA validation processes (tsc, node --check).
QA_PROCESS_TIMEOUT = int(os.environ.get("QG_QA_PROCESS_TIMEOUT", "120"))

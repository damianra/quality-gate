"""Structured JSON logging with run correlation (FR-P15).

Every log record is emitted as one JSON object so service-mode logs are
greppable/indexable. ``run_id_var`` carries the run correlation id —
the ``pr-review/{repo}/{pr}`` thread id that also keys ``run_metrics``
— across the drain task and its ``asyncio.to_thread`` hops (contextvars
copy into to_thread), so "what happened to PR #N" stays one query.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time

#: Correlation id for the in-flight run; empty outside a run.
run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "qg_run_id", default=""
)


class JsonFormatter(logging.Formatter):
    """One JSON object per record: ts, level, logger, msg, run_id, exc."""

    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, object] = {
            "ts": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        run_id = run_id_var.get()
        if run_id:
            doc["run_id"] = run_id
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger at ``level``.

    Called by entry points (ingress ``main``): without it the WARNING
    default drops every informational run log.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[handler],
        force=True,
    )

# OCI-standard image; works identically under docker and podman.
# Build context must be the REPOSITORY ROOT:
#   docker|podman build -f packages/orchestrator/Dockerfile .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# git + ripgrep (MCP tools) + node (QA syntax validation fallback)
# + procps (pgrep for the FR-P7 orphan-process sweep at ingress startup)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git ripgrep nodejs procps \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock* ./
COPY packages ./packages

RUN uv sync --no-dev --package pr-review-orchestrator

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /work \
    && chown -R appuser:appuser /app /work
USER appuser

ENV PATH="/app/.venv/bin:$PATH" \
    REPO_PATH=/work

# PR_NUMBER must be provided at runtime (env or --pr-number).
CMD ["python", "-m", "pr_review_orch"]

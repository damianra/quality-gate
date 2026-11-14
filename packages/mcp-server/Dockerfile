# OCI-standard image; works identically under docker and podman.
# Build context must be the REPOSITORY ROOT:
#   docker|podman build -f packages/mcp-server/Dockerfile .
# FR-P14: base images pinned by index digest (dependabot bumps them).
FROM docker.io/library/python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# git (remote/diff helpers) + ripgrep (search_code tool)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.24@sha256:2381d6aa60c326b71fd40023f921a0a3b8f91b14d5db6b90402e65a635053709 /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock* ./
COPY packages ./packages

# Sync only this package (workspace-aware); lockfile used when present.
RUN uv sync --no-dev --package quality-gate-mcp

RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "-m", "quality_gate_mcp.server"]

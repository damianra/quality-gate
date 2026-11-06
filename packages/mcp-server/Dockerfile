# OCI-standard image; works identically under docker and podman.
# Build context must be the REPOSITORY ROOT:
#   docker|podman build -f packages/mcp-server/Dockerfile .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# git (remote/diff helpers) + ripgrep (search_code tool)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

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

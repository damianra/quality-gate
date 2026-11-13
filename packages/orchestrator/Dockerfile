# OCI-standard image; works identically under docker and podman.
# Build context must be the REPOSITORY ROOT:
#   docker|podman build -f packages/orchestrator/Dockerfile .
# FR-P14: base images pinned by index digest (dependabot bumps them).
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# git + ripgrep (MCP tools) + node (QA syntax validation fallback)
# + procps (pgrep for the FR-P7 orphan-process sweep at ingress startup)
# + curl/ca-certificates (FR-P13 osv-scanner download below)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git ripgrep nodejs procps curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# FR-P13: version-pinned osv-scanner so the FR-O24 dependency audit
# actually runs in the production image; checksum-verified at build.
ARG OSV_SCANNER_VERSION=2.7.1
RUN arch="$(dpkg --print-architecture)" && cd /tmp \
    && curl -fsSLO "https://github.com/google/osv-scanner/releases/download/v${OSV_SCANNER_VERSION}/osv-scanner_linux_${arch}" \
    && curl -fsSLO "https://github.com/google/osv-scanner/releases/download/v${OSV_SCANNER_VERSION}/osv-scanner_SHA256SUMS" \
    && grep " osv-scanner_linux_${arch}$" osv-scanner_SHA256SUMS | sha256sum -c - \
    && install -m 0755 "osv-scanner_linux_${arch}" /usr/local/bin/osv-scanner \
    && rm -f osv-scanner_linux_* osv-scanner_SHA256SUMS

COPY --from=ghcr.io/astral-sh/uv:0.5.24@sha256:2381d6aa60c326b71fd40023f921a0a3b8f91b14d5db6b90402e65a635053709 /uv /uvx /usr/local/bin/

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

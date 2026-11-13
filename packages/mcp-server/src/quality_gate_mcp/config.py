"""Configuration for the MCP server: env vars, timeouts and thresholds.

All secrets come exclusively from environment variables. Nothing is logged.
"""

from __future__ import annotations

import os

# --- GitHub API -------------------------------------------------------------

GITHUB_API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com")

# --- GitHub App auth (FR-P18) -------------------------------------------------

#: When all three are set, installation tokens are minted at run time
#: instead of using the GITHUB_TOKEN PAT. The key may carry literal
#: ``\n`` escapes (single-line env storage).
GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID", "")
GITHUB_APP_PRIVATE_KEY = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
GITHUB_APP_INSTALLATION_ID = os.environ.get(
    "GITHUB_APP_INSTALLATION_ID", "")


def github_token() -> str:
    """Return the token for GitHub calls (never log this value).

    FR-P18: the GitHub App path wins when configured — a 1h installation
    token minted at run time beats a long-lived PAT. Local runs keep
    working with plain ``GITHUB_TOKEN``.
    """
    from . import github_app  # local import: avoids the config cycle
    if github_app.app_configured():
        return github_app.installation_token()
    return os.environ.get("GITHUB_TOKEN", "")


# --- Timeouts (seconds) ------------------------------------------------------

#: Default wall-clock timeout for spawned processes (tsc, node --check, ...).
PROCESS_TIMEOUT = int(os.environ.get("QG_PROCESS_TIMEOUT", "120"))
#: Timeout for plain git commands.
GIT_TIMEOUT = int(os.environ.get("QG_GIT_TIMEOUT", "60"))
#: Timeout for ripgrep searches.
RG_TIMEOUT = int(os.environ.get("QG_RG_TIMEOUT", "30"))
#: Timeout for GitHub HTTP requests.
HTTP_TIMEOUT = float(os.environ.get("QG_HTTP_TIMEOUT", "30"))
#: Grace period between SIGTERM and SIGKILL when killing a process group.
KILL_GRACE_SECONDS = float(os.environ.get("QG_KILL_GRACE_SECONDS", "3"))

# --- Retries (FR-P3) ---------------------------------------------------------

#: Attempts for transient faults (429/5xx/network) on GitHub REST calls
#: and the ephemeral clone. Permanent 4xx/auth failures never retry.
RETRY_ATTEMPTS = int(os.environ.get("QG_RETRY_ATTEMPTS", "3"))
#: Backoff base seconds — delay is base * 2**attempt, or Retry-After.
RETRY_BACKOFF_S = float(os.environ.get("QG_RETRY_BACKOFF_S", "2"))

# --- Vibecoding scanner thresholds -------------------------------------------

LARGE_DIFF_FILES = int(os.environ.get("QG_LARGE_DIFF_FILES", "10"))
LARGE_DIFF_LINES = int(os.environ.get("QG_LARGE_DIFF_LINES", "600"))
#: Minimum run of comment-only added lines that counts as a "commented block".
COMMENTED_BLOCK_MIN = int(os.environ.get("QG_COMMENTED_BLOCK_MIN", "3"))
#: whitespace_ratio above which a diff is considered whitespace-dominated.
WHITESPACE_RATIO_THRESHOLD = float(os.environ.get("QG_WHITESPACE_RATIO_THRESHOLD", "0.6"))

# --- Sensitive configuration files (config churn) ----------------------------

#: fnmatch-style patterns for files whose modification is always flagged.
SENSITIVE_CONFIGS: tuple[str, ...] = (
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "tsconfig*.json",
    "jsconfig*.json",
    "app.json",
    "app.config.*",
    "eas.json",
    "babel.config.*",
    "metro.config.*",
    ".babelrc*",
    ".eslintrc*",
    "eslint.config.*",
    ".prettierrc*",
    "prettier.config.*",
    "Dockerfile*",
    "docker-compose*.y*ml",
    "compose.y*ml",
    ".env",
    ".env.*",
    "*.env",
)

#: Sensitive paths relative to directories (matched against any path segment).
SENSITIVE_DIRS: tuple[str, ...] = (
    ".github/workflows",
    "android/app",
    "ios/",
)

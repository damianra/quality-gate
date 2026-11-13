"""GitHub App authentication (FR-P18).

When ``GITHUB_APP_ID`` / ``GITHUB_APP_PRIVATE_KEY`` /
``GITHUB_APP_INSTALLATION_ID`` are set, tokens are minted at run time:
a short-lived RS256 JWT authenticates as the app, exchanged for an
installation access token (~1 h TTL) cached until near expiry. The PAT
path (``GITHUB_TOKEN``) stays the fallback for local runs.

Onboarding a repo becomes "install the app" — comments/statuses are
authored by the bot identity and scoped to the app's least-privilege
permissions (contents:read, pull_requests/issues:write, statuses).
"""

from __future__ import annotations

import time

import httpx
import jwt

from . import config

#: Cached installation token — minted lazily, refreshed before expiry.
_token_cache: dict[str, object] = {"token": "", "expires_at": 0.0}
#: Refresh margin: a cached token is reused until this close to expiry.
_REFRESH_MARGIN_S = 300.0
#: Installation tokens live ~1h; cache conservatively.
_TOKEN_TTL_S = 3300.0


def app_configured() -> bool:
    """True when all three app envs are set — the App path is active."""
    return bool(
        config.GITHUB_APP_ID
        and config.GITHUB_APP_PRIVATE_KEY
        and config.GITHUB_APP_INSTALLATION_ID
    )


def _private_key_pem() -> str:
    """PEM from env — supports ``\\n``-escaped single-line storage."""
    return config.GITHUB_APP_PRIVATE_KEY.replace("\\n", "\n")


def _app_jwt() -> str:
    """RS256 JWT authenticating as the app (10 min max per GitHub)."""
    now = int(time.time())
    return jwt.encode(
        {"iat": now - 60, "exp": now + 600,
         "iss": config.GITHUB_APP_ID},
        _private_key_pem(),
        algorithm="RS256",
    )


def installation_token() -> str:
    """Mint (or return the cached) installation access token."""
    now = time.time()
    if (_token_cache["token"]
            and float(_token_cache["expires_at"]) - _REFRESH_MARGIN_S > now):
        return str(_token_cache["token"])
    r = httpx.post(
        f"{config.GITHUB_API_URL.rstrip('/')}/app/installations/"
        f"{config.GITHUB_APP_INSTALLATION_ID}/access_tokens",
        headers={
            "Authorization": f"Bearer {_app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=config.HTTP_TIMEOUT,
    )
    r.raise_for_status()
    _token_cache.update(
        token=r.json()["token"], expires_at=now + _TOKEN_TTL_S)
    return str(_token_cache["token"])

"""Minimal httpx-based GitHub REST client.

The token is read exclusively from ``GITHUB_TOKEN`` (or injected explicitly)
and is never logged or embedded in URLs.
"""

from __future__ import annotations

from typing import Any

import httpx

from . import config


class GitHubClient:
    """Thin synchronous wrapper over the GitHub REST API."""

    def __init__(self, token: str | None = None, base_url: str | None = None) -> None:
        token = token if token is not None else config.github_token()
        self._client = httpx.Client(
            base_url=(base_url or config.GITHUB_API_URL).rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=config.HTTP_TIMEOUT,
        )

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- internals ---------------------------------------------------------- #

    def _get(self, path: str, **kw: Any) -> Any:
        r = self._client.get(path, **kw)
        r.raise_for_status()
        return r.json()

    def _get_all_pages(self, path: str, params: dict[str, Any] | None = None,
                       per_page: int = 100, max_pages: int = 10) -> list[Any]:
        """Follow per_page pagination up to ``max_pages``."""
        params = {**(params or {}), "per_page": per_page}
        items: list[Any] = []
        for page in range(1, max_pages + 1):
            params["page"] = page
            chunk = self._get(path, params=params)
            items.extend(chunk)
            if len(chunk) < per_page:
                break
        return items

    # -- pull requests ------------------------------------------------------- #

    def list_pulls(self, slug: str, state: str = "open") -> list[dict[str, Any]]:
        return self._get_all_pages(f"/repos/{slug}/pulls", {"state": state})

    def get_pr(self, slug: str, pr_number: int) -> dict[str, Any]:
        return self._get(f"/repos/{slug}/pulls/{pr_number}")

    def list_pr_files(
        self, slug: str, pr_number: int, page: int = 1, per_page: int = 30
    ) -> list[dict[str, Any]]:
        return self._get(
            f"/repos/{slug}/pulls/{pr_number}/files",
            params={"page": page, "per_page": per_page},
        )

    def get_pr_diff_text(self, slug: str, pr_number: int) -> str:
        """Fetch the full unified diff of a PR (may be large — use sparingly)."""
        r = self._client.get(
            f"/repos/{slug}/pulls/{pr_number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        r.raise_for_status()
        return r.text

    def create_pull(
        self, slug: str, head: str, base: str, title: str, body: str = ""
    ) -> dict[str, Any]:
        r = self._client.post(
            f"/repos/{slug}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
        if r.status_code == 422:
            # A PR already exists for this head — return it instead of failing.
            owner = slug.split("/", 1)[0]
            existing = self._get(
                f"/repos/{slug}/pulls",
                params={"head": f"{owner}:{head}", "state": "open"},
            )
            if existing:
                return existing[0]
        r.raise_for_status()
        return r.json()

    # -- comments ------------------------------------------------------------ #

    def list_issue_comments(self, slug: str, issue_number: int) -> list[dict[str, Any]]:
        return self._get_all_pages(f"/repos/{slug}/issues/{issue_number}/comments")

    def create_issue_comment(self, slug: str, issue_number: int, body: str) -> dict[str, Any]:
        r = self._client.post(
            f"/repos/{slug}/issues/{issue_number}/comments", json={"body": body}
        )
        r.raise_for_status()
        return r.json()

    def update_issue_comment(self, slug: str, comment_id: int, body: str) -> dict[str, Any]:
        r = self._client.patch(f"/repos/{slug}/issues/comments/{comment_id}", json={"body": body})
        r.raise_for_status()
        return r.json()

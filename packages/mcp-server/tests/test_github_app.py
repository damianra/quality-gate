"""FR-P18: GitHub App installation-token minting and PAT fallback."""
from __future__ import annotations

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from quality_gate_mcp import config, github_app


@pytest.fixture
def app_env(monkeypatch):
    """App envs + a real RSA keypair; clears the token cache."""
    key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(config, "GITHUB_APP_ID", "12345")
    monkeypatch.setattr(config, "GITHUB_APP_PRIVATE_KEY", pem)
    monkeypatch.setattr(config, "GITHUB_APP_INSTALLATION_ID", "678")
    github_app._token_cache.update(token="", expires_at=0.0)
    return {"private": pem,
            "public": key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo).decode()}


def test_app_configured_requires_all_three(app_env, monkeypatch) -> None:
    assert github_app.app_configured()
    monkeypatch.setattr(config, "GITHUB_APP_INSTALLATION_ID", "")
    assert not github_app.app_configured()


def test_escaped_newlines_in_key(app_env, monkeypatch) -> None:
    """Env storage commonly escapes PEM newlines as literal \\n."""
    one_line = app_env["private"].replace("\n", "\\n")
    monkeypatch.setattr(config, "GITHUB_APP_PRIVATE_KEY", one_line)
    token = github_app._app_jwt()
    claims = jwt.decode(
        token, app_env["public"], algorithms=["RS256"])
    assert claims["iss"] == "12345"
    assert claims["exp"] - claims["iat"] == 660


def test_installation_token_posts_with_jwt(app_env, monkeypatch) -> None:
    captured: dict = {}

    def fake_post(url, headers, timeout):
        captured["url"] = url
        captured["auth"] = headers["Authorization"]
        req = httpx.Request("POST", url)
        return httpx.Response(
            201, json={"token": "inst-tok-1"}, request=req)

    monkeypatch.setattr(github_app.httpx, "post", fake_post)
    assert github_app.installation_token() == "inst-tok-1"
    assert captured["url"].endswith(
        "/app/installations/678/access_tokens")
    # The Authorization bearer is a verifiable app JWT.
    claims = jwt.decode(
        captured["auth"].removeprefix("Bearer "),
        app_env["public"], algorithms=["RS256"])
    assert claims["iss"] == "12345"


def test_installation_token_cached(app_env, monkeypatch) -> None:
    calls = {"n": 0}

    def fake_post(url, headers, timeout):
        calls["n"] += 1
        req = httpx.Request("POST", url)
        return httpx.Response(
            201, json={"token": "cached"}, request=req)

    monkeypatch.setattr(github_app.httpx, "post", fake_post)
    github_app._token_cache.update(
        token="cached", expires_at=time.time() + 1200)
    assert github_app.installation_token() == "cached"
    assert calls["n"] == 0          # cache hit — no HTTP


def test_github_token_prefers_app(app_env, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "pat-fallback")
    monkeypatch.setattr(
        github_app, "installation_token", lambda: "app-tok")
    assert config.github_token() == "app-tok"


def test_github_token_falls_back_to_pat(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "pat-only")
    for attr in ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY",
                 "GITHUB_APP_INSTALLATION_ID"):
        monkeypatch.setattr(config, attr, "")
    assert config.github_token() == "pat-only"

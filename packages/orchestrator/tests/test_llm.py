"""Unit tests for the provider-agnostic LLM configuration.

No network: ``from_env`` gets injected env dicts and only the pure request
builders are exercised. ``complete`` itself is integration-level.
"""

from __future__ import annotations

import pytest

from pr_review_orch.llm import LLMConfig, LLMConfigError


def test_disabled_when_provider_unset() -> None:
    cfg = LLMConfig.from_env({})
    assert cfg.enabled is False
    with pytest.raises(LLMConfigError):
        cfg.complete("sys", "hi")


def test_ollama_needs_no_key() -> None:
    cfg = LLMConfig.from_env({"QG_LLM_PROVIDER": "ollama"})
    assert cfg.enabled and cfg.provider == "ollama"
    assert cfg.base_url == "http://localhost:11434/v1"
    assert cfg.api_key == ""
    assert cfg.model == "qwen2.5-coder:7b"  # provider default


def test_openai_requires_key() -> None:
    with pytest.raises(LLMConfigError, match="API key"):
        LLMConfig.from_env({"QG_LLM_PROVIDER": "openai"})


def test_canonical_env_is_fallback() -> None:
    cfg = LLMConfig.from_env(
        {"QG_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test"}
    )
    assert cfg.api_key == "sk-test"


def test_qg_key_beats_canonical() -> None:
    cfg = LLMConfig.from_env(
        {
            "QG_LLM_PROVIDER": "nvidia",
            "QG_LLM_API_KEY": "qg-key",
            "NVIDIA_API_KEY": "nv-key",
        }
    )
    assert cfg.api_key == "qg-key"
    assert cfg.base_url == "https://integrate.api.nvidia.com/v1"


def test_unknown_provider_rejected() -> None:
    with pytest.raises(LLMConfigError, match="unknown QG_LLM_PROVIDER"):
        LLMConfig.from_env({"QG_LLM_PROVIDER": "bogus"})


def test_custom_requires_base_url() -> None:
    with pytest.raises(LLMConfigError, match="QG_LLM_BASE_URL"):
        LLMConfig.from_env({"QG_LLM_PROVIDER": "custom", "QG_LLM_API_KEY": "k"})


def test_custom_with_base_url_and_model() -> None:
    cfg = LLMConfig.from_env(
        {
            "QG_LLM_PROVIDER": "custom",
            "QG_LLM_API_KEY": "k",
            "QG_LLM_BASE_URL": "https://llm.internal.example/v1/",
            "QG_LLM_MODEL": "my-model",
        }
    )
    assert cfg.base_url == "https://llm.internal.example/v1"  # trailing slash cut
    assert cfg.model == "my-model"


def test_openai_request_shape() -> None:
    cfg = LLMConfig.from_env(
        {"QG_LLM_PROVIDER": "openrouter", "QG_LLM_API_KEY": "sk-or"}
    )
    url, headers, payload = cfg._openai_request("sys", "user")
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-or"
    assert headers["X-Title"] == "quality-gate"
    assert payload["messages"][0] == {"role": "system", "content": "sys"}


def test_anthropic_request_shape() -> None:
    cfg = LLMConfig.from_env(
        {"QG_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "ak"}
    )
    url, headers, payload = cfg._anthropic_request("sys", "user")
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "ak"
    assert headers["anthropic-version"] == "2023-06-01"
    assert payload["system"] == "sys"
    assert payload["messages"] == [{"role": "user", "content": "user"}]

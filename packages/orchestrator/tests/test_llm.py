"""Unit tests for the provider-agnostic LLM configuration.

No network: ``from_env`` gets injected env dicts and only the pure request
builders are exercised. ``complete`` itself is integration-level.
"""

from __future__ import annotations

import pytest

from pr_review_orch.llm import LLMConfig, LLMConfigError, _parse_response


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


def test_openai_response_truncated_on_length() -> None:
    out = _parse_response(
        "openai",
        {
            "choices": [
                {"message": {"content": "{\"findings\":"}, "finish_reason": "length"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2048},
        },
    )
    assert out["truncated"] is True
    assert out["finish_reason"] == "length"
    assert out["usage"] == {"prompt_tokens": 10, "completion_tokens": 2048}


def test_openai_response_max_tokens_reason_is_truncated() -> None:
    # Some routers (incl. OpenRouter providers) emit "max_tokens".
    out = _parse_response(
        "openai",
        {"choices": [{"message": {"content": "cut"}, "finish_reason": "max_tokens"}]},
    )
    assert out["truncated"] is True


def test_openai_response_stop_is_not_truncated() -> None:
    out = _parse_response(
        "openai",
        {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        },
    )
    assert out["truncated"] is False
    assert out["text"] == "ok"
    assert out["finish_reason"] == "stop"


def test_openai_response_missing_fields_is_safe() -> None:
    out = _parse_response("openai", {"choices": []})
    assert out["text"] == ""
    assert out["truncated"] is False
    assert out["usage"] == {"prompt_tokens": 0, "completion_tokens": 0}


def test_anthropic_response_truncated_on_max_tokens() -> None:
    out = _parse_response(
        "anthropic",
        {
            "content": [{"type": "text", "text": "partial"}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 5, "output_tokens": 2048},
        },
    )
    assert out["truncated"] is True
    assert out["finish_reason"] == "max_tokens"
    assert out["usage"] == {"prompt_tokens": 5, "completion_tokens": 2048}


def test_anthropic_response_end_turn_is_not_truncated() -> None:
    out = _parse_response(
        "anthropic",
        {
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 4, "output_tokens": 2},
        },
    )
    assert out["truncated"] is False
    assert out["text"] == "done"

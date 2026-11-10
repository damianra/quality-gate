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


def test_for_role_overrides_only_scoped_vars() -> None:
    env = {
        "QG_LLM_PROVIDER": "ollama",
        "QG_LLM_MODEL": "qwen3:8b",
        "QG_LLM_MAX_TOKENS": "4096",
        "QG_LLM_PROVIDER_QA": "openrouter",
        "QG_LLM_MODEL_QA": "cohere/north-mini-code:free",
        "QG_LLM_API_KEY_QA": "sk-or",
    }
    qa = LLMConfig.for_role("qa", env)
    assert qa.provider == "openrouter"
    assert qa.model == "cohere/north-mini-code:free"
    assert qa.api_key == "sk-or"
    assert qa.max_tokens == 4096  # inherited from base block
    assert qa.role == "qa"

    harness = LLMConfig.for_role("harness", env)
    assert harness.provider == "ollama"
    assert harness.model == "qwen3:8b"
    assert harness.api_key == ""  # ollama needs no key; QA override does not leak


def test_for_role_is_case_insensitive() -> None:
    env = {"QG_LLM_PROVIDER_SECURITY": "ollama"}
    cfg = LLMConfig.for_role("SECURITY", env)
    assert cfg.provider == "ollama"
    assert cfg.role == "security"


def test_for_role_falls_back_to_canonical_key() -> None:
    env = {
        "QG_LLM_PROVIDER_REVIEW": "openrouter",
        "QG_LLM_MODEL_REVIEW": "nvidia/nemotron-3.5-lightning:free",
        "OPENROUTER_API_KEY": "sk-or-canonical",
    }
    review = LLMConfig.for_role("review", env)
    assert review.provider == "openrouter"
    assert review.api_key == "sk-or-canonical"


def test_for_role_empty_override_inherits_base() -> None:
    env = {
        "QG_LLM_PROVIDER": "ollama",
        "QG_LLM_MODEL": "qwen3:8b",
        "QG_LLM_MODEL_QA": "",  # empty suffix var = unset
    }
    qa = LLMConfig.for_role("qa", env)
    assert qa.model == "qwen3:8b"


def test_json_mode_role_override() -> None:
    env = {
        "QG_LLM_PROVIDER": "ollama",
        "QG_LLM_MODEL": "qwen3:8b",
        "QG_LLM_JSON_MODE_SECURITY": "1",
    }
    security = LLMConfig.for_role("security", env)
    assert security.json_mode is True
    # base/other roles are unaffected
    assert LLMConfig.for_role("qa", env).json_mode is False


def test_openai_request_json_mode_adds_response_format() -> None:
    cfg = LLMConfig.from_env(
        {"QG_LLM_PROVIDER": "ollama", "QG_LLM_MODEL": "m",
         "QG_LLM_JSON_MODE": "true"}
    )
    _, _, payload = cfg._openai_request("sys", "user")
    assert payload["response_format"] == {"type": "json_object"}
    # disabled -> key absent
    cfg2 = LLMConfig.from_env(
        {"QG_LLM_PROVIDER": "ollama", "QG_LLM_MODEL": "m"}
    )
    _, _, payload2 = cfg2._openai_request("sys", "user")
    assert "response_format" not in payload2


def test_for_role_unknown_role_rejected() -> None:
    with pytest.raises(LLMConfigError, match="unknown role"):
        LLMConfig.for_role("bogus", {})


def test_devin_cli_needs_no_key_or_model() -> None:
    cfg = LLMConfig.from_env({"QG_LLM_PROVIDER": "devin-cli"})
    assert cfg.enabled is True
    assert cfg.provider == "devin-cli"
    assert cfg.model == ""  # CLI default applies


def test_devin_cli_argv_is_read_only_and_shell_free() -> None:
    cfg = LLMConfig.from_env({"QG_LLM_PROVIDER": "devin-cli"})
    argv = cfg._devin_cli_argv("sys", "user")
    assert argv[0] == "devin"
    assert "--print" in argv and argv[-1] == "sys\n\nuser"
    assert "--permission-mode" in argv and "auto" in argv
    assert "--respect-workspace-trust" in argv and "false" in argv
    assert "--model" not in argv  # unset model -> CLI default


def test_devin_cli_argv_includes_model_when_set() -> None:
    cfg = LLMConfig.from_env(
        {"QG_LLM_PROVIDER": "devin-cli", "QG_LLM_MODEL": "swe-2-high"}
    )
    argv = cfg._devin_cli_argv("sys", "user")
    i = argv.index("--model")
    assert argv[i + 1] == "swe-2-high"


def _fake_spawn(result):
    def _spawn(repo_path, command, args=None, timeout=None):
        return {**result, "command": [command, *(args or [])]}
    return _spawn


def test_devin_cli_completion_returns_stdout_and_pgid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pr_review_orch.llm.mcp_tools.spawn_process",
        _fake_spawn({
            "pgid": 4242, "exit_code": 0, "stdout": "review out",
            "stderr": "", "timed_out": False, "duration_s": 1.5,
        }),
    )
    cfg = LLMConfig.from_env({"QG_LLM_PROVIDER": "devin-cli"})
    out = cfg.complete_with_meta("sys", "user", cwd="/repo")
    assert out["text"] == "review out"
    assert out["pgid"] == 4242
    assert out["truncated"] is False
    assert out["usage"] == {"prompt_tokens": 0, "completion_tokens": 0}


def test_devin_cli_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pr_review_orch.llm.mcp_tools.spawn_process",
        _fake_spawn({
            "pgid": -1, "exit_code": 127, "stdout": "",
            "stderr": "command not found: devin",
            "timed_out": False, "duration_s": 0.0,
        }),
    )
    cfg = LLMConfig.from_env({"QG_LLM_PROVIDER": "devin-cli"})
    with pytest.raises(RuntimeError, match="devin-cli failed"):
        cfg.complete_with_meta("sys", "user", cwd="/repo")


def test_devin_cli_timeout_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pr_review_orch.llm.mcp_tools.spawn_process",
        _fake_spawn({
            "pgid": 4242, "exit_code": -9, "stdout": "",
            "stderr": "killed", "timed_out": True, "duration_s": 60.0,
        }),
    )
    cfg = LLMConfig.from_env({"QG_LLM_PROVIDER": "devin-cli"})
    with pytest.raises(RuntimeError, match="timed_out=True"):
        cfg.complete_with_meta("sys", "user", cwd="/repo")

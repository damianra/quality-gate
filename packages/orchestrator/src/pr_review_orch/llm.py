"""Provider-agnostic LLM configuration and a minimal chat client.

Design goals:

- The user picks a provider with ``QG_LLM_PROVIDER`` and supplies the key
  via environment variables only (per ``AGENTS.md`` — keys are never logged
  or embedded in errors).
- Ollama, NVIDIA NIM, OpenRouter and most self-hosted endpoints speak the
  OpenAI chat-completions API, so one code path covers them; Anthropic uses
  its native Messages API behind the same interface.

Supported ``QG_LLM_PROVIDER`` values:

===========  ====================  ==========================================
provider     api                   default base URL
===========  ====================  ==========================================
ollama       openai-compatible     http://localhost:11434/v1 (no key needed)
openai       openai-compatible     https://api.openai.com/v1
anthropic    anthropic native      https://api.anthropic.com
nvidia       openai-compatible     https://integrate.api.nvidia.com/v1
openrouter   openai-compatible     https://openrouter.ai/api/v1
custom       openai-compatible     requires ``QG_LLM_BASE_URL``
===========  ====================  ==========================================
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

_DEFAULT_TIMEOUT = 60.0


@dataclass(frozen=True)
class Provider:
    """Static defaults for one LLM provider family."""

    api: str  # "openai" (chat completions) or "anthropic" (messages)
    base_url: str | None
    needs_key: bool
    canonical_env: tuple[str, ...]  # provider-native key vars honored as fallback
    default_model: str


PROVIDERS: dict[str, Provider] = {
    "ollama": Provider(
        api="openai",
        base_url="http://localhost:11434/v1",
        needs_key=False,
        canonical_env=(),
        default_model="qwen2.5-coder:7b",
    ),
    "openai": Provider(
        api="openai",
        base_url="https://api.openai.com/v1",
        needs_key=True,
        canonical_env=("OPENAI_API_KEY",),
        default_model="gpt-4o-mini",
    ),
    "anthropic": Provider(
        api="anthropic",
        base_url="https://api.anthropic.com",
        needs_key=True,
        canonical_env=("ANTHROPIC_API_KEY",),
        default_model="claude-sonnet-4-5-20250929",
    ),
    "nvidia": Provider(
        api="openai",
        base_url="https://integrate.api.nvidia.com/v1",
        needs_key=True,
        canonical_env=("NVIDIA_API_KEY",),
        default_model="meta/llama-3.3-70b-instruct",
    ),
    "openrouter": Provider(
        api="openai",
        base_url="https://openrouter.ai/api/v1",
        needs_key=True,
        canonical_env=("OPENROUTER_API_KEY",),
        default_model="meta-llama/llama-3.3-70b-instruct",
    ),
    "custom": Provider(
        api="openai",
        base_url=None,  # QG_LLM_BASE_URL is mandatory for custom endpoints
        needs_key=True,
        canonical_env=(),
        default_model="",
    ),
}


class LLMConfigError(ValueError):
    """Invalid or incomplete LLM configuration."""


@dataclass
class LLMConfig:
    """Resolved LLM settings. ``enabled=False`` means no provider configured."""

    enabled: bool = False
    provider: str = ""
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.0
    timeout: float = _DEFAULT_TIMEOUT
    max_tokens: int = 2048

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "LLMConfig":
        """Build the config from env vars (``os.environ`` by default)."""
        env = os.environ if env is None else env
        name = env.get("QG_LLM_PROVIDER", "").strip().lower()
        if not name:
            return cls()

        spec = PROVIDERS.get(name)
        if spec is None:
            raise LLMConfigError(
                f"unknown QG_LLM_PROVIDER {name!r}; "
                f"expected one of {sorted(PROVIDERS)}"
            )

        # QG_LLM_API_KEY wins; the provider's canonical var is the fallback.
        api_key = env.get("QG_LLM_API_KEY", "")
        for var in spec.canonical_env:
            api_key = api_key or env.get(var, "")
        if spec.needs_key and not api_key:
            canonical = "/".join(spec.canonical_env)
            raise LLMConfigError(
                f"provider {name!r} requires an API key: set QG_LLM_API_KEY"
                + (f" or {canonical}" if canonical else "")
            )

        base_url = (env.get("QG_LLM_BASE_URL") or spec.base_url or "").rstrip("/")
        if spec.base_url is None and not base_url:
            raise LLMConfigError(f"provider {name!r} requires QG_LLM_BASE_URL")

        model = env.get("QG_LLM_MODEL") or spec.default_model
        if not model:
            raise LLMConfigError(f"provider {name!r} requires QG_LLM_MODEL")

        return cls(
            enabled=True,
            provider=name,
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=float(env.get("QG_LLM_TEMPERATURE", "0.0")),
            timeout=float(env.get("QG_LLM_TIMEOUT", str(_DEFAULT_TIMEOUT))),
            max_tokens=int(env.get("QG_LLM_MAX_TOKENS", "2048")),
        )

    # ------------------------------------------------------------------ #
    # Request construction (kept pure for unit tests)
    # ------------------------------------------------------------------ #

    def _openai_request(
        self, system: str, user: str
    ) -> tuple[str, dict[str, str], dict[str, object]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.provider == "openrouter":
            # OpenRouter asks apps to identify themselves for rate-limit tiers.
            headers["X-Title"] = "quality-gate"
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        return f"{self.base_url}/chat/completions", headers, payload

    def _anthropic_request(
        self, system: str, user: str
    ) -> tuple[str, dict[str, str], dict[str, object]]:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        return f"{self.base_url}/v1/messages", headers, payload

    # ------------------------------------------------------------------ #
    # Chat
    # ------------------------------------------------------------------ #

    def complete(self, system: str, user: str) -> str:
        """Single-shot completion. Returns the assistant message text."""
        if not self.enabled:
            raise LLMConfigError("LLM is disabled: set QG_LLM_PROVIDER")
        spec = PROVIDERS[self.provider]
        if spec.api == "anthropic":
            url, headers, payload = self._anthropic_request(system, user)
        else:
            url, headers, payload = self._openai_request(system, user)

        # Never attach headers to raised errors: they carry the API key.
        resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()

        if spec.api == "anthropic":
            return "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )
        return data["choices"][0]["message"]["content"]


def load() -> LLMConfig:
    """Shortcut for ``LLMConfig.from_env()`` against the process env."""
    return LLMConfig.from_env()

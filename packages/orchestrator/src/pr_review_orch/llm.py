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
import time
from dataclasses import dataclass
from typing import Any

import httpx

from quality_gate_mcp import tools as mcp_tools

_DEFAULT_TIMEOUT = 60.0


@dataclass(frozen=True)
class Provider:
    """Static defaults for one LLM provider family."""

    api: str  # "openai" (chat completions), "anthropic" (messages),
    # or "devin-cli" (local agent subprocess, FR-O17)
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
    "devin-cli": Provider(
        api="devin-cli",
        base_url="",  # local agent subprocess, no HTTP endpoint
        needs_key=False,  # auth lives in the `devin auth` session, not env
        canonical_env=(),
        default_model="",  # unset -> the CLI's configured default model
    ),
}


# Graph roles that may carry an independent LLM config via
# QG_LLM_<SETTING>_<ROLE> suffixes (SPEC.md FR-O15).
ROLES = ("harness", "review", "security", "qa")


def _normalize_usage(api: str, usage: dict[str, Any]) -> dict[str, int]:
    """Map provider-specific usage fields to prompt/completion tokens."""
    if api == "anthropic":
        return {
            "prompt_tokens": int(usage.get("input_tokens", 0)),
            "completion_tokens": int(usage.get("output_tokens", 0)),
        }
    return {
        "prompt_tokens": int(usage.get("prompt_tokens", 0)),
        "completion_tokens": int(usage.get("completion_tokens", 0)),
    }


# Provider stop signals that mean the answer was cut by a token cap:
# OpenAI-compatible `finish_reason` ("length"; some routers emit
# "max_tokens") and Anthropic `stop_reason` ("max_tokens").
_TRUNCATION_REASONS = {"length", "max_tokens"}


def _parse_response(api: str, data: dict[str, Any]) -> dict[str, Any]:
    """Extract text, usage and truncation status from a raw API response.

    Pure — unit-tested without network. ``truncated`` is ``True`` when
    the provider hit a token cap mid-answer, so callers can degrade
    instead of trusting cut-off output (e.g. half-written JSON). Note
    that reasoning models may burn ``max_tokens`` on chain-of-thought
    and return empty ``text`` with ``truncated=True``.
    """
    if api == "anthropic":
        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        finish_reason = data.get("stop_reason")
    else:
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        finish_reason = choice.get("finish_reason")
    return {
        "text": text,
        "usage": _normalize_usage(api, data.get("usage") or {}),
        "finish_reason": finish_reason,
        "truncated": finish_reason in _TRUNCATION_REASONS,
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
    max_tokens: int = 20000
    json_mode: bool = False
    role: str = ""

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
        if not model and spec.api != "devin-cli":
            # devin-cli may legitimately run model-less (CLI default).
            raise LLMConfigError(f"provider {name!r} requires QG_LLM_MODEL")

        return cls(
            enabled=True,
            provider=name,
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=float(env.get("QG_LLM_TEMPERATURE", "0.0")),
            timeout=float(env.get("QG_LLM_TIMEOUT", str(_DEFAULT_TIMEOUT))),
            max_tokens=int(env.get("QG_LLM_MAX_TOKENS", "20000")),
            json_mode=env.get("QG_LLM_JSON_MODE", "").strip().lower()
            in {"1", "true", "yes", "on"},
        )

    @classmethod
    def for_role(
        cls, role: str, env: dict[str, str] | None = None
    ) -> "LLMConfig":
        """Resolve the LLM config for one graph role (SPEC.md FR-O15).

        ``QG_LLM_<SETTING>_<ROLE>`` overrides the base ``QG_LLM_<SETTING>``
        for that role only; the base block doubles as the HARNESS config
        and the shared fallback. Empty suffixed vars are ignored — unset
        means "inherit". Unknown roles raise ``LLMConfigError``.
        """
        env = os.environ if env is None else env
        suffix = role.strip().upper()
        if suffix.lower() not in ROLES:
            raise LLMConfigError(
                f"unknown role {role!r}; expected one of {list(ROLES)}"
            )
        merged = dict(env)
        marker = f"_{suffix}"
        for key, value in env.items():
            if (
                key.startswith("QG_LLM_")
                and key.endswith(marker)
                and value.strip()
            ):
                merged[key[: -len(marker)]] = value
        cfg = cls.from_env(merged)
        cfg.role = suffix.lower()
        return cfg

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
        if self.json_mode:
            # Structured output: reasoning/local models (e.g. qwen3)
            # otherwise ignore the JSON contract and emit prose.
            payload["response_format"] = {"type": "json_object"}
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

    def complete_with_meta(
        self, system: str, user: str, cwd: str | None = None
    ) -> dict[str, Any]:
        """Single-shot completion returning text plus usage/latency metrics.

        ``usage`` is normalized to ``prompt_tokens``/``completion_tokens``
        across providers (Anthropic reports input/output names).
        ``finish_reason`` carries the raw provider stop signal and
        ``truncated`` flags answers cut short by a token cap — callers
        MUST check it before trusting structured output (FR-O14).
        ``cwd`` is the working directory for subprocess providers
        (``devin-cli``); ignored by HTTP providers. CLI completions also
        return ``pgid`` so the caller can register it in ``active_pgids``.
        """
        if not self.enabled:
            raise LLMConfigError("LLM is disabled: set QG_LLM_PROVIDER")
        spec = PROVIDERS[self.provider]
        if spec.api == "devin-cli":
            return self._devin_cli(system, user, cwd)
        if spec.api == "anthropic":
            url, headers, payload = self._anthropic_request(system, user)
        else:
            url, headers, payload = self._openai_request(system, user)

        # Never attach headers to raised errors: they carry the API key.
        started = time.monotonic()
        resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
        duration_s = round(time.monotonic() - started, 3)
        resp.raise_for_status()
        data = resp.json()

        return {**_parse_response(spec.api, data), "duration_s": duration_s}

    def _devin_cli_argv(self, system: str, user: str) -> list[str]:
        """Argv for the non-interactive devin run (FR-O17). Pure for tests.

        ``--permission-mode auto`` keeps the agent read-only; print mode
        skips workspace-trust prompts so automated runs do not stall.
        """
        prompt = f"{system}\n\n{user}" if system else user
        argv = [
            "devin",
            "--permission-mode", "auto",
            "--respect-workspace-trust", "false",
            "--verbosity", "0",
        ]
        if self.model:
            argv += ["--model", self.model]
        argv += ["--print", prompt]
        return argv

    def _devin_cli(
        self, system: str, user: str, cwd: str | None
    ) -> dict[str, Any]:
        """Run the devin CLI as a tracked subprocess and return metadata.

        ``pgid`` is surfaced so the caller MUST register it in
        ``active_pgids`` for the cleanup node — same contract as other
        ``spawn_process`` consumers. Non-zero exits and timeouts raise
        ``RuntimeError`` for the caller to degrade per FR-O16.
        """
        argv = self._devin_cli_argv(system, user)
        res = mcp_tools.spawn_process(
            cwd or os.getcwd(), argv[0], argv[1:], timeout=int(self.timeout)
        )
        if res["timed_out"] or res["exit_code"] != 0:
            raise RuntimeError(
                f"devin-cli failed (exit={res['exit_code']},"
                f" timed_out={res['timed_out']}): {res['stderr'][-300:]}"
            )
        return {
            "text": res["stdout"],
            "duration_s": res["duration_s"],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "finish_reason": None,
            "truncated": False,
            "pgid": res["pgid"],
        }

    def complete(self, system: str, user: str) -> str:
        """Single-shot completion. Returns the assistant message text.

        Truncation is not surfaced here — use ``complete_with_meta`` when
        the caller needs to detect token-cap cut-offs.
        """
        return self.complete_with_meta(system, user)["text"]


def load() -> LLMConfig:
    """Shortcut for ``LLMConfig.from_env()`` against the process env."""
    return LLMConfig.from_env()

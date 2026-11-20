# Data handling — what leaves the host (FR-O34)

This document states exactly which data exits a quality-gate
deployment, where it goes, and how to shut the lane off. It exists so
an operator can answer "does PR code leave my perimeter?" without
reading the graph.

## What leaves the host

Nothing leaves the host **unless an LLM provider is configured**. With
every `QG_LLM_PROVIDER*` unset the review is fully deterministic:
fetch (GitHub API only), analysis, scanners, verdict — all local.

When a role has a provider configured, that role's call sends:

| payload block | content | bound |
|---|---|---|
| system prompt | the role playbook + output contract | static, ~2 KB |
| PR metadata | number, title, base/head sha | small |
| PR body | the PR description | `QG_PROMPT_MAX_BODY_CHARS` |
| deterministic signals | `analyze_diff` JSON (secret/shape signals, fingerprints only — never secret text) | small |
| changed files + diff | added-line hunks with `dNNN` ids | `QG_SCOPE_*` + `QG_PROMPT_MAX_DIFF_CHARS` |
| changed-file contents | file text | `QG_PROMPT_MAX_FILES_CHARS` |
| repo rules | `AGENTS.md` (review role only) | `QG_PROMPT_MAX_AGENTS_CHARS` |
| role extras | dependency-audit / scanner / validator output | top-10 JSON slices |
| related heads | dependent-file excerpts | `QG_RELATED_HEAD_*` |

The deterministic lane **never emits matched secret text**: gitleaks
reports a sha256 fingerprint + rule id. Anything a regex recognizes as
a secret additionally gets replaced by `[REDACTED:<kind>]` before the
payload is built — see below.

## Where it goes

Per `QG_LLM_PROVIDER` (per role, `QG_LLM_*_<ROLE>` overrides apply):

| provider | endpoint reachable | transport |
|---|---|---|
| `ollama` (default) | `http://localhost:11434` — stays on host | loopback HTTP |
| `openai` | `api.openai.com` | HTTPS |
| `anthropic` | `api.anthropic.com` | HTTPS |
| `nvidia` | `integrate.api.nvidia.com` | HTTPS |
| `openrouter` | `openrouter.ai` | HTTPS |
| `custom` | whatever `QG_LLM_BASE_URL` names — check it points at an endpoint you control | per scheme |
| `devin-cli` | **no HTTP egress from the orchestrator** — the payload is passed as an argv prompt to the local `devin` CLI process, which then talks to whatever the user's `devin auth` session is configured for | local subprocess |

Provider-side retention, training-data and telemetry policies are
**operator responsibilities** — quality-gate cannot control what the
provider does with a compliant request.

## Prompt redaction (always on)

Inside `LLMConfig.complete_with_meta` — the single funnel every call
(HTTP and `devin-cli` argv) passes through — a vendored detector
(`secrets.py`) scrubs each prompt string before the request is built:

- **Key shapes**: AWS `AKIA…`, GitHub `gh[pousr]_…`/`github_pat_…`,
  `sk-…`/`sk-ant-…`, Slack `xox…-`, GCP `AIza…`, PEM private-key
  blocks, JWTs.
- **Assignments**: `<name> = "<value>"` where the name looks
  credential-ish and the value clears an entropy floor — covers
  unshaped secrets.

Matches become `[REDACTED:<kind>]`; the secret text is never logged,
stored or returned. The `llm_calls` audit record carries a
`redactions` count proving the pass ran (absent = disabled). One gap:
a call that raises *after* scrubbing records an error entry with no
meta, so its `redactions` is absent even though the payload was
scrubbed — the count certifies completed calls, not attempts.

**Debug-only opt-out:** `QG_LLM_REDACT=0` — accepts the role suffix
(`QG_LLM_REDACT_SECURITY=0` disables it for the security role only).
Never leave it off in a deployment that reviews untrusted PRs.

## Kill switches

| switch | effect |
|---|---|
| `QG_LLM_PROVIDER` unset (default) | **zero LLM egress** — review still runs fully (deterministic findings only). This is the documented way to disable all model traffic. |
| `QG_LLM_PROVIDER_<ROLE>` unset | that role stays deterministic while others call out. |
| `QG_LLM_REDACT=0` | redaction pass off (debug only — payload goes out unscrubbed). |
| `QG_SANDBOX_ALLOW_NET=0` (default) | the PR-controlled sandbox stage runs `--network none`; it can never reach out regardless of the LLM lane. |
| `QG_SEC_TOOLS` | scanners run inside the network-less sandbox — they do not egress. |

## What does NOT leave the host

- `GITHUB_TOKEN` / `DATABASE_URL` — env-only; used for the GitHub API
  fetch path and checkpointing, never embedded in prompts, logs or
  findings.
- The full repository — `pr_context` is bounded to the PR's blast
  radius (`QG_SCOPE_MAX_FILES`/`QG_SCOPE_MAX_CHARS`); the tree is
  never sent wholesale.
- Matched secret text — redaction plus fingerprint-only findings
  means no code path can put a recognized credential into a prompt,
  a comment, a log line or a metrics row.

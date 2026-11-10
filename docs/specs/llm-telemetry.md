# Spec — English verdict + provider-agnostic LLM telemetry

**Status:** accepted — implemented on `feat/verdict-telemetry`.
**Scope:** amends SPEC.md §3 (INV-5) and §6.1, extends FR-O13/FR-O18.
Requirement IDs `FR-T*` are stable; tests and PRs reference them.

## 1. Motivation

The posted PR comment mixes Spanish scaffolding with English content and
offers only a one-line metrics summary. This change makes the verdict
comment fully English, enriches it with a per-model telemetry block, and
captures performance data (tokens/s, memory, provider-reported footprint)
for every LLM call — degrading gracefully for providers that report
little or nothing.

## 2. Requirements

- **FR-T1 English verdict contract.** The posted comment is entirely in
  English: heading `## Functional validation`, verdict line
  `**Verdict:** ✅ Approved | ❌ Changes required`, `**Result:**`,
  `**Note:**`, `**Metrics:**` and the unchanged idempotency marker
  `<!-- pr-review head:<sha7> -->`. INV-5 is amended accordingly: chat
  answers and README_ES stay Spanish; the PR comment is English.

- **FR-T2 Per-call telemetry.** `complete_with_meta` additionally
  returns:
  - `perf.completion_tps` — completion tokens ÷ call duration (derived,
    `None` when either input is missing/zero).
  - `memory` — host process RSS in MB sampled before/after the call
    (`rss_before_mb`, `rss_after_mb`, `rss_delta_mb`), read from
    `/proc/self/status` with a `resource` fallback.
  - `provider_stats` — optional provider-reported footprint. For
    `ollama` the client queries `{base}/api/ps` (base URL minus `/v1`)
    and reports `model_ram_mb`/`model_vram_mb` for the loaded model;
    any failure or non-matching provider yields `None`.
  Telemetry collection MUST never raise, never delay the call
  (best-effort, short timeouts) and never reject a PR.

- **FR-T3 Aggregation and comment detail.** `metrics.summarize` adds
  `llm.by_model`: per-model totals (calls, prompt/completion/total
  tokens, total duration, mean completion_tps, max RSS delta, max
  reported model VRAM). `metrics.summary_line` appends the per-model
  token split (`[model: tokens]`). `metrics.detail_block` renders a
  markdown table of per-model telemetry; the synthesizer passes it to
  `post_verdict`, which appends it to the comment inside a collapsible
  `<details>` block titled *LLM telemetry*. Providers that report
  nothing still appear with their call/token counts — the block is
  model-agnostic, never empty when calls exist.

- **FR-T4 Schema evolution.** `llm_call_metrics` gains columns
  `completion_tps`, `rss_delta_mb`, `model_size_mb`, `model_vram_mb`
  via `ALTER TABLE … ADD COLUMN IF NOT EXISTS` in the bootstrap batch
  (idempotent, no migration tooling). All new fields also land in the
  `raw` jsonb so nothing is lost.

- **FR-T5 Isolated per-role sessions.** Every graph role
  (`harness`, `review`, `security`, `qa`) issues an independent,
  stateless completion: its own `LLMConfig` (role-scoped
  `QG_LLM_*_<ROLE>` vars), its own system prompt and its own bounded
  payload — calls never share conversation state. The deployment
  default is `ollama` + `qwen3:8b` for all four roles, with
  `QG_LLM_JSON_MODE` enabled for the three specialists (structured
  findings contract) and disabled for the harness (prose summary).

## 3. Contracts

### 3.1 Verdict comment (English — supersedes SPEC.md §6.1 Spanish text)

```markdown
## Functional validation

**Verdict:** ✅ Approved | ❌ Changes required

- ✅|⚠️|❌ **check** — detail

**Result:** <summary>
**Note:** <note>

**Metrics:** <total_s; slowest node; LLM calls/tokens + per-model split; processes>

<details><summary>LLM telemetry</summary>

| role | model | calls | tokens (p+c) | s | tok/s | ΔRSS MB | model VRAM MB |
|---|---|---|---|---|---|---|---|
| ... | ... | ... | ... | ... | ... | ... | ... |

</details>

<!-- pr-review head:<sha7> -->
```

The `<details>` block is omitted when no LLM calls ran.

### 3.2 `llm_calls` entry additions

```text
{..., perf: {completion_tps: float|null},
      memory: {rss_before_mb, rss_after_mb, rss_delta_mb},
      provider_stats: {model_ram_mb, model_vram_mb} | null}
```

Absent fields mean "provider did not report" — consumers must treat
missing telemetry as unknown, never as zero-cost.

## 4. Non-functional

- **Model-agnostic.** A provider change (keyed API returning only
  `usage`) must produce a still-useful table: counts, tokens, duration.
- **No new dependencies.** RSS sampling uses stdlib only.
- **Never-blocking.** Telemetry hooks use ≤3 s timeouts and swallow all
  exceptions, consistent with FR-O16/FR-O18 degradation policy.

# quality-gate

Autonomous PR review for GitHub repositories of any language or
ecosystem. Replaces manual review:
on every PR the system profiles the diff, detects *vibecoding* (whitespace
noise, dead-comment blocks, TODOs, debug leftovers, secrets, dependency
churn, scope drift, prompt-injection attempts), runs structural
validation with the best available toolchain for the ecosystem, and
posts a contractual verdict comment on GitHub.

## Architecture

Two independently-versioned packages in one `uv` workspace:

```text
packages/mcp-server     stateless FastMCP server — deterministic tools
                        (git/GitHub API, ripgrep, process spawning)
packages/orchestrator   stateful LangGraph brain — router, specialists,
                        synthesizer, mandatory cleanup
.github/workflows       selective CI (paths filters) + pr-review trigger
compose.yaml            postgres (checkpointer) + mcp + orchestrator
```

```text
START -> router -> fan-out {review, security, qa} -> synthesizer -> cleanup -> END
```

- **Postgres** is used *only* by `langgraph-checkpoint-postgres` so a run can
  resume when a PR gets new commits (`thread_id = pr-review/<repo>/<pr>`).
- **Process safety is contractual**: `shell=True` is forbidden everywhere,
  every validation process spawns in a new process group, its `pgid` is
  registered in `active_pgids`, and the mandatory `cleanup` node (plus a
  `finally` safety net in `run_pr_review`) `SIGTERM`s/`SIGKILL`s every group.
  The GitHub Action additionally asserts no orphans remain.

## Quickstart

```bash
# 1. deps (installs both packages, editable)
uv sync --all-packages

# 2. unit tests (no network/git needed)
uv run pytest packages

# 3. local stack (postgres + mcp on :8000/sse + orchestrator)
export GITHUB_TOKEN=ghp_...
export REPO_UNDER_TEST=/path/to/repo-with-pr
export PR_NUMBER=123
podman compose up -d            # or: docker compose up -d
podman compose run --rm orchestrator
```

## Local test

Fastest end-to-end check — runs the full graph against a **local
checkout** of the PR head, posts nothing to GitHub (`--dry-run`) and
uses the in-memory checkpointer (no Postgres needed):

```bash
export GITHUB_TOKEN=ghp_...           # contents:read + pull_requests/issues:write
export REPO_PATH=/path/to/checkout    # repo checked out at the PR branch
export PR_NUMBER=123
uv run --package pr-review-orchestrator pr-review --dry-run
```

Output is a JSON verdict on stdout — `approve`, `summary`, `checks`,
`cleanup_report` and the `metrics` block (durations, per-model tokens,
telemetry). Exit code: `0` approved, `1` changes required. Without
`--dry-run` it posts/updates the real verdict comment — point
`REPO_PATH` at a throwaway repo when testing that path.

To exercise the **webhook** path instead, bring up the ingress + a
tunnel (next section).

## Registering the MCP in an IDE (manual debugging)

Point your MCP-capable IDE at the stdio server — e.g. `.vscode/mcp.json`:

```json
{
  "servers": {
    "quality-gate": {
      "command": "uv",
      "args": ["run", "--package", "quality-gate-mcp", "quality-gate-mcp"],
      "env": { "GITHUB_TOKEN": "ghp_..." }
    }
  }
}
```

Or attach to the SSE endpoint started by compose's `debug` profile
(`docker compose --profile debug up -d mcp-server`):
`http://localhost:8000/sse`.

## Verdict contract

The published comment is always:

```markdown
## Functional validation

**Verdict:** ✅ Approved | ❌ Changes required

- ✅/⚠️/❌ **check** — detail

**Result:** <summary>
**Note:** <note>

**Metrics:** <one-liner>
<details><summary>LLM telemetry</summary>   # per-model table, when calls ran

<!-- pr-review head:<sha7> -->
```

Idempotency: re-runs on the same head **update** the existing marked
comment; a PR is `pending` until a marker matching its `head:<sha7>` exists.

## Environment variables

Copy `.env.example` to `.env` (gitignored) and fill in the values — it lists
every variable below with defaults. `compose.yaml` picks it up automatically.

- `GITHUB_TOKEN` — env-only, never logged/embedded (required)
- `DATABASE_URL` — Postgres checkpointer DSN (optional)
- `REPO_PATH`, `PR_NUMBER`, `DRY_RUN` — orchestrator inputs
- `QG_*` — timeouts and scanner thresholds (see `config.py` files)
- `QG_SCOPE_MAX_FILES`, `QG_SCOPE_MAX_CHARS` — blast-radius caps: agents
  review the diff + changed files + direct dependents only, never the
  whole repo
- `QG_LLM_*` — optional LLM provider (see below)

### Metrics

Every run reports a `metrics` block (CLI JSON + `**Metrics:**` line in
the PR verdict): per-node durations, LLM calls with prompt/completion
tokens split per model, and spawned-process timings — so you can see
where time and tokens go. A collapsible `<details>` block adds per-model
telemetry: tokens (p+c), duration, completion tok/s, host RSS delta and
the model footprint the provider reports (Ollama `/api/ps` → RAM/VRAM).
Fields a provider does not return render as `—`; telemetry never blocks
a run.

### LLM provider (optional)

The review is deterministic by default. Setting `QG_LLM_PROVIDER` enables
LLM-backed enrichment in the orchestrator; keys stay in env vars only
(`QG_LLM_API_KEY` or the provider's canonical variable). The `review`,
`security` and `qa` specialists each call their role config over the
bounded `pr_context`; LLM findings appear prefixed `LLM:` in the verdict
and any failure degrades to an info note — it never rejects a PR by
itself. The synthesizer also calls the `harness` role (the base
`QG_LLM_*` config — e.g. local Ollama) for a short executive summary of
the verdict, recorded in `llm_call_metrics` like every other role.

| `QG_LLM_PROVIDER` | Key env (fallback) | Default base URL |
|---|---|---|
| `ollama` | — (no key) | `http://localhost:11434/v1` |
| `openai` | `OPENAI_API_KEY` | `https://api.openai.com/v1` |
| `anthropic` | `ANTHROPIC_API_KEY` | `https://api.anthropic.com` |
| `nvidia` | `NVIDIA_API_KEY` | `https://integrate.api.nvidia.com/v1` |
| `openrouter` | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` |
| `custom` | `QG_LLM_API_KEY` | requires `QG_LLM_BASE_URL` |
| `devin-cli` | — (`devin auth` session) | local subprocess (bare host only), no URL |

Each provider ships a default model; override with `QG_LLM_MODEL`.

Every `QG_LLM_*` var also accepts a role suffix — `_HARNESS`, `_REVIEW`,
`_SECURITY` or `_QA` — to configure that graph role independently (e.g.
`QG_LLM_MODEL_QA`). The base block is the harness config and the shared
fallback, so a local Ollama can drive the harness while specialists run
on OpenRouter `:free` models (see `.env.example`).

Prompt-injection hardening (FR-O25) is always on: `analyze_diff` flags
added lines resembling LLM directives or chat-template role markers as
`prompt_injection` findings (warning; info in test/docs/pattern-
definition contexts), every evidence block sent to the models is
wrapped in explicit untrusted-data sentinels, and LLM findings can only
reference files actually changed in the PR.

## Service mode (webhook)

The stack can also run as a service instead of per-invocation: nginx
exposes ONE public port, the ingress verifies and queues GitHub
`pull_request` webhooks, each accepted event clones the PR head into an
ephemeral worktree, and the graph posts the verdict on the PR (metrics
land in `run_metrics` / `llm_call_metrics`).

```bash
GITHUB_TOKEN=ghp_... \
GITHUB_WEBHOOK_SECRET=<repo webhook secret> \
QG_ALLOWED_REPOS=your-org/your-repo \
QG_INGRESS_PORT=8089 \
docker compose up -d ingress nginx

# Production TLS (FR-P10): caddy terminates HTTPS on :443 and proxies to
# the loopback nginx — set QG_PUBLIC_HOST to the public hostname and
# bring the stack up with the tls profile:
QG_PUBLIC_HOST=qg.example.com \
docker compose --profile tls up -d
# Webhook URL becomes https://qg.example.com/github/webhook.

# LLM: the ingress defaults to a HOST ollama (http://localhost:11434,
# reached via host.docker.internal) so the model gets full machine
# resources — just `ollama serve` + `ollama pull qwen3:8b` on the host.
# Prompts >4k hit ollama's num_ctx=4096 default — see the ctx-tuning
# note in docs/local-webhook-setup.md for the one-command fix.
# Only if the host has no ollama, fall back to the container:
docker compose --profile llm up -d ollama
docker compose exec ollama ollama pull qwen3:8b
QG_LLM_BASE_URL=http://ollama:11434/v1 docker compose up -d ingress
```

For a **local webhook test** (no compose, no public IP) bring the
ingress up on the host and publish it through a tunnel:

```bash
set -a && source .env && set +a       # GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET,
                                      # QG_ALLOWED_REPOS, QG_INGRESS_PORT=8089
uv run qg-ingress                     # http://127.0.0.1:8089
curl http://127.0.0.1:8089/healthz    # -> {"status":"ok"}

# zero-install SSH tunnel -> https://<id>.lhr.life (valid TLS cert)
ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 \
    -R 80:localhost:8089 nokey@localhost.run
# alternatives: cloudflared tunnel --url http://localhost:8089
#             ngrok http 8089
```

Then register `https://<id>.lhr.life/github/webhook` in the repo
(Settings → Webhooks): content type `application/json`, secret =
`GITHUB_WEBHOOK_SECRET`, events: Pull requests. Verify with
`curl https://<id>.lhr.life/healthz` and check GitHub's *Recent
deliveries* for `202` (accepted) / `401` (bad signature) / `403` (repo
not allowed). See [docs/local-webhook-setup.md](docs/local-webhook-setup.md)
for the full bring-up, all exposure options, caveats and teardown.

Public-exposure controls (FR-O19..O22/P9/P10, INV-6): caddy's TLS
endpoint is the only public surface in production (nginx, postgres and
the ingress backend all bind loopback or the internal network); nginx
applies 10 r/s per IP and a 1 MB body cap on the webhook path; every
payload is HMAC-verified before parsing; `QG_ALLOWED_REPOS` is
deny-by-default; the token reaches git only via env headers; each
worktree is removed after its run plus a startup sweep. The
`mcp-server` SSE service is behind a `debug` profile — its
`spawn_process` tool is unauthenticated command execution and must
never be public. All service-mode containers restart automatically
(`unless-stopped`).

## CI

- `mcp-ci.yml` / `orchestrator-ci.yml` — `paths:`-filtered unit tests.
- `pr-review.yml` — on `pull_request`, spins a Postgres service, runs the
  graph, and fails if any spawned validator/scanner process survives.

## Splitting the monorepo later

`git subtree split --prefix=packages/mcp-server` (or `git filter-repo`)
extracts either package with history intact; the packages only couple via
the `quality-gate-mcp` dependency declared in the orchestrator's pyproject.

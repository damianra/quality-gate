# quality-gate

Autonomous PR review for Expo/React Native repos. Replaces manual review:
on every PR the system profiles the diff, detects *vibecoding* (whitespace
noise, dead-comment blocks, TODOs, debug leftovers, secrets, dependency
churn, scope drift), runs structural validation (`tsc --noEmit` /
`node --check`), and posts a contractual verdict comment on GitHub.

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

Dry-run locally without the stack (no comment posted, in-memory checkpointer):

```bash
REPO_PATH=/path/to/checkout PR_NUMBER=123 GITHUB_TOKEN=ghp_... \
  uv run --package pr-review-orchestrator pr-review --dry-run
```

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

Or attach to the SSE endpoint started by compose:
`http://localhost:8000/sse`.

## Verdict contract

The published comment is always:

```markdown
## Validación funcional

**Veredicto:** ✅ Aprobado | ❌ Cambios requeridos

- ✅/❌ **check** — detail

**Result:** <summary>
**Note:** <note>

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

Every run reports a `metrics` block (CLI JSON + `**Métricas:**` line in
the PR verdict): per-node durations, LLM calls with prompt/completion
tokens, and spawned-process timings — so you can see where time and
tokens go.

### LLM provider (optional)

The review is deterministic by default. Setting `QG_LLM_PROVIDER` enables
LLM-backed enrichment in the orchestrator; keys stay in env vars only
(`QG_LLM_API_KEY` or the provider's canonical variable).

| `QG_LLM_PROVIDER` | Key env (fallback) | Default base URL |
|---|---|---|
| `ollama` | — (no key) | `http://localhost:11434/v1` |
| `openai` | `OPENAI_API_KEY` | `https://api.openai.com/v1` |
| `anthropic` | `ANTHROPIC_API_KEY` | `https://api.anthropic.com` |
| `nvidia` | `NVIDIA_API_KEY` | `https://integrate.api.nvidia.com/v1` |
| `openrouter` | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` |
| `custom` | `QG_LLM_API_KEY` | requires `QG_LLM_BASE_URL` |
| `devin-cli` | — (`devin auth` session) | local subprocess, no URL |

Each provider ships a default model; override with `QG_LLM_MODEL`.

Every `QG_LLM_*` var also accepts a role suffix — `_HARNESS`, `_REVIEW`,
`_SECURITY` or `_QA` — to configure that graph role independently (e.g.
`QG_LLM_MODEL_QA`). The base block is the harness config and the shared
fallback, so a local Ollama can drive the harness while specialists run
on OpenRouter `:free` models (see `.env.example`).

## CI

- `mcp-ci.yml` / `orchestrator-ci.yml` — `paths:`-filtered unit tests.
- `pr-review.yml` — on `pull_request`, spins a Postgres service, runs the
  graph, and fails if any `tsc`/`node --check`/`rg` process survives.

## Splitting the monorepo later

`git subtree split --prefix=packages/mcp-server` (or `git filter-repo`)
extracts either package with history intact; the packages only couple via
the `quality-gate-mcp` dependency declared in the orchestrator's pyproject.

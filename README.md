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

- `GITHUB_TOKEN` — env-only, never logged/embedded (required)
- `DATABASE_URL` — Postgres checkpointer DSN (optional)
- `REPO_PATH`, `PR_NUMBER`, `DRY_RUN` — orchestrator inputs
- `QG_*` — timeouts and scanner thresholds (see `config.py` files)

## CI

- `mcp-ci.yml` / `orchestrator-ci.yml` — `paths:`-filtered unit tests.
- `pr-review.yml` — on `pull_request`, spins a Postgres service, runs the
  graph, and fails if any `tsc`/`node --check`/`rg` process survives.

## Splitting the monorepo later

`git subtree split --prefix=packages/mcp-server` (or `git filter-repo`)
extracts either package with history intact; the packages only couple via
the `quality-gate-mcp` dependency declared in the orchestrator's pyproject.

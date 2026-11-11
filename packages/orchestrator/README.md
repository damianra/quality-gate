# pr-review-orchestrator

Stateful [LangGraph](https://langchain-ai.github.io/langgraph/) orchestrator
that reviews a pull request end-to-end and guarantees that **no spawned
process outlives the run**.

## Graph

```text
START -> router -> fan-out {review, security, qa} -> synthesizer -> cleanup -> END
```

- **router** — calls `analyze_pr_signals` + `get_pr_diff`, then selects
  specialists (`review` always; `security` on secrets/deps/config churn;
  `qa` when code files changed — `QG_QA_CODE_EXTS`).
- **review / security / qa** — specialists emitting structured `findings`.
  `qa` spawns the best available validator for each ecosystem
  (`tsc --noEmit`, `node --check`, `compileall`, `go vet`, `cargo check`)
  through `spawn_process` and registers every `pgid` in `active_pgids`.
- **synthesizer** — merges findings into the verdict and posts the
  contractual comment via `post_verdict` (skipped on `--dry-run`).
- **cleanup** — mandatory terminal node; `SIGTERM` then `SIGKILL` on every
  pgid in `active_pgids`. `run_pr_review` additionally wraps `invoke` in a
  `finally` that re-reads the checkpointed state and kills any pgid left
  behind by a failed/cancelled run.

## Resumability

When `DATABASE_URL` is set, the graph is compiled with
`langgraph-checkpoint-postgres` (thread id = `pr-review/<repo>/<pr>`), so a
re-run after new commits resumes cleanly. Without it, an in-memory
checkpointer is used.

## Run

```bash
PR_NUMBER=123 REPO_PATH=/path/to/checkout GITHUB_TOKEN=... \
    uv run --package pr-review-orchestrator pr-review --dry-run
```

Exit code is `0` when the PR is approved, `1` when rejected (usable as a
red/green check in CI).

## Env vars

- `GITHUB_TOKEN` (required, env only — never logged)
- `DATABASE_URL` (optional; Postgres checkpointer)
- `PR_NUMBER`, `REPO_PATH`, `DRY_RUN`
- `QG_QA_PROCESS_TIMEOUT`, `QG_CLEANUP_GRACE_SECONDS`
- `QG_LLM_PROVIDER` — optional: `ollama`, `openai`, `anthropic`, `nvidia`,
  `openrouter`, `custom` or `devin-cli` (see `llm.py` for per-provider
  defaults). `devin-cli` runs the local `devin` agent as a tracked
  subprocess — needs no API key, uses your Devin account auth
- `QG_LLM_API_KEY` / canonical vars (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
  `NVIDIA_API_KEY`, `OPENROUTER_API_KEY`), `QG_LLM_MODEL`,
  `QG_LLM_BASE_URL`, `QG_LLM_TEMPERATURE`, `QG_LLM_TIMEOUT`,
  `QG_LLM_MAX_TOKENS`
- Role scoping: append `_HARNESS`, `_REVIEW`, `_SECURITY` or `_QA` to any
  `QG_LLM_*` var to give that graph role its own provider, model, key and
  generation settings. Unset role vars inherit the base block, which is
  also the harness config (see `.env.example`).

## Tests

```bash
uv run pytest packages/orchestrator
```

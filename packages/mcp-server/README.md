# quality-gate-mcp

Stateless [MCP](https://modelcontextprotocol.io) server exposing
deterministic development tools for autonomous PR review.

## Tools

- **`list_prs(repo_path)`** — open PRs marked `pending`/`reviewed` via the
  `<!-- pr-review head:<sha7> -->` comment marker.
- **`get_pr_diff(repo_path, pr_number, page)`** — PR stats + paginated file
  list (no inline patches).
- **`get_file_diff(repo_path, pr_number, file_path)`** — unified patch of a
  single file.
- **`analyze_pr_signals(repo_path, pr_number)`** — deterministic vibecoding
  scanner: `whitespace_ratio`, `large_diff`, `config_churn`,
  `new_dependencies`, `debug_leftovers`, `todos_added`, `commented_blocks`,
  `secret_candidates`, `files_outside_hint`.
- **`search_code(repo_path, query, limit)`** — ripgrep over the checkout.
- **`spawn_process(repo_path, command, args, timeout)`** — runs a command in
  a **new process group** (`start_new_session=True`) and returns its `pgid`.
  Register the pgid in graph state so cleanup can `killpg` it.
- **`post_verdict(repo_path, pr_number, approve, checks, result, note)`** —
  posts/updates the contractual `## Validación funcional` comment.
  Idempotent via the marker.
- **`create_pr(repo_path, head, title, body, base)`** — opens a PR.

## Run

```bash
uv run --package quality-gate-mcp quality-gate-mcp                 # stdio
uv run --package quality-gate-mcp python -m quality_gate_mcp.server \
    --transport sse --port 8000                                    # SSE
```

## Env vars

- `GITHUB_TOKEN` (required for GitHub API tools)
- `GITHUB_API_URL` (default `https://api.github.com`)
- `QG_PROCESS_TIMEOUT`, `QG_GIT_TIMEOUT`, `QG_RG_TIMEOUT`
- `QG_LARGE_DIFF_FILES`, `QG_LARGE_DIFF_LINES`, `QG_WHITESPACE_RATIO_THRESHOLD`

## Guarantees

- No state, no database.
- `shell=True` is never used; all subprocesses are argv lists.
- Spawned processes live in their own process group → `os.killpg` works.

## Tests

```bash
uv run pytest packages/mcp-server
```

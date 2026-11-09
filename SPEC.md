# quality-gate — System Specification

**Status:** seed document — committed at repository bootstrap, documenting
the implemented baseline; updated forward from here (one commit per spec
change).
**Purpose:** spec-first document for spec-driven development (SDD). Every new
feature MUST be specified here (or in a linked spec) before implementation;
requirements carry stable IDs (`FR-*`, `NFR-*`, `INV-*`) that tests and PRs
reference.

## 1. Overview

`quality-gate` is an autonomous pull-request review system, originally
targeted at Expo/React Native repos. On every PR it profiles the diff,
detects *vibecoding* (whitespace noise, dead commented blocks, TODOs,
debug leftovers, hardcoded secrets, dependency churn, scope drift), runs
structural validation (`tsc --noEmit` / `node --check`), and posts a
contractual verdict comment on GitHub.

The system replaces manual review and enforces one non-negotiable
invariant: **no spawned process may outlive a review run**.

## 2. Architecture

### 2.1 Repository layout

```text
packages/mcp-server     quality-gate-mcp      — stateless FastMCP server
packages/orchestrator   pr-review-orchestrator — stateful LangGraph brain
.github/workflows       path-filtered CI + pr-review trigger
.devin/workflows        local sub-agent workflows (/pr-review)
compose.yaml            postgres + mcp-server + orchestrator stack
```

`uv` workspace monorepo, Python >= 3.11, hatchling builds. The
orchestrator depends on the MCP package as a library
(`quality-gate-mcp` workspace source); the MCP server is also usable
standalone over stdio or SSE.

### 2.2 Runtime data flow

```text
GitHub PR event ──> pr-review.yml ──> orchestrator
                                       │
                              router ──┤  analyze_pr_signals, get_pr_diff
                                       │  (via quality_gate_mcp)
                                       ├─ fan-out ─> review | security | qa
                                       │              └─ qa spawns tsc/node
                                       │                 (pgid -> active_pgids)
                                       ├─ synthesizer ─> post_verdict
                                       └─ cleanup ─> killpg(active_pgids)
```

- **Postgres** is used only by `langgraph-checkpoint-postgres` so a run
  can resume when a PR gains commits (`thread_id = pr-review/<repo>/<pr>`).
- Without `DATABASE_URL`, an `InMemorySaver` is used (no resumability).
- The MCP server is fully stateless: no persistence, everything is
  re-derived per call.

## 3. Non-negotiable invariants

- **INV-1 Process safety.** `shell=True` is forbidden; every subprocess is
  an argv list. Long-running commands spawn in a new process group
  (`start_new_session=True`) and their `pgid` is registered in
  `active_pgids` so cleanup can `os.killpg` it.
- **INV-2 Guaranteed cleanup.** `cleanup` is the only node reaching `END`.
  `run_pr_review` additionally wraps `graph.invoke` in `try/finally` that
  re-reads the checkpointed state and kills any pgid a failed/cancelled
  run left behind. The GitHub Action asserts no orphans remain (`pgrep`
  on `tsc --noEmit|node --check|rg --line-number`).
- **INV-3 Secrets env-only.** `GITHUB_TOKEN`, `DATABASE_URL` and all LLM
  API keys come from environment variables only — never in code, logs,
  errors, commit messages or CLI arguments.
- **INV-4 Verdict contract.** The published PR comment format is fixed
  (§6.1) and idempotent via the `<!-- pr-review head:<sha7> -->` marker.
- **INV-5 Language policy.** Code, comments, docs and commits in English;
  user-facing text (verdict comment, chat) in Spanish. `README.md` and
  `README_ES.md` are mirrors updated in the same PR.

## 4. Functional requirements

### 4.1 `quality-gate-mcp` (MCP tools server)

Each tool is a pure Python function in `tools.py`, registered explicitly
in `server.py` (registration kept separate so `tools.py` stays importable
and unit-testable without the MCP runtime).

- **FR-M1 `list_prs(repo_path)`** — list open PRs of the repo resolved
  from its `origin` remote; each entry carries `status: pending|reviewed`
  computed by marker match against the PR head sha.
- **FR-M2 `get_pr_diff(repo_path, pr_number, page, per_page=30)`** — PR
  stats (`title`, `head_sha`, `base_ref`, additions/deletions/changed_files)
  plus a paginated file list without inline patches.
- **FR-M3 `get_file_diff(repo_path, pr_number, file_path)`** — unified
  `patch` of one file (`None` when GitHub truncates), `status`,
  additions/deletions.
- **FR-M4 `analyze_pr_signals(repo_path, pr_number)`** — full unified diff
  fetched once, then `analyze_diff` produces the signals dict (§6.2) plus
  `diff_text` for downstream context building.
- **FR-M5 `search_code(repo_path, query, limit=50)`** — ripgrep over the
  checkout; returns `[{file, line, text}]`; empty on timeout/error.
- **FR-M6 `spawn_process(repo_path, command, args, timeout)`** — spawn in
  a new process group; always return `pgid` (≥0 even on spawn failure),
  `exit_code`, `stdout`, `stderr`, `timed_out`, `command`, `duration_s`.
  On timeout the group is SIGTERM'd then SIGKILL'd before returning
  (`QG_KILL_GRACE_SECONDS` grace).
- **FR-M7 `post_verdict(...)`** — build the contractual comment and
  create it, or update in place the latest comment containing any
  pr-review marker. Returns `{action: created|updated, url, head_sha}`.
- **FR-M8 `create_pr(repo_path, head, title, body, base)`** — open a PR;
  on HTTP 422 return the existing PR for that head instead of failing.
- **FR-M9 Transports** — `stdio` by default; `--transport sse --host --port`
  serves HTTP (used by compose on `:8000/sse`).

### 4.2 `pr-review-orchestrator` (LangGraph graph)

Topology: `START -> router -> fan-out {review, security, qa} ->
synthesizer -> cleanup -> END`. Every node is wrapped by `_timed` so its
duration lands in `node_durations`.

- **FR-O1 Router** — calls `analyze_pr_signals` + paginated `get_pr_diff`
  (up to `QG_MAX_FILES_PAGES` × 30 files), builds `pr_context` via
  `scope.build_pr_context`, selects specialists via `choose_agents`.
- **FR-O2 Specialist selection** — `review` always; `security` when
  `secret_candidates | new_dependencies | config_churn` is non-empty;
  `qa` when any changed file ends in `.ts .tsx .js .jsx .mjs .cjs`.
- **FR-O3 ReviewAgent** — findings for: missing `AGENTS.md` (info),
  whitespace-dominated diff (warning ≥0.6/>20 lines; info ≥0.3/>50),
  oversized PR (warning), commented-out blocks (warning), added TODOs
  (info), files outside PR-body scope hints (warning), debug leftovers
  (warning).
- **FR-O4 SecurityAgent** — `secret_candidates` are blockers; new
  `package.json` dependencies and sensitive-config churn are warnings.
- **FR-O5 QAAgent** — prefer `node_modules/.bin/tsc --noEmit` when a
  `tsconfig.json` exists; otherwise `node --check` on up to 20 changed JS
  files. Every spawn registers its pgid in `active_pgids`. Non-zero exit
  is a blocker, timeout is a warning, exit 127 (`command not found`) is
  info and stops the loop. All runs pass `uv run pytest packages -q`.
- **FR-O6 Specialist isolation** — a specialist exception never kills the
  run; nodes catch it and append to `errors`.
- **FR-O7 Synthesizer** — any blocker finding or internal error forces
  rejection; maps findings to the checks list, builds the summary and
  metrics report, calls `post_verdict` (skipped on `dry_run`).
- **FR-O8 Cleanup node** — `cleanup.terminate_pgids` SIGTERMs every
  registered pgid, escalates to SIGKILL after `QG_CLEANUP_GRACE_SECONDS`,
  reports `{terminated, killed, already_dead, failed}`; safe on empty/
  duplicate/dead pgids.
- **FR-O9 Runner** — `run_pr_review(repo_path, pr_number, dry_run)`
  initializes state, chooses `PostgresSaver` when `DATABASE_URL` is set
  else `InMemorySaver`, uses `thread_id = pr-review/<repo>/<pr>`, and
  guarantees pgid termination via the `finally` safety net.
- **FR-O10 CLI** — `pr-review` entry point (`python -m pr_review_orch`):
  `--repo-path/--pr-number/--dry-run` with `REPO_PATH/PR_NUMBER/DRY_RUN`
  env fallbacks; prints JSON `{approve, summary, posted, cleanup_report,
  metrics}`; exit `0` approved, `1` rejected.
- **FR-O11 Metrics** — every run produces a report: `total_s`, per-node
  durations (sorted desc), LLM call count + prompt/completion/total
  tokens + detail, process timings. `summary_line` is embedded as the
  `**Métricas:**` line in the verdict comment.
- **FR-O12 Blast-radius scoping** — `scope.build_pr_context` returns the
  truncated diff (≤ half the char budget), full contents of changed
  files, and direct dependents discovered by import-spec matching in
  pure Python (JS/TS quoted specs + Python imports; skips `node_modules`,
  `dist`, `build`, `__pycache__`, `.venv`, `coverage`, `.next`, `.expo`;
  ≤ 5000 files scanned). Hard caps: `QG_SCOPE_MAX_FILES`,
  `QG_SCOPE_MAX_CHARS`; `truncated` flag reports when caps kicked in.
- **FR-O13 LLM provider (optional)** — `llm.py` resolves
  `QG_LLM_PROVIDER` ∈ {ollama, openai, anthropic, nvidia, openrouter,
  custom}; OpenAI-compatible providers share one code path, Anthropic
  uses Messages API. `QG_LLM_API_KEY` wins over canonical env vars;
  missing key/`QG_LLM_BASE_URL`/`QG_LLM_MODEL` raise `LLMConfigError`.
  `complete_with_meta` returns `{text, duration_s, usage, finish_reason,
  truncated}` with usage normalized to prompt/completion tokens. Unset
  provider = fully deterministic review, no LLM calls.
- **FR-O14 LLM completion contract** — `truncated` is `True` when the
  provider stopped on a token cap: OpenAI-compatible
  `finish_reason` ∈ {`length`, `max_tokens`}, Anthropic
  `stop_reason == "max_tokens"`. A truncated or failed completion MUST
  NOT silently feed downstream consumers: they degrade to deterministic
  findings, note "LLM enrichment unavailable", and record the attempt
  in `llm_calls`. `QG_LLM_MAX_TOKENS` MUST cover the expected response
  shape — free-tier models with mandatory reasoning burn the budget on
  chain-of-thought and can return empty `text` with `truncated=True`.
- **FR-O15 Role-scoped LLM config** — every `QG_LLM_*` setting accepts a
  role suffix `QG_LLM_<SETTING>_<ROLE>` with ROLE ∈ {HARNESS, REVIEW,
  SECURITY, QA} (case-insensitive). `LLMConfig.for_role(role)` resolves
  role vars first, then the base `QG_LLM_*` block (which doubles as the
  HARNESS config and the shared fallback), then provider defaults.
  Empty suffixed vars are ignored — unset means "inherit". Each role
  gets an independent provider/model/key/base_url/temperature/timeout/
  max_tokens, e.g. harness on local Ollama while specialists use
  OpenRouter `:free` models. Unknown roles raise `LLMConfigError`.
- **FR-O16 Specialist LLM wiring** — the harness node uses its role
  config to build each specialist's payload from `pr_context` (falling
  back to raw `pr_context` on failure); specialist nodes call
  `complete_with_meta` with their own role config, merge the structured
  response into `findings`, and record `{role, provider, model,
  duration_s, usage, truncated}` in `llm_calls`. Calls run concurrently
  inside the existing parallel fan-out. On provider error,
  `truncated=True`, or missing credentials the node MUST degrade to
  deterministic findings with an "LLM enrichment unavailable" note —
  LLM failure alone never rejects a PR.

## 5. Non-functional requirements

- **NFR-1 Determinism.** The review is reproducible: same inputs → same
  findings, no randomness, no hidden state.
- **NFR-2 Statelessness (MCP).** The MCP server persists nothing; all
  state lives in orchestrator graph state.
- **NFR-3 Resumability.** Postgres checkpoints let a re-run resume on new
  commits; a run without `DATABASE_URL` still works (in-memory).
- **NFR-4 Idempotency.** Re-runs on the same head update the existing
  marked comment; no duplicate verdict comments.
- **NFR-5 Bounded context.** LLM/sub-agent consumption is capped by
  `QG_SCOPE_MAX_FILES` / `QG_SCOPE_MAX_CHARS`; the whole repo is never
  sent as context.
- **NFR-6 Fault tolerance.** Specialist failures degrade to `errors`;
  the verdict still posts (rejected), cleanup still runs.
- **NFR-7 Package independence.** Packages version independently;
  coupling is only the `quality-gate-mcp` dependency — either package can
  be split out with `git subtree split`.
- **NFR-8 Observability.** Per-node timings, LLM usage and process
  timings are reported in CLI JSON and the PR comment.

## 6. Contracts

### 6.1 Verdict comment (user-facing, Spanish — non-negotiable)

```markdown
## Validación funcional

**Veredicto:** ✅ Aprobado | ❌ Cambios requeridos

- ✅/❌ **check** — detail

**Result:** <summary>
**Note:** <note>

**Métricas:** <total_s; slowest node; LLM calls/tokens; processes>

<!-- pr-review head:<sha7> -->
```

- Marker regex: `<!--\s*pr-review head:(?P<sha>[0-9a-fA-F]{7,40})\s*-->`.
- A PR is `pending` until a marker matching its `head:<sha7>` exists.

### 6.2 Signals schema (`analyze_diff` output)

```text
files, per_file{added,removed}, files_changed, lines_changed,
whitespace_ratio, large_diff, config_churn[], new_dependencies[],
debug_leftovers[], todos_added[], commented_blocks[{file,lines,sample}],
secret_candidates[], files_outside_hint[], scope_hints[]
```

### 6.3 Finding schema

```text
{agent: "review|security|qa", severity: "info|warning|blocker",
 title: str, detail: str, file: str | None}
```

### 6.4 Graph state (`PRReviewState`, TypedDict total=False)

Reducer-merged lists (fan-out safe via `operator.add`): `findings`,
`active_pgids`, `errors`, `node_durations`, `llm_calls`, `processes`.
Set by router: `pr_title, pr_body, head_sha, base_ref, files, signals,
pr_context, agents_to_run`. Set by synthesizer: `verdict, posted,
metrics`. Set by cleanup: `cleanup_report`.

### 6.5 GitHub API client

Minimal `httpx` wrapper: `list_pulls`, `get_pr`, `list_pr_files`
(paginated), `get_pr_diff_text`, `create_pull` (422-tolerant),
`list_issue_comments`, `create_issue_comment`, `update_issue_comment`.
Bearer auth, `X-GitHub-Api-Version: 2022-11-28`, `GITHUB_API_URL`
override for GH Enterprise.

## 7. Configuration surface

All via env vars; `.env.example` is the canonical template.

| Group | Vars |
|---|---|
| Required | `GITHUB_TOKEN`, `PR_NUMBER` (runtime), `REPO_UNDER_TEST` (compose) |
| Orchestrator | `DATABASE_URL`, `REPO_PATH`, `DRY_RUN`, `QG_CLEANUP_GRACE_SECONDS`, `QG_QA_PROCESS_TIMEOUT`, `QG_MAX_FILES_PAGES`, `QG_SCOPE_MAX_FILES`, `QG_SCOPE_MAX_CHARS` |
| MCP server | `GITHUB_API_URL`, `QG_PROCESS_TIMEOUT`, `QG_GIT_TIMEOUT`, `QG_RG_TIMEOUT`, `QG_HTTP_TIMEOUT`, `QG_KILL_GRACE_SECONDS`, `QG_LARGE_DIFF_FILES`, `QG_LARGE_DIFF_LINES`, `QG_COMMENTED_BLOCK_MIN`, `QG_WHITESPACE_RATIO_THRESHOLD`, `QG_LOG_LEVEL` |
| LLM (optional) | `QG_LLM_PROVIDER`, `QG_LLM_MODEL`, `QG_LLM_API_KEY`, `QG_LLM_BASE_URL`, `QG_LLM_TEMPERATURE`, `QG_LLM_TIMEOUT`, `QG_LLM_MAX_TOKENS` (+ canonical `*_API_KEY` fallbacks) |

## 8. Delivery surfaces

- **CI — `mcp-ci.yml` / `orchestrator-ci.yml`:** `paths:`-filtered unit
  tests (`uv sync --all-packages` → `uv run pytest packages/<pkg> -v`);
  orchestrator CI also triggers on mcp-server changes.
- **CI — `pr-review.yml`:** on `pull_request` (opened/synchronize/reopened),
  Postgres service, `concurrency.cancel-in-progress: false` (cleanup must
  complete), signal-scan summary step, orchestrator run, and a final
  `pgrep` orphan assertion (`if: always()`).
- **Images:** OCI Dockerfiles per package, built from repo root, uv-based
  `uv sync --no-dev --package <pkg>`, non-root `appuser` (uid 10001).
- **Compose:** `postgres` (healthchecked), `mcp-server` (SSE :8000),
  `orchestrator` (depends on postgres, mounts `REPO_UNDER_TEST` → `/work`
  read-write for `tsc` caches).

## 9. Testing requirements

- **NFR/T1** `uv run pytest packages -q` must pass; pure helpers
  (`analyze_diff`, `build_verdict_comment`, `parse_remote`, scope scan)
  are unit-tested without network/git.
- **NFR/T2** Existing suites: `test_signals`, `test_verdict`,
  `test_remote_parse`, `test_spawn` (mcp); `test_agents`, `test_cleanup`,
  `test_llm`, `test_metrics`, `test_scope` (orchestrator).
- **NFR/T3** New code paths that spawn processes MUST add a cleanup
  assertion; behavior changes MUST ship tests.

## 10. Implementation status (commit map)

Linear history on `main` — no merge commits. Bootstrap order (hashes are
volatile during early history edits; commits are identified by subject):

| # | Commit subject |
|---|---|
| 1 | `chore`: uv workspace scaffold for the monorepo (+ this spec seed) |
| 2 | `feat(mcp-server)`: deterministic FastMCP tools |
| 3 | `feat(orchestrator)`: LangGraph graph + mandatory cleanup |
| 4 | `test`: scanner, verdict format, remotes, cleanup unit tests |
| 5 | `build`: OCI images + compose stack |
| 6 | `ci`: path-filtered CI + pr-review workflow |
| 7 | `docs`: bilingual READMEs |
| 8 | `docs`: `.env.example` template |
| 9 | `docs`: AGENTS.md + `/pr-review` sub-agent workflow |
| 10 | `revert` of a docs change (churn, resolved in place) |
| 11 | `ci`: vibecoding signal scan in pr-review workflow |
| 12 | `docs`: conventional-commits rule in AGENTS.md |
| 13 | `docs`: mandatory sub-agent delegation in `/pr-review` |
| 14 | `feat(orchestrator)`: provider-agnostic LLM config |
| 15 | `feat(orchestrator)`: run metrics + blast-radius scoping |
| 16 | `docs`: FR-O14 LLM completion contract + truncation handling |
| 17 | `feat(orchestrator)`: `truncated`/`finish_reason` in `complete_with_meta` |
| 18 | `docs`: FR-O15 role-scoped LLM config + FR-O16 wiring contract |

## 11. Extension points (SDD backlog)

Planned hooks already shaped by the codebase — spec them here before
implementing:

- **LLM-enriched specialists** — role-scoped config (FR-O15),
  truncation flag (FR-O14) and `pr_context` are in place; specialists
  currently run deterministic-only. Remaining per FR-O16: prompt
  contracts, node wiring, `llm_calls` recording.
- **Sub-agent delegation** — `/pr-review` workflow delegates to a
  fresh-context sub-agent locally; the same pattern is intended for
  specialist fan-out on larger PRs.
- **Additional validators** — QAAgent's `_qa_commands` is the seam for
  eslint/tsc strictness/tests runners beyond `node --check`.
- **New signals** — `analyze_diff` is the single pure-function seam for
  new vibecoding detectors; thresholds belong in `config.py` as `QG_*`.
- **PR authoring flow** — `create_pr` exists; an "auto-fix PR" agent is
  not yet specced.
- **Monorepo split** — `git subtree split --prefix=packages/<pkg>` is the
  documented exit path.

## 12. Contribution conventions (normative)

See `AGENTS.md` (authoritative): Conventional Commits in English
(`<type>(<scope>): <imperative>`), bilingual root READMEs mirrored in the
same PR, English code/docs + Spanish user-facing text, no `shell=True`,
env-only secrets, green `uv run pytest packages -q`, no vibecoding
leftovers.

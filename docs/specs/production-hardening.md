# Spec — production hardening (service mode)

**Status:** implemented — FR-P1..P19 via PRs #35–#52, steady-state
follow-ups FR-P20 #55 · FR-P22 #56 · FR-P21 #57. Phase-3 deployment
readiness implemented: FR-P23 #63 · FR-P24 #64 · FR-P25 #65 ·
FR-P26 #66 · FR-P27 #67 · FR-P28 #68 · FR-P29 #69 · FR-P30 #70.
Phase-4 local-host readiness spec'd from a rootless-podman host audit
(2026-11-13): FR-P31..P35 open.
**Scope:** amends SPEC.md
FR-O19..O22 (service ingress/queue), FR-O5/FR-L2 (QA validators),
FR-O9 (runner signature), §7 (configuration surface), §8 (delivery
surfaces) and §9 (testing). Requirement IDs `FR-P*`/`NFR-P*` are
stable; tests and PRs reference them.

## 1. Motivation

A full-repository audit found the review pipeline functionally complete
but not operable as a company service. The gaps are plumbing, not
intelligence:

- **The verdict cannot gate a merge.** `post_verdict` only creates or
  updates a comment (`tools.py`); nothing posts a commit status or check
  result, so a "Changes required" verdict does not block the merge button
  under branch protection. The system's stated purpose (SPEC.md §1
  "enforces") is unmet.
- **Reviews can be lost silently.** The run queue lives in process
  memory (`ingress.py` `_QUEUE`/`_PENDING`), worker exceptions are
  swallowed into a log line, and `persist_run` only writes on success —
  a crashed or failed run leaves the PR `pending` forever, invisible to
  GitHub (which does not retry a `202`) and to the team.
- **Resumability is broken in service mode.** `_thread_id` derives the
  slug from the `repo_path` basename, which is the ephemeral
  `qg-<random>` worktree — every run of the same PR opens a new
  checkpoint thread, and `run_metrics.repo` stores a temp path instead
  of `owner/repo`.
- **The deploy surface is unsafe.** compose publishes the MCP server
  (whose `spawn_process` tool executes arbitrary commands) on `:8000`
  with no authentication, publishes Postgres on `:5432` with default
  credentials, terminates no TLS, and sets `restart: "no"` on every
  service. Validators that execute PR-controlled code (`cargo check`
  runs `build.rs`/proc-macros) inherit the worker's full environment —
  including `GITHUB_TOKEN` and `DATABASE_URL`.
- **The team cannot answer "did the review run?"** Logs are
  unstructured, `/healthz` is liveness-only, failed runs leave no row
  and no comment, and there is no backup, alerting or deploy runbook.

This spec converts the service mode into something a 10–30 developer
team can run on a single VM without GPU. It explicitly does **not**
scale the system: no message broker, no autoscaling, no multi-region.
Every fix is sized for tens of PRs/day.

## 2. Requirements

### Run correctness and delivery

- **FR-P1 Explicit repository identity.** `run_pr_review` gains a
  `repo_slug` parameter (fallback: `origin` remote slug for the
  CLI/Action path). `thread_id` becomes `pr-review/{repo_slug}/{pr}`
  regardless of the worktree directory name; `run_metrics.repo` stores
  `repo_slug`. The ingress passes `event["repo"]`. Restores NFR-3
  resumability in service mode and makes metrics aggregable per repo.

- **FR-P2 Failure is never silent.** Any accepted delivery whose run
  raises or times out MUST produce an externally visible outcome in the
  same run lifecycle:
  1. a commit status / check result of `error` on the head sha
     (FR-P8), and/or a "review failed" comment update on the PR; and
  2. a `run_metrics` row carrying `status` (`ok|error|timeout`) and the
     exception summary — `persist_run` runs in a `finally`, not only on
     the success path.
  GitHub deliveries acknowledged `202` are therefore never dropped
  without a trace.

- **FR-P3 Bounded retries on transient faults.** The GitHub REST client
  and the ephemeral clone retry on 429/5xx/network errors with
  exponential backoff (default 3 attempts, `QG_RETRY_ATTEMPTS`,
  `QG_RETRY_BACKOFF_S`), honoring `Retry-After` when present.
  Permanent failures (4xx, auth) do not retry. Retried calls stay
  inside the existing timeouts budget.

- **FR-P4 Durable run queue (minimal).** Accepted events are persisted
  before the `202` response — a `pending_runs` Postgres table
  (`delivery_id`, `repo`, `pr_number`, `head_sha`, `clone_url`,
  `status`, `attempts`, timestamps) replaces the in-memory queue as the
  source of truth; the in-process worker continues to dispatch and
  coalesce exactly as today, but on startup it re-enqueues rows left in
  `queued`/`running` (crashed) state. No broker; a crashed run resumes
  or is reported per FR-P2. Delivery dedupe (`X-GitHub-Delivery`) moves
  to a unique key on the table so it survives restarts.

- **FR-P5 Graceful shutdown.** Drain tasks are tracked (not fire-and-
  forget `create_task`); on shutdown the worker waits up to
  `QG_SHUTDOWN_GRACE_S` (default 120) for in-flight runs before
  cancelling, and a worktree is never `rmtree`d while its run is still
  executing in the `to_thread` worker.

- **FR-P6 Event filtering.** `_extract_event` drops `draft: true`
  pull_request payloads and accepts the `ready_for_review` action —
  drafts are skipped on open but reviewed when marked ready.

- **FR-P7 Orphan process sweep.** The startup sweep that removes stale
  `qg-*` worktrees also kills leftover validator/scanner processes from
  crashed runs (same signature set as the CI `pgrep` assertion) before
  the directories are removed — a `kill -9`'d worker must not leak
  `tsc`/`cargo`/`osv-scanner` children. Refined by FR-P20: signature
  matches are killed only when attributable to a `qg-*` worktree, so
  unrelated developer processes on the shared host are never hit.

### Enforcement

- **FR-P8 Commit status gating.** On every accepted delivery the
  service posts a commit status on the head sha — `pending` when the
  run starts, `success`/`failure`/`error` when it finishes — with
  context `quality-gate` (`QG_STATUS_CONTEXT` override). Repos then
  enforce the verdict via the standard branch-protection "require
  status checks" rule, making the review an actual gate. The
  `GITHUB_TOKEN` documentation gains `repo:status` (classic PAT) or
  *Commit statuses: write* (fine-grained); the verdict comment and its
  marker contract (INV-4) are unchanged and continue to carry the
  human-readable detail. A status post failure degrades to a comment
  and a log entry — it never fails the run.

### Exposure and sandboxing

- **FR-P9 Compose exposure hygiene.** In the service-mode stack:
  `mcp-server` moves behind a `debug` compose profile (never started
  with the service stack; its unauthenticated SSE `spawn_process`
  endpoint is a debugging surface only); `postgres` uses `expose`
  instead of publishing `5432`; every long-running service sets
  `restart: unless-stopped`. A `compose.prod.yaml` overlay (or
  documented profile) expresses the production set.

- **FR-P10 TLS termination.** The deployment ships a TLS front-end —
  a `caddy` compose service (automatic certificates, `deploy/Caddyfile`)
  or a documented reverse-proxy alternative — so the only public port is
  `443` proxying to nginx/ingress. Bare HTTP webhook URLs are a
  local-tunnel option only; production GitHub webhooks are HTTPS.

- **FR-P11 Subprocess environment scrubbing.** `spawn_tracked` accepts
  an explicit env allowlist; validator/audit/LLM-CLI spawns receive a
  minimal environment (`PATH`, `HOME`, `LANG`, tool-required vars).
  `GITHUB_TOKEN`, `DATABASE_URL`, webhook secret and all `QG_LLM_*` keys
  MUST NOT appear in a spawned validator's environment. The git-auth
  env (`GIT_CONFIG_*` extraHeader) is scoped to git invocations only —
  never propagated to validator children. `/proc/<pid>/environ` of a
  spawned validator contains no secrets — this is a testable invariant.

- **FR-P12 Validator execution policy.** Validators split into
  *syntax-only* (`tsc --noEmit`, `node --check`, Python `compile()` —
  never execute PR code) and *executing* (`cargo check` runs
  `build.rs`/proc-macros; `go vet` may compile cgo). In service mode,
  executing validators are **disabled by default**
  (`QG_EXEC_VALIDATORS=0` default in service mode, `1` for CLI/Action
  runs where the operator chose the checkout); a disabled validator
  emits an info finding. The long-term fix — running executing
  validators in a no-egress sandbox (netns/firejail/gVisor) — is SDD
  backlog, not required here.

- **FR-P13 Production image contents.** The orchestrator image ships
  `osv-scanner` (version-pinned binary) so the FR-O24 audit actually
  runs in production; validator toolchains present in the image are a
  documented deployment requirement surfaced by a `qg-doctor`-style
  startup check that logs which validators/scanners resolved.

- **FR-P14 Supply-chain pinning.** Container base images and the `uv`
  installer are pinned by digest/version (`python:3.11-slim@sha256:…`,
  `uv:<ver>`), compose images pinned likewise
  (`postgres:16.x-alpine`, `nginx:1.27.x-alpine`, a tagged `ollama`
  release); Dependabot/Renovate covers docker + github-actions; CI gains
  a job that builds both Dockerfiles and scans the repo/images
  (`trivy`/`pip-audit` or `uv`-native audit), failing on criticals.

### Observability and operations

- **FR-P15 Structured logging with correlation.** All services log
  JSON; every webhook delivery gets a `run_id` (delivery id or
  `thread_id`) propagated through ingress → graph state → llm_calls →
  `run_metrics`, so "what happened to PR #N" is one `grep`/`psql`
  query. `ingress.main()` initializes logging (`QG_LOG_LEVEL`) —
  today event logs are dropped by the WARNING default.

- **FR-P16 Readiness and alerting.** `GET /readyz` reports Postgres
  reachability and queue depth (separate from liveness `/healthz`). A
  configurable dead-man ping (`QG_HEARTBEAT_URL`, e.g.
  healthchecks.io) is hit after every completed run — silence beyond
  the interval means the service is dead and the team gets paged; the
  run's `status` column (FR-P2) plus a documented query feeds a
  failed-run alarm.

- **FR-P17 Backups and restore.** A documented `pg_dump` cron/systemd
  timer ships (daily dump → object storage, 14–30 day retention) with a
  tested restore procedure in the ops runbook. Losing `pgdata` loses
  checkpoints + metrics history — acceptable but must be recoverable.

- **FR-P18 GitHub App authentication (recommended path).** The service
  supports `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY`/`GITHUB_APP_INSTALLATION_ID`
  in addition to the PAT: installation tokens (1 h TTL) minted at run
  time, least-privilege permissions (`contents:read`,
  `pull_requests:write`, `issues:write`, `statuses`/`checks:write`),
  comments authored as the bot, and per-repo onboarding reduced to
  "install the app" instead of hand-configuring webhooks + tokens. The
  PAT path stays supported for local runs.

### LLM deployment profiles

- **FR-P19 Supported LLM deployment profiles.** Three profiles are
  codified in config + docs, selected per role by the existing FR-O15
  machinery (no new code path):
  - `deterministic` — `QG_LLM_PROVIDER` unset; zero LLM calls; the
    supported baseline for hosts without GPU and for code that must not
    leave the perimeter.
  - `api` — role-scoped API providers (FR-O13/O15); the recommended
    profile on CPU-only hosts where code egress is allowed. Documented
    cost envelope (~10–11k input tokens per specialist call; ~$0.01–0.20
    per review depending on model tier).
  - `local` — ollama; supported only where the host has a GPU or where
    multi-minute per-call latency is acceptable. On CPU-only hosts this
    profile is **documented as not viable for interactive review**
    (8B-class reasoning models yield ~3–8 min/call → timeouts against
    `QG_LLM_TIMEOUT` and degraded findings).
  `QG_LLM_TIMEOUT`/`QG_LLM_MAX_TOKENS` defaults are stated per profile;
  the deployment default for a CPU-only box changes from `local` to
  `deterministic` or `api`.

### Steady-state hardening (follow-ups)

- **FR-P20 Self-owned orphan sweep.** The FR-P7 signature sweep only
  kills process groups the service could have spawned: a `pgrep` hit is
  SIGKILLed only when its leader's cwd resolves inside
  `QG_WORKTREE_ROOT` (the `qg-*` worktrees) or its argv references a
  `qg-*` path. A developer's own `cargo check`/`tsc` running elsewhere
  on the shared VM must never be killed — verified by a test that
  plants a decoy process outside the worktree root.
- **FR-P21 Failure-matrix end-to-end coverage.** A `tests/e2e/` suite
  (pytest `e2e` marker, excluded from the default unit run) exercises
  the service path against injected faults and asserts the *externally
  visible* outcome — the unit suite alone proved each mechanism, this
  proves the composition: a signed webhook → `pending_runs` row →
  commit status `pending` → run → `success`/`failure`/`error`;
  Postgres down at enqueue → `500` (GitHub retries), nothing queued;
  process restart mid-queue → recovery re-runs `queued`/`running` rows;
  a worker killed mid-run → startup leaves no validator processes
  matching the FR-P7 signature and no `qg-*` dirs; LLM unreachable →
  run completes degraded, not hung.
- **FR-P22 Operations runbook.** `docs/ops-runbook.md` consolidates
  what today is scattered across READMEs and `docs/`: production
  bring-up (TLS profile, webhook registration, App install vs PAT,
  branch protection requiring `quality-gate`), day-2 ops (`/readyz`,
  heartbeat, failed-run queries, queue inspection), the backup/restore
  link, the NFR-P1 external port-scan verification procedure, and
  recovery procedures (restart, DB restore, stuck `pending_runs`).

### Deployment readiness (phase-3 follow-ups)

- **FR-P23 Public readiness path.** The nginx front proxies
  `GET /readyz` (alongside `/healthz`) so external monitors reach the
  readiness signal through the single TLS surface — today only the
  backend port answers it and the runbook's public `curl /readyz`
  check returns 404. The endpoint discloses only `status`,
  `postgres` and `queue_depth`; that exposure is accepted and
  documented here. NFR-P1 is unchanged — one public port.

- **FR-P24 Compose secret hygiene.** `POSTGRES_USER`,
  `POSTGRES_PASSWORD`, `POSTGRES_DB` and both `DATABASE_URL`
  injections resolve from env (`${VAR:?}` required-checks) instead of
  committed literals; `.env.example` documents them. The loopback/
  internal binding stays — a read of `compose.yaml` alone no longer
  yields working credentials.

- **FR-P25 Container log rotation.** Long-running compose services
  declare the `json-file` driver with bounded `max-size`/`max-file` so
  an unattended host cannot fill its disk on structured logs.
  Defaults are sane (`10m` × `5`); the knobs are overridable via env.

- **FR-P26 Anthropic structured output.** `json_mode` on the
  `anthropic` provider is never silently ignored: when the Messages
  API supports a structured-output mechanism for the configured model
  it is sent; otherwise `.env.example` stops suggesting the flag for
  that provider. The JSON findings contract must not depend on prompt
  politeness alone where the API offers enforcement.

- **FR-P27 Off-box backup tier.** `deploy/backup.sh` ships each dump
  to object storage when `QG_BACKUP_S3_URI` is set (`aws s3 cp`;
  `rclone` documented as the alternative for non-AWS stores). The
  local tier and its retention are unchanged; `docs/backup-restore.md`
  gains the fetch step. Bucket lifecycle rules are the operator's
  remote retention tier.

- **FR-P28 AWS bring-up guide.** `docs/aws-deployment.md` covers the
  single-VM AWS deployment: EC2 sizing for the NFR-P5 ceiling, a
  security group exposing only 443 (SSH restricted to operator CIDRs),
  DNS → `QG_PUBLIC_HOST`, secrets pulled from SSM Parameter Store or
  Secrets Manager at boot (never baked into the AMI or printed to
  user-data logs), and the pgdata persistence choice (dedicated EBS
  volume vs. the documented RDS trade-off).

- **FR-P29 Immutable image delivery.** CI builds both package images
  and pushes tag+digest refs to a registry (GHCR). A
  `compose.prod.yaml` overlay consumes `image:` refs so the host runs
  without the source tree or a build toolchain; `compose.yaml` keeps
  `build:` for development.

- **FR-P30 Update and rollback procedure.** The ops runbook gains an
  upgrade section: deploy = pull the new tag and
  `compose -f compose.yaml -f compose.prod.yaml up -d`; rollback = pin
  the previous tag and re-deploy. Schema evolution is additive
  (memoized `IF NOT EXISTS` DDL), so an older binary reads a newer
  database safely.

### Local host readiness (phase-4 follow-ups — open)

A host audit (2026-11-13: rootless podman 5.4, docker-compose 2.26,
podman-compose 1.3, no Docker daemon) found the documented bring-up
path implicitly assumes Docker. These items close the gap for
podman-first dev hosts; none affects the production AWS path (FR-P28).

- **FR-P31 Build-context hygiene.** The repo root ships a
  `.dockerignore` covering `.venv/`, `.git/`, `__pycache__/`, `*.pyc`,
  `.pytest_cache/`, `pgdata/`, `target-repo/` and `.env*`. Today
  `COPY packages ./packages` bakes bytecode caches into both images
  and every build uploads the local `.venv` (100 MB+) and `.git` as
  context. `.env*` MUST stay out of the context even though no
  instruction copies it — defense in depth against future `COPY .`
  mistakes.

- **FR-P32 Socketless container-runtime path.** The runbook documents
  both podman bring-up paths: (a) `podman-compose` — the supported
  socketless path for the dev `compose.yaml`, no `podman.socket`
  required; and (b) `systemctl --user enable --now podman.socket` +
  `DOCKER_HOST=unix://$XDG_RUNTIME_DIR/podman/podman.sock` for
  docker-compose v2. Path (b) is required for `compose.prod.yaml`:
  its `build: !reset null` is a compose-spec extension podman-compose
  does not implement. All image refs (compose + Dockerfiles) are
  fully qualified (`docker.io/…`, `ghcr.io/…`) so pulls never depend
  on host short-name aliases or `unqualified-search-registries` —
  rootless podman hosts commonly ship neither. podman-compose also
  does **not** expand `${VAR:-default}` for vars unset in `.env` —
  the literal `${…}` string reaches the container, which flipped
  `QG_EXEC_VALIDATORS` to truthy (executing validators ON in service
  mode — an FR-P12 violation) on the audited host. Therefore every
  compose `${VAR:-…}` var MUST be explicitly set in `.env` when the
  socketless path is used; the checklist asserts
  `podman inspect … | grep '\${'` comes back empty after `up`. The
  dev bring-up checklist also covers the `REPO_UNDER_TEST` directory
  existing and `PR_NUMBER` being a real PR — both are `.env`
  placeholders that fail late today.

- **FR-P33 Rootless bind-mount ownership.** Under rootless podman,
  host-uid files appear as container `root`, so `appuser` (uid 10001)
  cannot write a host-owned `REPO_UNDER_TEST` bind mount and `/work`
  writes (validator caches) fail. Bring-up documents the fix —
  `podman unshare chown -R 10001:10001 <dir>` (uses the subuid map) or
  a world-writable dev-only dir — plus a write probe in the checklist.

- **FR-P34 Postgres host-port coexistence.** The postgres host
  publish becomes `127.0.0.1:${QG_PG_PORT:-5432}:5432` so the stack
  coexists with another podman/compose stack already bound to 5432 —
  observed on the dev host, where an unrelated project's postgres
  publishes `0.0.0.0:5432` and would block ours. Internal
  service-to-service traffic stays on `postgres:5432`; only the
  loopback publish moves, and `.env.example` notes that a host-side
  `DATABASE_URL` (local CLI runs) must match the chosen port.

- **FR-P35 Storage-driver advisory.** A podman host reporting
  `graph driver "overlay" overwritten by "vfs"` gets a documented
  check (`podman info --format '{{.Store.GraphDriverName}}'`) and a
  backup-first remediation note: `podman system reset` restores
  overlay but destroys ALL local images, containers and volumes —
  it MUST NOT run unattended on a host that also runs other stacks.
  `vfs` is a supported-if-slower state (full layer copies, higher
  disk use); the advisory exists so nobody "fixes" it by wiping a
  neighbour's database.

## 3. Contracts

### 3.1 Commit status (FR-P8)

```text
POST /repos/{owner}/{repo}/statuses/{head_sha}
{state: pending|success|failure|error, context: "quality-gate",
 target_url: <PR verdict comment url>, description: <one-line>}
```

`pending` on dispatch, `success` on approve, `failure` on rejected
verdict, `error` on internal failure (FR-P2). Never blocks a run.

### 3.2 `pending_runs` table (FR-P4)

```text
delivery_id TEXT PRIMARY KEY, repo TEXT NOT NULL, pr_number INT NOT NULL,
head_sha TEXT NOT NULL, clone_url TEXT NOT NULL,
status TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|failed
attempts INT NOT NULL DEFAULT 0,
created_at / started_at / finished_at TIMESTAMPTZ,
error TEXT
```

Bootstrapped with the same `CREATE TABLE IF NOT EXISTS` memoized-DDL
pattern as FR-O18.

### 3.3 `run_metrics` additions (FR-P2/P15)

`status TEXT NOT NULL DEFAULT 'ok'` and `error TEXT` columns via
idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS`, plus `run_id`
correlation already present (FR-P1 fixes its value).

### 3.4 New `QG_*` settings (amends SPEC.md §7)

| Setting | Default | Purpose |
|---|---|---|
| `QG_STATUS_CONTEXT` | `quality-gate` | commit-status context (FR-P8) |
| `QG_RETRY_ATTEMPTS` | 3 | transient-fault retries (FR-P3) |
| `QG_RETRY_BACKOFF_S` | 2 | backoff base seconds (FR-P3) |
| `QG_SHUTDOWN_GRACE_S` | 120 | drain wait on shutdown (FR-P5) |
| `QG_EXEC_VALIDATORS` | `0` service / `1` CLI | executing-validator gate (FR-P12) |
| `QG_HEARTBEAT_URL` | unset | dead-man ping after each run (FR-P16) |
| `GITHUB_APP_ID` / `GITHUB_APP_PRIVATE_KEY` / `GITHUB_APP_INSTALLATION_ID` | unset | App auth path (FR-P18) |

## 4. Non-functional

- **NFR-P1 Single public surface.** Only the TLS endpoint is reachable
  from outside; Postgres, the MCP SSE server and the ingress backend
  bind to the compose network/loopback. Verifiable by an external port
  scan in the deploy runbook.
- **NFR-P2 No silent loss.** Every accepted delivery ends in
  `done`/`failed` in `pending_runs` with an externally visible outcome
  (status and/or comment). A service restart loses zero queued work.
- **NFR-P3 No secret inheritance.** Spawned validator/audit processes
  receive no secret-bearing env vars (FR-P11) — testable.
- **NFR-P4 Bounded blast radius unchanged.** Retries, queue persistence
  and status posting add no new external surface: egress stays GitHub +
  configured LLM endpoint + heartbeat URL.
- **NFR-P5 Scale ceiling.** The design targets tens of PRs/day on one
  VM. Anything requiring a broker, autoscaling or multi-AZ is
  explicitly out of scope.

## 5. Implementation order (informative)

Phase 0 (pilot blockers, independent PRs): FR-P9/P10 (exposure+TLS),
FR-P1 (thread_id), FR-P11/P12 (env scrub + validator policy), FR-P8
(gating), FR-P19 decision. Phase 1 (reliable pilot): FR-P2/P3/P4
(delivery guarantees), FR-P15/P16 (observability), FR-P17 (backups),
FR-P14 (supply chain), FR-P13 (image). Phase 2 (steady state): FR-P18
(GitHub App), FR-P7 hardening, e2e coverage of the failure matrix
(SPEC.md §9 extension), ops runbook. Phase 2 items landed as FR-P18
(#52) plus FR-P20 (self-owned sweep), FR-P21 (failure-matrix e2e) and
FR-P22 (ops runbook). Phase 3 (deployment readiness, one PR per item):
FR-P23 readiness path, FR-P24 compose secrets, FR-P25 log rotation,
FR-P26 Anthropic structured output, FR-P27 off-box backups, FR-P28 AWS
guide, FR-P29 image delivery, FR-P30 update/rollback.

## 6. Deferred backlog (explicit non-goals for now)

- **No-egress validator sandboxing.** `QG_EXEC_VALIDATORS=0` mitigates
  `cargo check`/`go vet` executing PR code; real containment needs
  netns/seccomp or a microVM — a separate effort.
- **Per-repo configuration** (`.quality-gate.yml`). If implemented, it
  must be read from the *base* branch or the server side — never from
  the PR's own tree, which the PR author controls.
- **LLM circuit breaker.** The existing per-call timeout + graceful
  degradation already prevents hangs; a breaker would only save
  latency on doomed calls — an optimization, not a gap.
- **Aggregate LLM cost budget.** `llm_calls` records per-call usage
  but nothing caps spend per day or per review; add a budget check
  when cost matters (the `deterministic` profile stays the zero-cost
  option).
- **LLM call retries.** A specialist call fails → deterministic
  degrade (FR-O16) on the first transient error; bounded retries would
  recover the enrichment on 429/5xx at the cost of latency.
- **Metrics endpoint.** `/readyz` + JSON logs + the dead-man heartbeat
  cover the NFR-P5 scale; a Prometheus/OpenMetrics surface is
  premature.

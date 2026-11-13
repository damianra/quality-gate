# Operations runbook (FR-P22)

Everything needed to bring up, monitor, and recover the quality-gate
service on a single VM. Assumes `compose.yaml` at the repo root and a
public hostname `QG_PUBLIC_HOST` pointing at the box. Deploying on AWS?
`docs/aws-deployment.md` (FR-P28) covers the EC2/SG/SSM/EBS layer —
this runbook applies on top of it.

## 1. Bring-up (production)

1. Copy `.env.example` → `.env` and fill in:
   - `GITHUB_WEBHOOK_SECRET` (required — every payload is HMAC-verified)
   - `QG_PUBLIC_HOST` (the public hostname for Caddy TLS)
   - `QG_ALLOWED_REPOS` (`owner/repo` or `owner/*`, deny-by-default)
   - **Auth**: either `GITHUB_TOKEN` (PAT) **or** the GitHub App triple
     `GITHUB_APP_ID` / `GITHUB_APP_PRIVATE_KEY` /
     `GITHUB_APP_INSTALLATION_ID` (FR-P18 — recommended; mints 1h
     installation tokens, comments as the bot). Onboarding a repo to the
     App path is just *install the app on it*.
   - `QG_HEARTBEAT_URL` (dead-man monitor, e.g. healthchecks.io)
   - LLM profile (FR-P19): leave `QG_LLM_PROVIDER` empty for
     `deterministic` on CPU-only hosts, or set an API provider.
2. Start the stack: `docker compose --profile tls up -d`
3. Register the webhook on each repo (or let the App subscribe):
   `https://<QG_PUBLIC_HOST>/github/webhook`, event `pull_request`,
   secret above.
4. Require the `quality-gate` commit status in branch protection
   (Repo → Settings → Branches → require status checks) — without this
   FR-P8 reports but does not block.
5. Schedule `deploy/backup.sh` (see `docs/backup-restore.md`) and point
   your dead-man monitor at `QG_HEARTBEAT_URL`.

## 2. Verifying the exposure contract (NFR-P1)

From **outside** the host, only 443 must answer:

```bash
nmap -Pn -p 80,443,5432,8000,8088,8089 <host>
# expected: 80 (ACME/redirect), 443 (caddy). Everything else closed.
curl -fsS https://<QG_PUBLIC_HOST>/readyz   # readiness, see §3
curl https://<host>:5432                  # must refuse/timeout
```

On the host, the loopback services still answer
(`127.0.0.1:8089` nginx, `127.0.0.1:5432` postgres, `127.0.0.1:8000`
mcp-server *only when* the `debug` profile is up).

## 3. Day-2 monitoring

- **Readiness**: `GET /readyz` → `{"status","postgres","queue_depth"}`;
  `503` when Postgres is down. `/healthz` is process liveness only.
  Both are proxied through nginx (FR-P23); `/readyz` carries its own
  tighter per-IP rate limit because each request touches Postgres.
- **Logs**: one JSON object per record, correlated by
  `run_id = pr-review/{repo}/{pr}` — the same key `run_metrics` uses.
  `journalctl -u docker | grep '"run_id":"pr-review/acme/widgets/7"'`
  traces one PR end to end.
- **Failed runs**:
  `SELECT * FROM run_metrics WHERE status <> 'ok';`
  `SELECT * FROM pending_runs WHERE status = 'failed';`
- **Queue inspection**: `SELECT status, count(*) FROM pending_runs
  GROUP BY status;` — a growing `queued` count with zero `running`
  means workers are wedged.
- **Dead-man**: silence past the monitor interval pages the team; a
  ping after every completed run is the liveness signal.

## 4. Recovery procedures

| Symptom | Action |
|---|---|
| Service down | `docker compose up -d` — containers restart `unless-stopped`; on boot the ingress sweeps orphans (FR-P7/P20), re-enqueues `queued`/`running` rows (FR-P4), and logs a `doctor` report of resolved validator binaries (FR-P13). |
| `pending_runs` stuck `queued` | Check `docker compose logs ingress` for clone/review errors; requeue manually with `UPDATE pending_runs SET status='queued' WHERE delivery_id='…'`, then `docker compose restart ingress`. |
| Webhook accepted but nothing ran | Row is in `pending_runs` — never lost (FR-P4). Check `failed` rows for the error; verify `QG_ALLOWED_REPOS` covers the repo (403 otherwise). |
| Postgres corrupt/lost | Restore per `docs/backup-restore.md`; rows in `queued`/`running` at restore time resume automatically. |
| High queue depth | `QG_MAX_CONCURRENT_RUNS` bounds parallelism; bursts coalesce per PR. A sustained backlog means clone or LLM latency — check `run_metrics.duration_s` trends. |
| Validators unexpectedly skipped | Read the `doctor` startup log — the binary is missing from the image (expected for `go`/`cargo`: executing validators ship but are gated by `QG_EXEC_VALIDATORS` in service mode, FR-P12). |

## 5. Updates and rollback (FR-P30)

Deploys are tag moves on immutable images, not rebuilds on the host.

**Update** (registry path, FR-P29):

```bash
cd /opt/quality-gate
git pull --ff-only                      # compose files + deploy/ + docs
export QG_IMAGE_TAG=<new-commit-sha>    # tag pushed by publish-images
docker compose -f compose.yaml -f compose.prod.yaml --profile tls pull
docker compose -f compose.yaml -f compose.prod.yaml --profile tls up -d
curl -fsS https://<QG_PUBLIC_HOST>/readyz
```

**Rollback** — pin the previous tag and re-deploy:

```bash
export QG_IMAGE_TAG=<previous-commit-sha>
docker compose -f compose.yaml -f compose.prod.yaml --profile tls up -d
```

Rollback is safe because schema evolution is additive only — every
table/column arrives via memoized `CREATE TABLE IF NOT EXISTS` /
`ALTER TABLE … ADD COLUMN IF NOT EXISTS`, so an older binary reads a
newer database without a downgrade step.

Notes:

- In-flight runs: `up -d` sends SIGTERM; the worker drains for up to
  `QG_SHUTDOWN_GRACE_S` (FR-P5) and `queued`/`running` `pending_runs`
  rows resume on the new version (FR-P4). No accepted review is lost.
- Config-only changes (e.g. log-rotation bounds, FR-P25) apply on
  container recreate — `up -d` recreates when the rendered config
  differs; use `--force-recreate` when it does not.
- Source-build path (dev): `git pull && docker compose --profile tls
  up -d --build`.

## 6. Security posture (what NOT to do)

- Never enable the `mcp-server` `debug` profile in production —
  `spawn_process` is unauthenticated command execution (FR-P9).
- Never publish `postgres:5432` or the ingress `:8088` beyond loopback.
- `GITHUB_TOKEN`/`DATABASE_URL`/the App private key stay env-only —
  validators run in a scrubbed environment (FR-P11) precisely so a PR
  can't exfiltrate them.
- `QG_EXEC_VALIDATORS=1` in service mode lets `cargo check`/`go vet`
  execute PR-controlled `build.rs`/cgo — opt in only for trusted-repo
  deployments (FR-P12).

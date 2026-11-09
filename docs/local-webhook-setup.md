# Local webhook exposure — operational guide

Everything needed to receive real GitHub `pull_request` webhooks against a
local quality-gate run: services to bring up, how to publish the endpoint,
the exact GitHub webhook settings, and how to tear it down.

```
GitHub --POST--> <public URL> --> ingress (:8089) --> clone PR head
        --> review graph --> verdict comment + run_metrics --> cleanup
```

## 1. Services checklist

| Piece | Needed for | Bring-up |
|---|---|---|
| **PostgreSQL** | Checkpointer + `run_metrics`/`llm_call_metrics` | Any reachable PG; set `DATABASE_URL`. Compose's own `postgres` works if port 5432 is free |
| **Ollama** (optional) | Harness/specialist LLM enrichment | `ollama serve` on host + `ollama pull qwen3:8b` |
| **Ingress** | Webhook receiver | `uv run qg-ingress` (bare host) **or** compose `ingress` |
| **Public surface** | GitHub must reach you | Tunnel (local testing) **or** nginx compose service / router forward |
| **`devin` CLI** (optional) | `devin-cli` role providers | `devin auth` session |

`.env` must define: `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET`,
`QG_ALLOWED_REPOS`, `QG_INGRESS_PORT`, `DATABASE_URL` (see `.env.example`).

## 2. Bring the services up

### Bare host (recommended for a quick local test)

The ingress binds loopback only — nothing is public yet.

```bash
set -a && source .env && set +a
uv run qg-ingress            # http://127.0.0.1:$QG_INGRESS_PORT
curl http://127.0.0.1:8089/healthz        # -> {"status":"ok"}
```

### Compose (production-shaped; nginx is the only public port)

```bash
docker compose up -d ingress nginx      # ingress on internal net only
docker compose --profile llm up -d ollama   # optional LLM fallback
```

Note: compose's `postgres` service publishes 5432 — if a standalone
postgres already owns the host port, start the stack without it and point
`DATABASE_URL` at the existing instance instead.

## 3. Publish the endpoint

### Option A — SSH tunnel, zero install (what this repo was tested with)

```bash
ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 \
    -R 80:localhost:8089 nokey@localhost.run
# prints: https://<id>.lhr.life tunneled with tls termination
```

Valid TLS cert -> GitHub *SSL verification: Enable*.

### Option B — cloudflared quick tunnel

```bash
cloudflared tunnel --url http://localhost:8089
# prints https://<random>.trycloudflare.com
```

### Option C — ngrok (account required)

```bash
ngrok http 8089   # https://<id>.ngrok-free.app
```

### Option D — real exposure (router / VPS)

Port-forward 8089 on the router, or put the nginx compose service behind
your own reverse proxy with a managed cert. Only needed for a permanent
deployment — a tunnel is the right tool for local testing.

## 4. GitHub webhook settings

Repo -> **Settings -> Webhooks -> Add webhook**:

| Field | Value |
|---|---|
| Payload URL | `https://<tunnel-or-host>:<port>/github/webhook` |
| Content type | **`application/json`** — required: the ingress parses the raw body as JSON; the default `application/x-www-form-urlencoded` wraps the JSON in a `payload` field and gets rejected |
| Secret | the `GITHUB_WEBHOOK_SECRET` value from `.env` (generate with `openssl rand -hex 32`; same value in both places — GitHub signs `X-Hub-Signature-256` with it) |
| SSL verification | **Enable** — tunnels terminate TLS with a valid cert; *Disable* is only for self-signed certs |
| Events | *Let me select individual events* -> only **Pull requests** (uncheck Pushes). Covers `opened`, `synchronize`, `reopened` — the only actions the ingress processes |

The repo must also appear in `QG_ALLOWED_REPOS` (`owner/repo` or
`owner/*`) — otherwise deliveries get a `403`.

## 5. Verify

```bash
curl https://<public-url>/healthz                      # {"status":"ok"}
curl -X POST https://<public-url>/github/webhook \
     -H 'Content-Type: application/json' -d '{}'       # 401 — unsigned
```

Then on GitHub: *Webhooks -> Recent deliveries* shows each POST and its
response code. In the ingress log, `202` = accepted+queued,
`401` = bad signature, `403` = repo not allowed. Finished runs land in
`run_metrics` and the verdict appears as a PR comment.

## 6. Caveats learned in this repo's testing

- **Ephemeral URLs** — free tunnels assign a new hostname each session;
  update the GitHub webhook if the tunnel restarts.
- **ISP DNS hijack** — some ISPs resolve tunnel subdomains to a walled-
  garden loopback, so *local* `curl` to the public URL can fail while
  GitHub reaches it fine. Verify via DoH IP + `curl --resolve` if in
  doubt.
- **LLM failure is safe** — enrichment degrades to an info finding; a
  dead ollama/provider never rejects a PR by itself.

## 7. Teardown

```bash
# bare-host path: stop ingress + tunnel (postgres can stay)
kill <uvicorn pid> <ssh tunnel pid>
# compose path:
docker compose down        # add --profile llm to include ollama
# worktrees clean themselves per run; sweep leftovers:
rm -rf /tmp/qg-*
```

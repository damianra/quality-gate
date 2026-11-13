# AWS deployment guide (FR-P28)

Single-VM deployment of the quality-gate service on AWS, sized for the
NFR-P5 ceiling: tens of PRs/day, one EC2 instance, no GPU. The bring-up
steps themselves live in `docs/ops-runbook.md` §1 — this guide covers
only the AWS-specific layer around them.

## 1. Topology

```text
GitHub webhook ──HTTPS──> EC2 :443 (caddy) ──> nginx :8089 ──> ingress :8088
                              │
                              └── postgres (loopback :5432) on EBS-backed pgdata
```

- **Instance**: `t3.small` (2 vCPU / 2 GB) handles
  `QG_MAX_CONCURRENT_RUNS=2` plus Postgres comfortably; `t3.medium` if
  you raise concurrency. ARM (`t4g.small`) works and is ~20% cheaper —
  the images are multi-arch (pinned by index digest, FR-P14) and
  `osv-scanner` resolves its binary via `dpkg --print-architecture`.
- **LLM profile**: `deterministic` (zero egress) or `api` (Claude /
  OpenAI / OpenRouter). `local` (ollama) is not viable on these
  CPU-only shapes — see FR-P19.
- **DNS**: an A record (Elastic IP or a stable public IP) pointing at
  the box; set it as `QG_PUBLIC_HOST` — Caddy needs it for the Let's
  Encrypt certificate.

## 2. Security group

| Port | Source | Purpose |
|---|---|---|
| 443 | `0.0.0.0/0` | webhooks (Caddy TLS) |
| 80 | `0.0.0.0/0` | ACME http-01 + HTTPS redirect |
| 22 | operator CIDRs | SSH admin (or use SSM Session Manager) |
| all else | — | closed |

`5432`, `8088`, `8089` and `8000` must NOT be open: compose already
binds them to loopback/the internal network (FR-P9) — the security
group is the second, independent layer. Verify with the external port
scan in `docs/ops-runbook.md` §2 after bring-up.

## 3. IAM and secrets

- Attach an **instance profile** (IAM role), never static keys:
  `ssm:GetParameter` on `/quality-gate/*` and `s3:PutObject` on the
  backup bucket are the only permissions the box needs.
- Store each secret as an SSM **SecureString** parameter:
  `/quality-gate/GITHUB_TOKEN` (or the GitHub App triple),
  `GITHUB_WEBHOOK_SECRET`, `POSTGRES_PASSWORD`, `QG_LLM_API_KEY`.
  Non-secrets (`QG_ALLOWED_REPOS`, `QG_PUBLIC_HOST`) may be plain
  String. AWS Secrets Manager works too — same pattern via
  `aws secretsmanager get-secret-value`.
- **user-data renders `.env` at first boot.** The script contains
  parameter *names*, never values: secrets must not be baked into the
  AMI, must not appear as user-data literals (visible to anyone with
  `DescribeInstanceAttribute`), and must not reach cloud-init logs —
  do NOT enable `set -x`.

Sample `user-data`:

```bash
#!/bin/bash
set -euo pipefail   # no `set -x` — command output would leak to logs
apt-get update && apt-get install -y docker.io docker-compose-v2 awscli
systemctl enable --now docker

mkdir -p /opt/quality-gate && cd /opt/quality-gate
git clone https://github.com/<owner>/quality-gate.git .

# Render .env from SSM SecureStrings — only names live in this script.
p() { aws ssm get-parameter --name "/quality-gate/$1" \
        --with-decryption --query Parameter.Value --output text; }
umask 077
cat > .env <<EOF
GITHUB_TOKEN=$(p GITHUB_TOKEN)
GITHUB_WEBHOOK_SECRET=$(p GITHUB_WEBHOOK_SECRET)
POSTGRES_USER=qualitygate
POSTGRES_PASSWORD=$(p POSTGRES_PASSWORD)
POSTGRES_DB=qualitygate
QG_ALLOWED_REPOS=$(p QG_ALLOWED_REPOS)
QG_PUBLIC_HOST=$(p QG_PUBLIC_HOST)
# LLM profile — deterministic when unset; for the `api` profile:
QG_LLM_PROVIDER=$(p QG_LLM_PROVIDER)
QG_LLM_API_KEY=$(p QG_LLM_API_KEY)
EOF
chmod 600 .env

docker compose --profile tls up -d
```

## 4. Storage

Two layers, pick deliberately:

- **Docker data on EBS.** The simplest durable layout is one extra gp3
  volume mounted at `/var/lib/docker` **before** the first
  `compose up` — images, `pgdata`, worktrees and logs all land on it:

  ```bash
  mkfs.ext4 /dev/xvdf
  mkdir -p /var/lib/docker && mount /dev/xvdf /var/lib/docker
  echo '/dev/xvdf /var/lib/docker ext4 defaults,nofail 0 2' >> /etc/fstab
  ```

  Snapshot the volume for belt-and-suspenders recovery on top of the
  logical dumps (§5).
- **RDS alternative.** A `db.t4g.micro` (~$15/mo) trades the compose
  postgres for automated snapshots, patching and instance-independent
  state. At the NFR-P5 scale it is not required — the compose
  postgres + S3 dumps is recoverable — but it is the supported
  upgrade path if checkpoints/metrics become precious. If used, point
  `DATABASE_URL` at the RDS endpoint and drop the `postgres` service.

## 5. Backups and monitoring

- Schedule `deploy/backup.sh` on the host (cron/systemd — see
  `docs/backup-restore.md`) with `QG_BACKUP_S3_URI=s3://<bucket>/quality-gate`.
  The bucket stays private; a lifecycle rule expiring objects at 14–30
  days is the remote retention tier. `s3:PutObject` on the instance
  role is all the job needs.
- `QG_HEARTBEAT_URL` → your dead-man monitor (e.g. healthchecks.io) —
  silence past the interval pages the team.
- Optional: the CloudWatch agent for disk/memory alarms; a free
  "DiskSpaceUtilization" on the EBS volume is the one worth having.

## 6. Bring-up checklist (AWS deltas)

1. Launch the instance (§1) with the SG (§2) and instance profile (§3).
2. Attach/mount the EBS volume at `/var/lib/docker` (§4) before first
   `compose up`.
3. Run the user-data (or the equivalent manual steps): renders `.env`
   from SSM, `docker compose --profile tls up -d`.
4. `docs/ops-runbook.md` §1 steps 3–5: webhook registration, branch
   protection on `quality-gate`, backup schedule, heartbeat.
5. `docs/ops-runbook.md` §2: external port scan — only 80/443 answer.

## 7. Cost envelope (us-east-1, rough)

| Item | ~Monthly |
|---|---|
| t3.small on-demand | $15 |
| 30 GB gp3 EBS | $2.5 |
| S3 backups (14-day tier) | < $1 |
| EIP while attached | $0 |
| **Infra total** | **~$18** |

Plus LLM API spend on the `api` profile (~$0.01–0.20/review by model
tier, FR-P19) — tens of PRs/day stays well under a dollar a day.

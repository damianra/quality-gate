# Postgres backup & restore (FR-P17)

The `postgres` service holds all durable state: LangGraph checkpoints
(resumability), `run_metrics` / `llm_call_metrics` (telemetry) and
`pending_runs` (the durable webhook queue). Losing the `pgdata` volume
loses that history — the service keeps working, but checkpoints and the
accepted-not-yet-run queue are gone, so backups matter.

## Backup

`deploy/backup.sh` runs `pg_dump --format=custom` inside the running
`postgres` container, gzips to `${QG_BACKUP_DIR:-./backups}` and prunes
dumps older than `${QG_BACKUP_RETENTION_DAYS:-14}` days.

Schedule it from the host:

```cron
# /etc/cron.d/quality-gate — 02:30 daily
30 2 * * * qg cd /opt/quality-gate && ./deploy/backup.sh >> /var/log/qg-backup.log 2>&1
```

or with a systemd timer:

```ini
# qg-backup.timer
[Timer]
OnCalendar=*-*-* 02:30:00
Persistent=true
```

```ini
# qg-backup.service
[Service]
Type=oneshot
WorkingDirectory=/opt/quality-gate
ExecStart=/opt/quality-gate/deploy/backup.sh
```

The script keeps a local tier only. Copy `${BACKUP_DIR}` off-box
(`rsync`, `rclone`, object storage) from a wrapper for real disaster
recovery — a dump on the same disk as `pgdata` protects against
accidental drops, not disk loss.

## Restore

Run against a compose stack whose `postgres` is up (other services may
stay down):

```bash
# 1. Stop writers so no run mutates the DB mid-restore.
docker compose stop ingress orchestrator

# 2. Restore into a clean database (custom format; --clean drops objects).
gunzip -c backups/qualitygate-2026-11-13-023000.dump.gz \
  | docker compose exec -T postgres \
      pg_restore -U qualitygate -d qualitygate --clean --if-exists

# 3. Restart the stack.
docker compose up -d

# 4. Verify: rows are back and the service is ready.
docker compose exec -T postgres \
    psql -U qualitygate -c "SELECT count(*) FROM run_metrics;"
curl -fsS https://<QG_PUBLIC_HOST>/readyz
```

On restart the ingress re-enqueues any `pending_runs` rows left in
`queued`/`running` (FR-P4), so reviews accepted before the outage
resume automatically — expect a burst of catch-up reviews.

## Restore test

Test the procedure quarterly on a scratch stack: copy the dump to a
throwaway `pgdata` (or `docker compose -p qg-restore` project), restore,
and confirm `run_metrics`/`pending_runs` row counts match the source
backup log. An untested backup is not a backup.

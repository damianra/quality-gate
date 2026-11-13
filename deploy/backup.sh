#!/usr/bin/env bash
# FR-P17: daily Postgres backup for the quality-gate compose stack.
#
# Dumps the `qualitygate` database (LangGraph checkpoints + run_metrics +
# pending_runs) via `docker compose exec -T postgres pg_dump`, gzipped to
# ${QG_BACKUP_DIR:-./backups}. Files older than ${QG_BACKUP_RETENTION_DAYS:-14}
# days are pruned. Copy the dumps off-box (object storage, rsync) from a
# wrapper if needed — this script keeps the local tier only.
#
# Cron example (02:30 daily, repo root):
#   30 2 * * * cd /opt/quality-gate && ./deploy/backup.sh >> /var/log/qg-backup.log 2>&1
#
# systemd timer alternative: point a oneshot unit at this script, calendar
# `*-*-* 02:30:00`. See docs/backup-restore.md for the restore procedure.

set -euo pipefail

BACKUP_DIR="${QG_BACKUP_DIR:-./backups}"
RETENTION_DAYS="${QG_BACKUP_RETENTION_DAYS:-14}"
STAMP="$(date +%F-%H%M%S)"
OUT="${BACKUP_DIR}/qualitygate-${STAMP}.dump.gz"

mkdir -p "${BACKUP_DIR}"

docker compose exec -T postgres \
    pg_dump -U qualitygate -d qualitygate --format=custom \
  | gzip > "${OUT}"

echo "backup written: ${OUT} ($(du -h "${OUT}" | cut -f1))"

find "${BACKUP_DIR}" -name 'qualitygate-*.dump.gz' \
    -mtime "+${RETENTION_DAYS}" -delete

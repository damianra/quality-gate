#!/usr/bin/env bash
# FR-P17: daily Postgres backup for the quality-gate compose stack.
#
# Dumps the `qualitygate` database (LangGraph checkpoints + run_metrics +
# pending_runs) via `docker compose exec -T postgres pg_dump`, gzipped to
# ${QG_BACKUP_DIR:-./backups}. Files older than ${QG_BACKUP_RETENTION_DAYS:-14}
# days are pruned. Credentials resolve inside the container from the
# POSTGRES_* env (FR-P24) — nothing is read from the host env.
#
# FR-P27 off-box tier: when QG_BACKUP_S3_URI is set (e.g.
# s3://my-bucket/quality-gate), each dump is uploaded with `aws s3 cp`
# after the local write. Auth comes from the instance IAM role,
# AWS_PROFILE or standard AWS_* env vars — never from this script.
# Non-AWS object stores: use rclone in a wrapper instead
# (`rclone copy ${QG_BACKUP_DIR} remote:bucket/quality-gate`). Bucket
# lifecycle rules are the remote retention tier.
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
    sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  | gzip > "${OUT}"

echo "backup written: ${OUT} ($(du -h "${OUT}" | cut -f1))"

if [[ -n "${QG_BACKUP_S3_URI:-}" ]]; then
    if command -v aws >/dev/null 2>&1; then
        DEST="${QG_BACKUP_S3_URI%/}/$(basename "${OUT}")"
        aws s3 cp "${OUT}" "${DEST}"
        echo "backup uploaded: ${DEST}"
    else
        echo "QG_BACKUP_S3_URI set but aws CLI not on PATH —" \
             "dump kept locally only" >&2
    fi
fi

find "${BACKUP_DIR}" -name 'qualitygate-*.dump.gz' \
    -mtime "+${RETENTION_DAYS}" -delete

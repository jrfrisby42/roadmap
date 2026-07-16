#!/usr/bin/env bash
#
# backup-dbs.sh — off-box backup of every per-team SQLite DB to S3.
#
# Runs a WAL-safe hot backup (sqlite3 .backup) of each /data/tenants/*/roadmap.db,
# gzips it, and uploads to s3://$BUCKET/$PREFIX/<team>/<UTC-timestamp>.db.gz.
# Designed for cron. Auto-discovers teams (glob), so new teams are covered with
# no config change.
#
# Setup (see the "Automated backups" steps in the deploy notes):
#   sudo cp tools/backup-dbs.sh /opt/roadmap/backup-dbs.sh
#   sudo chmod +x /opt/roadmap/backup-dbs.sh
#   # cron (every 6h): run `sudo crontab -e` and add:
#   0 */6 * * * BUCKET=frazil-flow-backups /opt/roadmap/backup-dbs.sh >> /var/log/roadmap-backup.log 2>&1
#
# Requirements:
#   - sqlite3, aws cli, gzip on PATH (present on the prod host).
#   - The EC2 instance role must allow s3:PutObject on s3://$BUCKET/$PREFIX/*.
#     (The attachments-bucket role grant is prefix-scoped to items/* + intake/*,
#      so use a DEDICATED bucket, or extend the policy — do NOT assume it works;
#      the first run will surface an AccessDenied if not.)
#   - Retention: set an S3 lifecycle rule on $PREFIX/ to expire old backups
#     (e.g. 30 days). The role is denied ListBucket, so the script cannot prune.
#
set -euo pipefail

BUCKET="${BUCKET:-frazil-flow-backups}"   # override via env: BUCKET=my-bucket ./backup-dbs.sh
PREFIX="${PREFIX:-db-backups}"
TENANTS_DIR="${TENANTS_DIR:-/data/tenants}"
REGION="${AWS_REGION:-us-west-2}"

ts="$(date -u +%Y%m%d-%H%M%SZ)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

shopt -s nullglob
dbs=("$TENANTS_DIR"/*/roadmap.db)
if [ ${#dbs[@]} -eq 0 ]; then
  echo "[$(date -u +%FT%TZ)] backup: no team DBs found under $TENANTS_DIR" >&2
  exit 1
fi

fail=0
for db in "${dbs[@]}"; do
  team="$(basename "$(dirname "$db")")"
  out="$tmp/${team}.db"
  # WAL-safe consistent snapshot (does NOT lock out the live app)
  if ! sqlite3 "$db" ".backup '$out'"; then
    echo "[$(date -u +%FT%TZ)] backup: sqlite .backup FAILED for $team" >&2; fail=1; continue
  fi
  gzip -f "$out"
  dest="s3://$BUCKET/$PREFIX/$team/$ts.db.gz"
  if aws s3 cp "$out.gz" "$dest" --region "$REGION" --only-show-errors; then
    echo "[$(date -u +%FT%TZ)] backup: $team -> $dest ($(stat -c%s "$out.gz") bytes)"
  else
    echo "[$(date -u +%FT%TZ)] backup: UPLOAD FAILED for $team -> $dest" >&2; fail=1
  fi
done

exit $fail

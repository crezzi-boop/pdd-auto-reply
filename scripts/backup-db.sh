#!/usr/bin/env bash
set -euo pipefail
cd /home/ubuntu/pdd-auto-reply
PW="$(grep -E '^MYSQL_PASSWORD=' .env | head -1 | cut -d= -f2-)"
TS="$(date +%Y%m%d-%H%M%S)"
sudo docker exec -e MYSQL_PWD="$PW" pdd_mysql mysqldump --single-transaction --routines --triggers pdd_auto_reply \
  | gzip > "/home/ubuntu/pdd-backups/db-${TS}.sql.gz"
find /home/ubuntu/pdd-backups -name 'db-*.sql.gz' -mtime +7 -delete
echo "$(date '+%F %T') backup ok: db-${TS}.sql.gz"

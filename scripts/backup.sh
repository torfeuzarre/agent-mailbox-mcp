#!/usr/bin/env bash
# Sauvegarde à chaud cohérente de la base (sqlite3 .backup), rotation 30 jours.
# Cron conseillé : 15 3 * * * /opt/agent-mailbox-mcp/scripts/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
STAMP=$(date +%Y%m%d-%H%M%S)
docker compose exec -T memoire python -c "
import sqlite3; s=sqlite3.connect('/data/memoire.db'); d=sqlite3.connect('/data/backup-tmp.db'); s.backup(d); d.close()"
mv data/backup-tmp.db "backups/memoire-$STAMP.db"
gzip "backups/memoire-$STAMP.db"
find backups -name 'memoire-*.db.gz' -mtime +30 -delete
echo "OK backups/memoire-$STAMP.db.gz"

#!/usr/bin/env bash
# Накатывает схему на любую базу: Supabase, RDS, локальный Postgres.
#
#   ./scripts/apply_schema.sh                      # берёт DATABASE_URL из .env
#   ./scripts/apply_schema.sh "postgresql://..."   # или явно
#
# Идемпотентно: всё через IF NOT EXISTS / CREATE OR REPLACE, можно гонять повторно.
set -euo pipefail

cd "$(dirname "$0")/.."

DB_URL="${1:-}"
if [ -z "$DB_URL" ] && [ -f .env ]; then
    DB_URL="$(grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2-)"
fi
if [ -z "$DB_URL" ]; then
    echo "не задан DATABASE_URL: передай первым аргументом или впиши в .env" >&2
    exit 1
fi

for f in sql/001_schema.sql sql/002_hybrid_search.sql; do
    echo "→ $f"
    psql "$DB_URL" -v ON_ERROR_STOP=1 -f "$f"
done

psql "$DB_URL" -c "SELECT count(*) AS threads FROM threads;"
echo "схема на месте"

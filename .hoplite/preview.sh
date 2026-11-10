#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Use an isolated local database when no deployment database is configured.
if [[ -z "${DATABASE_URL:-}" && ! -f .env ]]; then
  pg_ctlcluster 16 main status >/dev/null || pg_ctlcluster 16 main start
  user="$(id -un)"
  if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname = '$user'" | grep -q 1; then
    runuser -u postgres -- createuser "$user"
  fi
  if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname = 'shop_preview'" | grep -q 1; then
    runuser -u postgres -- createdb -O "$user" shop_preview
  fi
  export DATABASE_URL="postgresql:///shop_preview?host=/var/run/postgresql&user=$user"
  export DB_SSL=disable
fi
export SERVICE_ROLE=web PORT=3000
exec .venv/bin/python app.py

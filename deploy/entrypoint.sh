#!/bin/sh
# Container start: make the data volume writable, migrate, optionally seed the
# demo county, then run the app as a non-root user.
#
# The volume arrives owned by root, so the chown has to happen before we drop
# privileges. Nothing after the setpriv line runs with root.
set -e

if [ -d /data ]; then
  chown -R 10001:10001 /data
fi

exec setpriv --reuid=10001 --regid=10001 --init-groups sh -c '
  set -e                     # a failed migration stops here, never serves a half-built database
  alembic upgrade head
  if [ "$MC_SEED_ON_BOOT" = "1" ]; then
    python -m app.seed --if-empty
  fi
  if [ -n "$MC_DIRECTORY_FILE" ]; then
    python tools/import_meetings.py "$MC_DIRECTORY_FILE" --if-empty
  fi
  exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --forwarded-allow-ips="*"
'

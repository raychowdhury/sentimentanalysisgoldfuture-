#!/usr/bin/env bash
# Container entrypoint. Two roles selected by $CONTAINER_ROLE:
#   - "runner" (default) : install crontab + run cron in foreground
#   - "api"              : run the FastAPI service
set -euo pipefail

ROLE="${CONTAINER_ROLE:-runner}"

case "$ROLE" in
  runner)
    echo "[entrypoint] starting cron-driven autoresearch runner"
    # Load env into cron by rewriting the crontab with the current environment.
    printenv | grep -E '^(ANTHROPIC_API_KEY|FRED_API_KEY|DATABASE_URL|CLAUDE_MODEL|LOOP_INTERVAL_HOURS|META_OPTIMIZE_EVERY)=' \
      > /etc/autoresearch.env || true
    { cat /etc/autoresearch.env; cat /app/crontab; } > /etc/cron.d/autoresearch
    chmod 0644 /etc/cron.d/autoresearch
    crontab /etc/cron.d/autoresearch
    touch /var/log/autoresearch.log

    # Run one cycle immediately so day-one logs are populated, then hand off to cron.
    python main.py once || echo "[entrypoint] initial run failed — cron will retry"

    cron -f &
    tail -f /var/log/autoresearch.log
    ;;

  api)
    echo "[entrypoint] starting FastAPI on :8000"
    exec uvicorn api:app --host 0.0.0.0 --port 8000
    ;;

  *)
    echo "[entrypoint] unknown CONTAINER_ROLE: $ROLE" >&2
    exit 1
    ;;
esac

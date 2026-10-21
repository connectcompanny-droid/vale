#!/bin/sh
# One image, three roles. Platforms with one start command per service (Railway, Render...)
# set VALE_ROLE on each service; docker-compose overrides the command instead.
#   VALE_ROLE=api      webhooks + health (default). RUN_MIGRATIONS=true applies Alembic first.
#   VALE_ROLE=worker   conversations, documents, audio (arq)
#   VALE_ROLE=monitor  watches the cita previa site
set -e
case "${VALE_ROLE:-api}" in
  api)
    if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then alembic upgrade head; fi
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers \
      --forwarded-allow-ips='*' --workers "${WEB_CONCURRENCY:-2}"
    ;;
  worker)  exec arq app.jobs.WorkerSettings ;;
  monitor) exec python run_monitor.py ;;
  *) echo "unknown VALE_ROLE=$VALE_ROLE (use api, worker or monitor)" >&2; exit 1 ;;
esac

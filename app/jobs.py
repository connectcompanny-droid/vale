"""arq worker for conversations: `arq app.jobs.WorkerSettings`.

Scale by running more worker containers; each handles WORKER_CONCURRENCY jobs at once.
"""

import logging

from arq.connections import RedisSettings
from arq.cron import cron

from app.channels.base import IncomingMessage
from app.config import get_settings
from app.logging_setup import setup_logging

log = logging.getLogger(__name__)


async def handle_message_job(ctx, payload: dict) -> None:
    from app.router import handle

    await handle(IncomingMessage(**payload))


async def cleanup_job(ctx) -> None:
    from app.maintenance import cleanup

    await cleanup()


async def startup(ctx) -> None:
    setup_logging()
    log.info("worker %s started", get_settings().instance_id)


class WorkerSettings:
    functions = [handle_message_job]
    cron_jobs = [cron(cleanup_job, hour={3}, minute={17})]
    on_startup = startup
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = get_settings().worker_concurrency
    job_timeout = 300
    max_tries = 3
    keep_result = 0

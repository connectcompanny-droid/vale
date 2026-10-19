"""Job queue.

The webhook only validates and enqueues, so it answers WhatsApp in milliseconds no matter how
slow Claude, the document reader or speech-to-text are. Workers (arq on Redis) do the work and
can be scaled horizontally. In "inline" mode (dev and tests) jobs run in-process.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict

from app.channels.base import IncomingMessage
from app.config import get_settings

log = logging.getLogger(__name__)

_pool = None
_local_locks: dict[str, asyncio.Lock] = {}


async def _redis_pool():
    global _pool
    if _pool is None:
        from arq import create_pool
        from arq.connections import RedisSettings

        _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _pool


async def enqueue_message(msg: IncomingMessage) -> None:
    if get_settings().queue_mode == "redis":
        pool = await _redis_pool()
        # _job_id dedupes provider retries at the queue level too.
        await pool.enqueue_job("handle_message_job", asdict(msg), _job_id=f"msg:{msg.message_id}" if msg.message_id else None)
        return
    from app.router import handle

    await handle(msg)


@asynccontextmanager
async def user_lock(phone: str, timeout: int = 180):
    """One message at a time per client, across every worker. Keeps a conversation in order
    (consent before data, data before confirmation) even when messages arrive in a burst."""
    if get_settings().queue_mode == "redis":
        pool = await _redis_pool()
        lock = pool.lock(f"lock:user:{phone}", timeout=timeout, blocking_timeout=timeout)
        acquired = await lock.acquire()
        try:
            yield
        finally:
            if acquired:
                try:
                    await lock.release()
                except Exception:
                    log.warning("user lock for %s expired before release", phone)
        return
    lock = _local_locks.setdefault(phone, asyncio.Lock())
    async with lock:
        yield


async def queue_depth() -> int | None:
    if get_settings().queue_mode != "redis":
        return None
    pool = await _redis_pool()
    return await pool.zcard("arq:queue")

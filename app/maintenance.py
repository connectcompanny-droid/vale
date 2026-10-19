"""Housekeeping. Runs daily from the worker (cron)."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from app.db import session_scope
from app.models import ProcessedMessage


async def cleanup(days: int = 7) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with session_scope() as s:
        res = await s.execute(delete(ProcessedMessage).where(ProcessedMessage.created_at < cutoff))
        return res.rowcount or 0

"""Global, database-backed guard for each external site.

Every monitor process, on every server, goes through here before touching the government site:
- `acquire_visit` enforces SITE_MIN_GAP_SECONDS between ANY two visits, cluster-wide, with one
  atomic UPDATE. Ten servers or one, the site sees the same pace.
- `pause` / `paused_until` share a firewall or TLS pause with all processes.
Works on Postgres (production) and SQLite (dev/tests).
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import session_scope
from app.models import SiteState

ICP = "icp"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


async def _ensure(site: str) -> None:
    try:
        async with session_scope() as s:
            if await s.get(SiteState, site) is None:
                s.add(SiteState(site=site, visits=0))
    except IntegrityError:
        pass  # another process created it


async def acquire_visit(site: str = ICP, *, priority: bool = False) -> bool:
    """True if this process may visit the site now. `priority` (bookings the client approved)
    skips the pacing gap but never a pause."""
    await _ensure(site)
    now = _now()
    gap = 0 if priority else get_settings().site_min_gap_seconds
    async with session_scope() as s:
        res = await s.execute(
            update(SiteState)
            .where(
                SiteState.site == site,
                or_(SiteState.paused_until.is_(None), SiteState.paused_until <= now),
                or_(SiteState.last_visit_at.is_(None), SiteState.last_visit_at <= now - timedelta(seconds=gap)),
            )
            .values(last_visit_at=now, visits=SiteState.visits + 1)
            .execution_options(synchronize_session=False)
        )
        return res.rowcount == 1


async def pause(site: str, minutes: int, reason: str) -> datetime:
    await _ensure(site)
    until = _now() + timedelta(minutes=minutes)
    async with session_scope() as s:
        st = await s.get(SiteState, site)
        current = _aware(st.paused_until)
        if current is None or current < until:
            st.paused_until, st.pause_reason = until, reason
    return until


async def paused_until(site: str = ICP) -> datetime | None:
    async with session_scope() as s:
        st = await s.get(SiteState, site)
        until = _aware(st.paused_until) if st else None
    return until if until and until > _now() else None


async def resume(site: str = ICP) -> None:
    async with session_scope() as s:
        st = await s.get(SiteState, site)
        if st:
            st.paused_until, st.pause_reason = None, None

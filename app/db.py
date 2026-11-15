from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(url: str | None = None) -> None:
    global _engine, _sessionmaker
    s = get_settings()
    url = url or s.database_url
    kwargs = {"pool_pre_ping": True}
    if not url.startswith("sqlite"):
        kwargs.update(pool_size=s.db_pool_size, max_overflow=s.db_max_overflow, pool_recycle=1800)
    else:
        # Local single-machine mode: the API and the monitor are two processes sharing one file.
        kwargs["connect_args"] = {"timeout": 30}
    _engine = create_async_engine(url, **kwargs)
    if url.startswith("sqlite") and ":memory:" not in url:
        @event.listens_for(_engine.sync_engine, "connect")
        def _wal(dbapi_conn, _record):  # readers never block the writer
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)


async def dispose() -> None:
    if _engine is not None:
        await _engine.dispose()


async def create_all() -> None:
    from app import models  # noqa: F401  (register tables)

    if _engine is None:
        init_engine()
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def engine():
    if _engine is None:
        init_engine()
    return _engine


_current: ContextVar[AsyncSession | None] = ContextVar("vale_session", default=None)


def current_session() -> AsyncSession | None:
    """The session of the unit of work running right now, if any. Code that must write while a
    caller's transaction is open joins it instead of opening a second connection, which SQLite
    (one writer at a time) would make wait until it times out."""
    return _current.get()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    async with _sessionmaker() as session:
        token = _current.set(session)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            _current.reset(token)

"""Bring the database schema up to date (also for databases first created without Alembic).

    python scripts/migrate.py

Local SQLite files made by AUTO_CREATE_TABLES before Alembic was used have the original tables
but no version stamp; they are stamped at the initial revision and then upgraded normally.
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import inspect  # noqa: E402

from app.db import engine  # noqa: E402

INITIAL = "c66dc969e49e"


async def _tables() -> set[str]:
    async with engine().connect() as conn:
        return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))


def main() -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    tables = asyncio.run(_tables())
    if "users" in tables and "alembic_version" not in tables:
        print("Existing database without a version stamp: marking it as the initial schema.")
        command.stamp(cfg, INITIAL)
    command.upgrade(cfg, "head")
    print("Database schema is up to date.")


if __name__ == "__main__":
    main()

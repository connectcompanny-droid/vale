"""Slot monitor process: `python run_monitor.py`.

You can run several (on one or more servers): they share the site pace and pauses through the
database and never check the same procedure at once.
"""

import asyncio
import logging

from app.citas.monitor import Monitor
from app.citas.providers.icp import ICPProvider
from app.config import get_settings
from app.db import create_all
from app.logging_setup import setup_logging

log = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    if get_settings().auto_create_tables:
        await create_all()
    await Monitor(ICPProvider()).run_forever()


if __name__ == "__main__":
    asyncio.run(main())

"""Entry point for the slot monitor: `python worker.py`."""

import asyncio
import logging

from app.citas.monitor import Monitor
from app.citas.providers.icp import ICPProvider
from app.db import create_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def main() -> None:
    await create_all()
    await Monitor(ICPProvider()).run_forever()


if __name__ == "__main__":
    asyncio.run(main())

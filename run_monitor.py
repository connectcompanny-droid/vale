"""Slot monitor process: `python run_monitor.py`.

You can run several (on one or more servers): they share each site's pace and pauses through the
database and never check the same procedure at once. One monitor runs per enabled appointment
system (ENABLED_SITES), each with its own pace, pauses and browser.
"""

import asyncio
import logging

from app.citas.catalog import enabled_sites
from app.citas.monitor import Monitor
from app.config import get_settings
from app.db import create_all
from app.logging_setup import setup_logging

log = logging.getLogger(__name__)


def build_monitors() -> list[Monitor]:
    monitors: list[Monitor] = []
    sites = enabled_sites()
    if "icp" in sites:
        from app.citas.providers.icp import ICPProvider

        monitors.append(Monitor(ICPProvider(), site="icp", primary=True))
    if "aeat" in sites:
        try:
            from app.citas.providers.aeat import AEATProvider
        except ImportError:
            log.error("ENABLED_SITES lists aeat, but its robot is not installed yet (run scripts/try_aeat.py first)")
        else:
            monitors.append(Monitor(AEATProvider(), site="aeat", primary=not monitors))
    return monitors


async def main() -> None:
    setup_logging()
    if get_settings().auto_create_tables:
        await create_all()
    monitors = build_monitors()
    if not monitors:
        raise SystemExit("No appointment system enabled: set ENABLED_SITES (e.g. icp)")
    await asyncio.gather(*(m.run_forever() for m in monitors))


if __name__ == "__main__":
    asyncio.run(main())

"""Slot monitor: checks availability, offers slots, books approved ones, expires stale offers.

Scaling model
- Traffic to the government site is capped GLOBALLY by `site_guard` (one visit every
  SITE_MIN_GAP_SECONDS across all processes). More clients or more servers never raise it.
- One check per (province, trámite) group, claimed atomically, so several monitor processes
  can run side by side without ever checking the same procedure twice.
- Groups are served least-recently-checked first, so none starves when there are many.
- A browser session kept open for an offer belongs to the process that opened it; only that
  process books it. If it dies, the offer expires and the client goes back to the queue.
- Firewall blocks and TLS errors pause the site for every process at once. Vale never tries to
  get around them.
"""

import asyncio
import logging

from sqlalchemy.orm import selectinload

from app.channels import Button, get_channel
from app.citas import service, site_guard
from app.citas.providers.base import CitaProvider, Slot
from app.config import get_settings
from app.db import session_scope
from app.i18n import t
from app.models import CitaRequest, RequestState

log = logging.getLogger(__name__)
TICK_SECONDS = 15


class Monitor:
    def __init__(self, provider: CitaProvider, site: str = site_guard.ICP):
        self.provider = provider
        self.site = site
        self.sem = asyncio.Semaphore(get_settings().monitor_max_concurrency)

    # ---------- pauses (shared by every process through the database) ----------

    async def paused(self) -> bool:
        return await site_guard.paused_until(self.site) is not None

    async def _pause(self, reason: str, request_id: int) -> None:
        s = get_settings()
        minutes = s.blocked_pause_minutes if reason == "blocked_by_site" else s.tls_pause_minutes
        until = await site_guard.pause(self.site, minutes, reason)
        log.error("ALERT %s on request %s: site %s paused for every process until %s. "
                  "Never bypass it (no proxies, no captcha solving, no TLS bypass).", reason, request_id, self.site, until)

    # ---------- loop ----------

    async def run_forever(self) -> None:
        log.info("monitor %s started", get_settings().instance_id)
        try:
            while True:
                try:
                    await self.tick()
                except Exception:
                    log.exception("monitor tick failed")
                await asyncio.sleep(TICK_SECONDS)
        finally:
            await self.provider.close()

    async def tick(self) -> None:
        await self.expire_offers()
        await self.provider.sweep()
        if await self.paused():
            return
        await self.book_approved()
        async with session_scope() as s:
            groups = [(rep.id, others) for rep, others in await service.due_groups(s, get_settings().monitor_max_concurrency)]
        tasks = []
        for rep_id, others in groups:
            if not await site_guard.acquire_visit(self.site):
                break  # global pace reached: the rest waits for the next tick
            async with session_scope() as s:
                if not await service.claim_group(s, rep_id):
                    continue  # another process took it
            tasks.append(self.check_one(rep_id, others))
        await asyncio.gather(*tasks)

    async def expire_offers(self) -> None:
        async with session_scope() as s:
            for offer in await service.expire_offers(s):
                if (hk := offer.raw.get("hold_key")) and self.provider.owns(hk):
                    await self.provider.release(hk)
                user = offer.request.user
                await get_channel().send_text(user.phone, t("offer_expired", user.lang))

    async def check_one(self, request_id: int, group: list[int] | None = None) -> None:
        """Check the site once for `request_id` (the longest-waiting client). When slots open, that
        client gets the offer; the others in the group stay due and are served next, in order."""
        async with self.sem:
            async with session_scope() as s:
                req = await s.get(CitaRequest, request_id, options=[selectinload(CitaRequest.user)])
                if not req or req.state != RequestState.monitoring:
                    return
                profile = {**await service.get_profile(s, req.user_id), "phone": req.user.phone}
                province, tramite, phone, lang = req.province, req.tramite_code, req.user.phone, req.user.lang

            result = await self.provider.check(request_id, province, tramite, profile)

            async with session_scope() as s:
                req = await s.get(CitaRequest, request_id)
                if not req or req.state != RequestState.monitoring:  # cancelled meanwhile
                    if result.hold_key:
                        await self.provider.release(result.hold_key)
                    return
                outcome = result.status if not result.reason else f"{result.status}:{result.reason}"
                await service.mark_checked(s, req, outcome)
                if group and result.status != "slots":
                    await service.mark_group_checked(s, group, outcome, via=request_id)
                if result.reason in ("tls_untrusted", "blocked_by_site"):
                    await self._pause(result.reason, request_id)
                if result.status == "slots" and result.slots:
                    slot = result.slots[0]
                    offer = await service.create_offer(s, req, slot.office, slot.starts_at, {**slot.raw, "hold_key": result.hold_key})
                    await get_channel().send_buttons(phone, t("offer", lang, office=offer.office, starts_at=offer.starts_at, ttl=get_settings().offer_ttl_minutes), [
                        Button(f"offer:{offer.id}:confirm", t("confirm", lang)),
                        Button(f"offer:{offer.id}:reject", t("reject", lang)),
                    ])
                elif result.status == "needs_human":
                    await service.mark_needs_human(s, req, result.reason)
                    await get_channel().send_text(phone, t("needs_human", lang, url=self.provider.public_url))

    async def book_approved(self) -> None:
        async with session_scope() as s:
            jobs = [(r.id, o.office, o.starts_at, dict(o.raw), r.user.phone, r.user.lang)
                    for r, o in await service.requests_to_book(s)]
        for request_id, office, starts_at, raw, phone, lang in jobs:
            hold_key = raw.get("hold_key")
            if hold_key and not self.provider.owns(hold_key):
                continue  # the browser session lives in another process
            if not await site_guard.acquire_visit(self.site, priority=True):
                return  # site paused: approval stays valid until the offer expires
            async with session_scope() as s:
                profile = await service.get_profile(s, (await s.get(CitaRequest, request_id)).user_id)
            result = await self.provider.book(request_id, Slot(office, starts_at, raw), profile, phone, hold_key)
            async with session_scope() as s:
                req = await s.get(CitaRequest, request_id)
                if result.status == "booked":
                    await service.mark_booked(s, req, result.reference)
                    await get_channel().send_text(phone, t("booked", lang, office=office, starts_at=starts_at, reference=result.reference or ""))
                elif result.reason in ("tls_untrusted", "blocked_by_site"):
                    await self._pause(result.reason, request_id)
                elif result.status == "slot_gone":
                    req.state = RequestState.monitoring
                    await get_channel().send_text(phone, t("offer_expired", lang))
                else:
                    await service.mark_needs_human(s, req, result.reason or result.status)
                    await get_channel().send_text(phone, t("needs_human", lang, url=self.provider.public_url))

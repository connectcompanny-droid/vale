"""Background worker: checks slots, offers them, books approved ones, expires stale offers."""

import asyncio
import logging

from sqlalchemy.orm import selectinload

from app.channels import Button, get_channel
from app.citas import service
from app.citas.providers.base import CitaProvider, Slot
from app.config import get_settings
from app.db import session_scope
from app.i18n import t
from app.models import CitaRequest, RequestState

log = logging.getLogger(__name__)
TICK_SECONDS = 30


class Monitor:
    def __init__(self, provider: CitaProvider):
        self.provider = provider
        self.sem = asyncio.Semaphore(get_settings().monitor_max_concurrency)

    async def run_forever(self) -> None:
        log.info("monitor started")
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
        await self.book_approved()
        async with session_scope() as s:
            due = [r.id for r in await service.due_for_check(s, get_settings().monitor_max_concurrency)]
        await asyncio.gather(*(self.check_one(rid) for rid in due))

    async def expire_offers(self) -> None:
        async with session_scope() as s:
            for offer in await service.expire_offers(s):
                if hk := offer.raw.get("hold_key"):
                    await self.provider.release(hk)
                user = offer.request.user
                await get_channel().send_text(user.phone, t("offer_expired", user.lang))

    async def check_one(self, request_id: int) -> None:
        async with self.sem:
            async with session_scope() as s:
                req = await s.get(CitaRequest, request_id, options=[selectinload(CitaRequest.user)])
                if not req or req.state.value != "monitoring":
                    return
                profile = {**await service.get_profile(s, req.user_id), "phone": req.user.phone}
                province, tramite, phone, lang = req.province, req.tramite_code, req.user.phone, req.user.lang

            result = await self.provider.check(request_id, province, tramite, profile)

            async with session_scope() as s:
                req = await s.get(CitaRequest, request_id)
                if not req or req.state.value != "monitoring":  # cancelled meanwhile
                    if result.hold_key:
                        await self.provider.release(result.hold_key)
                    return
                await service.mark_checked(s, req, result.status if not result.reason else f"{result.status}:{result.reason}")
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
            jobs = [(r.id, o.id, o.office, o.starts_at, dict(o.raw), r.user.phone, r.user.lang) for r, o in await service.requests_to_book(s)]
        for request_id, _offer_id, office, starts_at, raw, phone, lang in jobs:
            async with session_scope() as s:
                profile = await service.get_profile(s, (await s.get(CitaRequest, request_id)).user_id)
            result = await self.provider.book(request_id, Slot(office, starts_at, raw), profile, phone, raw.get("hold_key"))
            async with session_scope() as s:
                req = await s.get(CitaRequest, request_id)
                if result.status == "booked":
                    await service.mark_booked(s, req, result.reference)
                    await get_channel().send_text(phone, t("booked", lang, office=office, starts_at=starts_at, reference=result.reference or ""))
                elif result.status == "slot_gone":
                    req.state = RequestState.monitoring
                    await get_channel().send_text(phone, t("offer_expired", lang))
                else:
                    await service.mark_needs_human(s, req, result.reason or result.status)
                    await get_channel().send_text(phone, t("needs_human", lang, url=self.provider.public_url))

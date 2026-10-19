"""Background worker: checks slots, offers them, books approved ones, expires stale offers."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

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
        self.paused_until: datetime | None = None

    def _pause_for_block(self, request_id: int) -> None:
        """The site's firewall rejected us. Respect it: stop everything for hours. Never retry
        faster, rotate IPs or disguise the browser."""
        minutes = get_settings().blocked_pause_minutes
        self.paused_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        log.error("ALERT blocked_by_site on request %s: checks paused for %s min.", request_id, minutes)

    def _pause_for_tls(self, request_id: int) -> None:
        """Untrusted certificate: stop ALL checks for a while. Retrying would send personal data
        over a connection we can't trust, and hammering a broken site helps nobody."""
        minutes = get_settings().tls_pause_minutes
        self.paused_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        log.error("ALERT tls_untrusted on request %s: checks paused for %s min. Check the server's "
                  "network/antivirus/proxy and the browser channel; never disable TLS verification.", request_id, minutes)

    def paused(self) -> bool:
        return self.paused_until is not None and datetime.now(timezone.utc) < self.paused_until

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
        if self.paused():
            return
        await self.book_approved()
        async with session_scope() as s:
            due = [(rep.id, others) for rep, others in await service.due_groups(s, get_settings().monitor_max_concurrency)]
        await asyncio.gather(*(self.check_one(rid, others) for rid, others in due))

    async def expire_offers(self) -> None:
        async with session_scope() as s:
            for offer in await service.expire_offers(s):
                if hk := offer.raw.get("hold_key"):
                    await self.provider.release(hk)
                user = offer.request.user
                await get_channel().send_text(user.phone, t("offer_expired", user.lang))

    async def check_one(self, request_id: int, group: list[int] | None = None) -> None:
        """Check the site once for `request_id` (the longest-waiting client). When slots open, that
        client gets the offer; the others in the group stay due and are served next, in order."""
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
                outcome = result.status if not result.reason else f"{result.status}:{result.reason}"
                await service.mark_checked(s, req, outcome)
                if group and result.status != "slots":
                    await service.mark_group_checked(s, group, outcome, via=request_id)
                if result.reason == "tls_untrusted":
                    self._pause_for_tls(request_id)
                elif result.reason == "blocked_by_site":
                    self._pause_for_block(request_id)
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
                elif result.reason == "tls_untrusted":
                    # Approved offer stays approved; booking is retried after the pause.
                    self._pause_for_tls(request_id)
                    continue
                elif result.status == "slot_gone":
                    req.state = RequestState.monitoring
                    await get_channel().send_text(phone, t("offer_expired", lang))
                else:
                    await service.mark_needs_human(s, req, result.reason or result.status)
                    await get_channel().send_text(phone, t("needs_human", lang, url=self.provider.public_url))

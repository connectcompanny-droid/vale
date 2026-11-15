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
import time
from datetime import timedelta

from sqlalchemy.orm import selectinload

from app import audit
from app.channels import Button, get_channel
from app.citas import service, site_guard
from app.citas.catalog import codes_for
from app.citas.providers.base import CitaProvider, Slot
from app.config import get_settings
from app.db import session_scope
from app.i18n import t
from app.models import CitaRequest, OfferState, RequestState, SlotOffer

log = logging.getLogger(__name__)
TICK_SECONDS = 15
BOOKING_GRACE = timedelta(minutes=2)


async def _notify(phone: str, text: str, buttons: list[Button] | None = None) -> bool:
    """Message a client without letting a WhatsApp failure undo work that is already done."""
    try:
        ch = get_channel()
        if buttons:
            await ch.send_buttons(phone, text, buttons)
        else:
            await ch.send_text(phone, text)
        return True
    except Exception:
        log.exception("could not message a client")
        return False


class Monitor:
    def __init__(self, provider: CitaProvider, site: str = site_guard.ICP, primary: bool = True):
        """One monitor per appointment system: its own pace and pauses, and only its own trámites.
        `primary` runs the shared housekeeping (payment links, reminders, cleanup)."""
        self.provider = provider
        self.site = site
        self.primary = primary
        self.codes = codes_for(site)
        self.sem = asyncio.Semaphore(get_settings().monitor_max_concurrency)
        self._last_cleanup = float("-inf")

    # ---------- pauses (shared by every process through the database) ----------

    async def paused(self) -> bool:
        return await site_guard.paused_until(self.site) is not None

    async def _pause(self, reason: str, request_id: int) -> None:
        s = get_settings()
        minutes = s.blocked_pause_minutes if reason == "blocked_by_site" else s.tls_pause_minutes
        until = await site_guard.pause(self.site, minutes, reason)
        try:
            from app.notifications import notify_pause

            await notify_pause(max(1, round(minutes / 60)), codes=self.codes)
        except Exception:
            log.exception("could not notify clients about the pause")
        log.error("ALERT %s on request %s: site %s paused for every process until %s. "
                  "Never bypass it (no proxies, no captcha solving, no TLS bypass).", reason, request_id, self.site, until)

    # ---------- loop ----------

    async def run_forever(self) -> None:
        log.info("monitor %s started for %s", get_settings().instance_id, self.site)
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
        if self.primary:
            await self.housekeeping()
        if await self.paused():
            return
        await self.book_approved()
        async with session_scope() as s:
            groups = [(rep.id, others) for rep, others in await service.due_groups(s, get_settings().monitor_max_concurrency, self.codes)]
        tasks = []
        for rep_id, others in groups:
            if not await site_guard.acquire_visit(self.site):
                break  # global pace reached: the rest waits for the next tick
            async with session_scope() as s:
                if not await service.claim_group(s, rep_id):
                    continue  # another process took it
            tasks.append(self.check_one(rep_id, others))
        for outcome in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(outcome, Exception):
                log.error("check failed: %r", outcome)

    async def housekeeping(self) -> None:
        """Messages that don't touch the government site: payment links and reminders."""
        from app import billing, notifications

        jobs = [billing.send_pending_links, notifications.send_reminders, notifications.send_external_reminders]
        if time.monotonic() - self._last_cleanup > 6 * 3600:  # also covers setups without the worker's cron
            from app import maintenance

            self._last_cleanup = time.monotonic()
            jobs.append(maintenance.cleanup)
        for job in jobs:
            try:
                await job()
            except Exception:
                log.exception("%s failed", job.__name__)

    async def expire_offers(self) -> None:
        async with session_scope() as s:
            expired = [(o.raw.get("hold_key"), o.request.user.phone, o.request.user.lang) for o in await service.expire_offers(s, self.codes)]
        for hold_key, phone, lang in expired:  # committed first: a failed message never blocks the tick
            if hold_key and self.provider.owns(hold_key):
                await self.provider.release(hold_key)
            await _notify(phone, t("offer_expired", lang))

    async def check_one(self, request_id: int, group: list[int] | None = None) -> None:
        """Check the site once for `request_id` (the longest-waiting client). When slots open, that
        client gets the offer; the others in the group stay due and are served next, in order."""
        async with self.sem:
            async with session_scope() as s:
                req = await s.get(CitaRequest, request_id, options=[selectinload(CitaRequest.user)])
                if not req or req.state != RequestState.monitoring:
                    return
                profile = await service.get_profile(s, req.user_id)
                profile = {**profile, "phone": service.gov_phone(req.user.phone, profile)}
                province, tramite, phone, lang = req.province, req.tramite_code, req.user.phone, req.user.lang

            result = await self.provider.check(request_id, province, tramite, profile)
            needs_human = False

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
                if result.status == "slots" and result.slots:
                    slot = result.slots[0]
                    offer = await service.create_offer(s, req, slot.office, slot.starts_at, {**slot.raw, "hold_key": result.hold_key})
                    sent = await _notify(phone, t("offer", lang, office=offer.office, starts_at=offer.starts_at, ttl=get_settings().offer_ttl_minutes), [
                        Button(f"offer:{offer.id}:confirm", t("confirm", lang)),
                        Button(f"offer:{offer.id}:reject", t("reject", lang)),
                    ])
                    if not sent:
                        # The client never saw the offer: free the slot and undo it, so the next check offers again.
                        if result.hold_key:
                            await self.provider.release(result.hold_key)
                        raise RuntimeError(f"offer for request {request_id} could not be delivered")
                elif result.status == "needs_human":
                    await service.mark_needs_human(s, req, result.reason)
                    needs_human = True
            # After the transaction: these write through their own sessions.
            if result.reason in ("tls_untrusted", "blocked_by_site"):
                await self._pause(result.reason, request_id)
            if needs_human:
                await _notify(phone, t("needs_human", lang, url=self.provider.public_url))

    async def book_approved(self) -> None:
        async with session_scope() as s:
            jobs = [(r.id, o.id, o.office, o.starts_at, dict(o.raw), service.aware(o.expires_at), r.user.phone, r.user.lang)
                    for r, o in await service.requests_to_book(s, self.codes)]
        for request_id, offer_id, office, starts_at, raw, expires_at, phone, lang in jobs:
            hold_key = raw.get("hold_key")
            if not hold_key or not self.provider.owns(hold_key):
                # The browser session lives in another process, or died with a restart. Once the
                # offer's time is over nobody can book it: put the client back in the queue.
                if expires_at + BOOKING_GRACE < service.utcnow():
                    async with session_scope() as s:
                        req = await s.get(CitaRequest, request_id)
                        offer = await s.get(SlotOffer, offer_id)
                        if req and req.state == RequestState.booking:
                            req.state = RequestState.monitoring
                            offer.state = OfferState.expired
                            audit.record(s, "booking_abandoned", req.user_id, req.id, offer_id=offer_id)
                    await _notify(phone, t("offer_expired", lang))
                continue
            if not await site_guard.acquire_visit(self.site, priority=True):
                return  # site paused: approval stays valid until the offer expires
            async with session_scope() as s:
                profile = await service.get_profile(s, (await s.get(CitaRequest, request_id)).user_id)
            result = await self.provider.book(request_id, Slot(office, starts_at, raw), profile, service.gov_phone(phone, profile), hold_key)
            message = pause_reason = None
            async with session_scope() as s:
                req = await s.get(CitaRequest, request_id)
                if result.status == "booked":
                    await service.mark_booked(s, req, result.reference)
                    message = t("booked", lang, office=office, starts_at=starts_at, reference=result.reference or "")
                elif result.reason in ("tls_untrusted", "blocked_by_site"):
                    pause_reason = result.reason
                elif result.status == "slot_gone":
                    req.state = RequestState.monitoring
                    message = t("offer_expired", lang)
                else:
                    await service.mark_needs_human(s, req, result.reason or result.status)
                    message = t("needs_human", lang, url=self.provider.public_url)
            if pause_reason:
                await self._pause(pause_reason, request_id)
            if message:  # sent after the result is saved: a booked cita is never lost to a failed message
                await _notify(phone, message)

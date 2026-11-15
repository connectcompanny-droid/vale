"""Proactive messages: the day-before reminder and the "site paused" notice.

Both are claimed with an atomic UPDATE before sending, so several monitor processes never send
the same message twice.
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import session_scope
from app.i18n import t
from app.models import CitaRequest, ExternalAppointment, OfferState, RequestState

log = logging.getLogger(__name__)
MADRID = ZoneInfo("Europe/Madrid")
PAUSE_NOTICE_EVERY = timedelta(hours=20)  # "at most once a day"


def appointment_date(starts_at: str) -> date | None:
    """Date of a slot as the site shows it, e.g. "23/10/2026 10:00" or "23-10-2026"."""
    m = re.search(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", starts_at or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


async def send_reminders(now: datetime | None = None) -> int:
    """Remind booked clients the day before their cita, from REMINDER_HOUR (Madrid time)."""
    from app.channels import get_channel

    local = (now or datetime.now(timezone.utc)).astimezone(MADRID)
    if local.hour < get_settings().reminder_hour:
        return 0
    tomorrow = local.date() + timedelta(days=1)
    async with session_scope() as s:
        booked = (await s.scalars(
            select(CitaRequest).options(selectinload(CitaRequest.offers), selectinload(CitaRequest.user))
            .where(CitaRequest.state == RequestState.booked, CitaRequest.reminded_at.is_(None))
        )).all()
        due = []
        for r in booked:
            offer = next((o for o in reversed(r.offers) if o.state == OfferState.approved), None)
            if offer and appointment_date(offer.starts_at) == tomorrow:
                due.append((r.id, r.user.phone, r.user.lang, offer.office, offer.starts_at))
    sent = 0
    for req_id, phone, lang, office, starts_at in due:
        async with session_scope() as s:
            res = await s.execute(update(CitaRequest).where(CitaRequest.id == req_id, CitaRequest.reminded_at.is_(None))
                                  .values(reminded_at=datetime.now(timezone.utc)))
            if res.rowcount != 1:
                continue
        try:
            await get_channel().send_text(phone, t("reminder", lang, office=office, starts_at=starts_at))
            sent += 1
        except Exception:
            log.exception("reminder for request %s failed", req_id)
    return sent


async def send_external_reminders(now: datetime | None = None) -> int:
    """Day-before reminders for appointments clients booked themselves (e.g. the digital certificate)."""
    from app.channels import get_channel

    local = (now or datetime.now(timezone.utc)).astimezone(MADRID)
    if local.hour < get_settings().reminder_hour:
        return 0
    tomorrow = local.date() + timedelta(days=1)
    async with session_scope() as s:
        rows = (await s.scalars(select(ExternalAppointment).options(selectinload(ExternalAppointment.user))
                                .where(ExternalAppointment.reminded_at.is_(None)))).all()
        due = [(a.id, a.user.phone, a.user.lang, a.what, a.starts_at, a.place) for a in rows if appointment_date(a.starts_at) == tomorrow]
    sent = 0
    for appt_id, phone, lang, what, starts_at, place in due:
        async with session_scope() as s:
            res = await s.execute(update(ExternalAppointment).where(ExternalAppointment.id == appt_id, ExternalAppointment.reminded_at.is_(None))
                                  .values(reminded_at=datetime.now(timezone.utc)))
            if res.rowcount != 1:
                continue
        try:
            await get_channel().send_text(phone, t("external_reminder", lang, what=what, starts_at=starts_at, place=place or "-"))
            sent += 1
        except Exception:
            log.exception("external reminder %s failed", appt_id)
    return sent


async def notify_pause(hours: int, now: datetime | None = None, codes: set[str] | None = None) -> int:
    """Tell waiting clients the site is paused, at most once a day per request, one message per person."""
    from app.channels import get_channel

    now = now or datetime.now(timezone.utc)
    cutoff = now - PAUSE_NOTICE_EVERY
    waiting = (RequestState.monitoring, RequestState.offer_pending)
    async with session_scope() as s:
        ids = list((await s.scalars(select(CitaRequest.id).where(
            CitaRequest.state.in_(waiting), *([CitaRequest.tramite_code.in_(codes)] if codes else []),
            or_(CitaRequest.pause_notified_at.is_(None), CitaRequest.pause_notified_at < cutoff),
        ))).all())
    people: dict[str, str] = {}
    for req_id in ids:
        async with session_scope() as s:
            res = await s.execute(update(CitaRequest).where(
                CitaRequest.id == req_id,
                or_(CitaRequest.pause_notified_at.is_(None), CitaRequest.pause_notified_at < cutoff),
            ).values(pause_notified_at=now))
            if res.rowcount != 1:
                continue
            req = await s.get(CitaRequest, req_id, options=[selectinload(CitaRequest.user)])
            people[req.user.phone] = req.user.lang
    for phone, lang in people.items():
        try:
            await get_channel().send_text(phone, t("site_paused", lang, hours=hours))
        except Exception:
            log.exception("pause notice to a client failed")
    return len(people)

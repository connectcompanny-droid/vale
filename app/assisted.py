"""Assisted mode: when the government site refuses automated access, Vale still does everything
around the booking, and the booking itself is done by a person.

- "self": Vale sends the client a booking kit (link, which options to pick, their own details
  ready to copy) and nudges them once a day until they tell Vale the appointment is booked.
- "operator": the request shows up in the operator panel (/admin/operador); a person on Vale's
  side looks for slots in a normal browser, offers one to the client in the chat, and books it
  only after the client confirms.

Neither path touches the government site automatically.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.orm import selectinload

from app.citas import service
from app.citas.catalog import ALL_PROVINCES, TRAMITES
from app.citas.providers.icp import BASE_URL
from app.config import get_settings
from app.db import session_scope
from app.i18n import t
from app.models import CitaRequest, RequestState
from app.notifications import MADRID

log = logging.getLogger(__name__)

DOC_TYPE = {"es": ("N.I.E.", "PASAPORTE"), "pt": ("N.I.E.", "PASAPORTE"), "en": ("N.I.E.", "PASAPORTE")}


def _option_hint(tramite_code: str) -> str:
    # The words the site's option contains, e.g. "ASIGNACIÓN NIE" for "POLICÍA-CERTIFICADOS Y ASIGNACIÓN NIE".
    return TRAMITES[tramite_code].match[0].replace("ASIGNACION", "ASIGNACIÓN")


def booking_kit(req: CitaRequest, profile: dict, lang: str) -> str:
    tramite = TRAMITES[req.tramite_code]
    nie = str(profile.get("nie") or "")
    number = nie or str(profile.get("document_number") or "")
    contact = []
    phone = service.gov_phone(req.user.phone, profile)
    if phone:
        contact.append(f"• Teléfono: {phone}" if lang == "es" else f"• Telefone: {phone}" if lang == "pt" else f"• Phone: {phone}")
    if profile.get("email"):
        contact.append(f"• Email: {profile['email']}")
    return t("booking_kit", lang,
             tramite=tramite.label.get(lang, tramite.label["es"]),
             province=ALL_PROVINCES.get(req.province, req.province.title()),
             url=BASE_URL, option=_option_hint(req.tramite_code),
             doc_type=DOC_TYPE[lang if lang in DOC_TYPE else "es"][0 if nie else 1],
             number=number, name=str(profile.get("full_name", "")).upper(),
             nationality=str(profile.get("nationality", "")).upper(), contact="\n".join(contact))


async def _claim(req_id: int, field, cutoff: datetime | None) -> bool:
    async with session_scope() as s:
        cond = field.is_(None) if cutoff is None else or_(field.is_(None), field < cutoff)
        res = await s.execute(update(CitaRequest).where(CitaRequest.id == req_id, cond)
                              .values({field.key: datetime.now(timezone.utc)}))
        return res.rowcount == 1


async def send_kits() -> int:
    """Booking kit for self-mode requests that just became ready; a short note for operator ones."""
    if get_settings().auto_checks:
        return 0
    from app.channels import get_channel

    async with session_scope() as s:
        reqs = (await s.scalars(select(CitaRequest).options(selectinload(CitaRequest.user))
                                .where(CitaRequest.state == RequestState.monitoring, CitaRequest.kit_sent_at.is_(None)))).all()
        jobs = []
        for r in reqs:
            if TRAMITES[r.tramite_code].site != "icp":
                continue  # other systems have their own guidance in the conversation
            profile = await service.get_profile(s, r.user_id)
            text = booking_kit(r, profile, r.user.lang) if r.mode == "self" else t("operator_waiting", r.user.lang)
            jobs.append((r.id, r.user.phone, text))
    sent = 0
    for req_id, phone, text in jobs:
        if not await _claim(req_id, CitaRequest.kit_sent_at, None):
            continue
        try:
            await get_channel().send_text(phone, text)
            sent += 1
        except Exception:
            log.exception("booking kit for request %s failed", req_id)
    return sent


async def send_nudges(now: datetime | None = None) -> int:
    """Once a day, after REMINDER_HOUR in Madrid, ask self-mode clients whether they got it."""
    s_ = get_settings()
    if s_.auto_checks:
        return 0
    from app.channels import get_channel

    now = now or datetime.now(timezone.utc)
    if now.astimezone(MADRID).hour < s_.reminder_hour:
        return 0
    cutoff = now - timedelta(hours=20)
    oldest = now - timedelta(days=s_.self_nudge_days)
    async with session_scope() as s:
        reqs = (await s.scalars(select(CitaRequest).options(selectinload(CitaRequest.user)).where(
            CitaRequest.state == RequestState.monitoring, CitaRequest.mode == "self",
            CitaRequest.kit_sent_at.is_not(None), CitaRequest.kit_sent_at < cutoff, CitaRequest.created_at > oldest,
            or_(CitaRequest.last_nudge_at.is_(None), CitaRequest.last_nudge_at < cutoff),
        ))).all()
        jobs = [(r.id, r.user.phone, r.user.lang, TRAMITES[r.tramite_code]) for r in reqs if TRAMITES[r.tramite_code].site == "icp"]
    sent = 0
    for req_id, phone, lang, tramite in jobs:
        if not await _claim(req_id, CitaRequest.last_nudge_at, cutoff):
            continue
        try:
            await get_channel().send_text(phone, t("booking_nudge", lang, tramite=tramite.label.get(lang, tramite.label["es"]), url=BASE_URL))
            sent += 1
        except Exception:
            log.exception("nudge for request %s failed", req_id)
    return sent


async def notify_operator(text: str) -> None:
    number = "".join(c for c in get_settings().operator_phone if c.isdigit())
    if not number:
        return
    from app.channels import get_channel

    try:
        await get_channel().send_text(number, text)
    except Exception:
        log.exception("could not notify the operator")

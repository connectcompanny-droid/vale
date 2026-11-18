"""Deterministic business operations. Neither the LLM nor the browser worker touches the
database directly; both go through these functions, which enforce the rules:

- one active request per user and procedure (no slot hoarding),
- nothing is booked without an approved offer,
- every action is written to the audit log,
- deleting a user removes all personal data.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import audit
from app.citas.catalog import PROVINCES, TRAMITES, tramite_enabled
from app.config import get_settings
from app.crypto import decrypt_json, encrypt_json
from app.documents.extractor import ExtractedDocument
from app.models import ACTIVE_STATES, AuditEvent, CitaRequest, Document, OfferState, RequestState, SlotOffer, User, WebMessage, ExternalAppointment


class RuleError(Exception):
    """A request that breaks a business rule; the message is safe to show the agent."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes; treat them as UTC.
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


# ---------- users ----------

async def find_user(session: AsyncSession, phone: str) -> User | None:
    return await session.scalar(select(User).where(User.phone == phone))


async def get_or_create_user(session: AsyncSession, phone: str, name: str | None = None, lang: str = "es") -> tuple[User, bool]:
    user = await session.scalar(select(User).where(User.phone == phone))
    if user:
        return user, False
    user = User(phone=phone, name=name, lang=lang, history=[])
    session.add(user)
    await session.flush()
    return user, True


async def give_consent(session: AsyncSession, user: User) -> None:
    user.consent_at = utcnow()
    audit.record(session, "consent_given", user.id)


async def delete_user_data(session: AsyncSession, user: User) -> None:
    """Right to erasure: remove documents, requests, offers and the user; keep only an anonymous audit trail."""
    uid = user.id
    req_ids = (await session.scalars(select(CitaRequest.id).where(CitaRequest.user_id == uid))).all()
    if req_ids:
        await session.execute(delete(SlotOffer).where(SlotOffer.request_id.in_(req_ids)))
    await session.execute(delete(CitaRequest).where(CitaRequest.user_id == uid))
    await session.execute(delete(Document).where(Document.user_id == uid))
    await session.execute(delete(ExternalAppointment).where(ExternalAppointment.user_id == uid))
    await session.execute(delete(WebMessage).where(WebMessage.user_key == user.phone))
    await session.execute(update(AuditEvent).where(AuditEvent.user_id == uid).values(user_id=None, detail={}))
    await session.delete(user)
    audit.record(session, "user_deleted")


# ---------- documents / profile ----------

PERSONAL_FIELDS = ("full_name", "document_number", "nie", "nationality", "birth_date", "expiry_date")


async def save_pending_data(session: AsyncSession, user: User, fields: dict, source: str, kind: str = "typed") -> Document:
    """Store data read from a photo, typed text or a voice note as PENDING. It is merged over the
    current confirmed profile, so a correction only needs the field that changed."""
    if user.consent_at is None:
        raise RuleError("consent_required")
    clean = {k: (str(v).strip().upper() if k in ("full_name", "document_number", "nie", "nationality") else v)
             for k, v in fields.items() if k in PERSONAL_FIELDS and v not in (None, "", [])}
    if not clean:
        raise RuleError("no_personal_data")
    base = {k: v for k, v in (await get_profile(session, user.id)).items() if k in PERSONAL_FIELDS}
    pending = await latest_pending(session, user.id)
    if pending:
        base.update({k: v for k, v in decrypt_json(pending.payload_encrypted).items() if k in PERSONAL_FIELDS})
        await session.delete(pending)  # only one pending read-back at a time
    base.update(clean)
    doc = Document(user_id=user.id, kind=kind, source=source, confirmed=False, payload_encrypted=encrypt_json(base))
    session.add(doc)
    await session.flush()
    audit.record(session, "data_pending", user.id, source=source, fields=sorted(clean))
    return doc


async def store_document(session: AsyncSession, user: User, doc: ExtractedDocument) -> Document:
    """A document photo becomes pending data awaiting the user's confirmation."""
    return await save_pending_data(session, user, doc.model_dump(), source="photo", kind=doc.document_kind)


async def latest_pending(session: AsyncSession, user_id: int) -> Document | None:
    return await session.scalar(
        select(Document).where(Document.user_id == user_id, Document.confirmed.is_(False))
        .order_by(Document.created_at.desc(), Document.id.desc()).limit(1)
    )


def pending_fields(doc: Document) -> dict:
    return decrypt_json(doc.payload_encrypted)


async def confirm_pending(session: AsyncSession, user: User, doc_id: int) -> list[CitaRequest]:
    doc = await session.get(Document, doc_id)
    if not doc or doc.user_id != user.id or doc.confirmed:
        raise RuleError("pending_data_not_found")
    doc.confirmed = True
    audit.record(session, "data_confirmed", user.id, source=doc.source)
    await session.flush()
    return await promote_ready_requests(session, user)


async def set_contact_email(session: AsyncSession, user: User, email: str) -> None:
    session.add(Document(user_id=user.id, kind="contact", source="contact", confirmed=True, payload_encrypted=encrypt_json({"email": email})))
    audit.record(session, "contact_email_set", user.id)


async def set_contact_phone(session: AsyncSession, user: User, phone: str) -> list[CitaRequest]:
    session.add(Document(user_id=user.id, kind="contact", source="contact", confirmed=True, payload_encrypted=encrypt_json({"phone": phone})))
    audit.record(session, "contact_phone_set", user.id)
    await session.flush()
    return await promote_ready_requests(session, user)


async def get_profile(session: AsyncSession, user_id: int) -> dict:
    """Merge the user's CONFIRMED documents (newest wins) into one decrypted profile."""
    docs = (await session.scalars(
        select(Document).where(Document.user_id == user_id, Document.confirmed.is_(True)).order_by(Document.created_at, Document.id)
    )).all()
    profile: dict = {}
    for d in docs:
        for k, v in decrypt_json(d.payload_encrypted).items():
            if v not in (None, "", []):
                profile[k] = v
    return profile


def is_web(phone: str | None) -> bool:
    return bool(phone) and phone.startswith("web:")


def missing_fields(profile: dict, tramite_code: str, user_phone: str | None = None) -> list[str]:
    """Fields still needed. The government form asks for a phone number: WhatsApp clients already
    have one, website clients must give it."""
    missing = [f for f in TRAMITES[tramite_code].needs if not profile.get(f)]
    if is_web(user_phone) and not profile.get("phone"):
        missing.append("phone")
    return missing


def gov_phone(user_phone: str, profile: dict) -> str:
    """The number typed into the government form."""
    return str(profile.get("phone") or "") if is_web(user_phone) else user_phone


def ready_state(req_or_paid, profile: dict, tramite_code: str, user_phone: str | None = None) -> RequestState:
    """Where a request should be given the client's data: missing data, unpaid activation, or watching."""
    from app import billing

    if missing_fields(profile, tramite_code, user_phone):
        return RequestState.awaiting_documents
    paid = bool(getattr(req_or_paid, "activation_paid_at", None))
    if billing.enabled() and not paid:
        return RequestState.awaiting_payment
    return RequestState.monitoring


async def promote_ready_requests(session: AsyncSession, user: User) -> list[CitaRequest]:
    profile = await get_profile(session, user.id)
    waiting = (await session.scalars(
        select(CitaRequest).where(CitaRequest.user_id == user.id, CitaRequest.state == RequestState.awaiting_documents)
    )).all()
    promoted = []
    for req in waiting:
        state = ready_state(req, profile, req.tramite_code, user.phone)
        if state == RequestState.monitoring:
            req.state = state
            audit.record(session, "monitoring_started", user.id, req.id)
            promoted.append(req)
        elif state == RequestState.awaiting_payment:
            req.state = state  # the monitor sends the payment link on its next tick
            audit.record(session, "awaiting_payment", user.id, req.id)
    return promoted


# ---------- requests ----------

async def active_requests(session: AsyncSession, user_id: int) -> list[CitaRequest]:
    return list((await session.scalars(
        select(CitaRequest).where(CitaRequest.user_id == user_id, CitaRequest.state.in_(ACTIVE_STATES)).order_by(CitaRequest.id)
    )).all())


async def create_request(session: AsyncSession, user: User, province: str, tramite_code: str) -> CitaRequest:
    if user.consent_at is None:
        raise RuleError("consent_required")
    if province not in PROVINCES:
        raise RuleError(f"province_not_supported: {province}")
    if tramite_code not in TRAMITES:
        raise RuleError(f"tramite_not_supported: {tramite_code}")
    if not tramite_enabled(tramite_code):
        raise RuleError(f"tramite_not_bookable_yet: {tramite_code}")
    for r in await active_requests(session, user.id):
        if r.tramite_code == tramite_code:
            raise RuleError("already_active_for_this_tramite")
    profile = await get_profile(session, user.id)
    state = ready_state(None, profile, tramite_code, user.phone)
    mode = get_settings().default_booking_mode if get_settings().default_booking_mode in ("self", "operator") else "self"
    if mode == "operator" and not get_settings().operator_enabled:
        mode = "self"
    req = CitaRequest(user_id=user.id, province=province, tramite_code=tramite_code, state=state, active_key="1", mode=mode)
    session.add(req)
    await session.flush()
    audit.record(session, "request_created", user.id, req.id, province=province, tramite=tramite_code, state=state.value)
    return req


def _close(req: CitaRequest, state: RequestState) -> None:
    req.state = state
    req.active_key = f"closed-{req.id}"  # frees the "one active per tramite" slot


async def cancel_request(session: AsyncSession, user: User, request_id: int) -> CitaRequest:
    req = await session.get(CitaRequest, request_id)
    if not req or req.user_id != user.id or req.state not in ACTIVE_STATES:
        raise RuleError("request_not_found")
    _close(req, RequestState.cancelled)
    audit.record(session, "request_cancelled", user.id, req.id)
    await session.flush()
    return req


async def due_groups(session: AsyncSession, limit: int, codes: set[str] | None = None) -> list[tuple[CitaRequest, list[int]]]:
    """One check per (province, trámite), not per client: the site sees a single visit no matter
    how many clients wait for the same procedure. The representative is the client who has waited
    longest (first come, first served); the rest of the group shares the result."""
    cutoff = utcnow() - timedelta(minutes=get_settings().monitor_min_interval_minutes)
    rows = (await session.scalars(
        select(CitaRequest).where(CitaRequest.state == RequestState.monitoring).order_by(CitaRequest.created_at, CitaRequest.id)
    )).all()
    groups: dict[tuple[str, str], list[CitaRequest]] = {}
    for r in rows:
        if codes is not None and r.tramite_code not in codes:
            continue  # belongs to another appointment system
        groups.setdefault((r.province, r.tramite_code), []).append(r)
    due = []
    for members in groups.values():
        last = max((aware(m.last_checked_at) for m in members if m.last_checked_at), default=None)
        if last is None or last <= cutoff:
            due.append((last, -len(members), members[0], [m.id for m in members[1:]]))
    # Never checked first, then least recently checked; ties go to the group with more clients.
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    due.sort(key=lambda d: (d[0] or epoch, d[1]))
    return [(rep, others) for _, _, rep, others in due[:limit]]


async def claim_group(session: AsyncSession, request_id: int) -> bool:
    """Atomically claim a group's check, so two monitor processes never check the same
    procedure at once. Works because the representative's own timestamp moves first."""
    cutoff = utcnow() - timedelta(minutes=get_settings().monitor_min_interval_minutes)
    res = await session.execute(
        update(CitaRequest)
        .where(CitaRequest.id == request_id, CitaRequest.state == RequestState.monitoring,
               or_(CitaRequest.last_checked_at.is_(None), CitaRequest.last_checked_at <= cutoff))
        .values(last_checked_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1


async def mark_checked(session: AsyncSession, req: CitaRequest, outcome: str) -> None:
    req.last_checked_at = utcnow()
    req.checks += 1
    audit.record(session, "checked", req.user_id, req.id, outcome=outcome)


async def mark_group_checked(session: AsyncSession, request_ids: list[int], outcome: str, via: int) -> None:
    for rid in request_ids:
        req = await session.get(CitaRequest, rid)
        if req and req.state == RequestState.monitoring:
            await mark_checked(session, req, f"{outcome} (shared check #{via})")


async def mark_needs_human(session: AsyncSession, req: CitaRequest, reason: str) -> None:
    req.state = RequestState.needs_human
    req.note = reason
    audit.record(session, "needs_human", req.user_id, req.id, reason=reason)


async def mark_failed(session: AsyncSession, req: CitaRequest, reason: str) -> None:
    _close(req, RequestState.failed)
    req.note = reason
    audit.record(session, "failed", req.user_id, req.id, reason=reason)


# ---------- offers & approval ----------

async def create_offer(session: AsyncSession, req: CitaRequest, office: str, starts_at: str, raw: dict | None = None) -> SlotOffer:
    offer = SlotOffer(
        request_id=req.id, office=office, starts_at=starts_at, raw=raw or {},
        expires_at=utcnow() + timedelta(minutes=get_settings().offer_ttl_minutes),
    )
    session.add(offer)
    req.state = RequestState.offer_pending
    await session.flush()
    audit.record(session, "offer_created", req.user_id, req.id, offer_id=offer.id, office=office, starts_at=starts_at)
    return offer


async def _owned_pending_offer(session: AsyncSession, user: User, offer_id: int) -> SlotOffer:
    offer = await session.get(SlotOffer, offer_id, options=[selectinload(SlotOffer.request)])
    if not offer or offer.request.user_id != user.id:
        raise RuleError("offer_not_found")
    if offer.state != OfferState.pending:
        raise RuleError(f"offer_{offer.state.value}")
    if aware(offer.expires_at) < utcnow():
        raise RuleError("offer_expired")
    return offer


async def latest_pending_offer_id(session: AsyncSession, user_id: int) -> int | None:
    """The newest pending offer of this user (for clients answering an offer by typing)."""
    return await session.scalar(
        select(SlotOffer.id).join(CitaRequest, SlotOffer.request_id == CitaRequest.id)
        .where(CitaRequest.user_id == user_id, SlotOffer.state == OfferState.pending)
        .order_by(SlotOffer.id.desc()).limit(1)
    )


async def approve_offer(session: AsyncSession, user: User, offer_id: int) -> SlotOffer:
    """The only path to a booking: the user's explicit tap on Confirm."""
    offer = await _owned_pending_offer(session, user, offer_id)
    offer.state = OfferState.approved
    offer.request.state = RequestState.booking
    audit.record(session, "offer_approved", user.id, offer.request_id, offer_id=offer.id)
    return offer


async def reject_offer(session: AsyncSession, user: User, offer_id: int) -> SlotOffer:
    offer = await _owned_pending_offer(session, user, offer_id)
    offer.state = OfferState.rejected
    offer.request.state = RequestState.monitoring
    audit.record(session, "offer_rejected", user.id, offer.request_id, offer_id=offer.id)
    return offer


async def expire_offers(session: AsyncSession, codes: set[str] | None = None) -> list[SlotOffer]:
    pending = (await session.scalars(
        select(SlotOffer).options(selectinload(SlotOffer.request).selectinload(CitaRequest.user)).where(SlotOffer.state == OfferState.pending)
    )).all()
    expired = []
    for o in pending:
        if codes is not None and o.request.tramite_code not in codes:
            continue
        if aware(o.expires_at) < utcnow():
            o.state = OfferState.expired
            if o.request.state == RequestState.offer_pending:
                o.request.state = RequestState.monitoring
            audit.record(session, "offer_expired", o.request.user_id, o.request_id, offer_id=o.id)
            expired.append(o)
    return expired


async def requests_to_book(session: AsyncSession, codes: set[str] | None = None) -> list[tuple[CitaRequest, SlotOffer]]:
    reqs = (await session.scalars(
        select(CitaRequest).options(selectinload(CitaRequest.offers), selectinload(CitaRequest.user)).where(CitaRequest.state == RequestState.booking)
    )).all()
    out = []
    for r in reqs:
        if codes is not None and r.tramite_code not in codes:
            continue
        approved = [o for o in r.offers if o.state == OfferState.approved]
        if approved:
            out.append((r, approved[-1]))
    return out


async def mark_booked(session: AsyncSession, req: CitaRequest, reference: str | None) -> None:
    _close(req, RequestState.booked)
    req.note = reference
    audit.record(session, "booked", req.user_id, req.id, reference=reference)


# ---------- assisted mode (the robot does not book: the client or an operator does) ----------

async def set_mode(session: AsyncSession, user: User, request_id: int, mode: str) -> CitaRequest:
    if mode not in ("self", "operator"):
        raise RuleError("invalid_mode")
    if mode == "operator" and not get_settings().operator_enabled:
        raise RuleError("operator_not_available")
    req = await session.get(CitaRequest, request_id)
    if not req or req.user_id != user.id or req.state not in ACTIVE_STATES:
        raise RuleError("request_not_found")
    req.mode = mode
    audit.record(session, "mode_set", user.id, req.id, mode=mode)
    return req


async def record_client_booking(session: AsyncSession, user: User, request_id: int, starts_at: str, office: str,
                                reference: str | None = None) -> CitaRequest:
    """The client booked it themselves (self mode). Stored like any booked cita, so the
    day-before reminder and the status work the same way."""
    req = await session.get(CitaRequest, request_id)
    if not req or req.user_id != user.id or req.state not in ACTIVE_STATES:
        raise RuleError("request_not_found")
    session.add(SlotOffer(request_id=req.id, office=office[:200] or "-", starts_at=starts_at[:60], raw={"by": "client"},
                          state=OfferState.approved, expires_at=utcnow()))
    await mark_booked(session, req, reference)
    audit.record(session, "booked_by_client", user.id, req.id)
    return req


async def operator_queue(session: AsyncSession) -> list[CitaRequest]:
    """What the operator works on: operator-mode requests waiting for a slot, offers out, and
    confirmed offers to book now (these first)."""
    rows = (await session.scalars(
        select(CitaRequest).options(selectinload(CitaRequest.user), selectinload(CitaRequest.offers))
        .where(CitaRequest.state.in_((RequestState.booking, RequestState.offer_pending, RequestState.monitoring)))
        .order_by(CitaRequest.created_at)
    )).all()
    order = {RequestState.booking: 0, RequestState.offer_pending: 1, RequestState.monitoring: 2}
    rows = [r for r in rows if r.mode == "operator" or r.state == RequestState.booking]
    return sorted(rows, key=lambda r: order[r.state])


async def operator_offer(session: AsyncSession, request_id: int, office: str, starts_at: str) -> SlotOffer:
    req = await session.get(CitaRequest, request_id)
    if not req or req.state != RequestState.monitoring:
        raise RuleError("request_not_waiting")
    offer = await create_offer(session, req, office.strip()[:200], starts_at.strip()[:60], {"by": "operator"})
    return offer


async def operator_booked(session: AsyncSession, request_id: int, reference: str) -> tuple[CitaRequest, SlotOffer]:
    req = await session.get(CitaRequest, request_id, options=[selectinload(CitaRequest.offers)])
    if not req or req.state != RequestState.booking:
        raise RuleError("not_confirmed_by_client")
    offer = next(o for o in reversed(req.offers) if o.state == OfferState.approved)
    await mark_booked(session, req, reference.strip()[:120] or None)
    audit.record(session, "booked_by_operator", req.user_id, req.id)
    return req, offer


async def operator_slot_gone(session: AsyncSession, request_id: int) -> CitaRequest:
    req = await session.get(CitaRequest, request_id, options=[selectinload(CitaRequest.offers)])
    if not req or req.state not in (RequestState.booking, RequestState.offer_pending):
        raise RuleError("nothing_to_release")
    for o in req.offers:
        if o.state in (OfferState.pending, OfferState.approved):
            o.state = OfferState.expired
    req.state = RequestState.monitoring
    audit.record(session, "slot_gone_operator", req.user_id, req.id)
    return req


async def operator_checked(session: AsyncSession, request_id: int) -> None:
    req = await session.get(CitaRequest, request_id)
    if req and req.state == RequestState.monitoring:
        await mark_checked(session, req, "no_slots:operator")

"""Deterministic business operations. Neither the LLM nor the browser worker touches the
database directly; both go through these functions, which enforce the rules:

- one active request per user and procedure (no slot hoarding),
- nothing is booked without an approved offer,
- every action is written to the audit log,
- deleting a user removes all personal data.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import audit
from app.citas.catalog import PROVINCES, TRAMITES
from app.config import get_settings
from app.crypto import decrypt_json, encrypt_json
from app.documents.extractor import ExtractedDocument
from app.models import ACTIVE_STATES, AuditEvent, CitaRequest, Document, OfferState, RequestState, SlotOffer, User


class RuleError(Exception):
    """A request that breaks a business rule; the message is safe to show the agent."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes; treat them as UTC.
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


# ---------- users ----------

async def get_or_create_user(session: AsyncSession, phone: str, name: str | None = None, lang: str = "pt") -> tuple[User, bool]:
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


def missing_fields(profile: dict, tramite_code: str) -> list[str]:
    return [f for f in TRAMITES[tramite_code].needs if not profile.get(f)]


async def promote_ready_requests(session: AsyncSession, user: User) -> list[CitaRequest]:
    profile = await get_profile(session, user.id)
    waiting = (await session.scalars(
        select(CitaRequest).where(CitaRequest.user_id == user.id, CitaRequest.state == RequestState.awaiting_documents)
    )).all()
    promoted = []
    for req in waiting:
        if not missing_fields(profile, req.tramite_code):
            req.state = RequestState.monitoring
            audit.record(session, "monitoring_started", user.id, req.id)
            promoted.append(req)
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
    for r in await active_requests(session, user.id):
        if r.tramite_code == tramite_code:
            raise RuleError("already_active_for_this_tramite")
    profile = await get_profile(session, user.id)
    state = RequestState.monitoring if not missing_fields(profile, tramite_code) else RequestState.awaiting_documents
    req = CitaRequest(user_id=user.id, province=province, tramite_code=tramite_code, state=state, active_key="1")
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


async def due_groups(session: AsyncSession, limit: int) -> list[tuple[CitaRequest, list[int]]]:
    """One check per (province, trámite), not per client: the site sees a single visit no matter
    how many clients wait for the same procedure. The representative is the client who has waited
    longest (first come, first served); the rest of the group shares the result."""
    cutoff = utcnow() - timedelta(minutes=get_settings().monitor_min_interval_minutes)
    rows = (await session.scalars(
        select(CitaRequest).where(CitaRequest.state == RequestState.monitoring).order_by(CitaRequest.created_at, CitaRequest.id)
    )).all()
    groups: dict[tuple[str, str], list[CitaRequest]] = {}
    for r in rows:
        groups.setdefault((r.province, r.tramite_code), []).append(r)
    due = []
    for members in groups.values():
        last = max((aware(m.last_checked_at) for m in members if m.last_checked_at), default=None)
        if last is None or last <= cutoff:
            due.append((members[0], [m.id for m in members[1:]]))
    return due[:limit]


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


async def expire_offers(session: AsyncSession) -> list[SlotOffer]:
    pending = (await session.scalars(
        select(SlotOffer).options(selectinload(SlotOffer.request).selectinload(CitaRequest.user)).where(SlotOffer.state == OfferState.pending)
    )).all()
    expired = []
    for o in pending:
        if aware(o.expires_at) < utcnow():
            o.state = OfferState.expired
            if o.request.state == RequestState.offer_pending:
                o.request.state = RequestState.monitoring
            audit.record(session, "offer_expired", o.request.user_id, o.request_id, offer_id=o.id)
            expired.append(o)
    return expired


async def requests_to_book(session: AsyncSession) -> list[tuple[CitaRequest, SlotOffer]]:
    reqs = (await session.scalars(
        select(CitaRequest).options(selectinload(CitaRequest.offers), selectinload(CitaRequest.user)).where(CitaRequest.state == RequestState.booking)
    )).all()
    out = []
    for r in reqs:
        approved = [o for o in r.offers if o.state == OfferState.approved]
        if approved:
            out.append((r, approved[-1]))
    return out


async def mark_booked(session: AsyncSession, req: CitaRequest, reference: str | None) -> None:
    _close(req, RequestState.booked)
    req.note = reference
    audit.record(session, "booked", req.user_id, req.id, reference=reference)

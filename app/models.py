"""Database schema.

Personal document data lives only in `documents.payload_encrypted` (Fernet). Original
images are never stored: they are read once by the extractor and discarded.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RequestState(str, enum.Enum):
    awaiting_documents = "awaiting_documents"
    monitoring = "monitoring"
    offer_pending = "offer_pending"  # a slot was found, waiting for the user's "vale"
    booking = "booking"
    needs_human = "needs_human"  # captcha or unexpected page: user must finish the step
    booked = "booked"
    cancelled = "cancelled"
    failed = "failed"


ACTIVE_STATES = {
    RequestState.awaiting_documents,
    RequestState.monitoring,
    RequestState.offer_pending,
    RequestState.booking,
    RequestState.needs_human,
}


class OfferState(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    lang: Mapped[str] = mapped_column(String(5), default="pt")
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Short rolling transcript for the agent (plain text only, no document data).
    history: Mapped[list] = mapped_column(JSON, default=list)

    documents: Mapped[list["Document"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    requests: Mapped[list["CitaRequest"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # passport, nie_card, tie, other
    payload_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="documents")


class CitaRequest(Base):
    __tablename__ = "cita_requests"
    # Legal guardrail: one active request per user per procedure (no hoarding of slots).
    __table_args__ = (UniqueConstraint("user_id", "tramite_code", "active_key", name="uq_one_active_request"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    province: Mapped[str] = mapped_column(String(64))
    tramite_code: Mapped[str] = mapped_column(String(64))
    state: Mapped[RequestState] = mapped_column(Enum(RequestState), default=RequestState.awaiting_documents, index=True)
    # "1" while active, "closed-<id>" once closed, so the unique constraint only binds active requests.
    active_key: Mapped[str] = mapped_column(String(32), default="1")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checks: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="requests")
    offers: Mapped[list["SlotOffer"]] = relationship(back_populates="request", cascade="all, delete-orphan")


class SlotOffer(Base):
    __tablename__ = "slot_offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("cita_requests.id", ondelete="CASCADE"), index=True)
    office: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[str] = mapped_column(String(64))  # as shown by the site, e.g. "23/10/2026 10:00"
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[OfferState] = mapped_column(Enum(OfferState), default=OfferState.pending)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    request: Mapped[CitaRequest] = relationship(back_populates="offers")


class AuditEvent(Base):
    """Append-only log of every action taken on a user's behalf (AI Act transparency).

    Kept without personal document data, and anonymised (user_id set to NULL) on deletion.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    request_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

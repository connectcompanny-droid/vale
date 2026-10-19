from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from app.citas import service
from app.citas.monitor import Monitor
from app.citas.providers.base import CheckResult, Slot
from app.db import session_scope
from app.documents.extractor import ExtractedDocument
from app.models import AuditEvent, CitaRequest, Document, RequestState, SlotOffer, User
from app.router import handle

PASSPORT = ExtractedDocument(
    document_kind="passport", full_name="MARIA SILVA", document_number="FX123456",
    nationality="BRASIL", confidence="high",
)


async def _user(phone="34600000001") -> User:
    async with session_scope() as s:
        return await s.scalar(select(User).where(User.phone == phone))


@pytest.mark.asyncio
async def test_consent_gate_blocks_everything(channel, make_msg):
    await handle(make_msg("Hola, necesito cita"))
    phone, text, buttons = channel.last
    assert [b.id for b in buttons] == ["consent:yes", "consent:no"]
    assert "Vale" in text and "¿Aceptas?" in text  # language detected as Spanish
    with patch("app.router.extract_document") as ex:
        await handle(make_msg(media=True))
        ex.assert_not_called()  # no document read before consent


@pytest.mark.asyncio
async def test_declining_consent_deletes_the_user(channel, make_msg):
    await handle(make_msg("oi"))
    await handle(make_msg(button="consent:no"))
    assert await _user() is None


@pytest.mark.asyncio
async def test_full_happy_path(channel, provider, make_msg):
    await handle(make_msg("oi, preciso de cita"))
    await handle(make_msg(button="consent:yes"))
    assert channel.last[1].startswith("agent:")

    async with session_scope() as s:
        user = await s.scalar(select(User))
        req = await service.create_request(s, user, "madrid", "nie")
        assert req.state == RequestState.awaiting_documents

    with patch("app.router.extract_document", return_value=PASSPORT):
        await handle(make_msg(media=True))
    assert "vigiando" in channel.last[1]
    async with session_scope() as s:
        assert (await s.scalar(select(CitaRequest))).state == RequestState.monitoring

    provider.next_check = CheckResult("slots", slots=[Slot("Aluche", "23/10/2026 10:00", {"radio_id": "r1"})], hold_key="h1")
    await Monitor(provider).tick()
    _, text, buttons = channel.last
    assert "Aluche" in text and buttons[0].id.endswith(":confirm")

    await handle(make_msg(button=buttons[0].id))
    async with session_scope() as s:
        assert (await s.scalar(select(CitaRequest))).state == RequestState.booking

    await Monitor(provider).tick()
    assert "Reservado" in channel.last[1] and "Justificante 123" in channel.last[1]
    async with session_scope() as s:
        req = await s.scalar(select(CitaRequest))
        assert req.state == RequestState.booked
        actions = (await s.scalars(select(AuditEvent.action))).all()
    assert {"consent_given", "document_stored", "offer_created", "offer_approved", "booked"} <= set(actions)


@pytest.mark.asyncio
async def test_documents_are_encrypted_at_rest(channel, make_msg):
    await handle(make_msg("oi"))
    await handle(make_msg(button="consent:yes"))
    with patch("app.router.extract_document", return_value=PASSPORT):
        await handle(make_msg(media=True))
    async with session_scope() as s:
        doc = await s.scalar(select(Document))
        assert b"FX123456" not in doc.payload_encrypted
        assert (await service.get_profile(s, doc.user_id))["document_number"] == "FX123456"


@pytest.mark.asyncio
async def test_one_active_request_per_tramite(channel, make_msg):
    await handle(make_msg("oi"))
    await handle(make_msg(button="consent:yes"))
    async with session_scope() as s:
        user = await s.scalar(select(User))
        req = await service.create_request(s, user, "madrid", "nie")
        with pytest.raises(service.RuleError):
            await service.create_request(s, user, "barcelona", "nie")
        await service.cancel_request(s, user, req.id)
        await service.create_request(s, user, "barcelona", "nie")  # allowed after cancelling


@pytest.mark.asyncio
async def test_offer_expires_and_releases_session(channel, provider, make_msg):
    await handle(make_msg("oi"))
    await handle(make_msg(button="consent:yes"))
    async with session_scope() as s:
        user = await s.scalar(select(User))
        await service.store_document(s, user, PASSPORT)
        req = await service.create_request(s, user, "madrid", "nie")
        offer = await service.create_offer(s, req, "Aluche", "x", {"hold_key": "h9"})
        offer.expires_at = offer.expires_at - timedelta(hours=1)
        offer_id = offer.id
    await Monitor(provider).expire_offers()
    assert provider.released == ["h9"]
    await handle(make_msg(button=f"offer:{offer_id}:confirm"))
    assert "expirou" in channel.last[1]  # late tap cannot book
    async with session_scope() as s:
        assert (await s.scalar(select(CitaRequest))).state == RequestState.monitoring


@pytest.mark.asyncio
async def test_other_user_cannot_approve_offer(channel, make_msg):
    await handle(make_msg("oi"))
    await handle(make_msg(button="consent:yes"))
    async with session_scope() as s:
        user = await s.scalar(select(User))
        await service.store_document(s, user, PASSPORT)
        req = await service.create_request(s, user, "madrid", "nie")
        offer_id = (await service.create_offer(s, req, "Aluche", "x")).id
    await handle(make_msg("oi", phone="34611111111"))
    await handle(make_msg(button="consent:yes", phone="34611111111"))
    await handle(make_msg(button=f"offer:{offer_id}:confirm", phone="34611111111"))
    async with session_scope() as s:
        assert (await s.scalar(select(CitaRequest))).state == RequestState.offer_pending


@pytest.mark.asyncio
async def test_delete_everything(channel, make_msg):
    await handle(make_msg("oi"))
    await handle(make_msg(button="consent:yes"))
    with patch("app.router.extract_document", return_value=PASSPORT):
        await handle(make_msg(media=True))
    await handle(make_msg("apaga tudo"))
    assert channel.last[2][0].id == "delete:yes"
    await handle(make_msg(button="delete:yes"))
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(User)) == 0
        assert await s.scalar(select(func.count()).select_from(Document)) == 0
        assert await s.scalar(select(func.count()).select_from(SlotOffer)) == 0
        assert all(e.user_id is None for e in (await s.scalars(select(AuditEvent))).all())


@pytest.mark.asyncio
async def test_duplicate_webhook_is_ignored(channel, make_msg):
    m = make_msg("oi", mid="same")
    await handle(m)
    await handle(m)
    assert len(channel.sent) == 1

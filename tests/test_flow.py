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
    _, text, buttons = channel.last
    assert "MARIA SILVA" in text and "FX123456" in text
    assert [b.id.split(":")[-1] for b in buttons] == ["ok", "fix"]
    async with session_scope() as s:  # not used until confirmed
        assert (await s.scalar(select(CitaRequest))).state == RequestState.awaiting_documents
    await handle(make_msg(button=buttons[0].id))
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
    assert {"consent_given", "data_pending", "data_confirmed", "offer_created", "offer_approved", "booked"} <= set(actions)


@pytest.mark.asyncio
async def test_documents_are_encrypted_at_rest(channel, make_msg):
    await handle(make_msg("oi"))
    await handle(make_msg(button="consent:yes"))
    with patch("app.router.extract_document", return_value=PASSPORT):
        await handle(make_msg(media=True))
    await handle(make_msg(button=channel.last[2][0].id))
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
        await service.confirm_pending(s, user, (await service.store_document(s, user, PASSPORT)).id)
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
        await service.confirm_pending(s, user, (await service.store_document(s, user, PASSPORT)).id)
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


@pytest.mark.asyncio
async def test_typed_data_goes_through_readback(channel, make_msg):
    await handle(make_msg("oi"))
    await handle(make_msg(button="consent:yes"))
    await handle(make_msg("DATA full_name=ana souza;document_number=ab123;nationality=brasil"))
    _, text, buttons = channel.last
    assert "ANA SOUZA" in text and "AB123" in text and "BRASIL" in text
    async with session_scope() as s:
        user = await s.scalar(select(User))
        assert await service.get_profile(s, user.id) == {}  # pending only
    await handle(make_msg("Está certo"))  # typed instead of tapping the button
    async with session_scope() as s:
        user = await s.scalar(select(User))
        assert (await service.get_profile(s, user.id))["full_name"] == "ANA SOUZA"


@pytest.mark.asyncio
async def test_correction_merges_only_the_changed_field(channel, make_msg):
    await handle(make_msg("oi"))
    await handle(make_msg(button="consent:yes"))
    with patch("app.router.extract_document", return_value=PASSPORT):
        await handle(make_msg(media=True))
    await handle(make_msg(button=channel.last[2][1].id))  # "Corrigir"
    assert "errado" in channel.last[1]
    await handle(make_msg("DATA full_name=MARIA SOUZA"))
    _, text, buttons = channel.last
    assert "MARIA SOUZA" in text and "FX123456" in text  # number kept from the photo
    await handle(make_msg(button=buttons[0].id))
    async with session_scope() as s:
        user = await s.scalar(select(User))
        profile = await service.get_profile(s, user.id)
        assert profile["full_name"] == "MARIA SOUZA" and profile["document_number"] == "FX123456"
        assert await s.scalar(select(func.count()).select_from(Document)) == 1  # stale pending replaced


@pytest.mark.asyncio
async def test_voice_note_is_transcribed_echoed_and_confirmed(channel, make_msg):
    await handle(make_msg("oi"))
    await handle(make_msg(button="consent:yes"))
    channel.media = (b"ogg", "audio/ogg")
    with patch("app.router.transcribe", return_value="DATA full_name=JOAO PEREIRA;document_number=GH998877"):
        await handle(make_msg(media=True, kind="audio"))
    texts = [t for _, t, _ in channel.sent]
    assert any("Entendi assim" in t and "JOAO PEREIRA" in t for t in texts)  # transcript shown to the client
    assert channel.agent.received[-1].startswith("[voice note]")
    _, text, buttons = channel.last
    assert "GH998877" in text and buttons[0].id.endswith(":ok")


@pytest.mark.asyncio
async def test_failed_transcription_asks_to_resend(channel, make_msg):
    from app.documents.transcriber import TranscriptionError

    await handle(make_msg("oi"))
    await handle(make_msg(button="consent:yes"))
    with patch("app.router.transcribe", side_effect=TranscriptionError("empty_transcript")):
        await handle(make_msg(media=True, kind="audio"))
    assert "áudio" in channel.last[1]


@pytest.mark.asyncio
async def test_old_readback_button_cannot_confirm_newer_data(channel, make_msg):
    await handle(make_msg("oi"))
    await handle(make_msg(button="consent:yes"))
    await handle(make_msg("DATA full_name=A B"))
    old = channel.last[2][0].id
    await handle(make_msg("DATA full_name=C D"))
    await handle(make_msg(button=old))
    async with session_scope() as s:
        user = await s.scalar(select(User))
        assert await service.get_profile(s, user.id) == {}

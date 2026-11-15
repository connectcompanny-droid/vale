import httpx
import pytest
from fastapi.testclient import TestClient

from app.channels.base import Button
from app.channels.evolution import EvolutionChannel, parse_evolution
from app.config import get_settings

TEXT = {
    "event": "messages.upsert",
    "instance": "vale",
    "data": {
        "key": {"remoteJid": "34611222333@s.whatsapp.net", "fromMe": False, "id": "MSG1"},
        "pushName": "Maria",
        "message": {"conversation": " Hola "},
        "messageType": "conversation",
    },
}


def _with(**changes):
    data = {**TEXT["data"], **changes}
    return {**TEXT, "data": data}


def test_text_message():
    m = parse_evolution(TEXT)
    assert (m.phone, m.message_id, m.text, m.push_name, m.has_media) == ("34611222333", "MSG1", "Hola", "Maria", False)


def test_ignores_own_group_and_other_events():
    assert parse_evolution(_with(key={**TEXT["data"]["key"], "fromMe": True})) is None
    assert parse_evolution(_with(key={**TEXT["data"]["key"], "remoteJid": "123@g.us"})) is None
    assert parse_evolution({**TEXT, "event": "connection.update"}) is None
    assert parse_evolution({**TEXT, "event": "MESSAGES_UPSERT"}) is not None  # Evolution's env-style name


def test_lid_sender_uses_the_phone_alternative():
    key = {"remoteJid": "1234567890@lid", "remoteJidAlt": "34611222333@s.whatsapp.net", "id": "M2", "fromMe": False}
    assert parse_evolution(_with(key=key)).phone == "34611222333"
    assert parse_evolution(_with(key={**key, "remoteJidAlt": None})) is None


def test_media_caption_and_wrappers():
    img = parse_evolution(_with(message={"imageMessage": {"caption": "meu NIE", "mimetype": "image/jpeg"}}))
    assert (img.has_media, img.media_kind, img.media_mime, img.text) == (True, "image", "image/jpeg", "meu NIE")
    audio = parse_evolution(_with(message={"ephemeralMessage": {"message": {"audioMessage": {"mimetype": "audio/ogg; codecs=opus"}}}}))
    assert audio.media_kind == "audio"
    ext = parse_evolution(_with(message={"extendedTextMessage": {"text": "oi"}}))
    assert ext.text == "oi"


@pytest.mark.asyncio
async def test_send_text_and_buttons_as_text(monkeypatch):
    sent = []

    def handler(request: httpx.Request):
        sent.append((request.url.path, request.headers["apikey"], request.content))
        return httpx.Response(200, json={"ok": True})

    ch = EvolutionChannel("http://bridge", "k1", "vale")
    monkeypatch.setattr(ch, "_client", lambda: httpx.AsyncClient(base_url=ch.base_url, headers={"apikey": ch.api_key},
                                                                transport=httpx.MockTransport(handler)))
    await ch.send_buttons("34611222333", "Aceita?", [Button("consent:yes", "Aceito"), Button("consent:no", "Não aceito")])
    path, key, body = sent[0]
    assert path == "/message/sendText/vale" and key == "k1"
    assert "*Aceito*" in body.decode() and "*Não aceito*" in body.decode()


def test_webhook_route(channel, monkeypatch):
    from app.main import app

    monkeypatch.setattr(get_settings(), "webhook_secret", "s3")
    with TestClient(app) as client:
        assert client.post("/webhook/evolution?secret=bad", json=TEXT).status_code == 401
        assert client.post("/webhook/evolution?secret=s3", json=TEXT).json() == {"ok": True}


@pytest.mark.asyncio
async def test_typed_confirm_answers_the_pending_offer(channel, make_msg):
    from datetime import datetime, timedelta, timezone

    from app.citas import service
    from app.db import session_scope
    from app.models import CitaRequest, OfferState, SlotOffer, User
    from app.router import handle

    async with session_scope() as s:
        user, _ = await service.get_or_create_user(s, "34600000009", None, "pt")
        await service.give_consent(s, user)
        doc = await service.save_pending_data(s, user, {"full_name": "ANA", "document_number": "FX1", "nationality": "BRASIL"}, source="text")
        await service.confirm_pending(s, user, doc.id)
        req = await service.create_request(s, user, "granada", "nie")
        offer = await service.create_offer(s, req, "CNP Granada", "20/11/2026 10:00")
        offer.expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        offer_id = offer.id
    msg = make_msg("confirmar")
    msg.phone = "34600000009"
    await handle(msg)
    async with session_scope() as s:
        assert (await s.get(SlotOffer, offer_id)).state == OfferState.approved

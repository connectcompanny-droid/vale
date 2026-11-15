import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import router as vale_router
from app.channels import MultiChannel, set_channel
from app.channels.web import WebChannel, history
from app.citas import service
from app.db import session_scope
from app.models import RequestState, User, WebMessage
from tests.conftest import FakeAgent, FakeChannel


@pytest.fixture
def both():
    """WhatsApp (fake) and the real website channel, side by side."""
    whatsapp = FakeChannel()
    set_channel(MultiChannel(whatsapp, WebChannel()))
    vale_router.set_agent(FakeAgent())
    yield whatsapp
    set_channel(None)


def _client():
    from app.main import app

    return TestClient(app)


def _session(client) -> dict:
    return {"x-vale-session": client.post("/chat/api/session").json()["token"]}


def _texts(client, h, after=0):
    return client.get(f"/chat/api/messages?after={after}", headers=h).json()["messages"]


def test_page_and_session_rules(both):
    with _client() as c:
        page = c.get("/chat")
        assert page.status_code == 200 and "Vale" in page.text and "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert c.get("/chat/api/messages").status_code == 401
        assert c.post("/chat/api/send", json={"text": "oi"}, headers={"x-vale-session": "short"}).status_code == 401
        assert c.post("/chat/api/send", json={}, headers=_session(c)).status_code == 400


def test_full_conversation_on_the_site_with_buttons(both):
    with _client() as c:
        h = _session(c)
        assert c.post("/chat/api/send", json={"text": "Hola", "hidden": True}, headers=h).json() == {"ok": True}
        msgs = _texts(c, h)
        assert len(msgs) == 1 and msgs[0]["from"] == "vale"  # the hidden greeting is not shown
        yes = msgs[0]["buttons"][0]
        assert yes["id"] == "consent:yes" and yes["label"] == "Acepto"

        c.post("/chat/api/send", json={"button_id": yes["id"], "button_label": yes["label"]}, headers=h)
        c.post("/chat/api/send", json={"text": "DATA full_name=ana souza;document_number=fx1;nationality=brasil"}, headers=h)
        msgs = _texts(c, h)
        readback = msgs[-1]
        assert "ANA SOUZA" in readback["text"] and readback["buttons"][0]["id"].startswith("data:")
        assert [m["text"] for m in msgs if m["from"] == "me"][:1] == ["Acepto"]
        c.post("/chat/api/send", json={"button_id": readback["buttons"][0]["id"], "button_label": "Está bien"}, headers=h)

        other = _session(c)  # another visitor sees nothing of this conversation
        assert _texts(c, other) == []
        assert both.sent == []  # nothing leaked to WhatsApp


@pytest.mark.asyncio
async def test_messages_are_encrypted_and_deleted_with_the_user(both):
    with _client() as c:
        h = _session(c)
        c.post("/chat/api/send", json={"text": "Olá"}, headers=h)
        c.post("/chat/api/send", json={"button_id": "consent:yes", "button_label": "Aceito"}, headers=h)
    async with session_scope() as s:
        rows = (await s.scalars(select(WebMessage))).all()
        assert rows and all(b"Aceito" not in m.body_encrypted and b"Ol" not in m.body_encrypted for m in rows)
        user = await s.scalar(select(User))
        key = user.phone
        assert key.startswith("web:") and len(key) == 32
        await service.delete_user_data(s, user)
    assert await history(key) == []


@pytest.mark.asyncio
async def test_site_clients_need_a_contact_phone_before_monitoring(both):
    from app.agent.orchestrator import Agent

    async with session_scope() as s:
        user, _ = await service.get_or_create_user(s, "web:" + "a" * 28, None, "pt")
        await service.give_consent(s, user)
        doc = await service.save_pending_data(s, user, {"full_name": "ANA", "document_number": "FX1", "nationality": "BRASIL"}, source="text")
        await service.confirm_pending(s, user, doc.id)
        req = await service.create_request(s, user, "granada", "nie")
        assert req.state == RequestState.awaiting_documents
        agent = Agent(client=object())
        status = await agent._run_tool(s, user, "get_my_status", {}, None, None)
        assert status["requests"][0]["missing"] == ["phone"]
        assert "error" in await agent._run_tool(s, user, "save_contact_phone", {"phone": "123"}, None, None)
        out = await agent._run_tool(s, user, "save_contact_phone", {"phone": "+34 611 222 333"}, None, None)
        assert out == {"saved": True, "now_monitoring": [req.id]} and req.state == RequestState.monitoring
        profile = await service.get_profile(s, user.id)
        assert service.gov_phone(user.phone, profile) == "34611222333"
    assert service.gov_phone("34600000001", {}) == "34600000001"  # WhatsApp clients: their own number


def test_photo_upload_reaches_the_document_reader(both, monkeypatch):
    from app.documents.extractor import ExtractedDocument

    seen = {}

    async def fake_extract(image, mime):
        seen.update(size=len(image), mime=mime)
        return ExtractedDocument(document_kind="passport", confidence="high", full_name="ANA SOUZA", document_number="FX1", nationality="BRASIL")

    monkeypatch.setattr(vale_router, "extract_document", fake_extract)
    with _client() as c:
        h = _session(c)
        c.post("/chat/api/send", json={"button_id": "consent:yes", "button_label": "Aceito"}, headers=h)
        photo = base64.b64encode(b"\xff\xd8fakejpeg").decode()
        assert c.post("/chat/api/send", json={"media": {"b64": photo, "mime": "image/jpeg"}}, headers=h).status_code == 200
        assert seen == {"size": 10, "mime": "image/jpeg"}
        msgs = _texts(c, h)
        assert msgs[-2]["kind"] == "image" and "b64" not in str(msgs)  # the photo itself is never stored
        assert "ANA SOUZA" in msgs[-1]["text"]
        assert c.post("/chat/api/send", json={"media": {"b64": photo, "mime": "application/zip"}}, headers=h).status_code == 415
        assert c.post("/chat/api/send", json={"media": {"b64": "!!!", "mime": "image/png"}}, headers=h).status_code == 400


def test_sending_is_rate_limited(both, monkeypatch):
    from app import web_chat

    monkeypatch.setitem(web_chat.LIMITS, "send", (2, 600))
    with _client() as c:
        h = _session(c)
        codes = [c.post("/chat/api/send", json={"text": "oi"}, headers=h).status_code for _ in range(3)]
    assert codes == [200, 200, 429]

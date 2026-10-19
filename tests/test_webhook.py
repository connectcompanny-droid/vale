from fastapi.testclient import TestClient

from app.channels.uazapi import UazapiChannel
from app.main import app

SAMPLE = {
    "EventType": "messages",
    "message": {
        "chatid": "34600000001@s.whatsapp.net",
        "messageid": "ABC123",
        "text": "Hola",
        "messageType": "conversation",
        "fromMe": False,
        "senderName": "Maria",
    },
}


def test_parse_text_message():
    m = UazapiChannel("http://x", "t").parse_webhook(SAMPLE)
    assert m.phone == "34600000001" and m.text == "Hola" and m.message_id == "ABC123" and not m.has_media


def test_ignores_own_and_group_messages():
    ch = UazapiChannel("http://x", "t")
    assert ch.parse_webhook({**SAMPLE, "message": {**SAMPLE["message"], "fromMe": True}}) is None
    assert ch.parse_webhook({**SAMPLE, "message": {**SAMPLE["message"], "chatid": "123@g.us"}}) is None


def test_button_and_media():
    ch = UazapiChannel("http://x", "t")
    b = ch.parse_webhook({**SAMPLE, "message": {**SAMPLE["message"], "buttonOrListid": "offer:1:confirm"}})
    assert b.button_id == "offer:1:confirm"
    img = ch.parse_webhook({**SAMPLE, "message": {**SAMPLE["message"], "messageType": "imageMessage", "text": ""}})
    assert img.has_media


def test_webhook_requires_secret():
    with TestClient(app) as client:
        assert client.post("/webhook/uazapi?secret=wrong", json=SAMPLE).status_code == 401
        assert client.post("/webhook/uazapi", json={"EventType": "presence"}).status_code == 401


def test_voice_note_is_audio_media():
    ch = UazapiChannel("http://x", "t")
    m = ch.parse_webhook({**SAMPLE, "message": {**SAMPLE["message"], "messageType": "audioMessage", "text": ""}})
    assert m.has_media and m.media_kind == "audio"

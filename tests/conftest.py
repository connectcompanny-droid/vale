import os

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")

from app import db, router  # noqa: E402
from app.channels import Button, Channel, IncomingMessage, set_channel  # noqa: E402
from app.citas.providers.base import BookResult, CheckResult, CitaProvider, Slot  # noqa: E402


class FakeChannel(Channel):
    def __init__(self):
        self.sent: list[tuple[str, str, list[Button]]] = []
        self.media = (b"img", "image/jpeg")

    def parse_webhook(self, payload):
        from app.channels.uazapi import UazapiChannel

        return UazapiChannel("http://x", "t").parse_webhook(payload)

    async def send_text(self, phone, text):
        self.sent.append((phone, text, []))

    async def send_buttons(self, phone, text, buttons):
        self.sent.append((phone, text, buttons))

    async def send_audio(self, phone, audio, mime):
        self.sent.append((phone, f"<audio {mime} {len(audio)}b>", []))

    async def download_media(self, message):
        return self.media

    @property
    def last(self):
        return self.sent[-1]


class FakeAgent:
    """Stands in for Claude. A message like "DATA full_name=ANA SOUZA;document_number=AB123"
    makes it call submit_personal_data the way the real agent would."""

    def __init__(self):
        self.received: list[str] = []

    async def reply(self, session, user, text, on_delete, on_data=None):
        self.received.append(text)
        body = text.removeprefix("[voice note] ")
        if body.startswith("DATA ") and on_data:
            await on_data(dict(kv.split("=", 1) for kv in body[5:].split(";")))
            return ""
        return f"agent:{text}"


class FakeProvider(CitaProvider):
    public_url = "https://example.test"

    def __init__(self):
        self.next_check = CheckResult("no_slots")
        self.next_book = BookResult("booked", reference="Justificante 123")
        self.released: list[str] = []

    async def check(self, request_id, province, tramite_code, profile):
        return self.next_check

    async def book(self, request_id, slot, profile, phone, hold_key):
        return self.next_book

    async def release(self, hold_key):
        self.released.append(hold_key)


@pytest_asyncio.fixture(autouse=True)
async def fresh_db(tmp_path):
    from app.voice.tts import NoSynthesizer, set_synthesizer

    db.init_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await db.create_all()
    router._seen.clear()
    set_synthesizer(NoSynthesizer())
    yield
    await db.dispose()


@pytest.fixture
def channel():
    ch = FakeChannel()
    set_channel(ch)
    ch.agent = FakeAgent()
    router.set_agent(ch.agent)
    return ch


@pytest.fixture
def provider():
    return FakeProvider()


def msg(text="", button=None, media=False, mid=None, phone="34600000001", kind="image"):
    import uuid

    return IncomingMessage(phone=phone, message_id=mid or uuid.uuid4().hex, text=text, button_id=button,
                           has_media=media, media_kind=kind if media else None)


@pytest.fixture
def make_msg():
    return msg


__all__ = ["Slot"]

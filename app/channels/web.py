"""Website chat channel: messages live in the database and the chat page polls for them.

A web client is a user whose `phone` is "web:<hash of the session token>". Everything above this
layer (consent, read-backs, offers, the agent) is the same code that serves WhatsApp.
"""

import base64
from datetime import timezone

from app.channels.base import Button, Channel, IncomingMessage
from app.crypto import decrypt_json, encrypt_json
from app.db import current_session, session_scope
from app.models import WebMessage

PREFIX = "web:"


def is_web(phone: str | None) -> bool:
    return bool(phone) and phone.startswith(PREFIX)


def _utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt  # SQLite returns naive UTC


async def store(user_key: str, direction: str, body: dict) -> int:
    m = WebMessage(user_key=user_key, direction=direction, body_encrypted=encrypt_json(body))
    if (s := current_session()) is not None:  # sent from inside a unit of work: same transaction
        s.add(m)
        await s.flush()
        return m.id
    async with session_scope() as s:
        s.add(m)
        await s.flush()
        return m.id


async def history(user_key: str, after: int = 0, limit: int = 200) -> list[dict]:
    from sqlalchemy import select

    async with session_scope() as s:
        rows = (await s.scalars(select(WebMessage).where(WebMessage.user_key == user_key, WebMessage.id > after)
                                .order_by(WebMessage.id.desc()).limit(limit))).all()
    return [{"id": m.id, "from": "vale" if m.direction == "out" else "me", "at": _utc(m.created_at).isoformat(),
             **decrypt_json(m.body_encrypted)} for m in reversed(rows)]


class WebChannel(Channel):
    def parse_webhook(self, payload: dict) -> IncomingMessage | None:
        return None  # web messages arrive through /chat/api/send

    async def send_text(self, phone: str, text: str) -> None:
        await store(phone, "out", {"text": text})

    async def send_buttons(self, phone: str, text: str, buttons: list[Button]) -> None:
        await store(phone, "out", {"text": text, "buttons": [{"id": b.id, "label": b.label} for b in buttons]})

    async def send_audio(self, phone: str, audio: bytes, mime: str) -> None:
        raise NotImplementedError("the web chat answers in text")  # the router falls back to text

    async def download_media(self, message: IncomingMessage) -> tuple[bytes, str]:
        media = (message.raw or {}).get("web_media") or {}
        return base64.b64decode(media.get("b64", "")), media.get("mime") or "application/octet-stream"

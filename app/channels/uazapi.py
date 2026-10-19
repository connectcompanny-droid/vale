"""UAZAPI (uazapiGO v2) adapter.

Endpoint paths and webhook field names follow uazapiGO v2. They are isolated here so
they can be checked against your Sofia integration and adjusted in one place.
"""

import base64
import logging
import re

import httpx

from app.channels.base import Button, Channel, IncomingMessage
from app.config import get_settings

log = logging.getLogger(__name__)

MEDIA_TYPES = {"image", "imageMessage", "document", "documentMessage"}


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value.split("@")[0])


class UazapiChannel(Channel):
    def __init__(self, base_url: str | None = None, token: str | None = None):
        s = get_settings()
        self.base_url = (base_url or s.uazapi_base_url).rstrip("/")
        self.token = token or s.uazapi_instance_token

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, headers={"token": self.token}, timeout=30)

    def parse_webhook(self, payload: dict) -> IncomingMessage | None:
        if payload.get("EventType") not in (None, "messages"):
            return None
        msg = payload.get("message") or {}
        if not msg or msg.get("fromMe") or msg.get("isGroup"):
            return None
        chat = msg.get("chatid") or msg.get("sender") or ""
        if chat.endswith("@g.us"):
            return None
        mtype = msg.get("messageType") or msg.get("type") or ""
        button_id = msg.get("buttonOrListid") or msg.get("buttonId") or None
        raw_text = msg.get("text") or msg.get("content") or ""
        return IncomingMessage(
            phone=_digits(chat),
            message_id=str(msg.get("messageid") or msg.get("id") or ""),
            text=raw_text.strip() if isinstance(raw_text, str) else "",
            button_id=button_id,
            has_media=mtype in MEDIA_TYPES,
            media_mime=msg.get("mimetype"),
            push_name=msg.get("senderName") or payload.get("chat", {}).get("name"),
            raw=payload,
        )

    async def send_text(self, phone: str, text: str) -> None:
        async with self._client() as c:
            r = await c.post("/send/text", json={"number": phone, "text": text})
            r.raise_for_status()

    async def send_buttons(self, phone: str, text: str, buttons: list[Button]) -> None:
        choices = [f"{b.label}|{b.id}" for b in buttons]
        async with self._client() as c:
            r = await c.post("/send/menu", json={"number": phone, "type": "button", "text": text, "choices": choices})
            if r.status_code >= 400:
                # Fallback: plain text with numbered options, still parseable by the router.
                log.warning("send/menu failed (%s); falling back to text", r.status_code)
                lines = "\n".join(f"{i + 1}. {b.label}" for i, b in enumerate(buttons))
                await self.send_text(phone, f"{text}\n\n{lines}")

    async def download_media(self, message: IncomingMessage) -> tuple[bytes, str]:
        async with self._client() as c:
            r = await c.post("/message/download", json={"id": message.message_id, "return_base64": True})
            r.raise_for_status()
            data = r.json()
        b64 = data.get("base64Data") or data.get("base64") or ""
        if "," in b64[:100]:
            b64 = b64.split(",", 1)[1]
        mime = data.get("mimetype") or message.media_mime or "image/jpeg"
        return base64.b64decode(b64), mime

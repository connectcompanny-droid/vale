"""Evolution API adapter (github.com/EvolutionAPI/evolution-api), free and open source.

Also works with Vale's own bridge in bridge/whatsapp, which speaks the same API, so the laptop
setup needs no Docker or database. WhatsApp-Web based APIs (Baileys) can't show reliable buttons,
so choices are sent as text with the exact words to type; the router maps those words back.
"""

import base64
import logging
import re

import httpx

from app.channels.base import Button, Channel, IncomingMessage
from app.config import get_settings

log = logging.getLogger(__name__)

MEDIA = {
    "imageMessage": "image",
    "documentMessage": "document",
    "documentWithCaptionMessage": "document",
    "audioMessage": "audio",
}


def _digits(jid: str) -> str:
    return re.sub(r"\D", "", (jid or "").split("@")[0].split(":")[0])


def _phone(key: dict) -> str | None:
    """Phone number of the sender. Newer WhatsApp versions hide it behind a "@lid" id."""
    jid = key.get("remoteJid") or ""
    if jid.endswith("@s.whatsapp.net"):
        return _digits(jid)
    for alt in (key.get("remoteJidAlt"), key.get("senderPn"), key.get("participantAlt")):
        if alt and alt.endswith("@s.whatsapp.net"):
            return _digits(alt)
    return None


def _unwrap(message: dict) -> dict:
    for wrapper in ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "documentWithCaptionMessage"):
        inner = (message.get(wrapper) or {}).get("message")
        if inner:
            return _unwrap(inner)
    return message


def parse_evolution(payload: dict) -> IncomingMessage | None:
    event = (payload.get("event") or "").lower().replace("_", ".")
    if event != "messages.upsert":
        return None
    data = payload.get("data") or {}
    if isinstance(data, list):  # some versions batch messages
        data = data[0] if data else {}
    key = data.get("key") or {}
    jid = key.get("remoteJid") or ""
    if key.get("fromMe") or jid.endswith("@g.us") or jid.endswith("@broadcast") or jid.endswith("@newsletter"):
        return None
    phone = _phone(key)
    if not phone:
        log.warning("evolution webhook: no phone number for jid kind %r; ignored", jid.split("@")[-1])
        return None
    message = _unwrap(data.get("message") or {})
    kind = next((MEDIA[k] for k in message if k in MEDIA), None)
    text = (
        message.get("conversation")
        or (message.get("extendedTextMessage") or {}).get("text")
        or (message.get("imageMessage") or {}).get("caption")
        or (message.get("documentMessage") or {}).get("caption")
        or ""
    )
    button = (
        (message.get("buttonsResponseMessage") or {}).get("selectedButtonId")
        or ((message.get("listResponseMessage") or {}).get("singleSelectReply") or {}).get("selectedRowId")
        or (message.get("templateButtonReplyMessage") or {}).get("selectedId")
    )
    media = next((message[k] for k in message if k in MEDIA), {}) or {}
    return IncomingMessage(
        phone=phone,
        message_id=str(key.get("id") or ""),
        text=text.strip() if isinstance(text, str) else "",
        button_id=button or None,
        has_media=kind is not None,
        media_kind=kind,
        media_mime=media.get("mimetype"),
        push_name=data.get("pushName"),
        raw=payload,
    )


class EvolutionChannel(Channel):
    def __init__(self, base_url: str | None = None, api_key: str | None = None, instance: str | None = None):
        s = get_settings()
        self.base_url = (base_url or s.evolution_base_url).rstrip("/")
        self.api_key = api_key or s.evolution_api_key
        self.instance = instance or s.evolution_instance

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, headers={"apikey": self.api_key}, timeout=30)

    def parse_webhook(self, payload: dict) -> IncomingMessage | None:
        return parse_evolution(payload)

    async def send_text(self, phone: str, text: str) -> None:
        async with self._client() as c:
            r = await c.post(f"/message/sendText/{self.instance}", json={"number": phone, "text": text})
            r.raise_for_status()

    async def send_buttons(self, phone: str, text: str, buttons: list[Button]) -> None:
        options = "\n".join(f"• *{b.label}*" for b in buttons)
        await self.send_text(phone, f"{text}\n\n{options}")

    async def send_audio(self, phone: str, audio: bytes, mime: str) -> None:
        async with self._client() as c:
            r = await c.post(f"/message/sendWhatsAppAudio/{self.instance}",
                             json={"number": phone, "audio": base64.b64encode(audio).decode()})
            r.raise_for_status()

    async def download_media(self, message: IncomingMessage) -> tuple[bytes, str]:
        async with self._client() as c:
            r = await c.post(f"/chat/getBase64FromMediaMessage/{self.instance}",
                             json={"message": {"key": {"id": message.message_id}}, "convertToMp4": False})
            r.raise_for_status()
            data = r.json()
        b64 = data.get("base64") or ""
        if "," in b64[:100]:
            b64 = b64.split(",", 1)[1]
        mime = data.get("mimetype") or message.media_mime or "application/octet-stream"
        return base64.b64decode(b64), mime

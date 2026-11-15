from app.channels.base import Button, Channel, IncomingMessage
from app.channels.uazapi import UazapiChannel

_channel: Channel | None = None


class MultiChannel(Channel):
    """WhatsApp and the website chat side by side: each client is answered where they wrote."""

    def __init__(self, whatsapp: Channel, web: Channel):
        self.whatsapp, self.web = whatsapp, web

    def _for(self, phone: str) -> Channel:
        from app.channels.web import is_web

        return self.web if is_web(phone) else self.whatsapp

    def parse_webhook(self, payload: dict) -> IncomingMessage | None:
        return self.whatsapp.parse_webhook(payload)

    async def send_text(self, phone: str, text: str) -> None:
        await self._for(phone).send_text(phone, text)

    async def send_buttons(self, phone: str, text: str, buttons: list[Button]) -> None:
        await self._for(phone).send_buttons(phone, text, buttons)

    async def send_audio(self, phone: str, audio: bytes, mime: str) -> None:
        await self._for(phone).send_audio(phone, audio, mime)

    async def download_media(self, message: IncomingMessage) -> tuple[bytes, str]:
        return await self._for(message.phone).download_media(message)


def get_channel() -> Channel:
    global _channel
    if _channel is None:
        from app.channels.web import WebChannel
        from app.config import get_settings

        if get_settings().whatsapp_provider.lower() == "evolution":
            from app.channels.evolution import EvolutionChannel

            whatsapp: Channel = EvolutionChannel()
        else:
            whatsapp = UazapiChannel()
        _channel = MultiChannel(whatsapp, WebChannel())
    return _channel


def set_channel(channel: Channel | None) -> None:
    """Swap the channel (tests, or a future Meta Cloud API / Telegram adapter)."""
    global _channel
    _channel = channel


__all__ = ["Button", "Channel", "IncomingMessage", "get_channel", "set_channel"]

from app.channels.base import Button, Channel, IncomingMessage
from app.channels.uazapi import UazapiChannel

_channel: Channel | None = None


def get_channel() -> Channel:
    global _channel
    if _channel is None:
        from app.config import get_settings

        if get_settings().whatsapp_provider.lower() == "evolution":
            from app.channels.evolution import EvolutionChannel

            _channel = EvolutionChannel()
        else:
            _channel = UazapiChannel()
    return _channel


def set_channel(channel: Channel) -> None:
    """Swap the channel (tests, or a future Meta Cloud API / Telegram adapter)."""
    global _channel
    _channel = channel


__all__ = ["Button", "Channel", "IncomingMessage", "get_channel", "set_channel"]

from app.channels.base import Button, Channel, IncomingMessage
from app.channels.uazapi import UazapiChannel

_channel: Channel | None = None


def get_channel() -> Channel:
    global _channel
    if _channel is None:
        _channel = UazapiChannel()
    return _channel


def set_channel(channel: Channel) -> None:
    """Swap the channel (tests, or a future Meta Cloud API / Telegram adapter)."""
    global _channel
    _channel = channel


__all__ = ["Button", "Channel", "IncomingMessage", "get_channel", "set_channel"]

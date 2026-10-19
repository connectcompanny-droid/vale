"""Channel-agnostic messaging interface.

Everything above this layer talks in IncomingMessage / Button, so moving from UAZAPI to
the Meta Cloud API, Telegram or SMS only means writing a new adapter.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Button:
    id: str  # opaque payload we get back when tapped, e.g. "offer:42:confirm"
    label: str


@dataclass
class IncomingMessage:
    phone: str  # E.164 digits, no "+"
    message_id: str
    text: str = ""
    button_id: str | None = None  # set when the user tapped one of our buttons
    has_media: bool = False
    media_kind: str | None = None  # "image" | "document" | "audio"
    media_mime: str | None = None
    push_name: str | None = None
    raw: dict = field(default_factory=dict)


class Channel(ABC):
    @abstractmethod
    def parse_webhook(self, payload: dict) -> IncomingMessage | None:
        """Turn a provider webhook into an IncomingMessage, or None to ignore it."""

    @abstractmethod
    async def send_text(self, phone: str, text: str) -> None: ...

    @abstractmethod
    async def send_buttons(self, phone: str, text: str, buttons: list[Button]) -> None: ...

    @abstractmethod
    async def download_media(self, message: IncomingMessage) -> tuple[bytes, str]:
        """Return (bytes, mime type) for an incoming image/document."""

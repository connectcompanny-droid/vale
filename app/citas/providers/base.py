from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Slot:
    office: str
    starts_at: str
    raw: dict = field(default_factory=dict)


@dataclass
class CheckResult:
    status: str  # "no_slots" | "slots" | "needs_human" | "error"
    slots: list[Slot] = field(default_factory=list)
    reason: str = ""
    hold_key: str | None = None  # id of a live browser session kept open for booking


@dataclass
class BookResult:
    status: str  # "booked" | "slot_gone" | "needs_human" | "error"
    reference: str | None = None
    reason: str = ""


class CitaProvider(ABC):
    """One government appointment system. The worker only talks to this interface."""

    #: Public page the user can open to finish a step themselves (captcha, unexpected form).
    public_url: str = ""

    @abstractmethod
    async def check(self, request_id: int, province: str, tramite_code: str, profile: dict) -> CheckResult: ...

    @abstractmethod
    async def book(self, request_id: int, slot: Slot, profile: dict, phone: str, hold_key: str | None) -> BookResult: ...

    def owns(self, hold_key: str) -> bool:
        """True if the held browser session `hold_key` lives in this process."""
        return True

    async def release(self, hold_key: str) -> None:  # noqa: B027
        """Close a held session (offer rejected or expired)."""

    async def sweep(self) -> None:  # noqa: B027
        """Close held sessions nobody will use any more (e.g. expired by another process)."""

    async def close(self) -> None:  # noqa: B027
        """Shut down shared resources."""

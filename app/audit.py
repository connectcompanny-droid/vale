from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent


def record(session: AsyncSession, action: str, user_id: int | None = None, request_id: int | None = None, **detail) -> None:
    session.add(AuditEvent(user_id=user_id, request_id=request_id, action=action, detail=detail))

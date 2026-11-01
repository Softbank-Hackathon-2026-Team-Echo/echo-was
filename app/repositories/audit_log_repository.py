from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


class AuditLogRepository:
    """거부 기록 저장. 기록은 지우지도 고치지도 않는다(DELETE 는 DB 트리거가 막는다)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, audit_log: AuditLog) -> None:
        self._session.add(audit_log)

"""현재 시각을 주는 시계. 일꾼·배포 코드는 DB now() 대신 주입한 시계로 시각을 정한다.

시험은 시계를 바꿔 임대 만료·제한 시간을 sleep 없이 재현한다.
"""

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """timezone-aware UTC 시각."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

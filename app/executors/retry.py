"""실제 실행기의 일시 오류 재시도 규칙.

- 시작 계열(빌드 start·배포 apply·내리기 remove·되돌리기): 실행기 안에서 1·2·4초 간격으로 3번 다시
  시도한 뒤에만 ExecutorUnavailableError 를 올린다.
- 조회 계열(poll): 예외를 올리지 않고 진행 중으로 답한다(단계 제한 시간이 끝낸다).

시험은 sleep 을 바꿔 실제로 기다리지 않는다.
"""

import asyncio
from collections.abc import Awaitable, Callable

START_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)

Sleep = Callable[[float], Awaitable[None]]


async def real_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class TransientError(Exception):
    """다시 시도하면 될 수 있는 외부 오류(연결 실패·5xx·429). code 는 고정 코드다."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def retry_transient[T](
    operation: Callable[[], Awaitable[T]],
    sleep: Sleep,
    delays: tuple[float, ...] = START_RETRY_DELAYS_SECONDS,
) -> T:
    """TransientError 면 delays 간격으로 다시 부른다.

    마지막 시도의 TransientError 는 그대로 올린다.
    """
    for delay in delays:
        try:
            return await operation()
        except TransientError:
            await sleep(delay)
    return await operation()

"""`python -m app.worker` 진입점. SIGTERM·SIGINT 를 받으면 지금 실행만 끝내고 멈춘다."""

import asyncio
import logging
import signal
import socket
import uuid

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory
from app.core.logging import configure_logging
from app.dependencies import build_source_repository_client
from app.executors import build_executor_set
from app.worker.runner import Worker

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    clock = SystemClock()
    owner = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    worker = Worker(
        session_factory=get_session_factory(),
        settings=settings,
        clock=clock,
        executors=build_executor_set(settings, clock),
        source_client=build_source_repository_client(settings, clock),
        owner=owner,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    logger.info(
        "worker started",
        extra={
            "action": "run_worker",
            "lease_owner": owner,
            "executor_mode": settings.executor_mode,
            "health_checker_mode": settings.health_checker_mode,
            "source_client_mode": settings.source_client_mode,
            "concurrency": settings.worker_concurrency,
        },
    )
    try:
        await worker.run(stop)
    finally:
        await get_engine().dispose()
    logger.info("worker stopped", extra={"action": "run_worker"})


if __name__ == "__main__":
    configure_logging("echo-worker", get_settings().log_level)
    asyncio.run(main())

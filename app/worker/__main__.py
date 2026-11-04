"""`python -m app.worker` 진입점. SIGTERM·SIGINT 를 받으면 지금 실행만 끝내고 멈춘다.

실제 실행기 모드(`EXECUTOR_MODE=real`)면 온프레 이미지 받기 자격 갱신 루프도 함께 돌린다.
실행기 묶음과 앱 환경 변수 보관함은 프로세스당 하나이고 멈출 때 HTTP 클라이언트를 닫는다.
"""

import asyncio
import logging
import signal
import socket
import uuid

from app.clients.app_secret_store import build_app_secret_store
from app.core.clock import SystemClock
from app.core.config import ExecutorMode, get_settings
from app.core.database import get_engine, get_session_factory
from app.core.logging import configure_logging
from app.dependencies import build_source_repository_client
from app.executors import build_executor_set
from app.executors.kubernetes.executors import KubernetesDeployExecutor
from app.services.executor_environment_lookup import DbEnvironmentLookup
from app.services.onprem_pull_refresher import OnpremPullSecretRefresher
from app.services.security_gate import build_public_tls_checker
from app.worker.runner import Worker

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    clock = SystemClock()
    owner = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    session_factory = get_session_factory()
    executors = build_executor_set(
        settings, clock, environment_lookup=DbEnvironmentLookup(session_factory)
    )
    app_secret_store = build_app_secret_store(settings, clock)
    worker = Worker(
        session_factory=session_factory,
        settings=settings,
        clock=clock,
        executors=executors,
        source_client=build_source_repository_client(settings, clock),
        app_secret_store=app_secret_store,
        owner=owner,
        tls_checker=build_public_tls_checker(settings),
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
            "app_secret_store_mode": settings.app_secret_store_mode,
            "concurrency": settings.worker_concurrency,
        },
    )
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(worker.run(stop))
            if settings.executor_mode is ExecutorMode.REAL and isinstance(
                executors.deploy, KubernetesDeployExecutor
            ):
                refresher = OnpremPullSecretRefresher(session_factory, executors.deploy, settings)
                group.create_task(refresher.run(stop))
    finally:
        await executors.aclose()
        await app_secret_store.aclose()
        await get_engine().dispose()
    logger.info("worker stopped", extra={"action": "run_worker"})


if __name__ == "__main__":
    configure_logging("echo-worker", get_settings().log_level)
    asyncio.run(main())

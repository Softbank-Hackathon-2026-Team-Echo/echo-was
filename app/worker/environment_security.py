"""검사 전용 workload 진입점. 배포 worker·API·실행기를 시작하지 않는다.

python -m app.worker.environment_security [--once | --preflight]
--preflight는 자격 값/외부 자원을 읽거나 DB를 변경하지 않는 설정·정책 재확인 목록이다.
"""

import argparse
import asyncio
import json
import signal

from app.clients.environment_security import LiveEnvironmentSecurityReader
from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory
from app.core.logging import configure_logging
from app.services.environment_security_collector import EnvironmentSecurityCollector
from app.services.environment_security_preflight import inspect_configuration


async def main(*, once: bool = False, preflight: bool = False) -> int:
    settings = get_settings()
    clock = SystemClock()
    factory = get_session_factory()
    try:
        if preflight:
            report = await inspect_configuration(factory, settings, clock.now())
            print(json.dumps(report))
            return 0 if report["configurationReady"] else 1
        if (
            not settings.security_environment_collector_enabled
            or not settings.security_gate_enabled
        ):
            raise ValueError("dedicated collector and security gate must be enabled")
        reader = LiveEnvironmentSecurityReader(settings, clock)
        collector = EnvironmentSecurityCollector(factory, clock, settings, reader)
        if once:
            try:
                count = await collector.run_once()
                print(
                    json.dumps(
                        {"recorded": count, "failedEnvironments": collector.failed_environments}
                    )
                )
                return 1 if collector.failed_environments else 0
            finally:
                await reader.aclose()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        await collector.run(stop)
        return 0
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    configure_logging("security-collector")
    raise SystemExit(asyncio.run(main(once=args.once, preflight=args.preflight)))

import asyncio
import logging
from typing import Annotated

from fastapi import Depends, FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.registry import discover
from app.auth.audit import register_audit_handlers
from app.core.config import get_settings
from app.core.database import get_engine
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.routers import (
    agent_token_router,
    auth_router,
    deploy_request_router,
    environment_kind_router,
    environment_router,
    invitation_router,
    organization_router,
    service_router,
)

configure_logging("echo-api", get_settings().log_level)
logger = logging.getLogger(__name__)

READINESS_TIMEOUT_SECONDS = 2.0

# 환경 종류 파일을 모두 불러온다. 동작 클래스가 빠진 종류가 있으면 첫 요청 전에 여기서 실패한다.
discover()

# 마이그레이션은 여기서 실행하지 않는다(lifespan·startup 금지). README 의 빠른 시작·배포 절을 본다.
app = FastAPI(title="Echo Control API")
# 프론트(다른 출처)가 쿠키를 실어 부를 수 있게 허용 목록 + credentials. 쓰기 요청의 Origin 은
# 검사기가 한 번 더 확인한다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().frontend_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)
app.add_middleware(RequestContextMiddleware)
register_exception_handlers(app)
register_audit_handlers(app)
app.include_router(auth_router.router)
app.include_router(organization_router.router)
app.include_router(invitation_router.router)
app.include_router(agent_token_router.router)
app.include_router(environment_router.router)
app.include_router(environment_kind_router.router)
app.include_router(service_router.router)
app.include_router(deploy_request_router.router)


@app.get("/healthz", status_code=status.HTTP_204_NO_CONTENT)
async def check_health() -> Response:
    # 프로세스 생존만 본다. 의존성 장애로 모든 Pod 가 재시작되지 않도록 DB 는 보지 않는다.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/readyz", status_code=status.HTTP_204_NO_CONTENT)
async def check_readiness(engine: Annotated[AsyncEngine, Depends(get_engine)]) -> Response:
    try:
        async with asyncio.timeout(READINESS_TIMEOUT_SECONDS), engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (TimeoutError, OSError, SQLAlchemyError) as exc:
        logger.warning(
            "database not ready",
            extra={"action": "check_readiness", "exc_type": type(exc).__name__},
        )
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

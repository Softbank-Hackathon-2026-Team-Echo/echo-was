import re
from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.secrets import InvalidSecretRefError, parse_secret_ref

DEFAULT_ENVIRONMENT_CHECK_TIMEOUT_SECONDS = 5.0
# 연결 주소 {서비스}.{환경}.{도메인} 이 DNS 이름 최대 길이(253)를 넘지 않게 한다
# (63 + 1 + 63 + 1 + 125).
PUBLIC_BASE_DOMAIN_MAX_LENGTH = 125
_DOMAIN_PATTERN = re.compile(
    r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)+$"
)


class ExecutorMode(StrEnum):
    """빌드·배포·대표 주소 라우터·내리기 실행기. 지금은 가짜(fake)만 쓸 수 있다."""

    FAKE = "fake"
    # 실제 실행기(인프라 3·5 이후). 값만 정해 두고 설정에서는 아직 받지 않는다.
    REAL = "real"


class HealthCheckerMode(StrEnum):
    FAKE = "fake"
    HTTP = "http"


class SourceClientMode(StrEnum):
    FAKE = "fake"
    GITHUB = "github"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # 환경 접속 확인(비밀값 조회 + 접속 확인 + 클러스터 정보 읽기) 전체에 거는 제한 시간.
    environment_check_timeout_seconds: float = Field(
        default=DEFAULT_ENVIRONMENT_CHECK_TIMEOUT_SECONDS, gt=0
    )

    # 퍼블릭 주소: 대표 {서비스}.{도메인}, 환경별 {서비스}.{환경}.{도메인}.
    public_base_domain: str = "apps.echo.test"

    executor_mode: ExecutorMode = ExecutorMode.FAKE
    health_checker_mode: HealthCheckerMode = HealthCheckerMode.FAKE
    source_client_mode: SourceClientMode = SourceClientMode.FAKE

    # GitHub App(비공개 저장소). 서버에 두는 비밀값은 개인키 참조 하나다(값은 비밀 저장소에).
    github_app_id: str | None = None
    github_app_private_key_ref: str | None = None
    github_app_install_url: str = "https://github.com/apps/echo-deploy/installations/new"
    github_api_base_url: str = "https://api.github.com"
    github_request_timeout_seconds: float = Field(default=10.0, gt=0)

    # 단계별 제한 시간(초).
    build_timeout_seconds: float = Field(default=900.0, gt=0)
    deploy_timeout_seconds: float = Field(default=300.0, gt=0)
    teardown_timeout_seconds: float = Field(default=300.0, gt=0)
    health_check_interval_seconds: float = Field(default=5.0, gt=0)
    health_check_request_timeout_seconds: float = Field(default=5.0, gt=0)
    # 가짜 실행기가 한 작업을 끝내는 데 걸리는 시간(초). 시험은 0.
    fake_executor_step_seconds: float = Field(default=2.0, ge=0)

    # 일꾼(python -m app.worker).
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    worker_lease_seconds: float = Field(default=30.0, gt=0)
    worker_concurrency: int = Field(default=1, ge=1, le=32)

    @field_validator("public_base_domain")
    @classmethod
    def _check_public_base_domain(cls, value: str) -> str:
        if len(value) > PUBLIC_BASE_DOMAIN_MAX_LENGTH or not _DOMAIN_PATTERN.fullmatch(value):
            raise ValueError("public_base_domain must be a lowercase domain of 125 chars or less")
        return value

    @field_validator("executor_mode")
    @classmethod
    def _check_executor_mode(cls, value: ExecutorMode) -> ExecutorMode:
        if value is not ExecutorMode.FAKE:
            raise ValueError("only the fake executor is available until the real one is built")
        return value

    @field_validator("github_app_private_key_ref")
    @classmethod
    def _check_private_key_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parse_secret_ref(value)
        except InvalidSecretRefError:
            raise ValueError("github_app_private_key_ref must be a secret reference") from None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()

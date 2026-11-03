import re
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.secrets import InvalidSecretRefError, parse_secret_ref

DEFAULT_ENVIRONMENT_CHECK_TIMEOUT_SECONDS = 5.0
# 퍼블릭 주소가 DNS 이름 최대 길이(253)를 넘지 않게 한다. 지금 주소는 레이블 하나(63자 이하)
# + 도메인이라 여유가 있고, 예전 두 단계 형식(63 + 1 + 63 + 1 + 125) 때 정한 값을 그대로 둔다.
PUBLIC_BASE_DOMAIN_MAX_LENGTH = 125
_DOMAIN_PATTERN = re.compile(
    r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)+$"
)


# 세션·초대는 7일, 로그인 상태 쿠키는 10분.
SEVEN_DAYS_SECONDS = 7 * 24 * 60 * 60
DEFAULT_RESERVED_ORG_SLUGS = ("team-echo",)

# 이전 계획 비율 단계(10·50·100%)마다 도착이 처리해야 할 요청 수와 단계 최대 대기 범위.
DEFAULT_MIGRATION_REQUIRED_REQUESTS = (20, 50, 50)
MIGRATION_SHIFT_STEP_COUNT = 3
MIGRATION_MIN_REQUIRED_REQUESTS = 1
MIGRATION_MAX_REQUIRED_REQUESTS = 10000
MIGRATION_MIN_MAX_WAIT_SECONDS = 5
MIGRATION_MAX_MAX_WAIT_SECONDS = 300


def _split_comma_list(value: object) -> object:
    """환경변수의 쉼표 구분 문자열을 목록으로 바꾼다. 빈 칸은 버린다."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


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

    # 퍼블릭 주소: 대표 {서비스}-{suffix}.{도메인}, 환경별 {서비스}-{환경}-{suffix}.{도메인}.
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

    # GitHub 로그인(API 서버 6). 위 GitHub App 의 사용자 인증을 쓴다. 넷 중 하나라도 비면
    # 로그인·콜백만 503 LOGIN_NOT_CONFIGURED 이고 나머지 API·일꾼·alembic 은 그대로 뜬다.
    github_app_client_id: str | None = None
    # client secret 은 값이 아니라 비밀 저장소 위치다(env:ECHO_SECRET_... 등).
    github_app_client_secret_ref: str | None = None
    github_oauth_base_url: str = "https://github.com"
    # 이 API 호스트의 콜백 공개 주소(…/api/v1/auth/github/callback).
    # GitHub App 콜백 목록의 값과 글자까지 같아야 한다.
    github_oauth_redirect_uri: str | None = None
    # 로그인 뒤 돌아갈 프론트 주소. 끝의 / 는 뗀다.
    frontend_base_url: str | None = None
    # 쿠키로 인증한 변경 요청이 허용되는 Origin(정확히 같아야 함). CORS 도 같은 목록을 쓴다.
    frontend_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    session_cookie_name: str = "echo_session"
    session_ttl_seconds: int = Field(default=SEVEN_DAYS_SECONDS, gt=0)
    session_cookie_secure: bool = True
    oauth_state_ttl_seconds: int = Field(default=600, gt=0)
    # 설정하면 이 GitHub 조직의 active 멤버만 로그인할 수 있다. 비면 모든 GitHub 계정.
    login_allowed_github_org: str | None = None
    # 사람이 API 로 만들 수 없는 조직 slug(데모 준비 명령만 만든다).
    reserved_org_slugs: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_RESERVED_ORG_SLUGS)
    )
    agent_token_default_days: int = Field(default=30, ge=1)
    agent_token_max_days: int = Field(default=90, ge=1)
    invitation_ttl_seconds: int = Field(default=SEVEN_DAYS_SECONDS, gt=0)

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

    # 이전 계획(API 서버 8·8-1). 비율 단계는 도착이 요청 N건을 처리했는지 센다.
    # 필요 건수(10·50·100% 단계 순, 계획마다 바꿀 수 있다)·단계 최대 대기(초)·예상 rps(예상 시간·
    # 가짜 카운터·simulate 에 쓴다)·카운터 읽기 주기·마지막 성공 읽기가 이보다 오래되면 판단 불가·
    # p95 참고 읽기 창, 단계마다 더하는 여유 시간(예상 시간 계산), 승인 대기 만료, 롤백 최대 시도.
    migration_required_requests_default: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_MIGRATION_REQUIRED_REQUESTS)
    )
    migration_max_wait_seconds_default: int = Field(
        default=30, ge=MIGRATION_MIN_MAX_WAIT_SECONDS, le=MIGRATION_MAX_MAX_WAIT_SECONDS
    )
    migration_expected_rps: int = Field(default=50, ge=1, le=10000)
    migration_count_poll_seconds: int = Field(default=1, ge=1, le=10)
    migration_counts_stale_seconds: int = Field(default=5, ge=1, le=60)
    migration_p95_reference_window_seconds: int = Field(default=30, ge=5, le=300)
    migration_step_overhead_seconds: int = Field(default=2, ge=0, le=60)
    migration_plan_approval_ttl_seconds: int = Field(default=24 * 60 * 60, gt=0)
    migration_rollback_max_attempts: int = Field(default=3, ge=1, le=10)

    @field_validator("public_base_domain")
    @classmethod
    def _check_public_base_domain(cls, value: str) -> str:
        if len(value) > PUBLIC_BASE_DOMAIN_MAX_LENGTH or not _DOMAIN_PATTERN.fullmatch(value):
            raise ValueError("public_base_domain must be a lowercase domain of 125 chars or less")
        return value

    @field_validator(
        "frontend_allowed_origins",
        "reserved_org_slugs",
        "migration_required_requests_default",
        mode="before",
    )
    @classmethod
    def _split_lists(cls, value: object) -> object:
        return _split_comma_list(value)

    @field_validator("migration_required_requests_default")
    @classmethod
    def _check_required_requests(cls, value: list[int]) -> list[int]:
        if len(value) != MIGRATION_SHIFT_STEP_COUNT or any(
            not MIGRATION_MIN_REQUIRED_REQUESTS <= item <= MIGRATION_MAX_REQUIRED_REQUESTS
            for item in value
        ):
            raise ValueError(
                "migration_required_requests_default must be 3 integers between 1 and 10000"
            )
        return value

    @field_validator("frontend_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.rstrip("/") or None

    @field_validator("github_app_client_secret_ref")
    @classmethod
    def _check_client_secret_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parse_secret_ref(value)
        except InvalidSecretRefError:
            raise ValueError("github_app_client_secret_ref must be a secret reference") from None
        return value

    @property
    def is_login_configured(self) -> bool:
        return all(
            (
                self.github_app_client_id,
                self.github_app_client_secret_ref,
                self.github_oauth_redirect_uri,
                self.frontend_base_url,
            )
        )

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

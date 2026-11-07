import re
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.secrets import InvalidSecretRefError, parse_secret_ref

DEFAULT_ENVIRONMENT_CHECK_TIMEOUT_SECONDS = 5.0
# 퍼블릭 주소가 DNS 이름 최대 길이(253)를 넘지 않게 한다. 지금 주소는 레이블 하나(63자 이하)
# + 도메인이라 여유가 있고, 예전 두 단계 형식(63 + 1 + 63 + 1 + 125) 때 정한 값을 그대로 둔다.
PUBLIC_BASE_DOMAIN_MAX_LENGTH = 125
_GITHUB_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_APP_SECRET_NAME_PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_DOMAIN_PATTERN = re.compile(
    r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)+$"
)


# 세션·초대는 7일, 로그인 상태 쿠키는 10분.
SEVEN_DAYS_SECONDS = 7 * 24 * 60 * 60
DEFAULT_RESERVED_ORG_SLUGS = ("team-echo",)

# 이전 계획 비율 단계(5·10·25·50·75·100%)마다 도착이 처리해야 할 요청 수와 단계 최대 대기 범위.
DEFAULT_MIGRATION_REQUIRED_REQUESTS = (7, 10, 20, 30, 30, 30)
MIGRATION_SHIFT_STEP_COUNT = 6
MIGRATION_MIN_REQUIRED_REQUESTS = 1
MIGRATION_MAX_REQUIRED_REQUESTS = 10000
MIGRATION_MIN_MAX_WAIT_SECONDS = 5
MIGRATION_MAX_MAX_WAIT_SECONDS = 300


# 데이터 확인 접속처 `<host>:<port>/<구독 이름>`(구독 이름은 PostgreSQL 식별자).
REPLICATION_ENDPOINT_PATTERN = re.compile(
    r"(?P<host>[A-Za-z0-9.-]+):(?P<port>[0-9]{1,5})/(?P<subscription>[a-z_][a-z0-9_]{0,62})"
)


def _is_replication_endpoint(value: str) -> bool:
    match = REPLICATION_ENDPOINT_PATTERN.fullmatch(value.strip())
    return match is not None and 1 <= int(match["port"]) <= 65535


def _split_comma_list(value: object) -> object:
    """환경변수의 쉼표 구분 문자열을 목록으로 바꾼다. 빈 칸은 버린다."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class ExecutorMode(StrEnum):
    """빌드·배포·내리기 실행기.

    real 이면 GitHub Actions 빌드 + 쿠버네티스 직접 반영(API 서버 3-2).
    """

    FAKE = "fake"
    REAL = "real"


class HealthCheckerMode(StrEnum):
    FAKE = "fake"
    HTTP = "http"


class EntrypointExecutorMode(StrEnum):
    """트래픽·대표 주소·지표 실행기. http 면 고정 진입점(Envoy 가중치 API)을 부른다."""

    FAKE = "fake"
    HTTP = "http"


class DataSyncCheckerMode(StrEnum):
    """데이터 확인 실행기. postgres 면 출발 PostgreSQL 의 pg_stat_replication 을 읽는다."""

    FAKE = "fake"
    POSTGRES = "postgres"


class DbCutoverExecutorMode(StrEnum):
    """DB 쓰기 전환 단계(API 서버 17) 실행기.

    disabled(기본)면 계획 만들기의 includeCutover=true 를 409 로 거절한다. kubernetes 면 같은
    클러스터(서울 GKE)의 echo-ops 네임스페이스에 인프라 컷오버 Job 을 만든다.
    """

    DISABLED = "disabled"
    FAKE = "fake"
    KUBERNETES = "kubernetes"


class SourceClientMode(StrEnum):
    FAKE = "fake"
    GITHUB = "github"


class EnvironmentAdapterMode(StrEnum):
    """환경 접속 확인 어댑터(API 서버 12). real 이면 실제 클러스터에 붙어 버전·노드 수를 읽는다."""

    SIMULATED = "simulated"
    REAL = "real"


class AppSecretStoreMode(StrEnum):
    """고객 앱 환경 변수 보관함(API 서버 10). gcp 면 GCP Secret Manager, fake 면 프로세스 메모리."""

    FAKE = "fake"
    GCP = "gcp"


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

    # 기본 simulated: 어댑터가 고정 값(simulated=true)을 돌려준다. 운영은 실환경 확인 뒤 real 로.
    environment_adapter_mode: EnvironmentAdapterMode = EnvironmentAdapterMode.SIMULATED

    executor_mode: ExecutorMode = ExecutorMode.FAKE
    health_checker_mode: HealthCheckerMode = HealthCheckerMode.FAKE
    source_client_mode: SourceClientMode = SourceClientMode.FAKE

    # 고정 진입점(echo-infra 인프라 5·6·9). http 면 트래픽 비율·대표 주소·지표 실행기가
    # 진입점의 가중치 API 를 부른다. 토큰은 값이 아니라 비밀 저장소 위치(env:ECHO_SECRET_...)다.
    entrypoint_executor_mode: EntrypointExecutorMode = EntrypointExecutorMode.FAKE
    entrypoint_base_url: str | None = None
    entrypoint_token_ref: str | None = None
    # 환경 id(또는 환경 종류) → 진입점 환경 이름. 비면 환경 종류(aws·gcp)를 그대로 쓴다.
    entrypoint_environment_map: Annotated[list[str], NoDecode] = Field(default_factory=list)
    entrypoint_request_timeout_seconds: float = Field(default=5.0, gt=0)
    # 서비스 대시보드 지표(운영 2). 진입점 VM 의 Prometheus(:9090)를 직접 읽는다. 비면 기능을 끈다.
    # 진입점이 basic auth 를 켜면 비밀번호 참조(env:ECHO_SECRET_...)를 넣는다. 비면 인증 없이.
    entrypoint_prometheus_url: str | None = None
    entrypoint_prometheus_basic_auth_user: str = "echo-otel"
    entrypoint_prometheus_basic_auth_password_ref: str | None = None
    # 같은 서비스·구간·간격의 지표 응답을 다시 쓰는 시간(초).
    service_metrics_cache_seconds: int = Field(default=5, ge=1)

    # 데이터 확인(인프라 7). postgres 면 출발 환경 PostgreSQL 에서 도착 구독의 복제 지연을 읽는다.
    # 환경 id(또는 종류) → `<host>:<port>/<그 환경의 구독 이름>`. 비밀번호는 비밀 참조만.
    data_sync_checker_mode: DataSyncCheckerMode = DataSyncCheckerMode.FAKE
    data_sync_databases: Annotated[list[str], NoDecode] = Field(default_factory=list)
    data_sync_user: str = "echo_monitor"
    data_sync_database: str = "postgres"
    data_sync_password_ref: str | None = None
    data_sync_ssl_mode: Literal["require", "verify-full", "disable"] = "require"
    data_sync_timeout_seconds: float = Field(default=3.0, gt=0)
    # 컷오버 결과(API 서버 16). 이전 계획 상세가 고객 DB 의 echo_ops.cutover_runs 를 읽는다.
    # 접속처·계정은 위 data_sync_* 를 쓰고 DB 이름만 다르다. 빈 값이면 기능을 끈다.
    # not_before 보다 먼저 끝난 계획은 읽지 않는다(표가 생기기 전 계획에 나중 행이 붙지 않게).
    # cache_seconds 는 못 찾음·창 열림·실패 결과를 다시 쓰는 시간이다.
    cutover_runs_database: str = "app"
    cutover_results_not_before: AwareDatetime | None = None
    cutover_result_cache_seconds: int = Field(default=5, ge=1)
    # DB 쓰기 전환 단계(API 서버 17). kubernetes 면 image 는 echo-infra deploy/values.env 의
    # CUTOVER_IMAGE(@sha256 digest, VAP 가 고정)와 글자까지 같아야 한다. 결과 행은 위 cutover_runs_*
    # 연결로 계획 id 로 읽으므로 data_sync_checker_mode = postgres 가 필요하다.
    db_cutover_executor_mode: DbCutoverExecutorMode = DbCutoverExecutorMode.DISABLED
    db_cutover_namespace: str = "echo-ops"
    db_cutover_job_image: str | None = None
    db_cutover_kube_api_url: str = "https://kubernetes.default.svc"
    db_cutover_service_account_dir: str = "/var/run/secrets/kubernetes.io/serviceaccount"
    db_cutover_kube_timeout_seconds: float = Field(default=10.0, gt=0)
    # Job 을 만든 뒤 끝날 때까지 기다리는 상한(초).
    # Job 의 activeDeadlineSeconds(120) + 종료 유예(30) 보다 길게 둔다.
    # 넘으면 DB_CUTOVER_TIMEOUT(자동으로 되돌리지 않음).
    migration_db_cutover_timeout_seconds: int = Field(default=180, ge=10, le=1800)
    migration_db_cutover_poll_seconds: int = Field(default=3, ge=1, le=60)
    # 재난 트리거가 만드는 긴급 이전 계획에도 DB 쓰기 전환(ALLOW_PROMOTE=true)을 넣는가.
    migration_db_cutover_on_disaster: bool = False

    # 실제 빌드·배포·내리기 실행기(executor_mode = real, API 서버 3-2).
    # aws 환경이 config.roleArn 을 비워 두면 이 역할을 넘겨받는다.
    executor_aws_role_arn: str | None = None
    # 고객 앱 빌드 워크플로(workflow_dispatch). 저장소는 owner/name.
    build_github_repository: str = "Softbank-Hackathon-2026-Team-Echo/echo-was"
    build_github_workflow: str = "customer-build.yml"
    build_github_ref: str = "main"
    # 이미지 정본(Artifact Registry) 접두 → aws 환경에서 쓸 ECR 접두. `<AR 접두>=<ECR 접두>,…`
    image_registry_mirrors: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # 온프레 앱 주소 DNS 레코드(Cloudflare). 토큰은 값이 아니라 비밀 저장소 위치다.
    cloudflare_api_token_ref: str | None = None
    cloudflare_zone_id: str | None = None
    cloudflare_api_base_url: str = "https://api.cloudflare.com/client/v4"
    # 온프레 이미지 받기 자격(Secret echo-pull) 확인 주기와 갱신 기준 남은 수명(초).
    onprem_pull_check_seconds: int = Field(default=300, ge=10)
    onprem_pull_min_remaining_seconds: int = Field(default=1800, ge=60)
    # ErrImagePull 이 이 시간보다 오래 이어지면 IMAGE_PULL_FAILED.
    deploy_image_pull_grace_seconds: float = Field(default=30.0, ge=0)
    kube_request_timeout_seconds: float = Field(default=15.0, gt=0)
    # 배포 실패 진단(컨테이너 로그 읽기 + 환경 변수 재조회) 전체 절대 상한(API 서버 4-1).
    # 넘으면 진단 없이 실패 처리를 계속한다.
    container_diagnostics_timeout_seconds: float = Field(default=10.0, gt=0)
    # GCE·GKE 메타데이터 서버(워크로드 신원 토큰). 시험에서 바꾼다.
    google_metadata_base_url: str = "http://metadata.google.internal"

    # 고객 앱 환경 변수 보관함(API 서버 10). fake + executor_mode real 이면 쓰기 API 가 409 이고
    # 실행기는 앱 Secret 을 건드리지 않는다. 이름 접두어는 보관함 IAM 조건과 같아야 한다.
    app_secret_store_mode: AppSecretStoreMode = AppSecretStoreMode.FAKE
    app_secret_project_id: str | None = None
    app_secret_name_prefix: str = "echo-app-"

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
    # 할 일이 없을 때 다시 집으러 가기까지 쉬는 시간(초). 비우면 worker_poll_interval_seconds.
    # 이전 계획의 다음 읽기·최소 관찰 끝을 이 간격만큼 늦게 집을 수 있어 따로 뺐다
    # (배포·내리기의 실행기 재조회 주기 worker_poll_interval_seconds 는 그대로).
    worker_idle_poll_seconds: float | None = Field(default=None, gt=0, le=10)
    worker_lease_seconds: float = Field(default=30.0, gt=0)
    worker_concurrency: int = Field(default=1, ge=1, le=32)

    # 이전 계획(API 서버 8·8-1). 비율 단계는 도착이 요청 N건을 처리했는지 센다.
    # 필요 건수(5·10·25·50·75·100% 순)·단계 최대 대기(초)·예상 rps(예상 시간·가짜 카운터·simulate)·
    # 카운터 읽기 주기·마지막 성공 읽기가 이보다 오래되면 판단 불가·
    # p95 참고 읽기 창, 단계마다 더하는 여유 시간(예상 시간 계산), 승인 대기 만료, 롤백 최대 시도.
    migration_required_requests_default: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_MIGRATION_REQUIRED_REQUESTS)
    )
    migration_max_wait_seconds_default: int = Field(
        default=30, ge=MIGRATION_MIN_MAX_WAIT_SECONDS, le=MIGRATION_MAX_MAX_WAIT_SECONDS
    )
    migration_expected_rps: int = Field(default=50, ge=1, le=10000)
    migration_min_observe_seconds: int = Field(default=3, ge=0, le=60)
    # 카운터 읽기 주기. 1초 미만도 받는다(필요 건수·5xx 판정은 그대로, 읽는 간격만 짧아진다).
    migration_count_poll_seconds: float = Field(default=1.0, ge=0.2, le=10)
    migration_counts_stale_seconds: int = Field(default=5, ge=1, le=60)
    migration_p95_reference_window_seconds: int = Field(default=30, ge=5, le=300)
    migration_step_overhead_seconds: int = Field(default=2, ge=0, le=60)
    migration_plan_approval_ttl_seconds: int = Field(default=24 * 60 * 60, gt=0)
    migration_rollback_max_attempts: int = Field(default=3, ge=1, le=10)

    # 보안 게이트(보안·거버넌스 5). 켜면 이전 승인과 일꾼 실행 시작 때 필수 보안 통제를 판정해
    # 막는다(실행 직전 TLS 접속 시험 포함). 기본은 꺼짐이고, 꺼져 있으면 판정·기록을 하지 않는다.
    security_gate_enabled: bool = False
    # 게이트가 켜진 일꾼이 공개 경로 TLS 를 주기로 시험해 C-TLS-1 준비 증거(유효 1시간)를
    # 남기는 간격. 증거가 비지 않게 유효기간보다 짧아야 한다(최대 50분).
    security_tls_probe_interval_seconds: int = Field(default=1800, ge=60, le=3000)
    # 게이트가 켜진 일꾼이 C-IAM-2·C-AUD-1 자기 시험(보안 5-3)을 도는 간격. 증거 유효기간(24시간)
    # 안에 세 번 이상 돌도록 최대 8시간.
    security_platform_self_test_interval_seconds: int = Field(default=21600, ge=300, le=28800)
    # 켜면 패치 승인이 반영 실행(APPLY_PATCH)을 만든다. 러너가 그 실행을 가져가는 길이 생기기 전에는
    # 켜지 않는다(실행이 QUEUED 로 마감까지 머물고 그동안 그 서비스의 새 사전 점검이 409 다).
    patch_apply_enabled: bool = False
    # 배포 실패 진단 만들기(POST .../diagnosis)가 본문의 simulate 를 받는가(SPEC api-98). 받으면
    # 러너 없이 WAS 가 가짜 진단(simulated = true)을 바로 끝내고, 그 패치 승인도 가짜 반영으로
    # 끝낸다.
    # 러너 진단(echo-ai-agent #22)이 붙기 전 화면 연동·시연용이다. 끄면 simulate 는 422 다.
    ai_diagnosis_simulate_enabled: bool = True
    # 켜면 본문 없는 진단 만들기가 러너에게 가는 실행(QUEUED)을 만든다. 꺼져 있으면(기본) 본문이
    # 없어도 가짜 진단(simulate = DIAGNOSED)으로 끝낸다 — CHANGE_PROPOSAL 을 가져가는 러너
    # (echo-ai-agent #22)가 배포되기 전에 켜면 실행이 마감(15분)까지 QUEUED 로 머물고 그동안 그
    # 서비스의 새 사전 점검이 409 다.
    ai_diagnosis_runner_enabled: bool = False
    # 켜면 러너가 검증한 진단 패치 반영(APPLY_PATCH SUCCEEDED)을 WAS 가 GitHub App 으로 서비스
    # 저장소에 브랜치 echoport/fix-<run8> + 커밋 + PR 로 넣고, 재배포를 그 커밋으로 만든다
    # (SPEC api-19). App 에 contents·pull_requests 쓰기 권한이 있어야 한다. 꺼지면 #98 동작 그대로
    # (원래 실패 커밋을 다시 배포). 가짜 반영(simulated)은 켜져 있어도 GitHub 를 부르지 않는다.
    ai_apply_commit_enabled: bool = False
    # CVE 감시(SPEC sec-6-cve-watch). 켜면 일꾼이 cve_watch_interval_seconds 마다 서비스 의존성을
    # OSV 로 조회해 새 HIGH·CRITICAL 취약점을 C-IMG-1 증거(source CVE_WATCH)로 남기고, 즉시 조회
    # API(`POST /security/cve-watch:check`)도 열린다. 끄면 둘 다 없다(조회 API 409).
    cve_watch_enabled: bool = False
    cve_watch_interval_seconds: int = Field(default=21600, ge=300, le=86400)
    # 가짜 권고 주입 API(`POST /security/cve-watch:inject`, simulated 증거 — 판정에 쓰지 않음).
    cve_watch_simulate_enabled: bool = True
    cve_watch_osv_url: str = "https://api.osv.dev"
    cve_watch_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    # 한 번에 상세를 받는 취약점 수 상한. 넘는 것은 미처리 목록에 남겨 다음 조회 때 먼저 본다.
    cve_watch_max_advisories: int = Field(default=200, ge=1, le=2000)
    # 판정 범위의 WAS 버전(C-IAM-2·C-AUD-1 의 증거·예외와 맞춘다). 비면 그 통제는 확인 불가다.
    was_version: str | None = Field(default=None, min_length=1, max_length=64)

    # 실제 재난 피드(API 서버 18). 켜면 일꾼이 interval 마다 USGS 지진 피드(GeoJSON, 인증 없음)를
    # 읽어 조직 기준(규모·반경)에 맞는 지진으로 재난(source = LIVE_FEED)을 만든다. 기본은 꺼짐이고,
    # 꺼져 있으면 일꾼이 피드를 부르지 않는다. 시연 주입(POST /disasters/feed:inject)은 스위치와
    # 상관없다.
    live_disaster_feed_enabled: bool = False
    live_disaster_feed_url: str = Field(
        default="https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
        min_length=1,
    )
    live_disaster_feed_interval_seconds: int = Field(default=60, ge=30, le=3600)
    live_disaster_feed_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    # 피드 응답 본문 상한(바이트). 넘으면 읽기를 멈추고 그 주기를 버린다(FEED_TOO_LARGE).
    # all_hour 피드는 보통 수십 KB 다.
    live_disaster_feed_max_bytes: int = Field(
        default=5 * 1024 * 1024, ge=64 * 1024, le=50 * 1024 * 1024
    )

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
        "entrypoint_environment_map",
        "data_sync_databases",
        "image_registry_mirrors",
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
                "migration_required_requests_default must be 6 integers between 1 and 10000"
            )
        return value

    @field_validator("entrypoint_environment_map")
    @classmethod
    def _check_environment_map(cls, value: list[str]) -> list[str]:
        for item in value:
            key, separator, name = item.partition("=")
            if not separator or not key.strip() or not name.strip():
                raise ValueError("entrypoint_environment_map items must be <environment>=<name>")
        return value

    @field_validator("image_registry_mirrors")
    @classmethod
    def _check_image_registry_mirrors(cls, value: list[str]) -> list[str]:
        for item in value:
            source, separator, mirror = item.partition("=")
            if not separator or not source.strip() or not mirror.strip():
                raise ValueError("image_registry_mirrors items must be <source>=<mirror>")
        return value

    @field_validator("build_github_repository")
    @classmethod
    def _check_build_github_repository(cls, value: str) -> str:
        if not _GITHUB_REPOSITORY_PATTERN.fullmatch(value):
            raise ValueError("build_github_repository must be owner/name")
        return value

    @field_validator("cloudflare_api_token_ref")
    @classmethod
    def _check_cloudflare_api_token_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parse_secret_ref(value)
        except InvalidSecretRefError:
            raise ValueError("cloudflare_api_token_ref must be a secret reference") from None
        return value

    @model_validator(mode="after")
    def _check_real_executor_settings(self) -> "Settings":
        """실제 실행기는 GitHub App·실제 소스 클라이언트·실제 헬스체커가 있어야 한다.

        시작 때 설정 오류로 막는다. 값(비밀값)은 읽지 않고 설정이 있는지만 본다.
        """
        if self.executor_mode is not ExecutorMode.REAL:
            return self
        missing = [
            name
            for name, is_set in (
                ("github_app_id", bool(self.github_app_id)),
                ("github_app_private_key_ref", bool(self.github_app_private_key_ref)),
                ("source_client_mode", self.source_client_mode is SourceClientMode.GITHUB),
                ("health_checker_mode", self.health_checker_mode is HealthCheckerMode.HTTP),
            )
            if not is_set
        ]
        if missing:
            raise ValueError(
                "executor_mode real requires github app, source_client_mode github and "
                "health_checker_mode http: " + ", ".join(missing)
            )
        return self

    @field_validator("app_secret_name_prefix")
    @classmethod
    def _check_app_secret_name_prefix(cls, value: str) -> str:
        if not _APP_SECRET_NAME_PREFIX_PATTERN.fullmatch(value):
            raise ValueError("app_secret_name_prefix must be lowercase letters, digits and hyphens")
        return value

    @model_validator(mode="after")
    def _check_app_secret_settings(self) -> "Settings":
        if self.app_secret_store_mode is AppSecretStoreMode.GCP and not self.app_secret_project_id:
            raise ValueError("app_secret_project_id is required when app_secret_store_mode is gcp")
        return self

    @property
    def is_app_secret_store_configured(self) -> bool:
        """실제 실행기 + 가짜 보관함 조합이 아닌가. 그 조합이면 앱 Secret 을 건드리지 않는다."""
        return not (
            self.app_secret_store_mode is AppSecretStoreMode.FAKE
            and self.executor_mode is ExecutorMode.REAL
        )

    @field_validator("entrypoint_base_url")
    @classmethod
    def _strip_entrypoint_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.rstrip("/") or None

    @field_validator("entrypoint_token_ref")
    @classmethod
    def _check_entrypoint_token_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parse_secret_ref(value)
        except InvalidSecretRefError:
            raise ValueError("entrypoint_token_ref must be a secret reference") from None
        return value

    @field_validator("entrypoint_prometheus_url")
    @classmethod
    def _strip_entrypoint_prometheus_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().rstrip("/") or None

    @field_validator("entrypoint_prometheus_basic_auth_password_ref")
    @classmethod
    def _check_prometheus_password_ref(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        try:
            parse_secret_ref(value)
        except InvalidSecretRefError:
            raise ValueError(
                "entrypoint_prometheus_basic_auth_password_ref must be a secret reference"
            ) from None
        return value

    @model_validator(mode="after")
    def _check_entrypoint_settings(self) -> "Settings":
        if self.entrypoint_executor_mode is EntrypointExecutorMode.HTTP and not (
            self.entrypoint_base_url and self.entrypoint_token_ref
        ):
            raise ValueError(
                "entrypoint_base_url and entrypoint_token_ref are required when "
                "entrypoint_executor_mode is http"
            )
        return self

    @model_validator(mode="after")
    def _check_db_cutover_settings(self) -> "Settings":
        if self.db_cutover_executor_mode is not DbCutoverExecutorMode.KUBERNETES:
            return self
        if not self.db_cutover_job_image or "@sha256:" not in self.db_cutover_job_image:
            raise ValueError(
                "db_cutover_job_image must be an image digest (…@sha256:…) when "
                "db_cutover_executor_mode is kubernetes"
            )
        if not self.cutover_results_enabled:
            raise ValueError(
                "db_cutover_executor_mode kubernetes needs data_sync_checker_mode postgres and "
                "cutover_runs_database to read cutover results"
            )
        return self

    @property
    def cutover_results_enabled(self) -> bool:
        """이전 계획 상세가 고객 DB 의 컷오버 결과 표를 읽어도 되는가."""
        return self.data_sync_checker_mode is DataSyncCheckerMode.POSTGRES and bool(
            self.cutover_runs_database
        )

    @field_validator("data_sync_databases")
    @classmethod
    def _check_data_sync_databases(cls, value: list[str]) -> list[str]:
        for item in value:
            key, separator, endpoint = item.partition("=")
            if not separator or not key.strip() or not _is_replication_endpoint(endpoint):
                raise ValueError(
                    "data_sync_databases items must be <environment>=<host>:<port>/<subscription>"
                )
        return value

    @field_validator("data_sync_password_ref")
    @classmethod
    def _check_data_sync_password_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parse_secret_ref(value)
        except InvalidSecretRefError:
            raise ValueError("data_sync_password_ref must be a secret reference") from None
        return value

    @model_validator(mode="after")
    def _check_data_sync_settings(self) -> "Settings":
        if self.data_sync_checker_mode is DataSyncCheckerMode.POSTGRES and not (
            self.data_sync_databases and self.data_sync_password_ref
        ):
            raise ValueError(
                "data_sync_databases and data_sync_password_ref are required when "
                "data_sync_checker_mode is postgres"
            )
        return self

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

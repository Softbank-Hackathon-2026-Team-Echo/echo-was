# Echo Control API (echo-was)

Team Echo 멀티 클라우드 관리 플랫폼의 **관리 평면 API 서버**다. AWS·GCP·온프레 클러스터를 환경으로 등록하고, 서비스를 여러 환경에 배포·이전하며, 그 상태를 한곳에서 보여 준다. 지금은 골격·공통 모델(환경·서비스·배포), 환경 API(등록·조회·수정·삭제·접속 확인, 가짜 어댑터), 서비스 API와 원터치 배포 API(접수 + 일꾼, 기본 가짜 실행기 + 실제 실행기: GitHub Actions 빌드·쿠버네티스 직접 반영), GitHub 로그인 + 조직·역할 권한(세션 쿠키·에이전트 토큰·거부 기록), 배포 이력·되돌리기·로그 API(배포 작업 로그는 DB, 빌드·실행 로그는 가짜 실행기), 이전 계획 생성·승인·실행 API(트래픽·지표·복제 확인은 가짜 실행기), 재난 버튼 → 피해 서비스 긴급 이전 계획 자동 생성 API, 감사 로그 저장·조회 API(쓰기·끝 상태·거부·에이전트 읽기), AI 사전 점검 API + 러너 계약(만들기·가져가기·이벤트·결과, C-AI-1 증거), AI 실행의 데이터 레지던시 기록·조직 요약 API까지 있다.

- **스택**: Python 3.13+, FastAPI, Pydantic v2, SQLAlchemy 2.0(async), PostgreSQL(asyncpg), Alembic, uv
- **배경·환경변수·실행·세팅**: [README.md](README.md) 참조 (여기서 중복하지 않는다)
- **원장**: 본선 필수 기능 정리 `final_features_v6.md`(F1~F17). 기능 번호는 이 원장을 따른다.

---

## 규칙 문서 (반드시 따른다)

작업 전 아래 문서를 우선한다. **충돌 시 우선순위: 용어 사전 > 코드 규칙 > 일반 관례.**

| 문서 | 언제 본다 |
|---|---|
| [.claude/rules/term-dictionary.md](.claude/rules/term-dictionary.md) | 도메인 용어·엔티티·필드명·Enum 코드를 정할 때 — **표준 영문 용어/식별자의 단일 출처**. 동음이의어(Service·Deployment·Environment·Config) 구분 포함 |
| [.claude/rules/db-schema.sql](.claude/rules/db-schema.sql) | 테이블·컬럼·타입·제약·인덱스 등 **물리 DB 스키마(DDL)의 단일 출처**. 직접 고치지 않고 `scripts/dump_schema.sh` 로 뽑는다. 모델 변경은 [.claude/logs/db-schema-changelog.md](.claude/logs/db-schema-changelog.md) 에 자동 기록된다 |
| [.claude/rules/db-migration.md](.claude/rules/db-migration.md) | 스키마를 바꿀 때 — Alembic revision 생성·검토·downgrade 규칙, `alembic check` 가 못 보는 것(CHECK·부분 인덱스·트리거) |
| [.claude/rules/backend-conventions.md](.claude/rules/backend-conventions.md) | 백엔드 코드(레이어·네이밍·비동기·예외·로깅·설정·테스트·린터)를 작성할 때. §2.5 에 이 저장소의 DB 규칙 |
| [.claude/rules/git-conventions.md](.claude/rules/git-conventions.md) | 커밋 메시지·브랜치명을 정할 때 — Conventional Commits 규칙 |

## 설계 문서

| 문서 | 언제 본다 |
|---|---|
| [docs/schema.md](docs/schema.md) | 공통 모델(ER·칸 설명·행 예시·상태 흐름·설계 이유·새 환경 추가 방법)을 다룰 때. **스키마 설명의 원본** |
| [docs/api/environments.md](docs/api/environments.md) | 환경 API 계약(엔드포인트·오류 코드·secretRef 형식·null 규칙)을 바꾸거나 쓸 때 |
| [docs/api/services.md](docs/api/services.md) · [docs/api/deploy-requests.md](docs/api/deploy-requests.md) | 서비스·원터치 배포·이력·되돌리기·로그 API 계약(역할·버전 상태·겹침 규칙·실패 코드·로그 줄·`simulated`)을 바꾸거나 쓸 때 |
| [docs/executors.md](docs/executors.md) | 실행기(빌드·배포·대표 주소·헬스·내리기·로그 읽기)를 바꾸거나 실제 구현을 붙일 때 — 멱등 키·호출 순서·로그 읽기·마스킹 계약 |
| [docs/api/variables.md](docs/api/variables.md) | 고객 앱 환경 변수 API(쓰기 전용·권한·오류·`pendingRedeploy`)를 바꾸거나 쓸 때. 주입 규칙은 [docs/executors.md](docs/executors.md) "앱 환경 변수" |
| [docs/api/security-exceptions.md](docs/api/security-exceptions.md) | 기한부 예외 API(통제별 범위 칸·7일 상한·자기 승인·상태)를 바꾸거나 쓸 때 |
| [docs/api/migration-plans.md](docs/api/migration-plans.md) | 이전 계획(만들기 거절 순서·승인·자기 승인·지문·단계 실행·롤백·중단·실행 로그)을 바꾸거나 쓸 때 |
| [docs/api/disasters.md](docs/api/disasters.md) | 재난(미리보기·트리거 흐름·도착 고르기·`plans[]`·복귀 차단·감사)을 바꾸거나 쓸 때 |
| [docs/api/audit-logs.md](docs/api/audit-logs.md) | 감사 로그(조회 질의·응답·행동 이름표·결과 규칙·`component`·기록하지 않는 것)를 바꾸거나 쓸 때 |
| [docs/api/security.md](docs/api/security.md) | 보안 매트릭스·증거 조회·이전 전후 비교 API(칸 계산·서비스 단위 통제·UNKNOWN·증거 목록 조건·`blocksMigration`)를 바꾸거나 쓸 때 |
| [docs/api/costs.md](docs/api/costs.md) | 클라우드 비용 API(ESTIMATE·BILLING 구분·계산·제외 항목·데이터 파일·청구 갱신·단가 출처)를 바꾸거나 쓸 때 |
| [docs/api/metrics.md](docs/api/metrics.md) | 서비스 대시보드 지표 API(진입점 Prometheus PromQL·`metricsStatus`·에러 버짓 공식·복제 지연·5초 캐시·basic auth 설정)를 바꾸거나 쓸 때 |
| [docs/api/prechecks.md](docs/api/prechecks.md) · [docs/agent-runner.md](docs/agent-runner.md) | AI 사전 점검 사람 쪽 API(만들기 거절 순서·입력 사본·`simulated`·`aiReviewed`·반영 실행 상세·이벤트 조회)와 러너 쪽 계약(토큰·임대·이벤트·`RunResult`·감사)을 바꾸거나 쓸 때 |
| [docs/api/ai-residency.md](docs/api/ai-residency.md) | AI 데이터 레지던시 조직 요약(계산 표·민감 등급 판정·정책 위반·기본 정책)을 바꾸거나 쓸 때. 러너 칸 규칙은 [docs/agent-runner.md](docs/agent-runner.md) |
| [docs/api/auth.md](docs/api/auth.md) · [docs/api/organizations.md](docs/api/organizations.md) | 로그인·세션·Origin·권한 검사 순서·역할 표·거부 기록, 조직·멤버·초대·에이전트 토큰 API 를 바꾸거나 새 엔드포인트에 검사기를 걸 때 |

---

## 구조

```text
app/
├─ main.py            # API 진입점 (FastAPI). /healthz, /readyz, 라우터 등록, 모듈 로드 때 discover()
├─ routers/           # auth·organization·invitation·agent_token + environment·environment_kind·service·service_variable·deploy_request·migration_plan·disaster·audit_log·security·security_exception·precheck·ai_residency 라우터
│                     #   (/api/v1, 조직 리소스는 /api/v1/orgs/{org_id} 아래. 라우트마다 검사기 하나) + runner 라우터(/api/v1/runner/runs, 러너·실행 토큰)
├─ audit/             # 감사 로그: actions(행동 이름표·쓰기 경로 표·러너 경로 표) · recorder(AuditRecorder) · lifecycle(배포·이전 대상 값·끝 줄) · context(요청 값 contextvar)
├─ auth/              # guards(require_login·require_human·require_org_role) · runner_guards(require_runner·require_run_token) · audit(거부·에이전트 읽기 감사 줄) · tokens · cookies · actors
├─ services/          # EnvironmentService · EnvironmentConnectionService · ServiceService · DeployRequestService(접수·되돌리기·이력·로그)
│                     #   AuthService(로그인·세션·/me) · OrganizationService · InvitationService · AgentTokenService
│                     #   DeployRequestProcessor · ServiceTeardownProcessor(일꾼 단계) · deploy_support · service_view · job_lease
│                     #   deploy_job_log(배포 작업 로그 기록기) · deploy_log_messages(로그·오류 고정 문장)
│                     #   MigrationPlanService(이전 계획 API) · MigrationPlanProcessor(이전 일꾼 단계) · migration_plan_support(단계·지문·대상)
│                     #   migration_counting(비율 단계 요청 N건 세기 규칙, 순수 계산)
│                     #   security_rules(공통 보안 규칙표·판정·매트릭스 칸 요약, 순수 계산, 보안 3)
│                     #   security_normalizers(Trivy·gitleaks·TLS 시험 원문 → 공통 증거, 비밀값 원문 버림, 보안 2)
│                     #   SecurityFindingService(증거 기록·사람 점검 기록·판정용 조회, 보안 2)
│                     #   BuildSecurityScanCollector(빌드 성공 직후 보안 검사 원문 → findings C-IMG-1·C-IMG-2, 실패해도 배포 계속, 보안 2·11)
│                     #   SecurityMatrixService(환경 × 4칸 매트릭스·증거 목록, 서비스 단위 통제는 지금 이미지로, C-SEC-1 은 서비스 중 연결 기준 키 비교를 조회 때 계산, 보안 4)
│                     #   SecurityCompareService(이전 전후 비교: 두 환경 매트릭스 칸 + 도착 기준 blocksMigration = 게이트 is_blocking, 보안 5-2)
│                     #   migration_plan_log(이전 실행 로그 기록기) · migration_log_messages · migration_guard(실행 중 접수 거절)
│                     #   AuditLogService(감사 로그 조회)
│                     #   DisasterService(재난 미리보기·만들기·종료, 피해 서비스 긴급 계획 트리거) · disaster_rules(반경·거리·분류·도착 고르기, 순수 계산)
│                     #   ServiceVariableService(앱 환경 변수 API) · app_env_values(배포 기록 참조 → 실행기 값, 표대로 None·{}·값)
│                     #   SecurityExceptionService(기한부 예외 요청·승인·철회·조회, 만든 시각은 앱 시계, 보안 3)
│                     #   SecurityGate(보안 게이트: 이전 승인·일꾼 시작 때 실행 판정, 실행 직전 C-IMG-3·C-TLS-1·C-SEC-1(출발·도착 앱 환경 변수 키) 확인, 기본 꺼짐, 보안 5)
│                     #   PeriodicTlsProbe(게이트가 켜진 일꾼의 공개 경로 TLS 주기 시험 → C-TLS-1 준비 증거, 매트릭스용)
│                     #   PrecheckService(AI 사전 점검 만들기·조회·이벤트·취소·입력 사본) · AgentRunService(러너: 가져가기·하트비트·이벤트·결과, 부를 때 만료)
│                     #   run_result(러너 결과 정규화·패치 상태 재계산, 순수 계산)
│                     #   ai_residency(레지던시 요약·판정·위반, 순수 계산) · AiResidencyService(조직 요약 조회)
│                     #   service_metrics(대시보드 지표: 범위 검증·PromQL 조립·응답 변환·5초 캐시, ServiceMetricsService, 운영 2)
├─ repositories/      # Environment·Service·ServiceEnvironment·DeployRequest·DeployRequestLog·Build·Deployment·Job Repository (조건부 UPDATE)
│                     #   User·Organization·OrganizationMember·Invitation·UserSession·AgentToken·AuditLog Repository
│                     #   MigrationPlan·MigrationPlanLog Repository · Disaster Repository · Finding Repository(보안 증거, 조직 조건 필수)
│                     #   ServiceEnvironmentVariable Repository(앱 환경 변수 행, 값 없음)
│                     #   SecurityException Repository(판정용: 승인·미철회만) · AgentRun Repository(AI 실행 조회·조건부 전이, 최근 성공한 AI 사전 점검)
│                     #   AgentRunner·AgentRunEvent Repository(러너 신원, 이벤트 ON CONFLICT 무시)
├─ clients/           # secret_client(env 만 실제) · source_repository(인터페이스 + 가짜) · github_source_repository(GitHub App)
│                     #   github_user(로그인·초대용 GitHub 사용자 클라이언트, 가짜는 tests/ 에만)
│                     #   tls_probe(공개 경로 TLS 접속 시험: 인증서·호스트명·버전·만료, 네트워크만.
│                     #     PublicHostTlsChecker = Echo 공개 도메인 주소만, 443, DNS 로 확인한 공인 IP 로만 접속)
│                     #   app_secret_store(앱 환경 변수 보관함: GCP Secret Manager + 가짜, 지우는 메서드 없음)
│                     #   prometheus_client(진입점 Prometheus query_range·query, basic auth 비밀 참조, 운영 2)
├─ executors/         # 실행기 인터페이스(base, 빌드·실행 로그 읽기·트래픽·지표·데이터 확인 포함) · 가짜(fake) · HttpHealthChecker · 고정 진입점(entrypoint: 트래픽·대표 주소·지표). 고르는 곳 build_executor_set
│                     #   실제 빌드 github_actions(workflow_dispatch) · 실제 배포·내리기 kubernetes/(access 접속기·api REST·manifests·executors)
│                     #   cloudflare_dns(온프레 앱 DNS) · google_metadata(메타데이터 토큰) · retry(일시 오류 재시도)
│                     #   postgres_data_sync(데이터 확인: 출발 pg_stat_replication 복제 지연, 도착이 이미 쓰기 담당이면 TARGET_IS_WRITER 로 통과, DATA_SYNC_CHECKER_MODE=postgres)
├─ worker/            # python -m app.worker: 할 일 폴링(SKIP LOCKED + 임대), run_once(), SIGTERM 처리
├─ adapters/          # 환경 종류별 어댑터: 설정 양식 + 동작 클래스(기본 가짜, `ENVIRONMENT_ADAPTER_MODE=real` 이면 실제 클러스터)
│   ├─ base.py        #   EnvironmentConfig, EnvironmentAdapter, ClusterInfo, 등록 데코레이터 2개
│   ├─ registry.py    #   discover()(양식·동작 짝 검사), validate_environment_config(by_alias), get_environment_adapter()
│   └─ aws.py · gcp.py · onprem.py   # 종류마다 파일 하나
├─ models/            # SQLAlchemy: services, environments, service_environments, deployments, builds, deploy_requests, deploy_request_logs, jobs
│                     #   users, organizations, organization_members, invitations, sessions(UserSession), agent_tokens, audit_logs
│                     #   migration_plans, migration_plan_logs, disasters(재난, API 서버 9), findings(보안 증거, 보안 2)
│                     #   security_exceptions(기한부 예외) · agent_runners·agent_runs·agent_run_events·agent_patch_approvals(AI 실행, 러너 계약 v1)
│                     #   service_environment_variables(앱 환경 변수 이름·참조·버전, 값 칸 없음)
├─ data/              # 클라우드 비용 데이터(운영 1): cloud_prices.toml·cloud_inventory.toml(손으로 고침) · cloud_billing.json(scripts/import_billing 이 씀). 읽는 곳 services/cost_catalog·cost_service, API routers/cost_router
├─ schemas/           # API 요청·응답 (공통 봉투 ApiResponse[T], 환경·서비스·배포 요청·조직 스키마)
├─ scripts/           # python -m app.scripts.demo_seed (데모 조직·멤버·에이전트 토큰) · issue_runner_token (러너 토큰 발급·철회)
├─ core/              # config, database, exceptions, exception_handlers, logging, middleware, secrets, clock, repo_url, log_masking
├─ dependencies.py    # Depends 주입 함수 (세션 팩토리·서비스·어댑터·소스 클라이언트·GitHub 사용자 클라이언트·시계·비밀값 조회기·로그용 실행기 묶음 get_executor_set)
└─ enums.py           # 배포·배포 요청·빌드·할 일·서비스 상태, 조직 역할·초대 상태·행위자 종류, 이전 계획 상태·종류·단계·원인, 보안 판정 값 (용어 사전 §3)
alembic/              # 마이그레이션
scripts/              # dump_schema.sh
tests/                # 단위 테스트 · adapter_contract.py·executor_contract.py(공통 계약) · github_fake.py·github_user_fake.py(가짜 GitHub)
                      #   integration/ (실제 PostgreSQL, deploy_harness.py, auth_support.py: 멤버·세션·토큰 행 + 쿠키 클라이언트,
                      #   migration_support.py: aws 서비스 중 + gcp 예비 + 개발자 A·B, conftest 의 migration 픽스처)
```

- 레이어는 `routers/`(API) → `services/`(유스케이스, `session.begin()` 블록으로 트랜잭션) → `repositories/`(DB) 순서다. 외부 시스템 호출은 `clients/` 와 `adapters/` 의 동작 코드가 맡는다.
- **권한 검사는 라우트마다 의존성 하나**: 조직 경로는 `ViewerAccess`·`DeveloperAccess`·`AdminAccess`·`Human*Access`(= `require_org_role(min_role, human_only)`), 조직 밖은 `Human`(`require_human`)·`LoggedIn`(`require_login`). 공개 경로는 `/healthz`·`/readyz`·로그인 시작·콜백·로그아웃뿐이다. 검사기는 요청 공용 세션을 쓰지 않고 팩토리로 짧은 세션을 따로 연다. 새 라우트에 검사기가 빠지면 `test_every_route_has_guards` 와 역할 표 시험(`test_role_table_covers_every_api_route`)이 실패한다.
- 세션은 `get_session_factory()`(`expire_on_commit=False`)로 만들고 요청 의존성은 트랜잭션을 열지 않은 세션을 넘긴다. 시험은 `app.dependency_overrides` 로 세션 팩토리·설정·어댑터 조회를 바꾼다.
- **import 방향**: `app.models` → `app.adapters` 만 허용한다. `app.adapters` 는 `app.models` 를 import 하지 않는다(테스트가 확인).
- Worker 진입점은 `python -m app.worker`(같은 이미지, compose `worker` 프로필). 로그 `component` 는 `echo-worker`. 일꾼은 `Worker.run_once()` 와 주입한 시계(`Clock`)로 시험한다(실제 sleep 없음). `Worker._advance` 는 `match job.kind` 로 처리기를 고르고 모르는 kind 는 `UnknownJobKindError`(조용히 DONE 금지).

---

## 핵심 원칙 (요약)

코드를 쓰기 전 위 문서를 보되, 자주 어기는 핵심만 추린다.

### 네이밍·식별자
- 필드·변수·컬럼은 **`snake_case`**. camelCase 금지(프론트 경계에서만 alias 변환).
- 레이어는 접미사로 드러낸다: `_router` / `Service` / `Client` / `Repository` / Model·Schema.
- 약어는 클래스명에서 첫 글자만 대문자(`HttpClient`, `AwsEnvironmentConfig`), 필드는 전부 소문자.
- 동사로 반환 형태를 고정: `get_*`(1건/없으면 예외) · `find_*`(nullable) · `search_*`(다건) · `check_*`/`validate_*`.

### 레이어 경계
- 흐름: `Router → Service → Repository → DB` / Service → Client·Adapter(외부).
- Router는 얇게(로직 없음, Service 호출 + Schema 변환만). Repository는 Model만 반환. Model은 Schema를 모른다.
- 모든 JSON 응답은 공통 봉투 **`ApiResponse[T]`** 로 감싼다. 에러도 같은 봉투(`success=False`, 도메인 예외의 `code`, 검증 실패 시 `details`)로 예외 핸들러가 만든다. 요청 ID 는 본문이 아니라 `X-Request-ID` 헤더로 준다(앞단이 준 값이 형식에 맞으면 이어 쓴다).

### 공통 모델 (이 저장소에서 자주 어기는 것)
- **서비스 하나는 여러 환경에 동시에 있을 수 있다.** 서비스↔환경은 `service_environments` 연결 행으로만 잇는다.
- **새 환경 종류 = `app/adapters/<kind>.py` 파일 하나**(설정 양식 `@register_environment_kind` + 동작 클래스 `@register_environment_adapter`). 모델·마이그레이션·`registry.py`·라우터·서비스를 고치지 않는다(NFR-15). `kind` 를 Enum·DB CHECK 로 만들지 않는다. 동작 클래스는 `tests/adapter_contract.py` 의 공통 계약을 통과해야 한다.
- `config` 키는 DB snake_case, API camelCase. API 경계는 `validate_environment_config(kind, config, by_alias=True)`, DB 쪽은 기본값(snake_case)으로 검사한다.
- **API 는 비밀값을 받지 않는다**(`secretRef` 위치만). 비밀값은 `SecretValue` 로만 다루고, 환경변수 직접 읽기는 `app/clients/secret_client.py` 한 곳만 허용한다(`ECHO_SECRET_` 접두어).
- **환경은 Session 객체로만 쓴다.** `session.add(env)` / `env.config = {...새 dict...}`. 제자리 수정(`env.config["x"] = ...`)은 저장되지 않고, `insert(Environment)`·`update(Environment)` 는 거부되며, Core·raw SQL 쓰기는 금지(검사를 건너뛴다).
- **DB 에 비밀값을 넣지 않는다.** 환경은 `secret_ref`(보관 위치)만, `config_snapshot` 에도 비밀 참조만.
- **배포 행은 지우지 않는다**(트리거가 막음). 재시도·되돌리기는 새 행이고, 되돌리기는 `rollback_of_id` + 원본의 이미지·설정 복사.
- 현재 배포는 `current_deployment_id` 값을 넣어 바꾼다(`current_deployment` 관계는 읽기 전용). 같은 연결의 배포만 가리킬 수 있다(복합 FK).
- `traffic_weight` 는 "마지막으로 프록시에 적용한 값"이다. 합계 100% 는 DB 가 아니라 이전 엔진이 맞춘다. 역할(서비스 중·예비)은 저장하지 않고 이 값과 현재 배포로 계산한다.

### 조직·권한 (자주 어기는 것)
- **서비스·환경은 조직에 속한다**(`organization_id`). API 가 쓰는 저장소 조회(`get_live_by_id`·`search_live`·`count_live`·`find_live_by_id`)는 `organization_id` 를 필수로 받고, 다른 조직 것은 각 리소스의 404 다. 배포 요청은 조직 칸이 없어 `services` 와 JOIN 해 거른다. 일꾼이 쓰는 조회(`find_by_id`)는 조직 조건 없이 둔다.
- 서비스 이름은 조직 안 유일(`uq_services_org_name_live`), 환경 이름도 조직 안 유일(`uq_environments_org_name_live`). 주소 유일성은 `services.host_suffix`(무작위 8자) + `public_host` 전체 unique 가 맡는다.
- 고객 앱 주소는 도메인 바로 아래 한 단계: 대표 `{서비스}-{suffix}.{도메인}`, 환경별 `{서비스}-{환경}-{같은 suffix}.{도메인}`(`deploy_support` 의 함수로만 만든다, 63자 넘으면 앞부분 54자로 자름). suffix 는 `get_host_suffix_generator` 로 주입하고 시험은 고정 값으로 바꾼다. 대표 주소가 겹치면 새 suffix 로 3번 다시 시도한 뒤 409 `PUBLIC_HOST_CONFLICT`.
- `env:` 비밀 참조는 `ECHO_SECRET_<조직 키>__` 로 시작해야 한다(422 `SECRET_REF_NOT_IN_ORGANIZATION`). 조직 slug 는 바꾸지 않는다.
- 403 은 `AuditedForbiddenError` 하위(ROLE_FORBIDDEN·AGENT_READ_ONLY·CANNOT_CHANGE_OWN_ROLE·ORIGIN_NOT_ALLOWED)로 raise 한다. 예외 처리기가 `request.state` 의 팩토리로 거부 줄을 `audit_logs` 에 따로 커밋한다. 401·404 는 기록하지 않는다.
- 멤버 변경은 조직 행 `FOR NO KEY UPDATE` 뒤 잠금 안에서 행위자 역할을 다시 읽는다(자기 역할 변경 금지·어드민 1명 이상). 첫 로그인·초대 적용·개인 조직 slug 는 `ON CONFLICT DO NOTHING`.
- 세션 쿠키 값·에이전트 토큰 원문(발급 응답 제외)·GitHub 사용자 토큰·client secret 은 저장·로그·응답에 넣지 않는다(DB 에는 SHA-256 만). 개발용·비상용 로그인과 설정으로 켜는 가짜 GitHub 는 만들지 않는다(가짜는 시험의 의존성 교체로만).

### 원터치 배포 (자주 어기는 것)
- **잠금 순서는 하나**: 조직(멤버 변경·초대만, `FOR NO KEY UPDATE`) → 환경(접수만, `FOR SHARE`) → 서비스 → 그 서비스의 연결(id 오름차순) → 배포 요청 → 빌드 → 할 일. 상태만 바꾸는 잠금은 `FOR NO KEY UPDATE`(`with_for_update(key_share=True)`). 접수에서 환경을 `FOR UPDATE` 로 잡으면 일꾼의 FK 검사(`FOR KEY SHARE`)와 교착이 생긴다(`test_accept_waiting_for_service_lock_does_not_deadlock_with_success_processing`).
- **상태는 조건부 UPDATE 로만**(`DeployRequestRepository.transition` 등, 0행이면 결과를 버리고 다시 읽는다). 일꾼의 쓰기 트랜잭션은 맨 끝에 `JobLease.hold()` 로 임대를 확인한다.
- **외부 호출은 트랜잭션 밖 + 멱등 키**(빌드 `builds.id`, 배포·되돌리기 `deployments.id`, 내리기 `연결 id:teardown_attempt`) + "요청함 시각 저장·커밋 → 호출 → external_ref 저장" 순서([docs/executors.md](docs/executors.md)).
- 일꾼·배포 코드의 시각은 주입한 `Clock` 으로 정한다(DB `now()` 에 기대지 않는다).
- 같은 연결은 실행 1 + 대기 1(부분 unique 2개). 새 대기 요청은 `enqueue_deploy_request` 로만 넣는다(밀어내기 포함).
- **예비 함께 띄우기(`standbyEnvironmentIds`, 이슈 #99)는 수동 요청 `config_snapshot` 에만 둔다.** 배포 기록·따라가기 사본에는 옮기지 않고(되돌리기가 다시 띄우지 않게), 일꾼 `_succeed` 가 기존 예비 따라가기 뒤에 연결 만들기 + `STANDBY_SYNC` 를 한다. 못 띄우면 `STANDBY_SKIPPED` 줄.
- 가짜 결과는 `simulated = true` 를 **행에 저장**하고, 응답 때 설정으로 다시 계산하지 않는다. 설치 토큰·App 개인키는 `SecretValue`/메모리로만 다루고 저장·로그·응답에 넣지 않는다.

### 실제 실행기 (API 서버 3-2, 자주 어기는 것)
- `EXECUTOR_MODE=real` 이 빌드(GitHub Actions)·배포·내리기(쿠버네티스 직접 apply)를 함께 바꾼다. **운영 기본값은 `fake`**(실환경 확인 전까지). real 은 GitHub App·`SOURCE_CLIENT_MODE=github`·`HEALTH_CHECKER_MODE=http` 가 필수(시작 때 설정 오류).
- **실행기 생성은 부작용이 없다**(토큰·비밀값은 첫 호출 때). 묶음은 프로세스당 하나: 일꾼은 시작 때, API 는 `ExecutorSetHolder`(lifespan 종료 때 닫음). 요청마다 실제 실행기를 만들지 않는다.
- 다시 불러도 소용없는 실패만 `BuildStartError`·`DeployApplyError(code)` 로 올린다. 시작 계열 일시 오류는 1·2·4초 재시도 뒤 `ExecutorUnavailableError`, **poll 계열은 예외 없이 진행 중 + 경고 로그**(예외 3번 = `WORKER_LOST`). 되돌리기는 예외 대신 `RevertFailed`.
- 실행기는 앱 네임스페이스(`{서비스}-{suffix}`, 라벨 `app.kubernetes.io/managed-by=echo-was`) 밖을 쓰지 않는다. 라벨 없는 같은 이름 네임스페이스는 쓰지도 지우지도 않는다. 시험은 스텁 전송(`tests/real_executor_fakes.py`)만 쓴다.
- 환경 config 새 칸은 모두 선택이고 **값이 없으면 키로 남기지 않는다**(`EnvironmentConfig` 직렬화기). 환경별 주소는 `ingressDomain` 이 있으면 `{서비스}-{suffix}.{ingressDomain}`, 이미 만든 연결 행은 바꾸지 않는다.

### 고객 앱 환경 변수 (API 서버 10, 자주 어기는 것)
- **값은 보관함(`AppSecretStore`)에만 둔다.** DB·로그·응답·예외·`config_snapshot` 에 값·값 일부·길이를 넣지 않는다(비밀 이름은 정상 응답의 `secretRef` 에만). 값은 `SecretValue` 로만 다룬다. 새 경로를 더하면 `test_secret_value_appears_nowhere_after_full_deploy` 대상에 넣는다.
- **보관함 비밀은 지우지 않는다.** 키 삭제·서비스 삭제는 행만 삭제 표시하고, 다시 넣으면 이전 `secret_name` 에 버전이 이어진다. 옛 배포 기록·되돌리기가 그 버전을 읽는다.
- **배포 기록에 버전을 고정한다**(`config_snapshot.envVars`, `_deployment_snapshot`). 새 배포·예비 따라가기는 그 연결의 지금 최신, 되돌리기는 대상 기록 그대로, 재실행은 기록 그대로. 값은 실행기 호출 직전에 트랜잭션 밖에서 꺼낸다(`AppEnvResolver`).
- **`env_values = None` 이면 실행기는 `{ns}-env` 를 읽지도 쓰지도 않는다**(옛 기록, 가짜 보관함 + 실제 실행기, gcp 모드의 `fake-sm:` 참조). `{}` 면 echo-was 소유 Secret 만 비운다. 손으로 만든 Secret 은 변수가 없는 연결에서 절대 건드리지 않는다. 쓸 때는 서버 측 apply 가 아니라 GET → PUT·POST 로 통째로 쓴다.
- **값 넣기 API 만 잠근 채 외부를 부른다**(서비스 → 연결 잠금 안에서 `add_version`, 호출 전체 10초). 다른 곳에 따라 하지 않는다.
- **복사(`variables:copy`, 이슈 #99)는 넣기 저장 경로(`ServiceVariableService._save`)를 대상·키마다 따로 탄다.** 원본 값은 잠금 밖에서 `access` 로 읽어 메모리로만 넘기고, 원본 행은 값(dataclass)으로만 든다(항목 실패의 롤백이 ORM 객체를 만료시킨다). 감사 줄은 키·환경 id 만.

### 되돌리기·배포 작업 로그 (자주 어기는 것)
- **되돌리기는 `trigger = ROLLBACK` 배포 요청**이다(`DeployRequestService.create_rollback`). 대상의 `build_id`(`build_reused = true`)·커밋·배포 기록 `config_snapshot` 사본을 쓰고, 접수 순간 현재 배포를 `rollback_base_deployment_id` 에 적는다. 일꾼 `_start` 가 현재 배포가 다르면 `CANCELLED(BASE_CHANGED)`. 새 배포 기록은 `rollback_of_id` = 대상의 배포 기록. 예비 따라가기는 `MANUAL`·`ROLLBACK` 성공(서비스 중 연결)일 때만.
- **배포 작업 로그 줄은 `DeployJobLog.append` 로만 쓴다.** 상태 전환과 같은 트랜잭션에서, 조건부 UPDATE 가 반영됐을 때만(`if await …transition(...)`) 부른다. 서비스 행을 잠근 트랜잭션 안이어야 한다(`seq = max + 1`). 새 줄 종류는 `DeployLogCode` + `deploy_log_messages.DEPLOY_LOG_ENTRIES` 에 고정 문장으로 더한다(외부 원문 금지). 빌드 진행 줄은 쓰지 않는다(공유 빌드).
- 빌드·실행 로그는 저장하지 않고 실행기 `read_logs`·`read_runtime_logs` 로 즉석 조회한다(트랜잭션 밖). 응답 직전 `mask_secrets()` 를 거친다. API 프로세스의 실행기는 `get_executor_set`(가짜가 아닌 모드면 None → 빈 페이지).

### 이전 계획 (자주 어기는 것)
- **승인 = 실행 시작.** 개발자 이상 사람만, 기본은 만든 사람이 아닌 사람(조직 `allow_self_approval` 이 켜져 있을 때만 자기 승인, 막힌 자기 승인·거절은 403 `SELF_APPROVAL_FORBIDDEN` = `AuditedForbiddenError`). 에이전트는 승인 불가.
- **`EXPIRED`·`STALE` 전환은 `begin()` 블록 안에서 적고 커밋한 뒤 블록 밖에서 409 를 올린다**(안에서 올리면 전환·로그가 되돌려진다). 지문(`read_fingerprint`)은 승인과 일꾼 시작 두 번 비교하고, 접속 확인 시각·`updated_at` 은 넣지 않는다.
- 잠금 순서: 서비스 → 연결(전체, id 순) → 이전 계획 → 할 일. 상태·위치(`current_step`·`step_phase`)는 `MigrationPlanRepository.transition`(조건부)으로만 바꾸고, 실행 로그 줄은 `MigrationPlanLogWriter.append` 로 반영됐을 때만 쓴다.
- **비율은 트래픽 실행기가 적용했다고 답했을 때만** 두 연결에 한 트랜잭션에서 함께 적는다(합계 100). 외부 호출은 "요청함 시각(`traffic_requested_at`·`rollback_requested_at`) 저장·커밋 → 호출(키 `{plan}:{step}`·`{plan}:rollback:{n}`) → 결과 저장".
- 실행 중(`APPROVED`·`RUNNING`·`ROLLING_BACK`) 그 서비스의 배포 요청·되돌리기·서비스 삭제는 서비스 행을 잠근 뒤 `check_no_migration_in_progress` 로 409 `MIGRATION_IN_PROGRESS`. 승인 대기 계획은 막지 않는다.
- 임대 상실(`lease.is_exhausted`, 한 번 참이면 계속 참)이어도 롤백 시도는 남은 횟수만큼 한다. `simulate` 는 처리기가 실패 값을 만들고 실행기를 부르지 않는다.
- **계획 종류 `mode` 는 만들 때 한 번 판정해 저장한다**(API 서버 8-3, `decide_plan_mode`). 서비스 행을 잠근 트랜잭션 안에서 가장 늦게 끝난 `SUCCEEDED` 계획(`find_last_succeeded_for_service`)을 거꾸로 하면 `FAILBACK`(그 계획이 `FAILBACK` 이면 `PLANNED`). 응답 때 다시 계산하지 않고, 요청으로 받지 않는다(422). `EMERGENCY` 는 재난 트리거만 만든다(`create_plan` 의 `disaster` 인자).
- **재난 트리거는 서비스마다 `create_plan`·`approve_plan` 을 그대로 부른다**(API 서버 9, `DisasterService`). 둘이 각자 트랜잭션을 열므로 열린 트랜잭션 없이 부르고, 실패의 롤백이 세션 객체를 전부 만료시키므로 서비스 사이에 ORM 객체를 들고 있지 않는다(값만 넘긴다). 한 서비스의 실패는 그 서비스의 결과만 바꾼다. 일꾼은 바뀌지 않는다. 도착이 ACTIVE 이고 기준을 넘은 재난의 피해 환경이면 409 `DISASTER_ACTIVE`(만들기만, 승인은 다시 보지 않는다). 재난 행의 환경 id 목록은 만든 순간 스냅숏이다.
- **비율 단계 판정은 "도착이 요청 N건을 5xx 허용 안에서 처리했나"**(API 서버 8-1, `migration_counting`). `OBSERVE` 한 번 실행 = 카운터(`MetricsProvider.read_counts`, 트랜잭션 밖) 한 번 읽기, 센 값은 `step_results[i].counting` 에 조건부 UPDATE 로 적고 진행 갱신은 로그 줄을 남기지 않는다. 세기를 끝내면 `judgeReason` 을 같은 트랜잭션에 적고 `JUDGE` 는 저장값으로만 판정한다(카운터 재읽기 금지). p95 는 판정에 쓰지 않는다(참고 값만). API 에 `observeSeconds` 는 없고 DB `observe_seconds` 칸 = 최대 대기다.
- **보안 게이트는 `SECURITY_GATE_ENABLED` 일 때만 주입한다**(보안 5, `SecurityGate`, API 는 `get_public_tls_checker`, 일꾼은 `Worker(tls_checker=)`). 승인·일꾼 시작 모두 **잠금 전·트랜잭션 밖에서 판정**(TLS 시험은 네트워크)하고, 잠금 안에서 지문이 같을 때 `SECURITY_GATE_PASSED`·`SECURITY_GATE_BLOCKED` 줄을 남긴다. 막힌 승인은 계획을 그대로 두고 커밋한 뒤 409 `SECURITY_GATE_BLOCKED`, 막힌 시작은 `FAILED(SECURITY_GATE_BLOCKED)`(트래픽 그대로). TLS 시험은 `PublicHostTlsChecker` 로만 한다(공개 도메인 주소·443·확인한 공인 IP). 기한부 예외는 승인·미철회만 읽는다.

### 감사 로그 (자주 어기는 것)
- **새 쓰기 경로에는 이름과 기록이 함께 간다.** `app/audit/actions.py` 의 `WRITE_ROUTE_ACTIONS` 에 이름을 더하고(빠지면 `test_every_write_route_has_an_action_name` 실패), 서비스의 변경 블록 **성공 분기**에서 `AuditRecorder.record` 를 부른다(`session.add` 만 한다). 아무것도 바뀌지 않은 멱등 성공과 409 분기에는 줄을 쓰지 않는다.
- **끝 상태를 새로 만드는 곳에는 끝 줄도 쓴다.** 배포 요청·이전 계획의 끝 상태 전환은 조건부 UPDATE 가 반영됐을 때 같은 트랜잭션에서 `record_deploy_request_finished`·`record_migration_plan_finished`(`app/audit/lifecycle.py`)를 부른다. 행의 `status` 는 조건부 UPDATE 뒤에도 옛 값이므로 끝 상태를 인수로 넘긴다. 사람 요청 안에서 서버 규칙으로 끝낸 것(만료·낡음·밀어내기·삭제 취소)은 SYSTEM `component = api` 다.
- 값은 명시적으로 받는다(`OrganizationAccess` 를 모른다). 요청 값(`route`·`path`·`request_id`)은 `RequestContextMiddleware` 가 묶은 contextvar 에서 기록 함수가 읽는다(요청 밖이면 NULL). 시각은 주입한 시계, 문자열은 칸 길이로 자른다(긴 이름으로 작업이 실패하면 안 된다).
- `details` 에 비밀값·토큰 원문·수정한 값을 넣지 않는다(수정은 `changedFields` 칸 이름만). `action` 은 옛 거부 줄의 `METHOD 경로 틀` 때문에 칸·응답이 문자열이다.
- 거부 줄과 에이전트의 성공한 GET·HEAD(2xx, 미들웨어가 응답 뒤) 줄만 따로 커밋한다(`app/auth/audit.py`). 에이전트 읽기 기록 실패는 로그만 남기고 응답을 바꾸지 않는다.
- 줄은 UPDATE·DELETE 트리거로 막혀 있다. 표를 세는 시험은 `result = DENIED` 등으로 거른다. 이 revision 은 행동 줄이 있으면 downgrade 가 멈춘다(운영은 forward-only).

### AI 사전 점검·러너 (자주 어기는 것)
- **러너 경로는 조직 경로와 자격 증명을 섞지 않는다.** `require_runner`(`echo_rnr_`, 가져가기만)·`require_run_token`(`echo_run_`, 그 실행만)은 `AUTH_GUARDS` 에 있다. 러너 쓰기 경로는 `WRITE_ROUTE_ACTIONS` 가 아니라 `RUNNER_ROUTE_ACTIONS` 에 넣는다(줄 없는 경로는 값 `None`).
- **러너 감사 줄은 SYSTEM `component = ai-runner`** 이고 가져가기·결과·시간 초과만 쓴다. `RUNNER` 행위자를 만들지 않는다.
- **`simulated` 는 "값을 지어냈는가"다.** 러너가 낸 값을 그대로 저장하고 모델 유무로 덮어쓰지 않는다. 모델 유무는 응답 `aiReviewed`(저장 안 함) 로 보인다. `simulated = true` 는 C-AI-1 증거가 아니다.
- **만료는 부를 때 한다**(`expire_overdue_runs`, 일꾼 루프 없음). 만료 전환은 `begin()` 안에서 적고 커밋한 뒤 블록 밖에서 409 를 올린다. `GET` 은 쓰지 않는다. 실행 토큰 해시를 지우지 않고 상태로 무효를 판정한다.
- **입력 사본·이벤트·결과는 저장 전에 가린다**(`mask_secrets_in_json`, 문자열마다. 직렬화 글자 전체에 걸면 잘린 개인 키 블록이 JSON 구조까지 지운다). 입력 사본에 `secretRef`·변수 값을 넣지 않는다.
- **사람 쪽 조회·이벤트(`GET /prechecks/{id}`·`/logs`)는 `PRECHECK`·`APPLY_PATCH` 를 연다**(`READABLE_RUN_KINDS`, `find_in_organization`, API 서버 10-3). 응답 `kind` 로 구분하고 반영 실행이면 `apply{parentRunId, patchId, patchHash, repo, branch, appliedCommitSha}` 가 찬다(칸은 더하기만, `oneOf` 로 나누지 않는다). 취소·패치 승인·거절은 `find_precheck`(사전 점검만) 그대로다.
- 이벤트·결과 본문은 FastAPI 본문 인자로 받지 않는다(`parse_runner_payload`). 검증 실패는 422 `RESULT_INVALID` 다. AI `findings` 는 `findings` 표에 넣지 않는다(2차).
- 러너 토큰 발급 명령(`app.scripts.issue_runner_token`)은 시험 DB 밖에서 함부로 돌리지 않는다. 운영 실행은 README "AI 러너 토큰"을 따른다.
- **레지던시 기록(`agent_runs.processing`)은 러너 신고 그대로다**(AI 3). 다시 계산하지 않고, `model`·`model_id` 와 맞추지 않는다(거절·실패면 모델이 null 이어도 자료는 나갔다). 기록이 없으면 지어내지 않고 "기록 없음"이며, 그런 실행이 있으면 민감 등급 판정을 `ZERO` 로 내지 않는다. 판정·집계는 `app/services/ai_residency.py` 순수 함수에만 둔다. 정책은 지금 서버 상수뿐이다(조직별 저장은 후속). 리전을 못 고르는 제공자(Anthropic 1P)는 리전 목록에 null 을 넣지 않고 `allowed_regionless_providers` 조건부 허용으로 다룬다(AD-12).

### 공통 안전 규칙
- I/O는 전부 `async def` + `await`. 동기 블로킹 호출은 `asyncio.to_thread`로 감싼다. async 세션에서 관계는 `selectinload` 로 미리 읽는다.
- 시각은 항상 **timezone-aware UTC**(`datetime.now(UTC)`). `utcnow()` 금지.
- 스키마 변경은 **모델 → Alembic revision → db-schema.sql(`scripts/dump_schema.sh`)** 한 세트. DB에 직접 DDL 금지, 적용된 revision 수정 금지.
- 마이그레이션은 **앱 시작 시 실행하지 않는다**(lifespan·startup 금지). 로컬은 `uv run alembic upgrade head`. 배포 환경의 실행 방식은 인프라 파트와 정한다.
- 시크릿은 `BaseSettings`로 주입. `os.getenv` 직접 호출·하드코딩 금지. 로그·예외에 시크릿·설정 값 노출 금지(설정 오류는 `invalid_fields` 경로만).
- 예외는 `AppError` 계층(`app/core/exceptions.py`)으로 raise, HTTP 매핑은 `exception_handlers.py` 한곳에서. 식별자는 메시지가 아니라 `fields`로. 빈 `except`·예외 뭉개기 금지.
- 로깅은 구조화형 JSON 한 줄: message 는 고정 문구, 값은 `extra`(`action`·도메인 ID). `component`(`echo-api`)·`request_id` 는 자동으로 붙는다. `service` 필드는 쓰지 않는다(사용자 앱 `service_id` 와 혼동). `print` 금지.
- 모든 함수 시그니처에 타입 힌트. 신문법(`str | None`, `list[T]`) 사용.
- 주석·docstring에 규칙 문서 위치(`backend-conventions §3` 등)를 인용하지 않는다.

---

## 도구

- **패키지·실행**: `uv`. 명령은 `uv run <cmd>` — 린트 `uv run ruff check .` / 포맷 `uv run ruff format .` / 타입 `uv run mypy app` / 테스트 `uv run pytest` / API `uv run --env-file .env uvicorn app.main:app`(`ECHO_SECRET_*` 를 프로세스 환경변수로 넣으려고 `--env-file`) / 마이그레이션 `uv run alembic upgrade head`.
- **로컬 DB**: `docker compose up -d --wait db`(PostgreSQL 17, 호스트 포트 55432, 개발용 더미 계정). 접속 정보는 `.env`(gitignore, `.env.example` 복사).
- **테스트**: `pytest` + `pytest-asyncio`. 함수명 `test_{대상}_{시나리오}_{기대}`. `tests/integration/` 은 `TEST_DATABASE_URL` 서버에 붙되 작업 폴더마다 다른 DB(`echo_<폴더 해시 8자>_test`, 없으면 만든다)를 쓰고, 서버가 없으면 이유를 출력하고 건너뛴다. 같은 작업 폴더의 pytest 는 advisory lock 으로 한 번에 하나씩 돈다.
- **마이그레이션 검증**: `upgrade head` → `downgrade base`(또는 `-1`) → `upgrade head` → `alembic check`. CHECK·부분 인덱스·트리거는 `tests/integration/test_migrations.py` 가 `pg_catalog` 로 비교한다.
- **품질 게이트**: Claude Code PreToolUse 훅(`.claude/settings.json`)이 `git add` 전 `ruff`·`mypy`, `git commit` 전 `ruff`·`mypy`·`pytest`, `git push` 전 `ruff`를 돌려 실패 시 차단한다(전체 pytest 는 커밋 때 한 번만 — `git add && git commit` 한 명령이 시험을 두 번 돌리지 않게. 제한: `add` 180초, `commit` 600초, `push` 180초. 제한을 넘으면 훅이 끊기고 차단하지 않으므로, 전체 pytest가 제한에 가까워지면 제한을 다시 본다). `pyproject.toml`이 없으면 건너뛴다. 훅의 `if` 조건은 heredoc·여러 줄 명령에서는 보수적으로 맞춰져 그 명령에도 돈다 — 파일 편집은 Edit/Write 도구로 한다.
- **스키마 동기화**: `app/models/*.py` 변경 시 PostToolUse 훅(`.claude/hooks/db_schema_changelog.py`)이 변경을 `.claude/logs/db-schema-changelog.md`에 기록하고 revision 생성과 `db-schema.sql` 동기화를 알린다.
- **PR 본문**: `write-pr` 스킬(`.claude/skills/write-pr/`)이 `.github/PULL_REQUEST_TEMPLATE.md` 구조(맨 위 `노션 태스크:` 줄)로 `docs/PR.md`(gitignore)를 작성한다.

# Echo 개발자 용어 사전 (Developer Term Dictionary)

- **적용 대상**: echo-was 의 코드·DB·API·문서에 쓰는 도메인 용어와 식별자
- **근거**: 본선 원장 `final_features_v6.md`(F1~F17, NFR) — 각 용어 옆에 근거 기능 번호를 적는다
- **우선순위**: 이 문서 > [backend-conventions.md](backend-conventions.md) > 일반 관례
- **함께 보기**: 표·칸 상세와 행 예시는 [docs/schema.md](../../docs/schema.md), 물리 DDL 은 [db-schema.sql](db-schema.sql)

`*` 표시는 이번(API 서버 1)에 표가 없는 **후속** 용어다. 이름만 먼저 정해 두고, 표는 담당 태스크에서 만든다.

---

## 1. 표기 규칙

- **하나의 표준 영문 용어**를 정하고, 언어별로 **표기법(case)만** 바꾼다. Python·SQL 은 `snake_case`, API JSON 은 `camelCase`(스키마 alias)를 쓴다.
- **테이블명은 `snake_case` 복수형**(`services`, `deployments`)이다. backend-conventions §2.5 의 단수형 규칙보다 이 규칙이 우선한다. Model 클래스명은 `PascalCase` 단수형(`Deployment`)이다. 연결 표는 두 이름을 잇는다(`service_environments` / `ServiceEnvironment`).
- **PK** 는 `id uuid`, FK 는 `{대상 단수}_id`(`service_id`, `environment_id`)다. 같은 표를 다른 뜻으로 가리키면 뜻을 앞에 붙인다(`current_deployment_id`, `rollback_of_id`).
- **Enum 코드** 규칙
  - 내부 상태·종류: `UPPER_SNAKE` (`PENDING`, `IN_PROGRESS`)
  - 사용자 설정에서 온 값: 설정 표기 그대로 소문자 (`aws`, `gcp`, `onprem`)
  - 외부 시스템 값: 원본 표기 그대로 저장
- **시각** 필드는 `*_at` 이름에 `timestamptz`(UTC) 타입을 쓴다.
- **이름(name) 칸**은 Kubernetes 이름 규칙(RFC 1123 label)을 따른다: 소문자·숫자·하이픈 1~63자, 처음과 끝은 소문자·숫자 (`shop-api`, `aws-seoul`). 이후 클러스터 리소스 이름으로 그대로 쓰기 때문이다.
- **이미지 참조**
  - `image_digest`: `sha256:` + 소문자 16진수 64자 값만 담는다.
  - `image_repository`: 레지스트리 저장소 URI 를 담는다(tag 없이).
  - 둘을 합친 배포 참조가 `image_ref` = `{image_repository}@{image_digest}` 다.
  - **tag 필드는 두지 않는다.** `latest` 같은 바뀌는 tag 로 배포하지 않는다.
- **비밀값**은 DB 에 두지 않는다. 비밀 저장소의 보관 위치만 `secret_ref` 로 담는다.

---

## 2. 핵심 용어 (이번에 표가 있는 것)

| 우리말 | 표준 영문 | 코드 (클래스 / 표·칸) | 뜻 | 근거 |
|---|---|---|---|---|
| 서비스 | Service | `Service` / `services` | 사용자가 배포하는 앱 하나(예: `shop-api`). 여러 환경에 동시에 있을 수 있다 | F1·F2 |
| 환경 | Environment | `Environment` / `environments` | 서비스를 올릴 수 있는 곳 하나(AWS EKS·GCP GKE·온프레 k3s 클러스터) | F2 |
| 환경 종류 | Environment kind | `environments.kind` | `aws`·`gcp`·`onprem`. DB Enum 이 아니라 설정 양식 레지스트리의 키다 | F2, NFR-15 |
| 서비스-환경 연결 | Service environment | `ServiceEnvironment` / `service_environments` | 서비스 하나가 환경 하나에 올라가 있는 관계. 현재 배포와 트래픽 비율을 담는다 | F2·F3·F7 |
| 배포 | Deployment | `Deployment` / `deployments` | 연결 하나에 이미지 하나를 올린 기록. 지우지 않는 이력이다 | F1 |
| 현재 배포 | Current deployment | `service_environments.current_deployment_id` | 그 연결에서 지금 돌고 있는 배포. 같은 연결의 배포만 가리킨다 | F1·F3 |
| 되돌리기 | Rollback | `deployments.rollback_of_id` | 예전 배포의 이미지·설정으로 **새 배포 행**을 만드는 것. 원본을 가리킨다. API 로는 이력에서 고른 성공 요청을 대상으로 한 `trigger = ROLLBACK` 배포 요청으로 한다(API 서버 4) | F1·F9 |
| 되돌리기 요청 | Rollback request | `deploy_requests.trigger = ROLLBACK`, `rollback_of_request_id` / `POST …/deploy-requests/{id}/rollback` | 이력에서 고른 `SUCCEEDED` 요청(대상)의 빌드·커밋·배포 기록 설정 사본으로 같은 연결에 다시 올리는 배포 요청. 새 빌드 없음(`build_reused = true`). 겹침·헬스체크·실패 처리는 원터치 배포와 같다 | F1·F9, API 4 |
| 되돌리기 기준 배포 | Rollback base deployment | `deploy_requests.rollback_base_deployment_id` | 되돌리기 접수 순간 연결의 현재 배포. 일꾼이 시작할 때 현재 배포가 이것과 다르면 `CANCELLED(BASE_CHANGED)` | API 4 |
| 현재 버전 표시 | Is current | 응답 `isCurrent` (저장 안 함) | 요청이 만든 배포 기록이 지금 그 연결의 현재 배포인가 | API 4 |
| 되돌리기 가능 | Rollbackable | 응답 `rollbackable` (저장 안 함) | `SUCCEEDED` · 배포 기록 있음 · 현재 아님 · 연결 살아 있음 · 서비스 `ACTIVE`. 판정은 배포 기록 id 로 한다 | API 4 |
| 배포 작업 로그 | Deploy job log | `DeployRequestLog` / `deploy_request_logs`, `source = DEPLOY_JOB` | 배포 요청 한 건의 진행 줄(접수·시작·빌드 연결·배포·헬스·성공·실패·되돌리기·취소). 상태 전환과 같은 트랜잭션에서 반영됐을 때만 쓰고, 요청마다 `seq` 1부터, 500줄 상한, 지우지 않는다 | F1, API 4 |
| 빌드 로그 · 실행 로그 | Build log · Runtime log | `source = BUILD` · `RUNTIME`, `BuildExecutor.read_logs` · `DeployExecutor.read_runtime_logs` | 실행기가 즉석으로 읽는 로그(저장 안 함). 실행기는 비밀값을 가려서 돌려준다. 지금은 가짜(`simulated`) | F1, API 4 |
| 로그 마스킹 | Log masking | `app/core/log_masking.py::mask_secrets` | 로그 응답 직전 알려진 비밀값 모양(GitHub 토큰·AWS 키·Bearer·`sk-`·개인 키 블록)을 `***` 로 바꾸는 방어용 이중 검사 | F16, API 4 |
| 트래픽 비율 | Traffic weight | `service_environments.traffic_weight` | 앞단 프록시에 **마지막으로 적용한** 비율(0~100). 실제 분배는 프록시(Envoy)가 한다. API 서버 3 에서는 가짜 대표 주소 라우터가 "적용했다"고 답한 값이며, 그 연결의 현재 배포 요청에 `simulated` 로 표시된다 | F3·F7 |
| 비율 적용 시각 | Traffic applied at | `service_environments.traffic_applied_at` | 위 비율을 프록시에 적용한 시각 | F7 |
| 어댑터 | Adapter | `app/adapters/<kind>.py` | 환경 종류마다 파일 하나. 설정 양식 + 동작 클래스를 함께 등록한다 | F2, NFR-15 |
| 설정 양식 | Environment config | `EnvironmentConfig` 하위 클래스 | 환경 종류별 `config` JSON 의 형식(Pydantic). 저장 전에 검사한다. 키는 DB snake_case, API camelCase | F2, NFR-15 |
| 어댑터 동작 클래스 | Environment adapter | `EnvironmentAdapter` 하위 클래스 | 종류마다 다른 클러스터 조작을 같은 메서드로 부른다. 지금은 `check_connection`·`read_cluster_info`(async). 실패는 `ConnectionCheckError(reason)` | F2, NFR-15 |
| 가짜 어댑터 | Simulated adapter | `SimulatedEnvironmentAdapter` | 네트워크 호출 없이 고정 값을 돌려주는 동작 클래스. 결과에 `simulated=True`. 이번 aws·gcp·onprem 은 모두 가짜 | F2 |
| 클러스터 정보 | Cluster info | `ClusterInfo` (저장 안 함) | `server_version`(쿠버네티스 `gitVersion` 모양), `node_count`, `simulated` | F2·F3 |
| 접속 상태 | Connection status | `environments.connection_status` | 마지막 접속 확인 결과(§3). 확인 시각 `connection_checked_at`, 실패 이유 `connection_error` | F2·F3 |
| 설정 | Config | `environments.config` (jsonb) | 환경 종류별 고유 설정(리전·클러스터 이름 등). 비밀값 없음 | F2 |
| 비밀 참조 | Secret reference | `environments.secret_ref` / API `secretRef` | 비밀 저장소의 보관 위치(`env:`·`k8s:`·`aws-sm:`·`gcp-sm:` §3). 값은 저장하지도 API 로 받지도 않는다 | F2·F16 |
| 비밀값 | Secret value | `SecretValue` (`app/core/secrets.py`) | 메모리에서만 쓰는 비밀값. repr·str·직렬화는 가리고 `get_secret_value()` 로만 꺼낸다 | F16 |
| 비밀값 조회기 | Secret client | `SecretClient` / `EnvVarSecretClient` (`app/clients/`) | secret_ref 로 비밀값을 읽는다. 지금은 `env` 저장소만 실제 조회 | F2·F16 |
| 설정 스냅샷 | Config snapshot | `deployments.config_snapshot` (jsonb) | 배포 때 쓴 설정(복제 수·환경변수 등). 되돌리기가 이 값을 복사한다. **비밀값 없이 참조만** | F1 |
| 이미지 digest | Image digest | `deployments.image_digest` | 배포 기준이 되는 이미지 내용 해시 | F1 |
| 위치 | Location | `EnvironmentConfig.display_location()` | 환경 카드의 "위치" 칸 값(aws·gcp 는 region, onprem 은 location) | F3 |
| 소프트 삭제 | Soft delete | `is_deleted`·`deleted_at` | 행을 지우지 않고 표시만 한다. 두 칸은 항상 함께 움직인다 | — |
| 배포 요청 | Deploy request | `DeployRequest` / `deploy_requests` | 원터치 배포 한 번(서비스 + 환경 1개 + 브랜치·커밋). 접수 순간 커밋 SHA·저장소·설정 사본을 고정한다. 지우지 않는 이력이다. 배포 기록(`deployments`)과 다르다 | F1 |
| 빌드 | Build | `Build` / `builds` | 서비스·저장소·커밋으로 이미지 하나를 만드는 일. 빌드 1 : 배포 N. 같은 서비스·저장소·커밋은 재사용한다 | F1 |
| 할 일 | Job | `Job` / `jobs` | 일꾼이 집어 갈 일(`DEPLOY_REQUEST`·`SERVICE_TEARDOWN`). 임대(`lease_token`)로 한 일꾼만 진행한다 | F1 |
| 일꾼 | Worker | `app/worker/`, `python -m app.worker` | 할 일 표를 폴링해 배포 요청·서비스 내리기를 한 단계씩 진행하는 별도 프로세스 | F1 |
| 서비스 중 | Serving | `ServiceEnvironmentRole.SERVING` (저장 안 함) | `traffic_weight > 0` 인 연결. 첫 성공 연결이 서비스 중이 된다 | F1·D5 |
| 예비 | Standby | `ServiceEnvironmentRole.STANDBY` (저장 안 함) | `traffic_weight = 0` 이고 현재 배포가 있는 연결. 재난·장애 때 이전(API 서버 8)이 넘어갈 곳 | F1·D5 |
| 예비 따라가기 | Standby sync | `deploy_requests.trigger = STANDBY_SYNC` | 서비스 중 연결이 성공하면 예비 연결이 같은 빌드·같은 설정으로 자동 배포되는 요청. `source_request_id` 로 원본을 가리킨다 | F1·D11 |
| 버전 상태 | Version status | `VersionStatus` (응답 `versionStatus`) | 예비 연결의 현재 이미지가 서비스 중 연결과 같으면 `IN_SYNC`, 다르면 `OUT_OF_SYNC` | F1·P2 |
| 대표 주소 | Main URL | `services.public_host` / 응답 `url`·`urls.main` | `https://{서비스}-{suffix}.{public_base_domain}`(도메인 바로 아래 한 단계). 서비스 중 연결로 간다 | F1·D8, API 3-1 |
| 환경별 주소 | Environment URL | `service_environments.public_host` / 응답 `url`·`urls.environment` | 환경 config 에 `ingressDomain` 이 있으면(클라우드) `https://{서비스}-{suffix}.{ingressDomain}`, 없으면(온프레·예전 환경) `https://{서비스}-{환경}-{suffix}.{public_base_domain}`(서비스의 suffix). 연결을 만들 때 정해 저장한다(환경 이름·`ingressDomain` 이 바뀌어도 그대로). 헬스체크 대상 | F1·D8, API 3-1·3-2 |
| 환경 하위 도메인 | Environment ingress domain | `environments.config.ingress_domain` / API `ingressDomain`, `EnvironmentConfig.environment_host_domain()` | 클라우드 환경(aws·gcp)의 환경별 주소 도메인. `public_base_domain` 바로 아래 한 단계(`aws-tokyo.echoport.uk`)여야 하고, 그 아래 와일드카드 DNS·인증서가 덮는다 | API 3-2 |
| 앱 네임스페이스 | App namespace | 대표 주소 첫 레이블 `{서비스}-{suffix}`, 라벨 `app.kubernetes.io/managed-by=echo-was` | 실제 배포 실행기가 고객 앱 한 연결을 올리는 쿠버네티스 네임스페이스. 실행기는 이 라벨이 붙은 네임스페이스 밖을 쓰지 않는다. 앱 비밀값은 환경 담당이 미리 둔 Secret `{네임스페이스}-env` 를 `envFrom`(optional)으로 읽기만 한다 | API 3-2 |
| 클러스터 접속기 | Cluster access provider | `ClusterAccessProvider` (`app/executors/kubernetes/access.py`) | 환경 종류별로 쿠버네티스 창구 주소·CA·토큰을 얻는다: gcp 메타데이터 토큰 + GKE API, aws Google ID 토큰 → STS → presigned EKS 토큰, onprem `apiUrl`(터널) + `secret_ref` 서비스 계정 토큰 | API 3-2 |
| 이미지 받기 자격 | Image pull secret | Secret `echo-pull`, 어노테이션 `echo.pull-expires-at` | 온프레 앱 네임스페이스에 두는 Artifact Registry 자격(메타데이터 액세스 토큰). 일꾼이 5분마다 보고 남은 수명이 30분 미만이면 갱신한다 | API 3-2 |
| 주소 suffix | Host suffix | `services.host_suffix` | 대표·환경별 주소 끝의 소문자·숫자 무작위 8자. 서비스를 만들 때 한 번 정하고 바꾸지 않는다(지운 서비스의 주소를 같은 이름의 새 서비스가 잇지 못하게). 레이블이 63자를 넘으면 앞부분을 54자로 자르고 suffix 는 남긴다 | API 3-1 |
| 실행기 | Executor | `app/executors/` (`BuildExecutor`·`DeployExecutor`·`MainRouteExecutor`·`HealthChecker`·`TeardownExecutor`) | 빌드·배포·대표 주소 연결·헬스체크·내리기와 빌드·실행 로그 읽기를 실제로 하는 쪽. 기본은 가짜(`simulated`), `EXECUTOR_MODE=real` 이면 GitHub Actions 빌드(`GithubActionsBuildExecutor`) + 쿠버네티스 직접 반영(`KubernetesDeployExecutor`·`KubernetesTeardownExecutor`, API 서버 3-2) | F1 |
| 멱등 키 | Idempotency key | 빌드 `builds.id`, 배포·되돌리기 `deployments.id`, 내리기 `연결 id:teardown_attempt` | 외부 작업을 두 번 만들지 않게 우리가 먼저 정해 넘기는 키. 같은 키 재호출은 같은 작업 | F1 |
| 되돌리기 결과 | Revert status | `deploy_requests.revert_status` | 배포 기록이 생긴 뒤 실패했을 때 직전 버전을 유지하려고 부른 되돌리기의 결과. 사용자가 하는 되돌리기(`rollback_of_id`, API 서버 4)와 다르다 | F1·D7 |
| 소스 저장소 클라이언트 | Source repository client | `SourceRepositoryClient` (`app/clients/source_repository.py`) | 저장소 접근 확인·브랜치/커밋 → 40자 SHA·빌드용 읽기 토큰. 가짜와 GitHub App 구현 | F1·D9 |
| GitHub App 개인키 참조 | GitHub App private key ref | 설정 `github_app_private_key_ref` | 서버에 두는 비밀값(App 개인키)의 보관 위치. 값은 저장·로그·응답에 넣지 않는다 | D9·F16 |
| 사용자 | User | `User` / `users` | GitHub 로 로그인한 사람. GitHub 숫자 번호(`github_id`)로 같은 사람을 가린다. 아이디(`github_login`)는 바뀔 수 있다 | F4 |
| 조직 | Organization | `Organization` / `organizations`, 경로 `/api/v1/orgs/{orgId}` | 고객 회사 하나. 서비스·환경·배포는 조직에 속하고 다른 조직에는 보이지 않는다(404). `slug` 는 전체 유일·변경 불가 | F4, NFR-07 |
| 개인 조직 | Personal organization | `organizations.personal = true` | 처음 로그인할 때 자동으로 만드는 조직. slug 는 GitHub 아이디 소문자(겹치면 `-2` …) | F4 |
| 조직 멤버 | Organization member | `OrganizationMember` / `organization_members` | 사람 × 조직 × 역할. 한 사람은 여러 조직에 속하고 조직마다 역할 하나다 | F4 |
| 역할 | Role | `OrganizationRole` / `organization_members.role` | 조직 안 권한. `VIEWER` < `DEVELOPER` < `ADMIN`. 되돌릴 수 있는 일(배포·롤백·이전 실행·AI 반영 승인·토큰)은 개발자, 삭제·사람 관리는 어드민 | F4, NFR-07 |
| 초대 | Invitation | `Invitation` / `invitations` | GitHub 아이디(숫자 번호로 저장) + 역할. 그 사람이 로그인하거나 `/me` 를 부르면 멤버가 된다. 7일 | F4 |
| 세션 | Session | `UserSession` / `sessions`, 쿠키 `echo_session` | 브라우저 로그인 하나. 쿠키 값은 SHA-256 만 저장, 7일 고정 | F4, NFR-07 |
| 에이전트 토큰 | Agent token | `AgentToken` / `agent_tokens`, `Authorization: Bearer echo_agt_…` | AI 에이전트 출입증. 조직 하나에 묶이고 그 조직을 읽기만 한다. 원문은 발급 응답에 한 번만 | F14·F16 |
| 거부 기록 | Denial record (audit log) | `AuditLog` / `audit_logs` | 권한이 없어 막힌 요청(403) 한 건. 요청과 따로 커밋하고 지우지 않는다. 조회·다른 행위 기록은 API 서버 7 | F5, NFR-07·08 |
| 권한 검사기 | Organization guard | `require_org_role(min_role, human_only=False)` (`app/auth/guards.py`) | 조직 경로마다 거는 FastAPI 의존성. 로그인 → 조직 멤버 → 최소 역할 → Origin 순서로 본문·리소스보다 먼저 검사 | F4, NFR-07 |
| 이전 계획 | Migration plan | `MigrationPlan` / `migration_plans`, 경로 `/services/{id}/migration-plans`·`/migration-plans/{id}` | 서비스 트래픽을 출발 연결(지금 100%)에서 같은 이미지의 예비 연결(도착)로 옮기는 계획과 그 실행 기록. 고정 5단계·단계별 예상 시간·롤백 조건을 담는다. 서비스당 끝나지 않은 계획은 하나, 지우지 않는다 | F10, API 8 |
| 출발 · 도착 | Source · Target | `source_service_environment_id` · `target_service_environment_id` | 출발 = 계획을 만들 때 비율 100 인 연결 하나. 도착 = 출발과 같은 이미지가 올라간 예비 연결(따라가는 중이면 `TARGET_SYNCING`, 없거나 다른 버전이면 `TARGET_NOT_READY`) | F10, API 8 |
| 이전 단계 | Migration step | `steps[i]` (`MigrationStepKind`) | 고정 5개: `0 PREFLIGHT`, `1 SHIFT(10)`, `2 SHIFT(50)`, `3 DATA_SYNC_CHECK`, `4 SHIFT(100)`. 만든 뒤 바꾸지 않는다. 실제 결과는 같은 번호의 `step_results[i]` | F10, API 8 |
| 사전 확인 | Preflight | 단계 0 `PREFLIGHT` | 도착 환경별 주소 헬스체크 1번 + 도착 이미지가 출발과 같은가. 실패하면 트래픽을 바꾸지 않고 `FAILED(PREFLIGHT_FAILED)`. AI 에이전트의 사전 점검 체크리스트(F11)와 다르다 | API 8 |
| 건수 세기 | Request counting | `step_phase = OBSERVE`, `step_results[i].counting` (`StepCounting`, `app/services/migration_counting.py`) | 비율을 적용한 뒤 출발·도착 환경의 누적 카운터를 1초마다 읽어 "도착이 요청 N건을 5xx 허용 안에서 처리했는가"를 세는 것(API 서버 8-1, 옛 "지켜보기 N초"를 바꿈). 첫 읽기가 기준값, 센 값 = 지금 − 기준값 | F7, API 8-1 |
| 필요 건수 · 최대 대기 | Required requests · Max wait | `steps[i].requiredRequests`, `maxWaitSeconds`(= `migration_plans.observe_seconds` 칸, `observe_until` = 적용 시각 + 최대 대기) | 비율 단계(10·50·100%)마다 도착이 처리해야 할 요청 수(기본 20·50·50)와 그만큼 기다리는 최대 시간(기본 30초). API 의 `observeSeconds` 는 없앴다 | API 8-1 |
| 허용 5xx | Allowed 5xx | `allowedErrors5xx` | max(1, floor(필요 건수 × `maxErrorRatePercent` ÷ 100)), Decimal 로 계산하고 필요 건수 기준으로 고정. 도착 5xx 가 이를 넘는 순간 `THRESHOLD_EXCEEDED` | API 8-1 |
| 트래픽 적음 | Low traffic | `metrics.lowTraffic`, `judgeReason = LOW_TRAFFIC` | 최대 대기 안에 필요 건수를 못 채웠지만 서비스 전체가 조용한 경우. 헬스체크로 통과(`PASSED`)한다 | API 8-1 |
| 도착 미수신 | Target not receiving | `TARGET_NOT_RECEIVING` | 최대 대기 끝에, 도착에 갔어야 할 만큼(정수식) 출발에 요청이 왔는데 도착이 기대의 4분의 1도 못 받은 경우. 진입점이 비율을 적용하지 않은 상황이라 롤백한다 | API 8-1 |
| 카운터 재시작 | Counters reset | 로그 `COUNTERS_RESET` | 읽은 누적값이 직전보다 작으면 진입점이 재시작해 0부터 다시 쌓였다고 보고 이미 센 건수를 이어 간다(화면 숫자는 줄지 않는다) | API 8-1 |
| 진행 숫자 | Progress | 응답 `progress` (저장 안 함, 단건만) | 세는 중인 비율 단계의 도착 처리 건수·5xx·허용·출발 건수·시작·마감·마지막 읽기 시각. 일꾼이 1초마다 센 값을 적고 화면은 1초 폴링 | API 8-1 |
| 판정 | Judge | `step_phase = JUDGE`, `step_results[i].judgeReason` (`MigrationJudgeReason`) | 비율 단계는 세기를 끝낸 뒤(건수 채움 `COUNT_REACHED` 또는 트래픽 적음 `LOW_TRAFFIC`) 헬스체크 한 번, 데이터 확인 단계는 복제 지연, 사전 확인은 헬스 + 같은 이미지. 통과하면 사람 개입 없이 다음 단계. p95 는 판정하지 않고 참고 값(`p95Ms`)만 남긴다 | F7, API 8·8-1 |
| 롤백 조건 | Rollback conditions | `rollback_conditions` (`maxErrorRatePercent`·`maxP95Ms`·`maxReplicationLagSeconds`) | 판정 실패 기준(기본 1.0%·800ms·1.0초). 오류율은 허용 5xx 계산에 쓰고, `maxP95Ms` 는 참고용(응답 `p95Advisory: true`). 헬스체크 실패는 늘 실패 | F10, API 8·8-1 |
| 이전 롤백 | Migration rollback | `status = ROLLING_BACK`, `rollback_status`, 키 `{plan}:rollback:{n}` | 판정 실패·판단 불가·적용 실패·중단·일꾼 상실 때 출발 100%·도착 0% 로 되돌리는 것. 배포 되돌리기(`rollback_of_id`)·실패한 새 버전 거두기(`revert_status`)와 다르다 | F10, API 8 |
| 계획 종류 | Migration plan mode | `migration_plans.mode` (`MigrationPlanMode`), 응답 `mode` | 계획을 만들 때 서버가 한 번 판정해 저장하는 종류. 같은 서비스의 가장 늦게 끝난 `SUCCEEDED` 계획을 출발·도착만 바꿔 거꾸로 하면 복귀(`FAILBACK`, 그 계획이 이미 복귀면 아님), 그 밖은 `PLANNED`. `EMERGENCY` 는 재난 자동 이전(API 서버 9)부터. 클라이언트는 정할 수 없다 | F10, API 8-3 |
| 지문 | Fingerprint | `migration_plans.fingerprint` | 계획을 만든 순간의 상태(서비스 상태·연결 값·출발/도착 환경 설정 해시·열린 배포 요청). 승인·일꾼 시작 때 다시 계산해 다르면 낡은 계획(`STALE`) | API 8 |
| 승인 | Approval | `approved_by_user_id`·`approved_at`, `POST …/approve` | 개발자 이상의 사람이 실행을 허락한 것. 승인이 곧 실행 시작이다. 에이전트는 할 수 없다 | F10, API 8 |
| 자기 승인 | Self approval | `self_approved`, 조직 `allow_self_approval` | 계획을 만든 사람이 스스로 승인하는 것. 조직 스위치가 꺼져 있으면(기본) 403 `SELF_APPROVAL_FORBIDDEN`. 어드민만 켠다 | F10, API 8 |
| 중단 | Abort | `abort_requested_at`·`aborted_by_user_id`, `POST …/abort` | 사람이 실행 중 계획을 멈추는 것. `RUNNING` 이면 롤백(`ABORTED`), 시작 전이면 `CANCELLED`. 멱등 | F10, API 8 |
| 이전 실행 로그 | Migration log | `MigrationPlanLog` / `migration_plan_logs` | 계획 한 건의 진행 줄. 계획 단계 번호(`step`)를 달고 상태 변경과 같은 트랜잭션에서 쓴다. 계획마다 `seq` 1부터, 500줄 상한 | F10·F17, API 8 |
| 트래픽 실행기 · 지표 공급자 · 데이터 확인기 | Traffic executor · Metrics provider · Data sync checker | `TrafficExecutor.set_weights` · `MetricsProvider.read_counts`·`read` · `DataSyncChecker.replication_lag` | 비율 나눠 보내기(실제 Envoy, 인프라 6) · 환경별 누적 요청·5xx 카운터(판정용, `RequestCounts`·`CountsUnavailable`)와 구간 지표(p95 참고용, 인프라 9) · 복제 지연(인프라 7). 트래픽·지표는 고정 진입점 실행기(`ENTRYPOINT_EXECUTOR_MODE=http`)가 실제 구현(카운터 `GET /counters`, API 서버 8-2), 데이터 확인은 가짜 | F7·F17, API 8·8-1·8-2 |
| 고정 진입점 | Entrypoint | `app/executors/entrypoint.py`, 설정 `ENTRYPOINT_*` | echo-infra 인프라 5 의 Envoy + 가중치 API(`PUT /weights`·`GET /summary`, 누적 카운터 `GET /counters`). 진입점 환경 이름(`aws`·`gcp`)은 환경 종류와 같고, 다르면 `ENTRYPOINT_ENVIRONMENT_MAP` 으로 환경 id 를 잇는다 | F6·F7·F17, API 8-2 |
| 진입점 업스트림 없음 | Entrypoint no hosts | `ENTRYPOINT_NO_HOSTS` (`CountsUnavailable.code`) | `GET /counters` 의 그 환경 항목 `hosts` 가 0(그 환경으로 갈 업스트림이 없음). 0건을 트래픽 적음으로 통과시키지 않으려고 판단 불가로 돌려준다(마감에 `METRICS_UNAVAILABLE` 롤백). `hosts` 키가 없으면 보지 않는다 | F7, API 8-2 |
| 조직 열쇠 키 | Organization secret key | `organization_secret_key(slug)` | slug 의 `-` 를 `_` 로 바꾸고 대문자(`team-echo` → `TEAM_ECHO`). `env:` 비밀 참조는 `ECHO_SECRET_{키}__` 로 시작해야 한다 | F16 |

---

## 3. 값 정의

### 배포 상태 (`DeploymentStatus`) — `deployments.status`

| 값 | 뜻 |
|---|---|
| `PENDING` | 배포 행을 만들었고 아직 시작하지 않음 (기본값) |
| `IN_PROGRESS` | 어댑터가 올리는 중 |
| `SUCCEEDED` | 헬스체크까지 통과. 이때 연결 행의 `current_deployment_id` 를 이 배포로 바꾼다 |
| `FAILED` | 오류·시간 초과 |
| `CANCELLED` | 시작 전에 취소 |

허용 전환 (DB 는 값만 CHECK 로 막고, 전환은 코드가 지킨다. 원터치 배포는 빌드 성공 뒤 `IN_PROGRESS` 로 만들고 성공 처리에서 `SUCCEEDED`, 배포 기록이 생긴 뒤 실패하면 `FAILED`):

| 지금 | 다음 |
|---|---|
| `PENDING` | `IN_PROGRESS`, `CANCELLED` |
| `IN_PROGRESS` | `SUCCEEDED`, `FAILED` |
| `SUCCEEDED`·`FAILED`·`CANCELLED` | 없음(끝 상태) |

- 끝 상태는 되감지 않는다. **재시도·되돌리기는 새 배포 행**을 만든다.
- 취소는 `PENDING` 에서만 한다. 올리는 중 취소는 클러스터에 중간 상태를 남기기 때문이다.

### 배포 요청 상태 (`DeployRequestStatus`) — `deploy_requests.status`

| 값 | 뜻 | 다음 |
|---|---|---|
| `QUEUED` | 접수됨, 시작 전(일꾼 대기 또는 같은 연결의 앞 요청 대기) | `BUILDING`, `CANCELLED` |
| `BUILDING` | 이미지 준비 중(재사용이면 바로 통과) | `DEPLOYING`, `FAILED` |
| `DEPLOYING` | 배포 실행기가 반영 중 | `VERIFYING`, `FAILED` |
| `VERIFYING` | 환경별 주소 헬스체크 중 | `SUCCEEDED`, `FAILED` |
| `SUCCEEDED`·`FAILED`·`CANCELLED` | 끝 상태 | 없음 |

- 같은 연결에는 실행 중(`BUILDING`·`DEPLOYING`·`VERIFYING`) 1개 + `QUEUED` 1개까지(부분 unique). 새 요청이 대기 요청을 밀어낸다.
- 일꾼이 임대를 3번 잃으면 어느 진행 상태에서든 `FAILED`(`WORKER_LOST`).
- 촉발(`trigger`): `MANUAL`(사람) · `STANDBY_SYNC`(예비 따라가기) · `ROLLBACK`(되돌리기, `rollback_of_request_id`·`rollback_base_deployment_id` 가 있다).
- 취소 이유(`cancel_reason`, `CANCELLED` 일 때만): `SUPERSEDED`(새 요청이 밀어냄) · `SERVICE_DELETED` · `BASE_CHANGED`(되돌리기를 시작할 때 연결의 현재 배포가 접수 때와 달랐다).
- 되돌리기의 가짜 실패 단계(`simulate.failAt`): `DEPLOYING` · `VERIFYING` 만(빌드 단계가 없다).
- 실패 이유(`error_code`, `FAILED` 일 때만, DB CHECK 없음): `REPO_NOT_ACCESSIBLE` · `REF_NOT_FOUND` · `SOURCE_PROVIDER_UNAVAILABLE` · `BUILD_FAILED` · `BUILD_TIMEOUT` · `DEPLOY_FAILED` · `DEPLOY_TIMEOUT` · `HEALTH_CHECK_FAILED` · `HEALTH_CHECK_TIMEOUT` · `PUBLIC_URL_UNREACHABLE` · `WORKER_LOST` · `INTERNAL_ERROR` · 실제 실행기만(API 서버 3-2) `BUILD_DISPATCH_FAILED`(빌드 워크플로를 시작하지 못함) · `CLUSTER_UNAUTHORIZED`(클러스터·자격 거절) · `IMAGE_PULL_FAILED`(이미지 받기 실패) · `CONTAINER_START_FAILED`(컨테이너가 계속 죽음). 뜻은 [docs/api/deploy-requests.md](../../docs/api/deploy-requests.md).
- 되돌리기 결과(`revert_status`): `SUCCEEDED` · `FAILED` · NULL(필요 없었음).
- 마지막 헬스 결과(`last_health_result`): `OK` · `HTTP_ERROR` · `UNREACHABLE`.
- 가짜 실패 단계(`simulate.failAt`): `BUILDING` · `DEPLOYING` · `VERIFYING`.

### 로그 값 (API 서버 4)

| 값 목록 | 표·칸 | 뜻 |
|---|---|---|
| `DEPLOY_JOB` · `BUILD` · `RUNTIME` | 로그 API `source` (`LogSource`) | 배포 작업 로그(DB) · 빌드 로그 · 실행 로그(실행기 즉석 조회) |
| `INFO` · `WARN` · `ERROR` | `deploy_request_logs.level`, 응답 `level` (`LogLevel`) | `FAILED`·`REVERT_FAILED` 는 ERROR, 헬스 실패 줄은 WARN |
| 요청 상태 7개 · `REVERT` | `deploy_request_logs.stage` (`DeployLogStage`) | 줄이 속한 단계 |
| `REQUEST_ACCEPTED` · `STARTED` · `BUILD_ATTACHED` · `BUILD_REUSED` · `DEPLOYMENT_CREATED` · `DEPLOY_REQUESTED` · `DEPLOY_APPLIED` · `HEALTH_PROBE_OK` · `HEALTH_PROBE_HTTP_ERROR` · `HEALTH_PROBE_UNREACHABLE` · `SUCCEEDED` · `STANDBY_SYNC_REQUESTED` · `FAILED` · `REVERT_SUCCEEDED` · `REVERT_FAILED` · `CANCELLED_SUPERSEDED` · `CANCELLED_SERVICE_DELETED` · `CANCELLED_BASE_CHANGED` · `LOG_TRUNCATED` | `deploy_request_logs.code` (`DeployLogCode`, DB CHECK 없음) | 코드별 고정 문장은 `app/services/deploy_log_messages.py` |

### 빌드·할 일·서비스 상태

| 값 목록 | 표·칸 | 뜻 |
|---|---|---|
| `QUEUED` → `RUNNING` → `SUCCEEDED`·`FAILED` | `builds.status` (`BuildStatus`) | `SUCCEEDED` 일 때만 digest 가 있다. 같은 서비스·저장소·커밋의 `FAILED` 아닌 빌드는 하나 |
| `QUEUED` → `RUNNING` → `DONE`·`FAILED`·`CANCELLED` | `jobs.status` (`JobStatus`) | 미룰 때는 `QUEUED` + `run_after`. `RUNNING` 이면 임대 칸이 있다 |
| `DEPLOY_REQUEST` · `SERVICE_TEARDOWN` · `MIGRATION_PLAN` | `jobs.kind` (`JobKind`) | 대상(`subject_id`)은 배포 요청 id · 서비스 id · 이전 계획 id(API 서버 8) |
| `ACTIVE` → `DELETING` → (소프트 삭제) | `services.status` (`ServiceStatus`) | 삭제 실패 이유 `delete_error`(`TEARDOWN_FAILED`·`TEARDOWN_TIMEOUT`·`WORKER_LOST`)는 `DELETING` 일 때만 |
| `SERVING` · `STANDBY` · `NONE` | 응답 `role` (`ServiceEnvironmentRole`, 저장 안 함) | 비율 > 0 · 비율 0 + 현재 배포 있음 · 현재 배포 없음 |
| `IN_SYNC` · `OUT_OF_SYNC` | 응답 `versionStatus` (`VersionStatus`) | 예비 연결만. 서비스 중 연결이 없으면 null |

### 이전 계획 값 (API 서버 8)

| 값 목록 | 표·칸 | 뜻 |
|---|---|---|
| `PENDING_APPROVAL` → `APPROVED` → `RUNNING` → `SUCCEEDED` / `RUNNING` → `ROLLING_BACK` → `ROLLED_BACK`·`FAILED` / `PENDING_APPROVAL` → `REJECTED`·`CANCELLED`·`EXPIRED`·`STALE` / `APPROVED` → `STALE`·`CANCELLED`·`FAILED` | `migration_plans.status` (`MigrationPlanStatus`) | 끝나지 않은 4개(`PENDING_APPROVAL`·`APPROVED`·`RUNNING`·`ROLLING_BACK`)는 서비스당 하나(부분 unique). 실행 중 3개(`APPROVED`·`RUNNING`·`ROLLING_BACK`)면 그 서비스의 배포 요청·되돌리기·삭제가 409 `MIGRATION_IN_PROGRESS` |
| `PLANNED` · `EMERGENCY` · `FAILBACK` | `migration_plans.mode` (`MigrationPlanMode`, 기본 `PLANNED`) | 보통 이전 · 재난 자동 이전(API 서버 9 부터, 지금 API 로는 안 만들어짐) · 앞선 성공 이전을 거꾸로 하는 복귀. 만들 때 판정해 저장하고 바꾸지 않는다(API 서버 8-3) |
| `PREFLIGHT` · `SHIFT` · `DATA_SYNC_CHECK` | `steps[i].kind` (`MigrationStepKind`) | 사전 확인 · 비율 단계 · 데이터 확인 |
| `APPLY` · `OBSERVE` · `JUDGE` | `migration_plans.step_phase` (`MigrationStepPhase`) | 비율 단계는 적용 → 건수 세기 → 판정(헬스체크), 사전 확인·데이터 확인은 판정만 |
| `COUNT_REACHED` · `LOW_TRAFFIC` | `step_results[i].judgeReason` (`MigrationJudgeReason`) | 비율 단계가 세기를 끝내고 판정으로 간 이유: 필요 건수 채움 · 최대 대기 끝 트래픽 적음 |
| `PENDING` · `RUNNING` · `PASSED` · `FAILED` · `SKIPPED` | `step_results[i].status` (`MigrationStepStatus`) | 롤백·실패로 끝나면 남은 단계는 `SKIPPED` |
| `PREFLIGHT_FAILED` · `TRAFFIC_APPLY_FAILED` · `HEALTH_CHECK_FAILED` · `METRICS_UNAVAILABLE` · `THRESHOLD_EXCEEDED` · `DATA_LAG_EXCEEDED` · `ABORTED` · `WORKER_LOST` · `INTERNAL_ERROR` · `TARGET_NOT_RECEIVING` | `migration_plans.error_code` (`MigrationErrorCode`) | 원인 코드. 판정 실패(`HEALTH_CHECK_FAILED`·`THRESHOLD_EXCEEDED`(도착 5xx 허용 초과)·`TARGET_NOT_RECEIVING`(도착 미수신)·`DATA_LAG_EXCEEDED`)와 판단 불가(`METRICS_UNAVAILABLE`, 카운터를 읽지 못함·오래됨)·적용 실패를 나눈다 |
| `SUCCEEDED` · `FAILED` · `NOT_NEEDED` | `migration_plans.rollback_status` (`MigrationRollbackStatus`) | 출발 100% 로 되돌린 결과. `NOT_NEEDED` 는 트래픽을 바꾼 적이 없었음 |
| `ERROR_RATE` · `HEALTH` · `DATA_LAG` · `TRAFFIC` · `TARGET_NOT_RECEIVING` · `LOW_TRAFFIC` (· `LATENCY`) | `simulate.reason` (`MigrationSimulateReason`) | 가짜 모드에서 `failAtStep` 단계를 일부러 실패(또는 트래픽 적음)시킬 원인. `DATA_LAG` 는 단계 3 만, `TARGET_NOT_RECEIVING` 은 1·2 만, 나머지는 1·2·4. `LATENCY` 는 저장된 옛 계획을 읽으려고 값만 남겼다(요청 422, 처리기는 무시) |
| `PLAN_CREATED` · `APPROVED` · `SELF_APPROVED` · `REJECTED` · `CANCELLED` · `EXPIRED` · `STALE` · `ABORT_REQUESTED` · `STARTED` · `PREFLIGHT_OK` · `PREFLIGHT_FAILED` · `WEIGHT_APPLIED` · `WEIGHT_APPLY_FAILED` · `COUNTERS_RESET` · `JUDGE_PASSED` · `JUDGE_FAILED` · `DATA_SYNC_OK` · `DATA_SYNC_FAILED` · `SUCCEEDED` · `ROLLBACK_STARTED` · `ROLLBACK_APPLIED` · `ROLLBACK_APPLY_FAILED` · `ROLLBACK_FAILED` · `FAILED` · `LOG_TRUNCATED` | `migration_plan_logs.code` (`MigrationLogCode`, DB CHECK 없음) | 코드별 고정 문장은 `app/services/migration_log_messages.py` |
| `serviceStatus` · `connections` · `sourceEnvironment` · `targetEnvironment` · `openDeployRequests` | 지문 항목(409 `PLAN_STALE` 의 `details[].field`) | 달라진 항목 이름 |

### 보안 판정 값 (보안·거버넌스 3)

| 값 목록 | 표·칸 | 뜻 |
|---|---|---|
| `C-IMG-1` · `C-IMG-2` · `C-IMG-3` · `C-CFG-1` · `C-TLS-1` · `C-TLS-2` · `C-NET-1` · `C-NET-2` · `C-IAM-1` · `C-IAM-2` · `C-SEC-1` · `C-AUD-1` · `C-AUD-2` · `C-CI-1` · `C-AI-1` · `C-DB-1` · `C-DST-1` · `C-POST-1` | 통제 ID (`SecurityControlId`) | 보안·거버넌스 1 SPEC §5.1 의 통제. 문서·화면과 같은 표기로 저장한다(UPPER_SNAKE 의 예외) |
| `PASS` · `WARN` · `FAIL` · `UNKNOWN` · `NA` | 증거·통제·매트릭스 칸 결과 (`SecurityResult`) | 확인하지 못한 것(`UNKNOWN`)과 목업은 PASS 가 아니다. `NA` 는 그 환경에 해당하지 않는 통제 |
| `REAL` · `CACHE` · `SIMULATED` | 증거 모드 (`SecurityFindingMode`) | `CACHE` 는 유효기간 안에서만 쓴다. `SIMULATED` 증거는 판정에 쓰지 않는다 |
| `TRIVY_IMAGE` · `TRIVY_CONFIG` · `TRIVY_SECRET` · `GITLEAKS` · `TLS_PROBE` · `MANUAL_REVIEW` · `WAS_TEST` · `WAS_STATE` · `AUDIT_LOG` · `PRECHECK` · `ENTRYPOINT` · `DATA_ENGINE` · `CLOUD_API` · `KUBERNETES_API` · `GITHUB_API` · `AI_REVIEW` | 증거 출처 (`SecurityFindingSource`) | 통제마다 받는 출처가 규칙표에 있다. `MANUAL_REVIEW` 는 점검자·근거 링크가 있어야 쓰고, `AI_REVIEW` 는 PASS 를 만들 수 없다 |
| `UNCONFIRMED` · `ESCALATED` · `DISMISSED` | AI 결과 확인 (`AiFindingConfirmation`) | 사람이 `ESCALATED` 로 바꿔야 차단 사유가 된다. 그 전에는 WARN, `DISMISSED` 는 쓰지 않는다 |
| `PREPARATION` · `EXECUTION` · `POST_SWITCH` | 판정 시점 (`SecurityCheckPhase`) | 준비(배포·설정 변경·주기 수집) · 실행(이전 승인·일꾼 시작, 준비 통제도 함께 본다) · 전환 직후 |
| `IAM` · `ENCRYPTION` · `AUDIT_LOG` · `POLICY` | 매트릭스 칸 (`SecurityMatrixCell`, API `iam`·`encryption`·`auditLog`·`policy`) | 칸 결과는 그 칸 통제의 가장 나쁜 값: FAIL > 필수 UNKNOWN > WARN(예외 포함) > 선택 UNKNOWN > PASS |
| `CONTROL_FAILED` · `CONTROL_WARNING` · `EVIDENCE_UNKNOWN` · `EVIDENCE_MISSING` · `EVIDENCE_EXPIRED` · `EVIDENCE_SIMULATED` · `EVIDENCE_INCOMPLETE` · `SOURCE_NOT_ALLOWED` · `SCOPE_MISMATCH` · `POLICY_VERSION_MISMATCH` · `PRECHECK_MISSING` · `AI_UNCONFIRMED` · `AI_ESCALATED` · `EXCEPTION_APPLIED` · `EXCEPTION_EXPIRED` · `NOT_APPLICABLE` | 판정 사유 (`SecurityReasonCode`) | 게이트가 막으면 통제 ID 와 함께 돌려준다(기계 판독용) |
| `NO_ISSUES` · `FIXABLE_HIGH_VULNERABILITY` · `VULNERABILITY_WARNING` · `SECRET_FOUND` · `MISCONFIGURATION_HIGH` · `MISCONFIGURATION_WARNING` · `ARTIFACT_MISMATCH` · `REPORT_INVALID` · `TLS_OK` · `TLS_CERT_INVALID` · `TLS_HOSTNAME_MISMATCH` · `TLS_EXPIRES_SOON` · `TLS_VERSION_TOO_LOW` · `TLS_HANDSHAKE_FAILED` · `TLS_TIMEOUT` · `TLS_UNREACHABLE` | 정규화 이유 (`SecurityFindingReason`, 보안 2) | 검사 원문을 증거로 바꾸며 정한 이유. 증거 한 건에 하나. 원문이 다른 대상 것이거나(`ARTIFACT_MISMATCH`) 모양이 틀리면(`REPORT_INVALID`) UNKNOWN, TLS 시간 초과·연결 불가도 UNKNOWN |

### 환경 종류 (`environments.kind`)

| 값 | 설정 양식 | `config` 필드 | 위치 |
|---|---|---|---|
| `aws` | `AwsEnvironmentConfig` | `region`(예 `ap-northeast-2`), `cluster_name` | `region` |
| `gcp` | `GcpEnvironmentConfig` | `project_id`(예 `echo-hack-123`), `region`(예 `asia-northeast3`), `cluster_name` | `region` |
| `onprem` | `OnpremEnvironmentConfig` | `location`(예 `team-laptop`), `kube_context` | `location` |

- 새 종류는 `app/adapters/<kind>.py` 파일 하나(설정 양식 + 동작 클래스)로 더한다. 표·마이그레이션·레지스트리·라우터·서비스는 고치지 않는다(NFR-15).
- 종류 이름은 클래스 변수일 뿐 `config` JSON 에 넣지 않는다.
- API 의 `config` 키는 camelCase(`clusterName`, `projectId`, `kubeContext`)다.

### 접속 상태 (`EnvironmentConnectionStatus`) — `environments.connection_status`

| 값 | 뜻 |
|---|---|
| `UNCHECKED` | 아직 확인하지 않았거나 config·secret_ref 가 바뀌어 다시 확인해야 함 (기본값). 시각·이유 없음 |
| `CONNECTED` | 마지막 확인 성공 |
| `FAILED` | 마지막 확인 실패. `connection_error` 에 이유 코드 |

### 접속 실패 이유 (`ConnectionErrorCode`) — `connection_error`, API `clusterInfoError`

`TIMEOUT` · `SECRET_REF_MISSING` · `SECRET_REF_INVALID` · `SECRET_NOT_FOUND` · `SECRET_STORE_UNSUPPORTED` · `CONFIG_INVALID` · `AUTH_FAILED` · `UNREACHABLE` · `UNKNOWN`. 뜻은 [docs/api/environments.md](../../docs/api/environments.md). DB CHECK 를 걸지 않는다(값을 더해도 마이그레이션 없음).

### 조직·권한 값 (API 서버 6)

| 값 목록 | 표·칸 | 뜻 |
|---|---|---|
| `VIEWER` · `DEVELOPER` · `ADMIN` | `organization_members.role`·`invitations.role` (`OrganizationRole`) | 순서 VIEWER < DEVELOPER < ADMIN. 에이전트 토큰은 역할 없이 읽기만 |
| `PENDING` → `ACCEPTED`·`EXPIRED`·`REVOKED` | `invitations.status` (`InvitationStatus`) | `ACCEPTED` 일 때만 `accepted_at`. 같은 조직·번호의 PENDING 은 하나 |
| `USER` · `AGENT` | `audit_logs.actor_type` (`ActorType`) | 세션 쿠키로 온 사람 · 에이전트 토큰 |
| `DENIED` | `audit_logs.result` (`AuditResult`) | 이번에는 거부만 기록 |

- 거부 이유(`audit_logs.reason`): `ROLE_FORBIDDEN` · `AGENT_READ_ONLY` · `CANNOT_CHANGE_OWN_ROLE` · `ORIGIN_NOT_ALLOWED`.
- 로그인 콜백 실패(프론트 `/login?error=`): `OAUTH_DENIED` · `OAUTH_STATE_MISMATCH` · `GITHUB_UNAVAILABLE` · `LOGIN_NOT_ALLOWED` · `LOGIN_NOT_CONFIGURED`.

### 비밀 저장소 (`SecretStore`) — `secret_ref` 의 첫 콜론 앞

| 값 | 위치 형식 | 지금 |
|---|---|---|
| `env` | `ECHO_SECRET_[A-Z0-9_]+` (서버 프로세스 환경변수). 환경 API 는 `ECHO_SECRET_{조직 키}__…` 만 받는다 | 실제 조회 |
| `k8s` | `<namespace>/<secret>/<key>` | 형식만 |
| `aws-sm` | 이름 또는 ARN(공백 없음) | 형식만 |
| `gcp-sm` | `projects/<p>/secrets/<s>[/versions/<v>]` | 형식만 |

---

## 4. 규칙

- `config_snapshot` 에는 **비밀값을 넣지 않는다.** 비밀이 필요한 환경변수는 `{"secret_ref": "..."}` 처럼 참조만 담는다. DB 가 막을 수 없으므로 저장 경로(API 서버 3)에서 검사한다.
- 되돌리기 행은 원본의 `image_repository`·`image_digest`·`config_snapshot` 을 복사하고 `rollback_of_id` 로 원본을 가리킨다.
- 서비스별 트래픽 비율의 합계(100%)는 DB 가 검사하지 않는다. 전환 중에는 잠깐 맞지 않을 수 있어서 이전 엔진(API 서버 8)이 맞춘다.
- 소프트 삭제된 행은 이름·연결 유일성에서 빠진다(같은 이름으로 다시 만들 수 있다).
- 살아 있는 연결이 있는 환경은 지울 수 없다(409 `ENVIRONMENT_IN_USE`). 연결을 만드는 쪽(배포 요청 접수)은 환경 행을 `FOR SHARE` 로 잠근 뒤 만든다(환경 삭제의 `FOR UPDATE` 와는 줄을 서고, 일꾼의 FK 검사 `FOR KEY SHARE` 와는 부딪히지 않아 교착이 없다).
- 서비스 삭제 = 모든 연결을 내린 뒤 소프트 삭제(`DELETING` 동안 PATCH·배포 요청 409). 내린 연결은 바로 소프트 삭제하고, 배포 기록·배포 요청은 남긴다.
- 쓰기 경로의 잠금 순서는 하나다: 조직(멤버 변경·초대만, `FOR NO KEY UPDATE`) → 환경(연결을 만들 때만) → 서비스 → 그 서비스의 연결(id 오름차순) → 배포 요청 → 빌드 → 할 일. 상태는 조건부 UPDATE 로만 바꾼다.
- 서비스 이름과 환경 이름은 조직 안에서 유일하다(주소 유일성은 `host_suffix` 와 `public_host` 전체 unique 가 맡는다). 다른 조직의 리소스는 404 로 숨긴다(멤버가 아닌 조직도 `ORGANIZATION_NOT_FOUND`).
- 멤버 변경은 조직 행을 잠근 뒤 잠금 안에서 행위자 역할을 다시 읽는다. 자기 역할은 못 바꾸고, 변경 뒤 어드민은 1명 이상이다.
- 세션 쿠키 값·에이전트 토큰 원문(발급 응답 제외)·GitHub 사용자 토큰·client secret 은 저장·로그·응답·예외 메시지에 넣지 않는다.
- 외부 호출은 트랜잭션 밖에서, 멱등 키와 함께, "요청함 시각 저장 → 호출 → 작업 번호 저장" 순서로 한다([docs/executors.md](../../docs/executors.md)).
- 가짜 실행기·가짜 소스 클라이언트 결과에는 `simulated = true` 를 행에 저장하고 응답에 싣는다(설정으로 다시 계산하지 않는다).
- 다른 파트·API 는 환경을 `id` 로만 가리킨다. `name` 은 표시용이고 바뀔 수 있다.
- 비밀값(`SecretValue`)은 저장·로그·응답·예외 메시지에 넣지 않는다.

---

## 5. 후속 용어\*

| 우리말 | 표준 영문 | 예정 표 | 뜻 | 근거 | 담당 |
|---|---|---|---|---|---|
| 감사 로그\* | Audit log | `audit_logs` (거부 기록만 API 서버 6 이 만듦) | 누가(사람·AI 에이전트) 무엇을 언제 했는지. 지우지 않는다. 거부 밖의 행위 기록·조회 API 가 후속 | F5, NFR-08 | API 서버 7 |
| 승인(에이전트 변경)\* | Approval (agent change) | (에이전트 변경에 붙음) | AI 수정의 운영 반영을 개발자 이상의 사람이 허락한 기록. 이전 계획 승인은 §2 | F15 | AI 파트 |
| 사전 점검\* | Preflight check | (미정) | 이전 전 점검 | F11 | API 서버 8 |
| 정책 | Policy | 표 없음 — 코드 `app/services/security_rules.py::RULES`, 버전 `POLICY_VERSION`(`sec-rules-YYYY.MM.DD`) | 모든 환경에 같이 적용하는 **버전이 붙은 보안 규칙표**(통제마다 필수 여부·판정 시점·범위·받는 출처·유효기간). 저장소 안 코드 + 시험으로 관리하고 통제를 끄는 설정이 없다. 규칙을 바꾸면 버전을 올린다 | F13, 보안 3 | 보안 파트 |
| finding\* | Finding | `findings` (보안 2) — 판정 입력 `SecurityFinding` | 클라우드별 보안 결과를 공통 스키마로 정규화한 한 건(통제·결과·모드·출처·범위·수집 시각·만료·정책 버전). 판정에서는 증거로 쓰여 사유 코드는 `EVIDENCE_*` 로 부른다 | F12, 보안 2·3 | 보안 파트 |
| 기한부 예외\* | Security exception | `security_exceptions` (가칭) — 판정 입력 `SecurityException` | 통제 하나를 범위 안에서 최대 7일 막지 않게 하는 기록. 사유·사람 승인자·만료가 있어야 하고 AGENT 는 만들거나 승인할 수 없다. 걸린 통제는 PASS 가 아니라 WARN 으로 보인다 | F13, 보안 3 | 보안 파트 |
| 보안 판정\* | Security decision | `security_decisions` (가칭) — `SecurityDecision` | 판정 한 번의 결과(정책 버전·시점·계획 종류·통제별 결과·사유·증거·예외 ID). 사람 승인 이전과 사전 승인 자동 DR 이 같은 함수·같은 표를 쓴다 | F13, 보안 3·5 | 보안 파트 |
| 에이전트\* | Agent | (미정) | 플랫폼에 들어 있는 AI 에이전트. 샌드박스 쓰기 + 운영 읽기만 | F14·F16 | AI 파트 |
| 샌드박스\* | Sandbox | (미정) | 에이전트가 점검·수정을 시험하는 격리 공간(별도 네임스페이스 또는 VM) | F14·F15 | AI 파트 |
| 측정 기록\* | Migration measurement | (미정) | 이전 중 오류율·지연 등 자동 기록 | F17 | 관측 담당 |

---

## 6. 혼동 주의 (동음이의어)

| 단어 | 이 저장소에서 | 헷갈리는 것 |
|---|---|---|
| Service | 사용자 앱(`services` 표, `service_id`) | Kubernetes `Service` 리소스, 코드 레이어 접미사 `*Service`(예: `DeploymentService`). 로그에 `service` 필드를 쓰지 않는 이유 |
| Deployment | 배포 이력 한 행(`deployments`) | Kubernetes `Deployment` 리소스 |
| Environment | 배포 대상 클러스터 하나(`environments`) | 실행 설정(`.env`, dev·prod 단계). 앱 실행 단계를 말할 때는 "실행 환경"·"단계"로 부른다 |
| Config | 환경 종류별 설정(`environments.config`), 배포 설정(`config_snapshot`) | 앱 설정 `Settings`(`app/core/config.py`) |
| Adapter | 환경 종류 하나의 묶음(설정 양식 + 이후 동작 코드) | 설정 양식은 어댑터의 "검사" 부분만 말한다 |
| Traffic weight | 마지막으로 프록시에 적용한 값 | 지금 실제로 흐르는 비율(측정값, F7·F17) |
| Deploy request | 원터치 배포 한 번(`deploy_requests`: 빌드·대기·취소·실패 이유 포함) | 배포 기록(`deployments`: 연결에 이미지를 올린 것, 빌드 성공 뒤에만 생긴다) |
| Revert | 실패한 새 버전을 거두는 실행기 호출(`revert_status`) | 사용자 되돌리기(`trigger = ROLLBACK` 배포 요청 → `rollback_of_id` 새 배포 행, API 서버 4), 이전 롤백(출발 100% 로 비율 되돌리기, `migration_plans.rollback_status`, API 서버 8) |
| Migration | 이전 계획(`migration_plans`, 트래픽을 다른 환경으로 옮기기) | DB 스키마 마이그레이션(Alembic revision). 코드에서는 `MigrationPlan*` 처럼 늘 `Plan` 을 붙인다 |
| Log | 배포 작업 로그(`deploy_request_logs`)·빌드 로그·실행 로그(API 서버 4) | 감사 로그(`audit_logs`, 누가 무엇을 했나), 서버 자체 구조화 로그(`logger`) |
| Session | 로그인 세션 한 행(`sessions`, 클래스 `UserSession`) | SQLAlchemy `Session`·`AsyncSession`(DB 세션). 그래서 모델 클래스 이름을 `UserSession` 으로 둔다 |
| Role | 조직 멤버 역할(`OrganizationRole`: VIEWER·DEVELOPER·ADMIN) | 서비스-환경 연결의 역할(`ServiceEnvironmentRole`: SERVING·STANDBY·NONE) |
| Organization | Echo 의 고객 조직(`organizations`) | GitHub 조직(로그인 허용 스위치 `login_allowed_github_org`) |

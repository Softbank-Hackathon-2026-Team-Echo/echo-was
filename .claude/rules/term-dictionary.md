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
| 되돌리기 | Rollback | `deployments.rollback_of_id` | 예전 배포의 이미지·설정으로 **새 배포 행**을 만드는 것. 원본을 가리킨다 | F1·F9 |
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
| 환경별 주소 | Environment URL | `service_environments.public_host` / 응답 `url`·`urls.environment` | `https://{서비스}-{환경}-{suffix}.{public_base_domain}`(서비스의 suffix). 연결을 만들 때 정해 저장한다(환경 이름이 바뀌어도 그대로). 헬스체크 대상 | F1·D8, API 3-1 |
| 주소 suffix | Host suffix | `services.host_suffix` | 대표·환경별 주소 끝의 소문자·숫자 무작위 8자. 서비스를 만들 때 한 번 정하고 바꾸지 않는다(지운 서비스의 주소를 같은 이름의 새 서비스가 잇지 못하게). 레이블이 63자를 넘으면 앞부분을 54자로 자르고 suffix 는 남긴다 | API 3-1 |
| 실행기 | Executor | `app/executors/` (`BuildExecutor`·`DeployExecutor`·`MainRouteExecutor`·`HealthChecker`·`TeardownExecutor`) | 빌드·배포·대표 주소 연결·헬스체크·내리기를 실제로 하는 쪽. 이번에는 가짜(`simulated`) | F1 |
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
- 촉발(`trigger`): `MANUAL`(사람) · `STANDBY_SYNC`(예비 따라가기).
- 취소 이유(`cancel_reason`, `CANCELLED` 일 때만): `SUPERSEDED`(새 요청이 밀어냄) · `SERVICE_DELETED`.
- 실패 이유(`error_code`, `FAILED` 일 때만, DB CHECK 없음): `REPO_NOT_ACCESSIBLE` · `REF_NOT_FOUND` · `SOURCE_PROVIDER_UNAVAILABLE` · `BUILD_FAILED` · `BUILD_TIMEOUT` · `DEPLOY_FAILED` · `DEPLOY_TIMEOUT` · `HEALTH_CHECK_FAILED` · `HEALTH_CHECK_TIMEOUT` · `PUBLIC_URL_UNREACHABLE` · `WORKER_LOST` · `INTERNAL_ERROR`. 뜻은 [docs/api/deploy-requests.md](../../docs/api/deploy-requests.md).
- 되돌리기 결과(`revert_status`): `SUCCEEDED` · `FAILED` · NULL(필요 없었음).
- 마지막 헬스 결과(`last_health_result`): `OK` · `HTTP_ERROR` · `UNREACHABLE`.
- 가짜 실패 단계(`simulate.failAt`): `BUILDING` · `DEPLOYING` · `VERIFYING`.

### 빌드·할 일·서비스 상태

| 값 목록 | 표·칸 | 뜻 |
|---|---|---|
| `QUEUED` → `RUNNING` → `SUCCEEDED`·`FAILED` | `builds.status` (`BuildStatus`) | `SUCCEEDED` 일 때만 digest 가 있다. 같은 서비스·저장소·커밋의 `FAILED` 아닌 빌드는 하나 |
| `QUEUED` → `RUNNING` → `DONE`·`FAILED`·`CANCELLED` | `jobs.status` (`JobStatus`) | 미룰 때는 `QUEUED` + `run_after`. `RUNNING` 이면 임대 칸이 있다 |
| `DEPLOY_REQUEST` · `SERVICE_TEARDOWN` | `jobs.kind` (`JobKind`) | 대상(`subject_id`)은 배포 요청 id · 서비스 id |
| `ACTIVE` → `DELETING` → (소프트 삭제) | `services.status` (`ServiceStatus`) | 삭제 실패 이유 `delete_error`(`TEARDOWN_FAILED`·`TEARDOWN_TIMEOUT`·`WORKER_LOST`)는 `DELETING` 일 때만 |
| `SERVING` · `STANDBY` · `NONE` | 응답 `role` (`ServiceEnvironmentRole`, 저장 안 함) | 비율 > 0 · 비율 0 + 현재 배포 있음 · 현재 배포 없음 |
| `IN_SYNC` · `OUT_OF_SYNC` | 응답 `versionStatus` (`VersionStatus`) | 예비 연결만. 서비스 중 연결이 없으면 null |

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
| 배포 로그\* | Deployment log | (저장 방식 미정) | 배포 작업 로그·빌드 로그·실행 로그 | F1 | API 서버 4 |
| 감사 로그\* | Audit log | `audit_logs` (거부 기록만 API 서버 6 이 만듦) | 누가(사람·AI 에이전트) 무엇을 언제 했는지. 지우지 않는다. 거부 밖의 행위 기록·조회 API 가 후속 | F5, NFR-08 | API 서버 7 |
| 이전 계획\* | Migration plan | `migration_plans` | 단계·예상 시간·롤백 조건을 담은 계획. 개발자 이상이 실행(검사기 `require_org_role(DEVELOPER, human_only=True)`) | F10 | API 서버 8 |
| 승인\* | Approval | (이전 계획·에이전트 변경에 붙음) | 개발자 이상의 사람이 실행·운영 반영을 허락한 기록(자기 승인 규칙은 API 서버 8) | F10·F15 | API 서버 8 |
| 사전 점검\* | Preflight check | (미정) | 이전 전 점검 | F11 | API 서버 8 |
| 정책\* | Policy | `policies` | 모든 환경에 같이 적용하는 보안 규칙(Rego 파일 하나) | F13 | 보안 파트 |
| finding\* | Finding | `findings` | 클라우드별 보안 결과를 공통 스키마로 정규화한 한 건 | F12 | 보안 파트 |
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
| Revert | 실패한 새 버전을 거두는 실행기 호출(`revert_status`) | 사용자 되돌리기(`rollback_of_id`, 새 배포 행, API 서버 4) |
| Session | 로그인 세션 한 행(`sessions`, 클래스 `UserSession`) | SQLAlchemy `Session`·`AsyncSession`(DB 세션). 그래서 모델 클래스 이름을 `UserSession` 으로 둔다 |
| Role | 조직 멤버 역할(`OrganizationRole`: VIEWER·DEVELOPER·ADMIN) | 서비스-환경 연결의 역할(`ServiceEnvironmentRole`: SERVING·STANDBY·NONE) |
| Organization | Echo 의 고객 조직(`organizations`) | GitHub 조직(로그인 허용 스위치 `login_allowed_github_org`) |

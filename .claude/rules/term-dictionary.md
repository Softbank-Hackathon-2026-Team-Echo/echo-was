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
| 트래픽 비율 | Traffic weight | `service_environments.traffic_weight` | 앞단 프록시에 **마지막으로 적용한** 비율(0~100). 실제 분배는 프록시(Envoy)가 한다 | F3·F7 |
| 비율 적용 시각 | Traffic applied at | `service_environments.traffic_applied_at` | 위 비율을 프록시에 적용한 시각 | F7 |
| 어댑터 | Adapter | `app/adapters/<kind>.py` | 환경 종류마다 하나. 이번에는 설정 양식만, 동작 코드(배포·상태 조회)는 API 서버 2 | F2, NFR-15 |
| 설정 양식 | Environment config | `EnvironmentConfig` 하위 클래스 | 환경 종류별 `config` JSON 의 형식(Pydantic). 저장 전에 검사한다 | F2, NFR-15 |
| 설정 | Config | `environments.config` (jsonb) | 환경 종류별 고유 설정(리전·클러스터 이름 등). 비밀값 없음 | F2 |
| 비밀 참조 | Secret reference | `environments.secret_ref` | 비밀 저장소(클라우드 Secrets Manager·k8s Secret, 로컬은 `.env`)의 보관 위치. 값은 저장하지 않는다 | F2·F16 |
| 설정 스냅샷 | Config snapshot | `deployments.config_snapshot` (jsonb) | 배포 때 쓴 설정(복제 수·환경변수 등). 되돌리기가 이 값을 복사한다. **비밀값 없이 참조만** | F1 |
| 이미지 digest | Image digest | `deployments.image_digest` | 배포 기준이 되는 이미지 내용 해시 | F1 |
| 위치 | Location | `EnvironmentConfig.display_location()` | 환경 카드의 "위치" 칸 값(aws·gcp 는 region, onprem 은 location) | F3 |
| 소프트 삭제 | Soft delete | `is_deleted`·`deleted_at` | 행을 지우지 않고 표시만 한다. 두 칸은 항상 함께 움직인다 | — |

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

허용 전환 (상태 전환 코드는 API 서버 3 에서 쓴다. 지금 DB 는 값만 CHECK 로 막는다):

| 지금 | 다음 |
|---|---|
| `PENDING` | `IN_PROGRESS`, `CANCELLED` |
| `IN_PROGRESS` | `SUCCEEDED`, `FAILED` |
| `SUCCEEDED`·`FAILED`·`CANCELLED` | 없음(끝 상태) |

- 끝 상태는 되감지 않는다. **재시도·되돌리기는 새 배포 행**을 만든다.
- 취소는 `PENDING` 에서만 한다. 올리는 중 취소는 클러스터에 중간 상태를 남기기 때문이다.

### 환경 종류 (`environments.kind`)

| 값 | 설정 양식 | `config` 필드 | 위치 |
|---|---|---|---|
| `aws` | `AwsEnvironmentConfig` | `region`(예 `ap-northeast-2`), `cluster_name` | `region` |
| `gcp` | `GcpEnvironmentConfig` | `project_id`(예 `echo-hack-123`), `region`(예 `asia-northeast3`), `cluster_name` | `region` |
| `onprem` | `OnpremEnvironmentConfig` | `location`(예 `team-laptop`), `kube_context` | `location` |

- 새 종류는 `app/adapters/<kind>.py` 파일 하나로 더한다. 표·마이그레이션·레지스트리는 고치지 않는다(NFR-15).
- 종류 이름은 클래스 변수일 뿐 `config` JSON 에 넣지 않는다.

---

## 4. 규칙

- `config_snapshot` 에는 **비밀값을 넣지 않는다.** 비밀이 필요한 환경변수는 `{"secret_ref": "..."}` 처럼 참조만 담는다. DB 가 막을 수 없으므로 저장 경로(API 서버 3)에서 검사한다.
- 되돌리기 행은 원본의 `image_repository`·`image_digest`·`config_snapshot` 을 복사하고 `rollback_of_id` 로 원본을 가리킨다.
- 서비스별 트래픽 비율의 합계(100%)는 DB 가 검사하지 않는다. 전환 중에는 잠깐 맞지 않을 수 있어서 이전 엔진(API 서버 8)이 맞춘다.
- 소프트 삭제된 행은 이름·연결 유일성에서 빠진다(같은 이름으로 다시 만들 수 있다).
- 살아 있는 연결이 있는 서비스·환경, current 배포가 있는 연결을 지울 때의 처리는 삭제 기능을 만드는 태스크(환경은 API 서버 2, 서비스·연결은 API 서버 3)에서 정한다.

---

## 5. 후속 용어\*

| 우리말 | 표준 영문 | 예정 표 | 뜻 | 근거 | 담당 |
|---|---|---|---|---|---|
| 빌드\* | Build | `builds` | 소스에서 이미지를 만드는 일. 빌드 1 : 배포 N (같은 이미지를 여러 환경에) | F1 | API 서버 3 |
| 배포 로그\* | Deployment log | (저장 방식 미정) | 배포 작업 로그·빌드 로그·실행 로그 | F1 | API 서버 4 |
| 역할\* | Role | (미정) | 뷰어·운영자·승인자·에이전트 4역할. 코드 값(`UPPER_SNAKE`)과 표는 담당 태스크에서 정한다 | F4, NFR-07 | API 서버 6 |
| 감사 로그\* | Audit log | `audit_logs` | 누가(사람·AI 에이전트) 무엇을 언제 했는지. 지우지 않는다 | F5, NFR-08 | API 서버 7 |
| 이전 계획\* | Migration plan | `migration_plans` | 단계·예상 시간·롤백 조건을 담은 계획. 승인자가 승인해야 실행 | F10 | API 서버 8 |
| 승인\* | Approval | (이전 계획·에이전트 변경에 붙음) | 승인자가 실행을 허락한 기록 | F10·F15 | API 서버 8 |
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

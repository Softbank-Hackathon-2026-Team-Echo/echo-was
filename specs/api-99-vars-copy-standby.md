# SPEC — 변수 다른 환경으로 복사 · 첫 배포 때 예비 환경 함께 띄우기 (echo-was, 이슈 #99 1·2번)

- 상태: **구현 PR** · 이슈 [#99](https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/99)(요청 김도영, echo-frontend#92). 배경: 도현님이 환경마다 변수를 다시 입력함
- 기준: echo-was main `30aca36`(#96 첫 배포 전 변수 PUT, #97 실패 진단). **마이그레이션 0**, `alembic heads` 하나(`31bc0563b24b`) 그대로
- 앞 작업: [api-10-app-env-secrets.md](api-10-app-env-secrets.md), [api-10-4-vars-before-first-deploy.md](api-10-4-vars-before-first-deploy.md)(배포 없는 연결), [api-3-one-touch-deploy.md](api-3-one-touch-deploy.md)(예비 따라가기)
- 범위 밖: 이슈 #99 의 3~6번(`env-template`·서비스별 활동·복제 수·CPU/메모리)

## 1. 계약 (이슈 칸 이름 유지)

| 항목 | 이슈 | 구현 |
|---|---|---|
| 복사 경로 | `POST /orgs/{orgId}/services/{id}/variables:copy` | 같음(`/api/v1` 아래) |
| 복사 요청 | `sourceEnvironmentId`, `targetEnvironmentIds[]`, `keys[]`, `overwrite=false` | 같음. 대상 1~10개, 키 1~40개 |
| 복사 응답 | `copied[{environmentId,key,version}]`, `failed[{environmentId,key,code}]` | 같음 + `copied[].pendingRedeploy`, 최상위 `simulated`(더하기만) |
| 복사 권한 | 개발자 이상 | 개발자 이상 **사람**(값 넣기 `PUT` 과 같은 검사기, 에이전트 403) |
| 같은 환경을 원본·대상에 | (지시: 400) | **422 `VALIDATION_ERROR`**(`reason = SOURCE_IN_TARGETS`). 저장소의 요청 칸 오류는 모두 422 이고 400 응답이 없다 |
| 배포 요청 | `standbyEnvironmentIds[]`(선택) | 같음. 0~5개 |
| 예비 응답 칸 | (이슈에 없음) | 상세 `standbyDeployRequestIds`(가칭) + `standbyEnvironmentIds`(가칭, 받은 목록) |
| 예비 = 주 환경·중복 | (지시: 400) | 422 `VALIDATION_ERROR`(`SAME_AS_ENVIRONMENT`·`DUPLICATE`) |
| 예비 환경 없음 | 404 | 404 `ENVIRONMENT_NOT_FOUND`(접수 거절), 접속 확인 `FAILED` 면 409 `ENVIRONMENT_UNHEALTHY` |

## 2. 결정

### 2-1. 복사 = 원본 읽기 + 넣기 저장 경로 재사용

1. 요청 칸 규칙(원본 ∈ 대상, 중복, 키 이름)을 먼저 본다.
2. 짧은 트랜잭션에서 서비스(404·삭제 중 409)·원본/대상 환경(같은 조직에 살아 있어야, 아니면 404 `SERVICE_ENVIRONMENT_NOT_FOUND`)을 확인하고 원본 연결의 키마다 `(비밀 이름, 최신 버전)` **값만** 모은다. 새 연결이 필요한 대상이 있으면 `check_no_migration_in_progress` 로 통째 409(넣기 #96 규칙).
3. 트랜잭션 밖에서 원본 값을 `AppSecretStore.access` 로 읽는다(10초). 장애·권한 거절은 통째 502, 파기된 버전은 그 키만 `ENV_SECRET_UNAVAILABLE`.
4. 대상 × 키마다 `_save`(= `PUT` 의 본체를 뽑은 것)를 따로 부른다: 서비스 → 연결 잠금, 연결이 없으면 배포 없는 연결, `overwrite = false` 면 있는 키는 **보관함을 부르기 전에** `ENV_VAR_ALREADY_EXISTS`, 40개 한도, 보관함 `add_version`, 감사 `SERVICE_VARIABLE_COPIED`.
5. 항목 실패(`ENV_VAR_ALREADY_EXISTS`·`ENV_VAR_LIMIT_EXCEEDED`·`MIGRATION_IN_PROGRESS`·`PUBLIC_HOST_CONFLICT`·`SERVICE_ENVIRONMENT_NOT_FOUND`·`SECRET_STORE_*`)는 `failed` 로 싣고 계속한다. 보관함 장애·권한 거절이 나면 남은 항목은 부르지 않고 같은 코드로 싣는다(항목마다 10초를 기다리지 않게).

- 항목마다 트랜잭션이 따로라 일부만 복사될 수 있다(응답이 그대로 보인다). 실패한 항목의 롤백은 세션 객체를 만료시키므로 원본은 값(dataclass)으로만 들고 다닌다.
- 값은 `SecretValue` 로만 넘긴다. 응답·감사 `details`·로그에는 키와 환경 id 뿐이다(`test_copy_to_deployed_and_pre_deploy_environments_writes_new_versions` 가 DB 전체·로그·응답에서 값과 변형을 찾는다).

### 2-2. 예비 함께 띄우기 = 요청 설정 사본 + 성공 처리에서 기존 따라가기

- **저장**: 표 칸을 더하지 않고 수동 요청의 `config_snapshot.standbyEnvironmentIds` 에 적는다(마이그레이션 0). 배포 기록(`_deployment_snapshot`)과 따라가기 요청 사본에서는 뺀다 → 되돌리기(배포 기록 사본을 씀)가 예비를 다시 띄우지 않는다. 일꾼은 `trigger = MANUAL` 일 때만 읽는다.
- **실행**: `DeployRequestProcessor._succeed`(서비스 → 연결 잠금 트랜잭션) 안, 기존 예비 따라가기 반복 바로 뒤. 주 연결이 서비스 중이고 서비스가 ACTIVE 일 때 환경마다
  - 이미 따라가는 예비(비율 0 + 현재 배포)면 기존 반복이 넣었으므로 건너뜀(중복 없음)
  - 연결 없음 → 환경 `FOR SHARE` 로 잡고 살아 있는지 다시 본 뒤 `create_service_connection`(저장점, 주소 충돌이면 그 환경만 건너뜀)
  - 배포 없는 연결(첫 배포 전 변수 PUT 이 만든 것 포함) → 그대로
  - 그 연결에 `_enqueue_standby_sync`(같은 빌드, 설정 사본에서 환경별 주소만 교체, 앱 환경 변수는 그 연결의 값)
- **건너뜀**: 주 연결이 서비스 중이 아님(예비에 직접 배포)·서비스 삭제 중·실행 중 이전(하나도 안 띄움)·환경 삭제·주소 충돌 → 원본 요청에 `STANDBY_SKIPPED`(WARN, 환경 id + 고정 사유) 줄. **주 환경 실패·취소면 성공 처리가 없으므로 아무것도 만들지 않는다.**
- **응답**: `POST` 202 에서는 `standbyDeployRequestIds = []`(아직 성공 전). 단건 조회가 `source_request_id = 이 요청 AND trigger = STANDBY_SYNC` 를 읽어 채운다(기존 예비 따라가기도 포함).

## 3. 영향 분석 — #96 '배포 없는 연결' 규칙과의 상호작용

| 대상 | 영향 |
|---|---|
| 잠금 순서 | 성공 처리가 서비스 → 연결을 잡은 뒤 환경을 `FOR SHARE` 로 잡는다(접수의 환경 → 서비스와 반대). 환경 삭제는 환경 `FOR UPDATE` 뒤 연결을 **잠그지 않고** 세기만 하고, 환경 수정·접속 확인도 서비스를 잠그지 않아 순환 대기가 없다. `FOR SHARE` 는 FK 의 `FOR KEY SHARE` 와 부딪히지 않는다 |
| 이전 지문 | 새 배포 없는 연결은 `connections` 에서 빠진다(#96). 새 `STANDBY_SYNC` 요청은 `openDeployRequests` 로 잡혀 승인 대기 계획을 STALE 로 만든다 — 기존 예비 따라가기와 같다 |
| 이전 가드 | 실행 중 이전이면 요청한 예비를 하나도 띄우지 않는다(넣기·접수와 같은 취지, 일꾼은 409 대신 건너뜀 줄). 기존 예비 따라가기는 예전처럼 이 검사를 받지 않는다 |
| 보안 매트릭스·C-SEC-1·게이트 | 따라가기 성공 전에는 배포 없는 연결이라 제외(#96). 성공 뒤에는 보통 예비와 같이 잡힌다(의도) |
| 대표 주소·지표 | 예비 연결은 비율 0 이라 `_route_main` 대상이 아니다. 지표 시리즈는 현재 배포가 생긴 뒤에 잡힌다 |
| 복사가 만든 연결 | `PUT` 과 같은 배포 없는 연결 — #96 표 그대로 |
| 환경 삭제 | 복사·예비가 만든 연결도 살아 있는 연결이라 환경 삭제가 409 `ENVIRONMENT_IN_USE`(#96 과 같은 운영 영향) |

## 4. 완료 기준 대응(시험)

- 복사 성공(배포한 환경 + 첫 배포 전 환경, 값 대조, 원본 버전 불변, tokyo 연결 NONE) · 감사 줄 키만 · DB/로그/응답에 값 없음 — `test_copy_to_deployed_and_pre_deploy_environments_writes_new_versions`
- 충돌(overwrite=false → `ENV_VAR_ALREADY_EXISTS`, 보관함 미호출) · overwrite=true → 새 버전 — `test_existing_key_fails_without_overwrite_and_gets_new_version_with_overwrite`
- 없는 키·요청 규칙·404 — `test_missing_key_and_request_rules`
- 이전 중 409(새 연결 필요할 때만) — `test_copy_needing_new_connection_during_migration_is_409`
- 보관함 장애 중 남은 항목 미호출·연결 롤백 — `test_store_outage_while_writing_fails_remaining_items_without_calling_again`
- 권한(뷰어·에이전트 403, 개발자 200) — `test_copy_is_human_developer_only` + 역할 표
- 예비 함께 배포 성공 → 연결 + STANDBY_SYNC + STANDBY 역할 + `standbyDeployRequestIds` — `test_first_deploy_with_standby_creates_connection_and_standby_sync`
- 주 환경 실패 → 예비 연결·요청 없음 — `test_main_failure_creates_no_standby_connection_or_sync`
- 되돌리기는 예비를 다시 띄우지 않음, 이미 예비면 한 번만, 예비에 직접 배포면 건너뜀, 그 사이 환경 삭제면 건너뜀 줄 — 같은 파일의 나머지 시험
- 순수 규칙 — `tests/test_vars_copy_standby_rules.py`

# SPEC — 첫 배포 전에도 환경별 환경 변수를 저장할 수 있게 한다 (echo-was, API 서버 10-4)

- 상태: **구현 PR** · 이슈 [#93](https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/93)(발견 이도현 11/6, `shap-api` 3회 실패)
- 기준: echo-was main `7c53d65`. 표·칸은 그대로 → **마이그레이션 0**, `alembic heads` 하나(`31bc0563b24b`) 그대로
- 앞 작업: [api-10-app-env-secrets.md](api-10-app-env-secrets.md)(변수 API), [sec-1-2-secret-delivery.md](sec-1-2-secret-delivery.md)(C-SEC-1 키 비교, #87)
- 범위 밖: **배포 요청 전 필수 변수 누락 검사**(저장소 `.env.example`·`echo.yaml` 선언과 비교해 빠진 키를 알리고 막기 — 별도 이슈), 프론트 안내 문구 제거(프론트 화면 이슈), 서비스 등록 때 환경별 연결 미리 만들기

## 1. 문제

변수 API 는 서비스 × 환경 연결(`service_environments`)이 있어야 동작하는데 연결은 첫 배포 요청 때만 생겼다. 그래서 `DATABASE_URL` 이 필수인 앱은 "배포 실패 → 변수 입력 → 다시 배포"를 환경마다 거쳐야 했다.

## 2. 결정 — 변수 넣기가 배포 없는 연결을 만든다 (작은 쪽)

| 경로 | 연결이 없을 때 지금 | 바뀐 뒤 |
|---|---|---|
| `PUT …/variables/{key}` | 404 `SERVICE_ENVIRONMENT_NOT_FOUND` | 환경이 서비스와 같은 조직에 살아 있으면 **배포 없는 연결**(비율 0, 현재 배포 없음 = 역할 NONE)을 만들고 저장(201) |
| `GET …/variables` | 404 | 같은 조건이면 200 `{ items: [], deployed: false }`. **연결을 만들지 않는다**(읽기 부작용 없음) |
| `DELETE …/variables/{key}` | 404 | 같은 조건이면 404 `ENV_VAR_NOT_FOUND`(지울 키가 없다). 연결을 만들지 않는다 |
| 위 셋, 환경이 다른 조직·없음·지움 | 404 `SERVICE_ENVIRONMENT_NOT_FOUND` | 그대로 |
| 첫 배포 요청 | 연결을 만든다 | 연결이 있으면 그대로 쓰고, 없으면 만든다(같은 함수 `create_service_connection`) |

- 연결 만들기는 배포 접수와 같은 함수·같은 잠금 순서(환경 `FOR SHARE` → 서비스 → 연결)다. 환경 삭제(`FOR UPDATE`, 살아 있는 연결이 있으면 409)와 줄을 서서 지운 환경에 연결이 생기지 않는다. 같은 서비스의 동시 `PUT`·배포 접수는 서비스 행 잠금으로 줄을 선다.
- 이미 연결이 있는 `PUT`·`DELETE` 는 예전과 같다(환경 행을 잠그지 않는다). 미리 읽은 연결이 없을 때만 환경을 잡는다.
- 보관함 호출이 실패하면 트랜잭션이 되돌아가 연결도 생기지 않는다.
- 환경 접속 확인 실패(`connectionStatus = FAILED`)여도 넣을 수 있다. 배포 접수가 따로 409 로 막는다.
- 응답에 선택 칸 `deployed`(목록·`PUT`)를 더한다: 연결에 현재 배포가 있는가. 화면이 "첫 배포 때 적용됩니다"를 보일 때 쓴다. 항목의 `appliedVersion = null`·`pendingRedeploy = true` 는 그대로다.

## 3. 영향 분석 — 배포 없는 연결이 잘못 잡히지 않는가

배포 없는 연결은 **첫 배포에 실패한 연결과 같은 모양**(비율 0, `current_deployment_id` null)이다. 그래서 아래 조회는 이미 그 모양을 걸러 왔고, 이 작업은 그 모양이 생기는 길을 하나 더할 뿐이다.

| 조회 | 걸러내는 조건 | 결과 |
|---|---|---|
| 서비스 상세 `environments[]` | `role_of`: 비율 0 + 현재 배포 없음 → `NONE`, `versionStatus` null | 서비스 중·예비로 안 보임(목록에는 NONE 으로 나옴) |
| 서비스 목록 | 연결을 읽지 않음(상태는 ACTIVE·DELETING 수명만) | 영향 없음 |
| 보안 매트릭스·C-SEC-1 기준 | `search_live_current_by_organization`: `current_deployment_id IS NOT NULL` | 행 `services`·판정·기준 연결에 안 들어감 |
| 보안 게이트 | 계획이 있어야 돈다. 계획 만들기가 `_check_target_ready` 에서 현재 배포 없는 도착을 409 `TARGET_NOT_READY`(`NO_DEPLOYMENT`)로 막음 | 게이트 대상이 되지 않음 |
| 예비 따라가기 | `_succeed`: 비율 0 **이고 현재 배포 있음**만 STANDBY_SYNC | 따라가지 않음 |
| 대표 주소·진입점 가중치 | `_route_main` 은 성공한 연결에만, 이전은 계획의 출발·도착만 | 가중치 대상 아님 |
| 재난 후보·복제 지연 | `_target_candidates`·`lag_targets`: 현재 배포 있음만 | 후보 아님 |
| 이전 출발 | `single_source`: 비율 > 0 | 출발 아님 |
| TLS 주기 시험·온프레 자격 갱신 | `search_live_current*`: 현재 배포 있음만 | 대상 아님 |
| 서비스 삭제 | 살아 있는 연결마다 내리기 실행기 호출(실패한 첫 배포 연결과 같음) | 내리고 변수 행 삭제 표시 |
| 환경 삭제 | 살아 있는 연결이 있으면 409 `ENVIRONMENT_IN_USE` | **변수만 넣은 환경도 서비스를 지워야 환경을 지울 수 있다**(운영 영향, 문서에 적음) |

## 4. 완료 기준 대응

- 배포한 적 없는 새 서비스의 두 환경에 `DATABASE_URL` 저장(201) → 첫 배포가 같은 연결로 성공하고 `config_snapshot.envVars` 에 고정, 배포 작업 로그 `App environment variables applied: 1 (keys: DATABASE_URL).` — 통합 시험.
- 배포 전 연결이 상세 NONE, 매트릭스·예비 따라가기·이전 도착에 안 잡힘 — 통합 시험.
- 기존 연결·배포 기록·되돌리기 동작 그대로 — 기존 변수·배포·되돌리기 시험 전체 통과.
- 마이그레이션 없음.

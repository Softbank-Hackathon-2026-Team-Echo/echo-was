# API 서버 8-3: 이전 계획 종류(mode) SPEC (변경분)

- 상태: Confirmed (11/3 지민 결정 A: 서버가 만들 때 판정해 저장)
- 확인일: 2026-11-03
- Depth: Light
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/8 (In Progress)
- 요청: echo-frontend#6 도영 댓글 https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-frontend/issues/6#issuecomment-6596584839 ("계획 응답에 mode(PLANNED·EMERGENCY·FAILBACK) 칸")
- 바꾸는 SPEC: `specs/api-8-migration-plans.md`, `docs/api/migration-plans.md`. 다르면 이 문서가 우선한다.
- 기준: echo-was `main` = `0c6884f`
- 관련 결정: 복귀 = 출발·도착을 바꾼 새 계획(api-8 SPEC AD-6), 재난 자동 이전 트리거 = API 서버 9 후보(위키 D-048), ADR 11/2 "재난이 끝나지 않은 지역으로는 복귀(Failback) 차단"(이번 범위 밖, 이 칸이 그 규칙의 바탕).

## 1. 목표

이전 계획마다 종류 `mode`(`PLANNED`·`EMERGENCY`·`FAILBACK`)를 서버가 **만들 때 판정해 저장**하고 목록·단건 응답에 내린다. 프론트가 "앞선 성공 이전을 거꾸로 하는 계획"을 추측하지 않게 한다.

## 2. 판정 규칙 (계획 생성 때 한 번, 같은 트랜잭션)

1. 같은 서비스의 `SUCCEEDED` 계획 중 `finished_at` 이 가장 늦은 것 하나를 `last` 라 한다(동률이면 `created_at`, 그다음 `id` 로 결정적으로 고른다).
2. `last` 가 있고, `last.source_service_environment_id == 새 계획.target_service_environment_id` 이고 `last.target_service_environment_id == 새 계획.source_service_environment_id` 이며, `last.mode != FAILBACK` 이면 → `FAILBACK`.
3. 그 밖 → `PLANNED`.
4. `EMERGENCY` 는 값만 정해 둔다. 재난 자동 이전(API 서버 9)이 계획을 만들 때부터 쓴다. 지금 API 로는 만들어지지 않는다.

이유:
- 성공한 이전만 본다. 롤백·실패·취소된 계획은 트래픽을 옮기지 않았다.
- "복귀의 복귀"(aws→gcp 성공, gcp→aws 복귀 성공, 다시 aws→gcp)는 복귀가 아니라 새 이전이라 `PLANNED` 다.
- 서비스당 열린 계획은 하나뿐이라(부분 유일 인덱스) 만든 뒤 실행 전에 `last` 가 바뀌지 않는다. 그래서 응답 때 다시 계산하지 않고 저장값을 쓴다.
- 클라이언트는 값을 정할 수 없다. 생성 요청 모양은 바꾸지 않는다(요청 모델의 기존 미지정 필드 처리 그대로).

## 3. 변경

- `app/enums.py`: `MigrationPlanMode(StrEnum)` = `PLANNED`·`EMERGENCY`·`FAILBACK`. 용어 사전에 추가.
- 모델 `migration_plans.mode`: `String(16) NOT NULL server_default 'PLANNED'`, CHECK `mode IN (...)`(이름 `mode_valid`). 마이그레이션은 **더하기만**(칸 + CHECK, 기존 행은 기본값 `PLANNED`). `alembic` 왕복·`alembic check`·스키마 덤프 파일 갱신.
- 생성 서비스: §2 판정. 판정 쿼리는 계획 생성 트랜잭션 안(서비스 잠금 뒤)에서 한다.
- 응답: `MigrationPlanSummaryResponse`(목록·단건 공통)에 `mode`. 단건 응답이 별도 모델이면 거기에도.
- 로그: 계획 생성 로그(있으면 PLAN_CREATED 류) 메타데이터에 `mode` 를 더한다. 로그 메시지 표가 있으면 표도. 없으면 만들지 않는다.
- 문서: `docs/api/migration-plans.md` 응답 예·필드 표에 `mode` 와 판정 규칙, `EMERGENCY` 는 API 서버 9 부터라는 문장. `specs/api-8-migration-plans.md` 에 이 SPEC 링크 한 줄(AD 표 또는 바뀐 SPEC 목록, 원문 결정은 지우지 않음).

## 4. 범위 밖

복귀 차단 규칙(재난 진행 중 지역으로 FAILBACK 거절), 재난 자동 이전, 목록 `mode` 필터, 프론트.

## 5. Acceptance criteria

- `AC-8-3-1` 첫 계획(성공 이력 없음) → `PLANNED`.
- `AC-8-3-2` aws→gcp `SUCCEEDED` 뒤 gcp→aws 계획 → `FAILBACK`.
- `AC-8-3-3` 2번 복귀가 `SUCCEEDED` 된 뒤 다시 aws→gcp → `PLANNED`.
- `AC-8-3-4` aws→gcp 가 `ROLLED_BACK`·`FAILED`·`CANCELLED`·`REJECTED`·`EXPIRED` 뿐이면 gcp→aws 는 `PLANNED`(성공만 본다).
- `AC-8-3-5` 성공 이력이 여럿이면 가장 늦게 끝난 것만 본다(예: aws→gcp 성공, 그 뒤 aws→onprem 성공 → gcp→aws 는 `PLANNED`, onprem→aws 는 `FAILBACK`).
- `AC-8-3-6` 목록·단건 응답에 `mode`, OpenAPI 에 enum 3값. 생성 요청에 `mode` 를 보내도 저장값은 서버 판정(또는 기존 요청 모델 규칙대로 422 — 실제 동작을 문서에 적는다).
- `AC-8-3-7` 마이그레이션: 기존 행 `PLANNED`, CHECK 가 다른 값을 막음, 왕복·`alembic check`·덤프 일치.
- 품질: ruff·format·mypy app·pytest 전부 통과(건너뜀 0, DB 포함).

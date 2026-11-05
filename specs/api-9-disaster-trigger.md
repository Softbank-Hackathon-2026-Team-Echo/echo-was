# API 서버 9: 재난(지진) 이벤트 → 피해 환경 서비스 긴급 이전 계획 자동 생성 — SPEC (확정)

- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/9 (Project #4)
- 저장소 경로: `specs/api-9-disaster-trigger.md`
- 상태: **확정(Confirmed)** — 지민 11/5 09:30 결정 ①~⑨ 전부 A(추천), AI 기본값 AD-1~14 그대로 채택(거부 없음)
- 기준: echo-was main `9d825e7a22eb94627a94b02927b35cd559f9d56e`(11/5, #69 AI 사전 점검·러너 API 병합 뒤) · echo-frontend main `b2b2c96`. Alembic head `286d9ea982d6`(하나, M1이 `alembic/versions/` 14개의 `down_revision` 사슬로 확인)
- 근거: 이슈 #9 완료 기준 · 프론트 계약 `docs/api-contract.md` 4-3(재난)·4-6(`failback`) · 위키 D-030·D-047·D-048·Q-032 · ADR 11/2(거리 방식, 상단 바 버튼 + 휴대폰 리모컨) · `specs/api-8-3-plan-mode.md`(EMERGENCY 자리) · `specs/api-5-environment-coordinates.md`(좌표, #68)
- 관련: echo-was #30(사전 승인 자동 DR, 형래) — 이 SPEC은 SYSTEM 승인을 만들지 않고 #30에 남긴다 · echo-frontend #17(긴급 복구 화면)
- Depth: Standard (새 표 1·라우터 1·서비스 2·순수 규칙 1, **마이그레이션 1개**)

## 1. 완료 기준

### 1-1. 이슈 원문

재난 이벤트 입력(시뮬레이션 버튼 등) 하나로 피해 환경의 서비스에 대한 이전 계획이 자동 생성되고, 승인 뒤 F7~F9 흐름이 실행된다.

### 1-2. 이번 범위의 읽기

- "재난 이벤트 입력" = 사람이 누르는 **재난 버튼** 한 번 = `POST /orgs/{org}/disasters {type, epicenter, magnitude}` (프론트 다이얼로그·휴대폰 리모컨이 같은 API를 부름). (D-1·D-3)
- "피해 환경" = 진원지에서 규모별 반경 안에 있는 환경(거리 방식, 좌표는 #68). (D-2)
- "이전 계획이 자동 생성" = 피해 환경에서 **서비스 중인** 서비스마다 `mode=EMERGENCY` 계획을 서버가 만든다. 도착은 서버 규칙으로 고른다. (D-6)
- "승인 뒤" = 버튼 누른 사람의 자기 승인(조직 스위치 `allowSelfApproval` ON일 때 같은 요청에서 자동) 또는 다른 개발자 승인. (D-5)
- "F7~F9 흐름" = 지금 이전 실행(사전확인 → 10/50/100% → 데이터 동기화 확인)과 실패 시 롤백 그대로. DB 쓰기 전환(`cutover.sh`)은 지금처럼 사람 단계(AD-13).

### 1-3. 완료 기준 재작성안 (병합 뒤 M1이 이슈 본문 수정 — 승인 항목)

| 지금 문구 | 바꿀 문구 | 메모 |
|---|---|---|
| 재난 이벤트 입력(시뮬레이션 버튼 등) 하나로 피해 환경의 서비스에 대한 이전 계획이 자동 생성되고, 승인 뒤 F7~F9 흐름이 실행된다. | ① 재난 API 한 번(`POST /disasters`, 진원지·규모)으로 서버가 반경·피해/안전 환경을 계산해 저장한다.<br>② 피해 환경에서 서비스 중인 서비스마다 `EMERGENCY` 계획이 만들어지고, 조직이 자기 승인을 허용하면 바로 승인·실행된다(아니면 승인 대기).<br>③ 재난이 끝나지 않은 피해 환경으로의 계획은 409로 막힌다(D-030).<br>④ 주기 헬스 감시·실제 지진 피드·SYSTEM 사전 승인(#30)은 2차. | 11/5 지민 결정(①~⑨ 전부 A) 반영. 재난은 시뮬레이션(`simulated: true`)임을 응답에 밝힌다 |

## 2. 확인된 사실 (기준 main `9d825e7`에서 M1이 줄 번호 재확인, 자세한 인용은 조사 `facts.md`)

- DB는 `mode=EMERGENCY`를 이미 받는다(`app/models/migration_plan.py` 74행 CHECK `mode_valid`). 만드는 코드가 없다 — `decide_plan_mode`(`app/services/migration_plan_service.py` 848~865행)는 PLANNED/FAILBACK만 돌려준다. `mode` 칸은 `sort_order=APPENDED_COLUMN_SORT_ORDER`(186~192행).
- 계획 만들기는 사람 id가 필수(`created_by_user_id=actor_user_id`, 351행)이고 승인자 칸도 사람(users FK) → SYSTEM 승인은 지금 칸으로 못 적는다. 자기 승인은 `allow_self_approval`이 막는다(503행 `approve_plan`, 600행). 운영 echo-demo는 ON(시연까지 유지).
- `create_plan`(226행~)·`approve_plan`(478행~)은 각자 `async with self._session.begin()`으로 트랜잭션을 연다 → 트리거는 열린 트랜잭션 없이 두 함수를 차례로 부르면 된다. 감사 줄은 서비스 안에서 `self._audit.record(...)`로 같은 트랜잭션에 쓴다(`MIGRATION_PLAN_CREATED` 295행, `MIGRATION_PLAN_APPROVED` 537행).
- 만들기는 **도착** 환경 접속 FAILED만 거절한다(`_check_environment_accepts` 873~877행). 실행 사전확인·판정도 도착만 본다(`migration_plan_processor.py` `_preflight`, `migration_counting.py` 8행) → 출발이 피해를 입어도 이전 흐름은 성립한다.
- 보안 게이트는 운영에서 꺼져 있다(`security_gate_enabled` 기본 false). 켜져도 EMERGENCY는 같은 규칙(`security_rules.py` 3~5행, sec-3 D-9).
- 좌표는 #68로 응답 때 계산된다(`app/core/places.py`, `EnvironmentConfig.coordinates()`). 저장 칸은 없다. 표에 없으면 null.
- 주기 헬스 감시·재난 표·알림(SSE·웹훅) 코드는 없다.
- 감사: `WRITE_ROUTE_ACTIONS` **29개**(`tests/integration/test_audit_logs.py` 146행 `== 29`, `tests/test_audit_recorder.py` 130~131행 `== 29`, `docs/api/audit-logs.md` 81행 "29개"). 모든 POST·PUT·PATCH·DELETE(로그아웃 제외, 러너 경로는 `RUNNER_ROUTE_ACTIONS`)는 이름이 있어야 한다. 역할 표 `tests/integration/test_authorization.py` 95행 `ROLE_TABLE`.
- 마이그레이션 시험: `tests/integration/test_migrations.py` 1023행 `HEAD_REVISION = "286d9ea982d6"`, `EXPECTED_CONSTRAINTS`(34행)·`EXPECTED_INDEXES`(737행)·`EXPECTED_TRIGGERS`(968행) 목록, 1864행 사슬 시험, 1914행 `test_alembic_check_reports_no_differences`, 1920·1929행 카탈로그 비교. 스키마 덤프 `scripts/dump_schema.sh` → `db-schema.sql`. DELETE 금지 트리거 선례: `deployments`(`b17994eaa7aa` 리비전).
- 라우터 등록: `app/main.py` 84~88행 `include_router` 끝이 `runner_router`.
- 진입점은 aws·gcp 두 이름만 안다(운영 `/weights`). onprem을 도착으로 고르면 실패한다(추론).
- 프론트는 재난·대피 후보를 브라우저에서 흉내 낸다(`DISASTER_API_READY=false`, 임계값 7.0, 반경 `round(50·e^(0.9(M−5.2)))`, 좌표 모르는 환경 `unknown`).

## 3. 범위

### 3.1 한다 (최소판)

1. 재난 표 `disasters` + 계획 칸 `migration_plans.disaster_id`(마이그레이션 1개).
2. 재난 API 5개(5절): 미리보기(GET)·만들기·목록·단건·종료.
3. 순수 규칙 모듈: 반경·임계값·피해/안전/모름 분류·도착 고르기(진원지에서 가장 먼 안전 예비).
4. 트리거: 만들기 요청 안에서 피해 서비스마다 `EMERGENCY` 계획 만들기 → 자기 승인 허용이면 `approve_plan` 호출 → 서비스별 결과 응답.
5. 복귀 차단: 계획 만들기에서 ACTIVE 재난 피해 환경이 도착이면 409 `DISASTER_ACTIVE`. 계획 요약·상세에 `disasterId`, 상세에 `failback`.
6. 감사 이름 2개, 계획 로그 코드 1개, 오류 코드(5절·AD-6).
7. 문서: `docs/api/disasters.md`(새), `docs/api/migration-plans.md`(EMERGENCY 문장 갱신·`disasterId`·`failback`·409), `docs/api/audit-logs.md`(31개), `docs/schema.md`, 용어 사전(있으면), `CLAUDE.md` 라우터 줄(있으면).

### 3.2 하지 않는다 (2차)

- 주기 헬스 감시로 재난 자동 생성, 헬스 폴링 자동 복귀(D-048)
- 실제 지진 피드, 대피 후보 점수 API(계약 4-4)
- SYSTEM 사전 승인·범위·철회(#30, 형래), 조직·서비스별 자동 이전 스위치
- SSE·Slack 알림, 쿨다운·지연 실행
- DB 쓰기 전환 자동화(`cutover.sh`를 일꾼이 부르기), 프론트 화면(`DISASTER_API_READY` 전환은 프론트 몫)
- 일꾼(`app/worker/`)·실행기 코드 변경, 새 JobKind, 새 환경 변수

## 4. 결정

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | 지진 이벤트를 서버에 저장(`disasters`), 서버가 반경·피해/안전 계산 | 지민 11/5 09:30 ①A |
| D-2 | 지진 M ≥ 7.0이면 이전, 반경 `round(50·e^(0.9·(M−5.2)))`km(M7.2 → 300km), 태풍은 기준 없음(규모 무관 대상, 반경도 같은 식). 미달이면 저장만 `meetsThreshold=false`. 숫자는 서버 코드 상수(환경 변수 아님) | 지민 ②A |
| D-3 | 신호는 사람 버튼만. 헬스 감시·지진 피드 2차 | 지민 ③A |
| D-4 | 켜는 스위치 없음 — 버튼(개발자 이상 사람) = 동의, 조직의 피해 서비스 전부 | 지민 ④A |
| D-5 | 버튼 누른 사람이 계획을 만든 사람. `allowSelfApproval`이면 같은 요청에서 자기 승인(실행 시작), 아니면 승인 대기 | 지민 ⑤A |
| D-6 | 도착 = 안전 ∧ 같은 이미지 예비 ∧ 도착 환경 접속 FAILED 아님 ∧ 진입점 이름 있음(aws·gcp 종류) 중 진원지에서 가장 먼 곳. 없으면 `NO_SAFE_TARGET` | 지민 ⑥A |
| D-7 | 멱등(열린 계획 서비스 건너뜀) + 기존 `cancel`·`abort`·재난 `end`. 쿨다운 없음 | 지민 ⑦A |
| D-8 | 수동 복귀 + 서버 차단 409 `DISASTER_ACTIVE`, 상세 `failback` | 지민 ⑧A, D-030 |
| D-9 | 알림 = 응답 `plans[]` + 폴링 + 계획 로그·감사 | 지민 ⑨A |
| D-10 | EMERGENCY도 보안 게이트 같은 판정, 느슨하게 하지 않음 | sec-3 D-9, sec-1 D-1 |
| D-11 | AI(AGENT)는 재난을 만들거나 끝낼 수 없다(403) | #30 완료 기준, sec-1 D-7 |

### 4-1. AI 기본값 (지민 채택, 거부 없음)

| ID | 내용 |
|---|---|
| AD-1 | 경로: `GET /orgs/{org}/disasters/preview?type&lat&lng&magnitude`(계약은 POST지만 **GET** — 저장 안 하는 조회라 감사 이름이 필요 없고 쓰기 경로 시험을 안 늘림) · `POST /orgs/{org}/disasters` · `GET /orgs/{org}/disasters?status=` · `GET /orgs/{org}/disasters/{id}` · `POST /orgs/{org}/disasters/{id}/end` |
| AD-2 | 검사기: 만들기·종료 `HumanDeveloperAccess`(에이전트 403), 조회·미리보기 `ViewerAccess` |
| AD-3 | 감사 이름 2개 `DISASTER_DECLARED`·`DISASTER_ENDED`(USER), 대상 종류 `disaster` → `WRITE_ROUTE_ACTIONS` 29 → **31**(시험 상수 29를 31로 고친다. `len(WRITE_ROUTE_ACTIONS)`로 바꾸지 않는다). 트리거가 만든 계획의 만들기·승인 감사 줄 `details`에 `disasterId`·`trigger:"DISASTER"` |
| AD-4 | 계획 로그 코드 `DISASTER_TRIGGERED`(data `{disasterId, magnitude, distanceKm}`)를 `PLAN_CREATED` 앞에. 로그 코드는 DB CHECK 없음 → 마이그레이션 불필요 |
| AD-5 | 피해 서비스 = 조직의 살아 있는 서비스 중 **서비스 중 연결(트래픽 > 0)의 환경이 피해 환경**인 것. 출발 환경 접속 상태는 보지 않는다(재난이면 FAILED일 수 있음) |
| AD-6 | 서비스별 처리는 서로 독립(한 서비스 실패가 다른 서비스를 막지 않음). 결과 `plans[]`: `{serviceId, planId|null, outcome: APPROVED·PENDING_APPROVAL·SKIPPED, reason|null, targetEnvironmentId|null}`. reason 코드는 기존 오류 코드 재사용(`MIGRATION_PLAN_OPEN`·`TARGET_NOT_READY`·`DEPLOY_IN_PROGRESS`·`TARGET_SYNCING`·`SECURITY_GATE_BLOCKED` 등, 저장소에 실제로 있는 코드 이름 그대로) + 새 `NO_SAFE_TARGET`·`SELF_APPROVAL_DISABLED`·`BELOW_THRESHOLD`(응답 reason 값; 오류 응답 코드로는 `DISASTER_NOT_FOUND`·`DISASTER_NOT_ACTIVE`·`DISASTER_ACTIVE`) |
| AD-7 | 재난 `simulated`는 늘 true, `source: SIMULATED`(버튼). 계획 `simulated`는 지금처럼 실행기 모드로 정한다(운영 false) — "재난은 시뮬레이션, 이전은 실제"가 구분된다 |
| AD-8 | 좌표 모르는 환경은 `unknownEnvironmentIds`(피해·안전 어느 쪽도 아님, 도착 후보에서 뺀다) |
| AD-9 | 재난 ACTIVE는 조직당 여러 개 허용. 복귀 차단은 "ACTIVE 재난 중 하나라도 그 환경을 피해로 가진 경우" |
| AD-10 | 재난 표는 지우지 않는다(종료만). 피해·안전·모름 환경 id 목록은 **만든 순간 스냅숏** |
| AD-11 | 보안 게이트는 그대로. 게이트에 막히면 계획은 승인 대기로 남고 결과 `outcome=PENDING_APPROVAL, reason=SECURITY_GATE_BLOCKED` |
| AD-12 | 계획 기본값(비율 단계 필요 건수·최대 대기·롤백 조건)은 설정 기본값 그대로. 재난이라고 빨리 넘기지 않는다(시연 약 95초, 리허설과 같음) |
| AD-13 | DB 쓰기 전환(`cutover.sh`)은 범위 밖. 자동 이전은 트래픽까지 |
| AD-14 | 프론트 인계: `DISASTER_API_READY`를 켜면 되는 응답 모양(계약 4-3 + `meetsThreshold`·`source`·`unknownEnvironmentIds`·`plans[]`), 계획 상세 `disasterId`·`failback`. 이슈 댓글 초안은 구현 뒤 M1이 지민님께 먼저 보임(워커는 GitHub에 쓰지 않음) |

## 5. API

공통: 접두어 `/api/v1`, 응답 `ApiResponse[T]` 봉투, camelCase(기존 스키마 관례 그대로).

| 메서드·경로 | 검사기 | 요청 | 응답 | 실패 |
|---|---|---|---|---|
| `GET /orgs/{org_id}/disasters/preview?type=&lat=&lng=&magnitude=` | `ViewerAccess` | 쿼리 | 200 `{radiusKm, meetsThreshold, affected[], safe[], unknown[]}` (저장 안 함, 환경 id 목록) | 422 `VALIDATION_ERROR`(lat −90~90, lng −180~180, magnitude 0~10, type) |
| `POST /orgs/{org_id}/disasters` | `HumanDeveloperAccess` | `{type: EARTHQUAKE\|TYPHOON, epicenter:{lat,lng}, magnitude}` | 201 `DisasterDetail` + `plans[]` | 422 · 403(뷰어·에이전트) |
| `GET /orgs/{org_id}/disasters?status=ACTIVE\|ENDED&page&size` | `ViewerAccess` | — | 200 `Page[DisasterDetail]`(기존 페이지 관례) | — |
| `GET /orgs/{org_id}/disasters/{disaster_id}` | `ViewerAccess` | — | 200 `DisasterDetail` | 404 `DISASTER_NOT_FOUND`(다른 조직도 404) |
| `POST /orgs/{org_id}/disasters/{disaster_id}/end` | `HumanDeveloperAccess` | `{}` | 200 `DisasterDetail`(status `ENDED`) | 404 · 409 `DISASTER_NOT_ACTIVE` |

`DisasterDetail`:

```text
{id, type, source: "SIMULATED", simulated: true, magnitude, epicenter{lat,lng}, radiusKm,
 meetsThreshold, status: ACTIVE|ENDED,
 affectedEnvironmentIds[], safeEnvironmentIds[], unknownEnvironmentIds[],
 createdBy{id,login}, endedBy{id,login}|null, startedAt, endedAt|null,
 plans: [{serviceId, planId|null, outcome: APPROVED|PENDING_APPROVAL|SKIPPED, reason|null,
          targetEnvironmentId|null}]}
```

- `createdBy`·`endedBy` 칸 모양은 기존 계획 응답의 사람 요약 관례를 따른다(위임).
- `plans[]`는 만들기 응답에서는 그 순간 결과, 조회에서는 `migration_plans.disaster_id`로 찾은 계획(outcome = 계획 상태 요약: PENDING_APPROVAL은 그대로, 그 밖의 상태는 `APPROVED`로 묶고 실제 상태는 계획 API로 보게 하거나 상태 값을 더 싣는다 — 위임, 문서에 적는다). SKIPPED 결과는 만들기 응답에만 있다(저장 안 함 — 다시 보려면 감사 줄 `details`).
- 계획 쪽 바뀜:
  - `POST /services/{id}/migration-plans`: 도착 환경이 ACTIVE 재난의 피해 환경이면 **409 `DISASTER_ACTIVE`** `details[{field:"disasterId", value}]`(만들기 거절 순서에서 "같은 환경" 다음).
  - `MigrationPlanSummary`·`Detail`: `disasterId: uuid|null` 추가. `Detail`에 `failback: {allowed, reason: "DISASTER_ACTIVE"|null, disasterId|null}` — 이 계획을 거꾸로 하는 복귀(도착 = 이 계획의 출발 환경)가 지금 막히는지.
  - `mode=EMERGENCY`는 재난 트리거만 만든다. 사람 경로는 지금처럼 PLANNED/FAILBACK.

### 5-1. 트리거 흐름 (`POST /disasters` 한 요청 안)

```text
1 (트랜잭션) 조직 환경 전부 읽기 → 좌표 → 반경·피해/안전/모름 → disasters INSERT(ACTIVE) + 감사 DISASTER_DECLARED
2 meetsThreshold=false 면 plans=[] 로 끝
3 (트랜잭션) 피해 환경에 서비스 중 연결(트래픽>0)이 있는 살아 있는 서비스 목록
4 서비스마다(독립, 실패는 그 서비스만):
   a 도착 고르기(D-6) → 없으면 SKIPPED NO_SAFE_TARGET
   b MigrationPlanService.create_plan(..., mode=EMERGENCY, disaster_id) — 기존 거절(열린 계획·배포 중·도착 미준비)은 SKIPPED + 그 코드
      계획 로그: DISASTER_TRIGGERED → PLAN_CREATED, 감사 MIGRATION_PLAN_CREATED details{disasterId, trigger:"DISASTER"}
   c allowSelfApproval 이면 approve_plan(actor=누른 사람) → APPROVED(SELF_APPROVED, 할 일 넣음)
      게이트 막힘 → PENDING_APPROVAL reason SECURITY_GATE_BLOCKED / 스위치 꺼짐 → PENDING_APPROVAL reason SELF_APPROVAL_DISABLED
5 201 DisasterDetail + plans[]
```

- 실행은 기존 일꾼(MIGRATION_PLAN 할 일)이 그대로 한다. 일꾼 코드 변경 없음.
- 재난 행은 4단계 실패와 상관없이 남는다(1단계 커밋). 서비스 처리 순서는 서비스 id 오름차순.
- 4단계 실패는 그 서비스의 결과만 바꾼다. 실패한 승인 시도가 남기는 거부 감사 줄 등 기존 동작은 그대로 둔다.

## 6. 데이터 — 마이그레이션 1개 (더하기만)

- 리비전 하나, **`down_revision = "286d9ea982d6"`**(지금 head). 구현 전에 `uv run alembic heads`로 다시 읽어 다르면 그 head 하나에 잇는다. 리비전 id는 alembic이 만든 값을 쓴다.
- 더하는 것만: 새 표 `disasters` + `migration_plans.disaster_id`(NULL 허용, 맨 뒤 칸) + 인덱스 + (선택) DELETE 금지 트리거. 기존 칸·CHECK·인덱스를 바꾸지 않는다.
- downgrade 포함: 칸·인덱스·(트리거·함수)·표 DROP. 재난과 이어진 계획이 있어도 downgrade는 칸을 지우므로 정보가 사라진다 — 문서·리비전 주석에 적는다(막지 않아도 된다, 위임).
- `db-schema.sql`을 `scripts/dump_schema.sh`(시험 DB 대상)로 다시 만들어 함께 커밋한다. `docs/schema.md`도 갱신.
- 시험 `tests/integration/test_migrations.py`: `HEAD_REVISION`을 새 리비전으로, 사슬 시험(새 리비전의 down_revision이 `286d9ea982d6`), `EXPECTED_CONSTRAINTS`·`EXPECTED_INDEXES`·`EXPECTED_TRIGGERS`에 새 항목, 새 리비전 downgrade→upgrade 왕복 시험 1개(선례 `test_findings_revision_downgrade_removes_table_and_upgrade_restores`). `test_alembic_check_reports_no_differences`가 통과해야 한다.

새 표 `disasters`:

| 칸 | 타입 | 제약 |
|---|---|---|
| `id` | uuid PK | |
| `organization_id` | uuid FK organizations RESTRICT | NOT NULL, 인덱스 `(organization_id, status, created_at)` |
| `type` | varchar(16) | CHECK `IN ('EARTHQUAKE','TYPHOON')` |
| `source` | varchar(16) | CHECK `IN ('SIMULATED')`, 기본 `SIMULATED` |
| `magnitude` | numeric(3,1) | CHECK 0~10 |
| `epicenter_lat` / `epicenter_lng` | double precision | CHECK 범위 |
| `radius_km` | integer | ≥ 0 |
| `meets_threshold` | boolean | NOT NULL |
| `status` | varchar(16) | CHECK `IN ('ACTIVE','ENDED')`, `(status='ENDED') = (ended_at IS NOT NULL)` |
| `affected_environment_ids` / `safe_environment_ids` / `unknown_environment_ids` | jsonb | NOT NULL, `jsonb_typeof = 'array'`, 만든 순간 스냅숏(AD-10) |
| `created_by_user_id` | uuid FK users | NOT NULL |
| `ended_by_user_id` | uuid FK users | NULL |
| `started_at` / `ended_at` | timestamptz | `started_at` NOT NULL |
| `created_at` / `updated_at` | timestamptz | 믹스인 |

- DELETE 금지(이전 계획처럼 트리거 또는 앱 규칙 — 위임).
- `migration_plans.disaster_id uuid NULL FK disasters RESTRICT`(맨 뒤 칸, `sort_order=APPENDED_COLUMN_SORT_ORDER` 관례 — `mode` 뒤), 인덱스 `(disaster_id)`.
- 새 JobKind 없음(jobs CHECK 그대로), 로그 코드는 DB CHECK 없음, 감사 표 변경 없음.
- 호환: 새 표·NULL 칸만 더하므로 롤링 배포 중 옛 파드는 칸을 모른 채 동작(옛 파드가 만든 계획은 `disaster_id` NULL).

## 7. 완료 판정 (AC)

| AC | 내용 | 확인 |
|---|---|---|
| AC-1 | 미리보기·만들기: near Tokyo(35.2,139.4) M7.2 → `radiusKm=300`, aws(ap-northeast-1) 피해, gcp(asia-northeast3) 안전, 좌표 없는 onprem `unknown`. 프론트 `previewDisaster`와 같은 값 | 단위(규칙) + 통합 |
| AC-2 | 피해 환경에서 서비스 중인 서비스 → `EMERGENCY` 계획(도착 = 가장 먼 안전 예비), `disasterId` 채움, 계획 로그 `DISASTER_TRIGGERED`가 `PLAN_CREATED` 앞 | 통합 |
| AC-3 | `allowSelfApproval=true` → 같은 응답에서 `APPROVED`·`selfApproved=true`(기존 칸)·할 일 1개, 가짜 실행기로 일꾼 처리 반복 → `SUCCEEDED` | 통합 |
| AC-4 | `allowSelfApproval=false` → `PENDING_APPROVAL` + `SELF_APPROVAL_DISABLED`, 다른 개발자 승인 → 실행 시작 | 통합 |
| AC-5 | M6.9 지진 → 재난 저장 `meetsThreshold=false`, 계획 0개. 태풍은 규모 무관 대상 | 통합 |
| AC-6 | 멱등·건너뜀: 열린 계획 있는 서비스 `SKIPPED MIGRATION_PLAN_OPEN`, 안전 예비 없음 `NO_SAFE_TARGET`, 도착 이미지 다름 `TARGET_NOT_READY`. 한 서비스 실패가 다른 서비스 계획을 막지 않음 | 통합 |
| AC-7 | 복귀 차단: ACTIVE 재난 중 gcp→aws 계획 409 `DISASTER_ACTIVE`(details disasterId), 상세 `failback.allowed=false` → `end` 뒤 201 `mode=FAILBACK`, `failback.allowed=true` | 통합 |
| AC-8 | 권한: 뷰어·에이전트 POST 403, 뷰어 GET 200, 다른 조직 404. `ROLE_TABLE`·검사기 자동 수집 시험 통과 | 통합 |
| AC-9 | 감사: `DISASTER_DECLARED`·`DISASTER_ENDED` USER 줄, `WRITE_ROUTE_ACTIONS` 31개, 계획 만들기·승인 줄 `details.disasterId` | 통합(수정) |
| AC-10 | 게이트 켬(주입) + 필수 통제 FAIL → 계획 `PENDING_APPROVAL`, `SECURITY_GATE_BLOCKED` 결과. PLANNED와 판정 같음 | 통합 |
| AC-11 | 마이그레이션 1개(down_revision `286d9ea982d6`), `alembic heads` 하나, `alembic check` 차이 없음, 새 리비전 downgrade/upgrade 왕복, `db-schema.sql` 일치 | `test_migrations` |
| AC-12 | (운영, M1) 시연 조직에서 near Tokyo M7.2 → demo 서비스 EMERGENCY 계획 자기 승인 → 약 95초 뒤 SUCCEEDED, 진입점 `{"aws":0,"gcp":100}`. `cutover.sh`는 사람 단계 | M1 운영 확인(병합·배포 뒤, 이 PR 밖) |
| AC-13 | ruff·format·mypy app·전체 pytest(건너뜀 0) 통과 | CI + M1 재검증 |

## 8. 이 PR 밖에서 M1이 할 일 (구현 뒤)

1. 운영 onprem 환경 `config.location`이 도시 표에 있는지 확인(없으면 `unknown`).
2. 병합·배포 뒤 미리보기 GET으로 피해 환경 계산 확인 → 지민 승인 뒤 AC-12 실제 POST(이전이 시작됨).
3. 프론트(김도영) 인계 댓글 초안: `DISASTER_API_READY` 전환 응답 모양, 미리보기 GET, 복귀 차단 409·`failback`.
4. #30(형래)에 "SYSTEM 사전 승인은 #30 범위로 남김" 메모 초안.

## 9. 규모 어림

| 묶음 | 파일 | 어림 줄 수 |
|---|---|---|
| 새 파일 | `app/models/disaster.py` · `app/repositories/disaster_repository.py` · `app/services/disaster_rules.py`(순수) · `app/services/disaster_service.py`(트리거) · `app/routers/disaster_router.py` · `app/schemas/disaster.py` · `alembic/versions/…_add_disasters.py` · `docs/api/disasters.md` · `specs/api-9-disaster-trigger.md` | ~1,000 |
| 고치는 파일 | `app/services/migration_plan_service.py`(EMERGENCY 경로·복귀 차단) · `app/models/migration_plan.py` · `app/schemas/migration_plan.py` · `app/repositories/migration_plan_repository.py` · `app/enums.py` · `app/core/exceptions.py` · `app/audit/actions.py` · `app/models/__init__.py` · `app/main.py` · `app/dependencies.py` · `db-schema.sql` · `docs/api/migration-plans.md` · `docs/api/audit-logs.md` · `docs/schema.md` · `CLAUDE.md` | ~300 |
| 시험 | 새 `tests/test_disaster_rules.py`(단위 ~10) · `tests/integration/test_disasters_api.py`(~14) · 복귀 차단 시험(~4) + 고침 `test_authorization.py` · `test_audit_logs.py` · `tests/test_audit_recorder.py` · `test_migrations.py` | ~900 |

- 합계 약 28파일, 약 2,200줄, 새 시험 ~28개. M5 구현 3~4시간.
- `tests/conftest.py`·`pyproject.toml`·`.github/workflows/`는 다른 PR(시험 시간 최적화)이 동시에 고치고 있다 → 꼭 필요할 때만 최소로 만진다(새 픽스처는 새 시험 파일이나 `tests/integration/` 안 helper에 둔다).

## 10. 위임한 세부사항 (워커가 정하고 보고)

- 클래스·함수 이름, 파일 나눔, `plans[]` 요약 계산 위치, 조회 `plans[]` outcome 요약 방식
- 반경 반올림·거리 계산(하버사인, 지구 반지름 6371km — 프론트 `geo.ts` `distanceKm`와 같은 식), 경계(거리 == 반경)는 피해로 본다
- `disasters` DELETE 금지 방식(트리거 vs 앱 규칙)
- 트리거의 서비스별 트랜잭션 경계(서비스마다 `create_plan`·`approve_plan`을 그대로 부르는 방식 권장), `create_plan`에 mode·disaster_id를 넘기는 방법(내부 인자 추가 권장, 사람 API 요청 스키마에는 넣지 않는다)
- 진입점 이름 확인 방법(D-6 ④: 환경 종류 aws·gcp 또는 진입점 매핑 설정에 있는 환경)

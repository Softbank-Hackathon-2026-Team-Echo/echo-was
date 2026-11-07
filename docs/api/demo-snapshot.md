# 시연 조직 스냅샷 · 복귀 API

시연 연습을 몇 번 돌려도 같은 시연 환경으로 돌아오게 한다. 조직 ADMIN 이 깨끗한 상태에서 스냅샷을 한 번 따 두고, 시연 뒤 `restore` 를 부르면 정해진 순서로 **기존 서비스 함수만** 불러 되돌린다. 프론트 버튼은 없다(API 만). 코드: `app/routers/demo_snapshot_router.py`, `app/services/demo_snapshot_service.py`, `app/repositories/demo_snapshot_repository.py`.

- 경로는 `/api/v1/orgs/{orgId}/demo` 아래, 공통 봉투 `ApiResponse`, camelCase.
- 새 표·마이그레이션 없음. 스냅샷은 감사 줄 하나(`action = DEMO_SNAPSHOT`, 대상 = 조직)의 `details.snapshot` 이다. 감사 줄은 고칠 수도 지울 수도 없어 스냅샷이 바뀌지 않고, 옛 스냅샷도 이력으로 남는다. 가장 늦은 줄(`seq` 최대)이 지금 스냅샷이다. 비밀값은 담지 않는다(id·이름·비율·커밋·digest·상태만).

## 엔드포인트

| 메서드 · 경로 | 성공 | 대표 실패 | 최소 역할 |
|---|---|---|---|
| `POST /demo/snapshot` | 201 `DemoSnapshot` | 409 `DEMO_STATE_NOT_CLEAN`(details 에 이유), 403 | 어드민(사람) |
| `GET /demo/snapshot` | 200 `DemoSnapshot`(가장 최근) | 404 `DEMO_SNAPSHOT_NOT_FOUND`, 403(에이전트 포함) | 어드민 |
| `POST /demo/restore` 본문 `{"dryRun": false}`(생략 가능) | 200 `DemoRestore` | 404 `DEMO_SNAPSHOT_NOT_FOUND`, 403 | 어드민(사람) |

## 스냅샷 조건 (깨끗한 상태에서만)

아래 중 하나라도 걸리면 409 `DEMO_STATE_NOT_CLEAN` 이고 아무것도 쓰지 않는다. `details[] = {field, reason, value(대상 id)}`.

| reason | 뜻 |
|---|---|
| `OPEN_MIGRATION_PLAN` | 열린 이전 계획(승인 대기·승인됨·실행 중·롤백 중) |
| `ACTIVE_DISASTER` | 진행 중 재난 |
| `OPEN_DEPLOY_REQUEST` | 끝나지 않은 배포 요청 |
| `SERVICE_NOT_SINGLE_SERVING` | 배포된 서비스인데 비율 100 연결이 정확히 하나가 아님 |
| `DEPLOYMENT_SOURCE_MISSING` | 현재 배포를 만든 성공 배포 요청이 없음(되돌리기 대상 없음) |
| `SERVICE_DELETING` | 지우는 중인 서비스 |

기록 내용: 서비스별 연결(환경·비율·현재 배포·그 배포를 만든 배포 요청·커밋·이미지 digest)과 서비스 중 환경, 환경 목록(이름·종류·접속 상태), 보안 예외(id·통제·범위·승인/철회·만료), 조직 `allowSelfApproval`.

## 복귀 단계 (순서 고정)

| # | `step` | 하는 일 | 부르는 기존 함수 |
|---|---|---|---|
| 1 | `MIGRATION_PLANS` | 승인 대기 → 취소, 승인됨·실행 중 → 중단(일꾼이 롤백). 롤백·중단 중이면 `WAITING`. 스냅샷 서비스 중 환경으로 돌아가는 계획은 남긴다(`KEEP_FAILBACK_PLAN`) | `cancel_plan` · `abort_plan` |
| 2 | `DISASTERS` | 진행 중 재난 종료(복귀 잠금 해제) | `end_disaster` |
| 3 | `AI_RUNS` | 열린 사전 점검·배포 실패 진단 취소, 스냅샷 뒤 끝난 실행의 결정 안 된 수정안 거절. 반영 실행(APPLY_PATCH)은 취소 함수가 없어 `WAITING` | `cancel_precheck` · `reject_patch` |
| 4 | `DEPLOY_REQUESTS` | 열린 배포 요청은 취소 API 가 없어 끝나길 `WAITING` | — |
| 5 | `TRAFFIC` | 서비스 중 환경이 100 이 아니면 그 환경으로 이전 계획 만들기 + 승인(일꾼이 진입점 실행기로 비율 적용). 자기 승인이 꺼져 있으면 `MANUAL_REQUIRED`(`APPROVAL_REQUIRED`, 다른 개발자 이상이 승인) | `create_plan` · `approve_plan` |
| 6 | `DEPLOYMENTS` | 연결의 현재 이미지가 스냅샷과 다르면 스냅샷 배포 요청으로 되돌리기(빌드 재사용). 서비스 중을 먼저, 예비는 그 뒤 | `create_rollback` |
| 7 | `DB_WRITER` | **하지 않음**(컷오버 Job 영역). 스냅샷 뒤 DB 쓰기 전환 단계가 든 계획이 있으면 `MANUAL_REQUIRED`(`DB_CUTOVER_SINCE_SNAPSHOT`, 대상 = 계획) | — |
| 8 | `DEMO_APP_DATA` | 데모 앱 순번 쓰기 표 비우기: 앱에 reset 엔드포인트가 없고 WAS 는 앱 DB 에 붙지 않아 항상 `MANUAL_REQUIRED`(`APP_RESET_ENDPOINT_MISSING`) | — |
| 9 | `AI_FIX_PULL_REQUESTS` | 스냅샷 뒤 AI 반영이 연 수정 PR 은 WAS 가 닫지 않음 → 있으면 주소와 함께 `MANUAL_REQUIRED` | — |
| 10 | `KEPT_SETTINGS` | 보안 예외·환경·조직 설정은 되돌리지 않음. 달라진 항목만 `KEPT` + 코드(`ENVIRONMENT_ADDED` 등), 같으면 `UNCHANGED` | — |
| 11 | `AUDIT_LOGS` | 감사 로그는 지우지 않음(`AUDIT_LOGS_NOT_DELETABLE`) | — |

각 함수의 검사(역할·자기 승인 스위치·보안 게이트·지문·잠금)를 그대로 탄다. 행위자는 `restore` 를 부른 어드민이다. 진입점 실행기를 직접 부르지 않는다(DB 비율 기록과 어긋나므로 이전 계획 경로로만).

상태 값: `OK`(이미 같음) · `DONE`(이번에 조치) · `STARTED`(조치 시작, 일꾼이 끝냄) · `WAITING`(앞 단계·진행 중 작업 대기) · `MANUAL_REQUIRED` · `FAILED`(기존 함수가 거절, `code` = 그 오류 코드) · `KEPT` · `WOULD_DO`(dryRun). 단계 상태 = 항목 중 가장 급한 것(`FAILED` > `MANUAL_REQUIRED` > `WAITING` > `STARTED` > `WOULD_DO` > `DONE` > `KEPT` > `OK`).

## 응답 `DemoRestore`

`{snapshotId, snapshotTakenAt, dryRun, restored, callAgain, manualRequired: [step], steps: [{step, status, items: [{action, status, targetType, targetId, label, code, detail}]}]}`

- `restored` = 1~6 단계가 모두 `OK`·`DONE`. `callAgain` = `STARTED`·`WAITING` 이 하나라도 있음.
- **여러 번 불러도 안전하다(멱등).** 한 번에 끝나지 않는 순서(중단 롤백 → 복귀 이전 → 되돌리기 배포)는 일꾼이 끝낸 뒤 다시 부르면 이어 간다.
- `dryRun: true` 는 아무 함수도 부르지 않고 할 일을 `WOULD_DO` 로 돌려준다.

## 감사

| action | 언제 | 대상 | details |
|---|---|---|---|
| `DEMO_SNAPSHOT` | 스냅샷 저장(= 저장 그 자체) | `organization` | `{snapshot}` |
| `DEMO_RESTORE` | dryRun 이 아니고 하나라도 조치(`DONE`·`STARTED`)했을 때만 | `organization` | `{snapshotId, restored, callAgain, steps: {step: status}, changedItems}` |

각 기존 함수는 원래 자기 줄(취소·중단·재난 종료·계획 만들기·승인·되돌리기 요청)을 그대로 쓴다. 아무것도 바뀌지 않은 호출과 dryRun 은 `DEMO_RESTORE` 줄을 쓰지 않는다.

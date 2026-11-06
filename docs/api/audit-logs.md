# 감사 로그 API

조직 안에서 일어난 **모든 변경 행동**과 **AI 에이전트의 행동**을 누가·언제·무엇을·결과로 `audit_logs` 에 남기고 조직별로 읽는다(F5, NFR-08). 쓰는 API 는 없다. 줄은 지우지도 고치지도 않는다(`DELETE`·`UPDATE` 금지 트리거).

## 조회

`GET /api/v1/orgs/{orgId}/audit-logs` · 뷰어 이상 사람, 그 조직의 에이전트 토큰. 남의 조직 404, 비로그인 401.

정렬은 최신순(`created_at DESC, seq DESC`)이다. 같은 시각이면 나중에 쓴 줄이 위다.

| 질의 | 값 | 뜻 |
|---|---|---|
| `actorType` | `USER`·`AGENT`·`SYSTEM` | 행위자 유형 |
| `result` | `SUCCEEDED`·`FAILED`·`DENIED` | 결과 |
| `action` | 문자열 하나(1~300자) | 행동 이름 정확히 일치 |
| `targetType` | 문자열(1~32자) | 대상 종류 |
| `targetId` | uuid | 대상 id. 접수 줄과 끝 줄을 잇는다 |
| `from` · `to` | 타임존이 있는 ISO 시각 | `from ≤ 시각 < to`. `from ≥ to` 는 422 |
| `q` | 1~100자 | `action`·대상 이름·사람 GitHub 아이디·토큰 이름에 대소문자 무시 부분 일치. `%`·`_` 는 글자 그대로 찾는다 |
| `includeReads` | bool, 기본 `true` | `false` 면 에이전트 읽기 줄(`RESOURCE_READ`)을 뺀다. 대시보드 최근 활동은 `includeReads=false&size=3` |
| `page` · `size` | 0 이상 · 1~100, 기본 0·20 | 페이지 |

잘못된 값은 422 `VALIDATION_ERROR` 다. 새 오류 코드는 없다.

### 응답

`ApiResponse[Page[AuditLogResponse]]`.

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "0b9c8f4e-3a55-4f0e-9a43-3e6f4a5d1c11",
        "at": "2026-11-04T12:00:05Z",
        "actor": {
          "type": "SYSTEM",
          "userId": null,
          "githubLogin": null,
          "agentTokenId": null,
          "agentTokenName": null,
          "component": "migration-worker"
        },
        "action": "MIGRATION_FINISHED",
        "route": null,
        "path": null,
        "target": {
          "type": "migration_plan",
          "id": "7a1d2c3e-1111-4c6a-8f00-5b1f7d9a2e44",
          "label": "shop-api · aws-seoul → gcp-seoul"
        },
        "result": "FAILED",
        "reason": null,
        "details": { "status": "ROLLED_BACK", "errorCode": "THRESHOLD_EXCEEDED", "simulated": true },
        "requestId": null
      }
    ],
    "total": 1,
    "page": 0,
    "size": 20
  }
}
```

| 칸 | 뜻 |
|---|---|
| `at` | 기록 시각(주입한 시계, UTC) |
| `actor` | `type` 과 그에 맞는 칸. 사람은 `userId`·`githubLogin`, 토큰은 `agentTokenId`·`agentTokenName`(폐기된 토큰도 이름을 보여 준다), 서버는 `component` |
| `action` | 행동 이름. API 서버 6 때 쌓인 거부 줄은 `POST /api/v1/orgs/{org_id}/services` 같은 문자열 그대로라 Enum 이 아니라 문자열로 읽는다 |
| `route` · `path` | `METHOD 경로 틀` · 실제 경로. 일꾼이 쓴 줄과 옛 거부 줄의 `route` 는 `null` |
| `target` | 없으면 `null`. `label` 은 기록할 때 복사한 이름이라 대상이 바뀌거나 지워져도 그대로다 |
| `reason` | 거부 이유 코드. 거부 줄에만 있다 |
| `details` | 행동별 값(아래 표), 기본 `{}`. 비밀값·토큰 원문은 넣지 않는다 |
| `requestId` | 그 줄을 쓴 요청의 `X-Request-ID`. 일꾼 줄은 `null` |

## 무엇을 기록하나

| 경우 | 행위자 | 트랜잭션 | 결과 |
|---|---|---|---|
| 조직 안 쓰기 33개 경로(기한부 예외 3개·AI 사전 점검 4개·재난 2개 포함) + 초대 적용 | `USER` | 그 작업과 같은 트랜잭션 | `SUCCEEDED` |
| 배포 요청·이전 계획·서비스 삭제의 끝 | `SYSTEM` | 끝 상태를 저장하는 같은 트랜잭션 | 끝 상태가 `SUCCEEDED` 면 `SUCCEEDED`, 그 밖은 `FAILED` |
| AI 실행 가져가기·결과 제출·시간 초과 | `SYSTEM`(`ai-runner`) | 그 상태 변경과 같은 트랜잭션 | 가져가기 `SUCCEEDED`, 결과는 실행 상태대로, 시간 초과 `FAILED` |
| 403 거부 | `USER`·`AGENT` | 요청과 따로 커밋 | `DENIED` |
| 에이전트 토큰의 성공한 GET·HEAD(2xx) | `AGENT` | 응답 뒤 따로 커밋(실패해도 응답은 그대로) | `SUCCEEDED` |

오래 걸리는 일은 **두 줄**이다. 누른 순간 USER 줄, 끝난 순간 SYSTEM 줄이고 같은 대상 id 로 이어진다(`targetId` 로 거른다). 줄은 고치지 않는다.

### 행동 이름

쓰기 경로 ↔ 이름 표는 `app/audit/actions.py` 의 `WRITE_ROUTE_ACTIONS` 다. 러너 경로는 별도 표 `RUNNER_ROUTE_ACTIONS` 다(heartbeat·events 는 값이 `None`, 줄을 쓰지 않는다). 앱의 모든 POST·PUT·PATCH·DELETE 중 `POST /api/v1/auth/logout` 만 빼고 전부 두 표 중 하나에 있어야 한다(`test_every_write_route_has_an_action_name`). **새 쓰기 경로를 더하면 이름도 더한다.**

| 이름 | 경로(`/api/v1/orgs/{orgId}` 아래) | 대상 | `details` |
|---|---|---|---|
| `ORGANIZATION_CREATED` | `POST /api/v1/orgs` | `organization` | `{}` |
| `ORGANIZATION_SETTINGS_CHANGED` | `PATCH /settings` | `organization` | `{allowSelfApproval: {from, to}}` |
| `MEMBER_ROLE_CHANGED` | `PATCH /members/{userId}` | `user` | `{role: {from, to}}` |
| `MEMBER_REMOVED` | `DELETE /members/{userId}` | `user` | `{role}` |
| `MEMBER_JOINED` | 경로 없음(`/me`·로그인 콜백의 초대 적용) | `user` | `{role, invitationId}` |
| `INVITATION_CREATED` | `POST /invitations` | `invitation` | `{githubLogin, role}` |
| `INVITATION_REVOKED` | `DELETE /invitations/{id}` | `invitation` | `{}` |
| `AGENT_TOKEN_ISSUED` | `POST /agent-tokens` | `agent_token` | `{name}` |
| `AGENT_TOKEN_REVOKED` | `DELETE /agent-tokens/{id}` | `agent_token` | `{}` |
| `ENVIRONMENT_CREATED` | `POST /environments` | `environment` | `{kind}` |
| `ENVIRONMENT_UPDATED` | `PATCH /environments/{id}` | `environment` | `{changedFields: [...]}`(값 없음) |
| `ENVIRONMENT_DELETED` | `DELETE /environments/{id}` | `environment` | `{}` |
| `ENVIRONMENT_CONNECTION_CHECKED` | `POST /environments/{id}/connection-check` | `environment` | `{connectionStatus, discarded?}` |
| `SERVICE_CREATED` | `POST /services` | `service` | `{}` |
| `SERVICE_UPDATED` | `PATCH /services/{id}` | `service` | `{changedFields: [...]}`(값 없음) |
| `SERVICE_DELETE_REQUESTED` | `DELETE /services/{id}` | `service` | `{retry?}`. 다시 내리기가 실제로 시작된 재요청이면 `retry: true` |
| `SERVICE_VARIABLE_SET` | `PUT /services/{id}/environments/{environmentId}/variables/{key}` | `service` | `{environmentId, key}` |
| `SERVICE_VARIABLE_DELETED` | `DELETE /services/{id}/environments/{environmentId}/variables/{key}` | `service` | `{environmentId, key}` |
| `SERVICE_VARIABLE_COPIED` | `POST /services/{id}/variables:copy` | `service`(이름표 = `서비스 · 대상 환경 · 키`) | `{environmentId, key, sourceEnvironmentId}`. 복사한 대상·키마다 한 줄(값·비밀 참조·버전 없음, 실패한 항목은 줄 없음, 이슈 #99) |
| `DEPLOY_REQUESTED` | `POST /services/{id}/deploy-requests` | `deploy_request` | `{environmentId, ref, commitSha, standbyEnvironmentIds?}`(예비 환경을 받았을 때만, 이슈 #99) |
| `ROLLBACK_REQUESTED` | `POST /deploy-requests/{id}/rollback` | `deploy_request` | `{rollbackOfId, environmentId}` |
| `MIGRATION_PLAN_CREATED` | `POST /services/{id}/migration-plans`, 재난 트리거 | `migration_plan` | `{sourceEnvironmentId, targetEnvironmentId}`. 재난이 만든 계획은 `disasterId`·`trigger: "DISASTER"` 를 더한다 |
| `MIGRATION_PLAN_APPROVED` | `POST /migration-plans/{id}/approve`, 재난 트리거의 자기 승인 | `migration_plan` | `{selfApproved}`. 재난이 만든 계획은 `disasterId`·`trigger: "DISASTER"` 를 더한다 |
| `MIGRATION_PLAN_REJECTED` | `POST /migration-plans/{id}/reject` | `migration_plan` | `{}` |
| `MIGRATION_PLAN_CANCELLED` | `POST /migration-plans/{id}/cancel` | `migration_plan` | `{}` |
| `MIGRATION_ABORT_REQUESTED` | `POST /migration-plans/{id}/abort` | `migration_plan` | `{}` |
| `SECURITY_EXCEPTION_REQUESTED` | `POST /security/exceptions` | `security_exception`(이름표 = 통제 ID) | `{expiresAt}`(사유 원문은 예외 행에만) |
| `SECURITY_EXCEPTION_APPROVED` | `POST /security/exceptions/{id}/approve` | `security_exception` | `{}` |
| `SECURITY_EXCEPTION_REVOKED` | `POST /security/exceptions/{id}/revoke` | `security_exception` | `{}` |
| `PRECHECK_REQUESTED` | `POST /prechecks` | `agent_run`(이름표 = 서비스 이름) | `{serviceId, targetEnvironmentId}` |
| `PRECHECK_CANCELLED` | `POST /prechecks/{id}/cancel` | `agent_run` | `{serviceId, targetEnvironmentId}` |
| `AGENT_PATCH_APPROVED` | `POST /prechecks/{id}/patches/{patchId}/approve`(같은 승인을 다시 보낸 200 은 기록 안 함) | `agent_run`(부모 실행) | `{patchId, patchHash, applyRunId}`(`applyRunId` 는 null 가능, 사유 원문은 결정 행에만) |
| `AGENT_PATCH_REJECTED` | `POST /prechecks/{id}/patches/{patchId}/reject`(같은 거절을 다시 보낸 200 은 기록 안 함) | `agent_run`(부모 실행) | `{patchId, patchHash}` |
| `DISASTER_DECLARED` | `POST /disasters` | `disaster`(이름표 `EARTHQUAKE M7.2`) | `{type, magnitude, radiusKm, meetsThreshold, affectedEnvironmentIds}`. 건너뛴 서비스가 있으면 같은 대상에 둘째 줄 `{skippedPlans: [{serviceId, planId, reason}]}`([disasters.md](disasters.md#감사)) |
| `DISASTER_ENDED` | `POST /disasters/{id}/end` | `disaster` | `{}` |
| `AGENT_RUN_CLAIMED` | `POST /api/v1/runner/runs:claim` 이 실행을 줌(SYSTEM `ai-runner`) | `agent_run` | `{runnerName, kind}` |
| `AGENT_RUN_FINISHED` | `POST /api/v1/runner/runs/{id}/result`(SYSTEM `ai-runner`) | `agent_run` | `{kind, status, failureCode?, simulated, aiReviewed, processing?}`(`processing` 은 레지던시 기록이 있을 때만). 반영 실행(`APPLY_PATCH`)이면 `aiReviewed: false` + `appliedCommitSha`(실패면 null) |
| `AGENT_RUN_TIMED_OUT` | 임대·마감이 지난 실행을 부를 때 만료(SYSTEM `ai-runner`) | `agent_run` | `{failureCode}` |
| `DEPLOY_FINISHED` · `ROLLBACK_FINISHED` | 끝 상태(SYSTEM). `trigger = ROLLBACK` 이면 `ROLLBACK_FINISHED`, 예비 따라가기는 `DEPLOY_FINISHED` + `details.trigger` | `deploy_request` | `{status, trigger, cancelReason?, errorCode?, simulated?}` |
| `MIGRATION_FINISHED` | 끝 상태(SYSTEM) | `migration_plan` | `{status, errorCode?, simulated?}` |
| `SERVICE_DELETED` | 삭제 완료(SYSTEM) | `service` | `{}` |
| `RESOURCE_READ` | 에이전트의 성공한 읽기(AGENT) | 없음 | `{}` |

거부 줄의 `action` 은 위 이름이고(`route` 에 `METHOD 경로 틀`), 표에 없는 경로(사람만 읽는 GET 등)는 `METHOD 경로 틀` 문자열이다.

대상 이름: 배포 요청은 `서비스 · 환경 · 커밋 7자`, 이전 계획은 `서비스 · 출발 → 도착`, 앱 환경 변수는 `서비스 · 환경 · 키 이름`, 재난은 `종류 M규모`, 그 밖은 그 행의 이름이다.

**앱 환경 변수 줄(API 서버 10)에는 누가 어떤 키를 바꿨는지만 적는다.** 값·비밀 참조(`secretRef`)·버전 번호는 어느 칸에도 넣지 않는다. 값을 다시 읽지 않으므로(쓰기 전용) 같은 값의 재저장도 새 버전이 생긴 변경이라 한 줄을 쓴다. 이 두 경로와 `details.retry` 는 SPEC(`specs/api-7-audit-logs.md`) 확정 뒤 더했다.

### `component` 값

| 값 | 뜻 |
|---|---|
| `deploy-worker` | 배포 일꾼: 성공·실패, 예비 따라가기가 대기 요청을 밀어냄, 시작 전 취소(`SERVICE_DELETED`·`BASE_CHANGED`) |
| `migration-worker` | 이전 일꾼의 모든 끝(`SUCCEEDED`·`ROLLED_BACK`·`FAILED`·`STALE`·`CANCELLED`) |
| `teardown-worker` | 서비스 삭제 완료, 삭제 일꾼의 배포 요청 취소 |
| `api` | 사람 요청을 처리하다 서버 규칙으로 **자동으로** 끝낸 것: 대기 요청 밀어내기(`SUPERSEDED`), 서비스 삭제의 배포 요청 취소, 승인·거절·취소 시도 때 만료(`EXPIRED`), 승인 때 낡음(`STALE`), 새 계획을 만들 때 옛 대기 계획 만료, 중단의 할 일 복구(`CANCELLED`). 누른 사람의 행동이 아니므로 USER 로 쓰지 않는다. 그래서 승인 시도가 `EXPIRED` 로 끝나면 USER 승인 줄은 없고 SYSTEM `api` 줄만 있다 |
| `ai-runner` | AI 실행 가져가기·결과 제출·시간 초과. 러너 신원은 이 행동만 한다(하트비트·이벤트는 줄이 없다). 사전 점검 요청 안에서 만료한 것도 이 값이다 |

## 기록하지 않는 것

- **아무것도 바뀌지 않은 성공**: 이미 내리는 중인 서비스의 다시 삭제(다시 내리기가 실제로 시작되면 `retry: true` 로 기록한다), 두 번째 중단, 이미 폐기된 토큰 폐기, 값이 그대로인 수정.
- **서비스 내리기 실패**(`delete_error`): 끝 상태가 아니라 줄이 없다. 끝 줄 `SERVICE_DELETED` 는 삭제 완료 때만 생기고, 다시 삭제 요청 때 `SERVICE_DELETE_REQUESTED`(`retry: true`)로 기록한다.
- 409 등으로 끝난 쓰기(그 트랜잭션이 되돌려지거나 그 분기에 줄이 없다).
- 사람의 읽기, 401·404, 로그인·로그아웃, 첫 로그인의 개인 조직 자동 생성, 데모 시드(`app/scripts/demo_seed`).
- 환경 등록·수정에 뒤따르는 접속 확인(`connection-check` 경로만 기록한다. 그 결과가 낡아 버려져도 `discarded: true` 로 남긴다).
- 조직이 없는 거부 줄(`POST /orgs` 의 Origin 거부 등)은 저장되지만 어떤 조직 조회에도 나오지 않는다.

## 쓰는 쪽 규칙

- 모든 줄은 `AuditRecorder.record`(`app/audit/recorder.py`)로 쓴다. `session.add` 만 하므로 **행 상태를 바꾸고 커밋되는 블록의 성공 분기**에서 부른다. 작업이 되돌려지면 줄도 사라지고, 줄을 못 쓰면 작업도 저장되지 않는다.
- 값은 명시적으로 넘긴다(조직 id·행위자·행동·대상·`details`). 요청 값(`route`·`path`·`request_id`)은 미들웨어가 묶어 둔 contextvar 에서 읽고, 요청 밖이면 NULL 이다.
- 끝 상태는 `app/audit/lifecycle.py` 의 `record_deploy_request_finished`·`record_migration_plan_finished` 로 쓴다. 조건부 UPDATE 가 반영됐을 때만 부른다(그래서 대상마다 끝 줄은 하나다).
- 문자열은 칸 길이로 자른다. 긴 이름 때문에 작업이 실패하지 않는다.

## 호환성·되돌리기

- **롤링 배포 중**: 옛 파드는 거부 줄만 옛 칸으로 INSERT 한다. 새 CHECK·NULL 허용·`details`·`seq` 기본값을 모두 통과한다. 롤아웃 수 분 동안 옛 파드가 처리한 작업은 성공 줄이 없고, 진행 중이던 배포·이전은 SYSTEM 끝 줄만 있을 수 있다(감수).
- **forward-only**: 운영에서 되돌릴 때는 코드만 되돌리고 revision 파일은 남기며 downgrade 하지 않는다. 옛 코드는 넓어진 스키마에서 거부 줄만 쓰므로 그대로 동작한다. 행동 줄이 쌓인 DB 의 downgrade 는 분명한 메시지로 실패한다(줄을 고치거나 지울 수 없다).
- 보관 기간·내보내기·외부 수집기·실시간 알림은 없다(후속).

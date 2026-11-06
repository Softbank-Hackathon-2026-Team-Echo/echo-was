# SPEC — 반영 실행(APPLY_PATCH) 상세·이벤트를 사람 쪽 API에서 조회한다 (echo-was, API 서버 10-3)

- 상태: **확정(Confirmed)** · 지민 결정 11/6 17:1x "R5a, was는 바로 진행". 이슈 [#88](https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/88)
- 기준: echo-was main `178b434`(#87 병합 뒤). 표·칸은 그대로 → **마이그레이션 0**, `alembic heads` 하나 그대로
- 앞 작업: [api-10-2-apply-patch-run.md](api-10-2-apply-patch-run.md) "범위 밖"의 "반영 실행 이벤트 화면"에 필요한 서버 경로
- 근거: echo-ai-agent `specs/ai-1-appendix-a-was-runner-contract.md` v1 — **바꾸지 않는다**(러너 경로·이벤트·결과 모양 그대로). 이벤트 단계 값은 지금 7개(`SANDBOX_CREATE`…`SANDBOX_DELETE`) 그대로이고, 러너(`apply_runner.py`)는 반영 단계를 `INPUT_LOAD`·`SANDBOX_CREATE`·`PATCH_VERIFY`·`SANDBOX_DELETE` + 고정 문장(`patch applied`·`image built`·`image scanned`·`healthz passed`·`image build failed` …)으로 낸다
- 범위 밖: 반영 실행 취소 API, 반영 실행 목록, 새 이벤트 단계(`PATCH_APPLY` 등 — 계약 보완 api-10-2 7절 4번), 화면(echo-frontend 후속), 운영 DB의 11/6 실행을 운영 API로 다시 읽어 보는 것(병합·배포 뒤 확인)

## 1. 목표

11/6 운영 E2E의 반영 실행(`72f466c7-…` APPLIED, `56c36cb5-…` IMAGE_BUILD_FAILED)처럼, 사람이 **운영 DB를 직접 읽지 않고** 반영 실행의 상태·실패 원인·브랜치·커밋·시각과 러너 이벤트(빌드 시간, trivy 수정 가능 HIGH/CRITICAL 수, healthz 시도 수, 빌드 오류 tail)를 API로 본다.

## 2. 경로 — 새 경로 없이 기존 조회 2개가 반영 실행도 받는다 (결정 D-1)

| 경로 | 지금 | 바뀐 뒤 |
|---|---|---|
| `GET /api/v1/orgs/{orgId}/prechecks/{id}` | `PRECHECK` 만, 반영 실행 id 는 404 `PRECHECK_NOT_FOUND` | `PRECHECK`·`APPLY_PATCH`. 응답 `kind` 로 구분 |
| `GET /api/v1/orgs/{orgId}/prechecks/{id}/logs` | 같음 | 같음. 응답 모양 그대로 |
| `POST …/{id}/cancel`, `POST …/{id}/patches/{patchId}/approve`·`reject` | `PRECHECK` 만 | **그대로**(반영 실행 id 는 404 `PRECHECK_NOT_FOUND`) |

- 이유: 화면은 부모 상세의 `patches[].apply.runId` 를 이미 갖고 있다 → 같은 상세·이벤트 경로에 그 id 를 넣으면 된다. `…/patches/{patchId}/apply` 신설은 경로·검사기·역할 표를 하나 더 늘리고 이벤트 페이지 경로도 따로 필요하다.
- 권한: 지금 조회와 같다 — 뷰어 이상(에이전트 토큰 읽기 가능, 에이전트 읽기 감사 줄 그대로). 다른 조직의 실행·`CHANGE_PROPOSAL`·없는 id 는 404 `PRECHECK_NOT_FOUND`(조직 멤버가 아니면 검사기의 404 `ORGANIZATION_NOT_FOUND`).
- `GET` 은 쓰지 않는다(만료 전환 없음) — 사전 점검과 같다.

## 3. `patches[].apply.runId` = applyRunId (결정 D-2)

이슈의 "`patches[].apply`에 `applyRunId`를 둔다"는 **이미 있는 `apply.runId`** 로 충족한다(API 서버 10 때부터 반영 실행 id). 같은 값을 다른 이름으로 한 칸 더 두지 않는다. 문서에 "`apply.runId` 를 상세·이벤트 경로에 넣으면 반영 실행을 연다"를 적는다.

## 4. 응답 — `PrecheckDetail` 에 두 칸을 더한다(더하기만)

```text
PrecheckDetail {
  id, kind,                          # 새: "PRECHECK" | "APPLY_PATCH"
  serviceId, sourceEnvironmentId, targetEnvironmentId,
  status, failureCode, simulated, aiReviewed, requestedByUserId,
  createdAt, claimedAt, startedAt, finishedAt, deadlineAt, leaseUntil,
  inputHash, sandboxId, steps[], model, processing,
  configDiff[], featureGaps[], findings[], patches[], blockedAttempts[],
  apply: {parentRunId, patchId, patchHash, repo, branch, appliedCommitSha} | null   # 새
}
```

| 칸 | 사전 점검(`PRECHECK`) | 반영 실행(`APPLY_PATCH`) |
|---|---|---|
| `kind` | `PRECHECK` | `APPLY_PATCH` |
| `apply` | null | 아래 표 |
| `status`·`failureCode`·`simulated` | 지금 그대로 | 반영 실행 표의 값(`failureCode` 는 러너 값 `IMAGE_BUILD_FAILED`·`PATCH_HASH_MISMATCH`… 또는 WAS `LEASE_EXPIRED`·`DEADLINE_EXCEEDED`) |
| `createdAt`…`leaseUntil` | 그대로 | 그대로(시작 = `startedAt` — 러너의 첫 heartbeat·이벤트로 `RUNNING` 이 된 시각, 종료 = `finishedAt`) |
| `steps` | 이벤트에서 계산 | 같은 계산(반영 실행의 이벤트) |
| `aiReviewed`·`model`·`processing`·`sandboxId` | 그대로 | false·null·null·null(반영 결과에 칸이 없다) |
| `configDiff`·`featureGaps`·`findings`·`patches`·`blockedAttempts` | 그대로 | 빈 배열 |

`apply`(반영 실행만):

| 칸 | 출처 | 결과 전 |
|---|---|---|
| `parentRunId` | `agent_runs.parent_run_id` | 있음 |
| `patchId`·`patchHash` | 입력 사본 `patch.id`·`patch.patchHash` | 있음 |
| `repo`·`branch` | 입력 사본 `repo`·`branch`(`applied/<반영 실행 id>`) — 반영 **대상** 브랜치 | 있음 |
| `appliedCommitSha` | 결과 `appliedCommitSha` | null. 실패·커밋 없는 성공도 null |

- 응답 모양을 `oneOf` 로 나누지 않는다: 지금 화면·생성 타입이 `PrecheckDetail` 한 모양을 쓰므로 칸을 더하기만 한다(기존 칸의 뜻·타입 불변).
- 이벤트는 `GET …/{id}/logs` 그대로: `{items: [{seq, at, step, level, message, data}], nextAfter}`. `data` 는 러너가 낸 객체(빌드 `seconds`, `fixableHigh`·`fixableCritical`, healthz `attempts`, 실패 `exitCode`·`tail`)다.
- **비밀값**: 이벤트·결과·입력 사본은 저장 전에 `mask_secrets_in_json` 을 거쳤다(러너 계약 그대로) → 응답에도 토큰 원문이 없다. 입력 사본 전체(`diff` 포함)는 응답에 싣지 않는다 — 패치 원문은 부모 상세 `patches[]` 에 있다.

## 5. 수용 기준(AC)

| # | 기준 | 시험(`tests/integration/test_apply_run_detail.py`) |
|---|---|---|
| AC-1 | 성공 반영 실행(11/6 `72f466c7-…` 모양): 결과 전 `QUEUED`·`apply{branch, patchHash…}`·`appliedCommitSha: null`·이벤트 0 → 결과 뒤 `kind APPLY_PATCH`·`SUCCEEDED`·`apply.appliedCommitSha`·`startedAt`·`finishedAt`·`steps`, 이벤트 7줄(빌드 시간·trivy 0·healthz 시도 수)·페이지; 부모는 `kind PRECHECK`·`apply null`·`patches[0].apply.runId` = 반영 실행 id | `test_apply_run_detail_and_events_for_applied_run` |
| AC-2 | 실패 반영 실행(11/6 `56c36cb5-…` 모양): `FAILED`·`IMAGE_BUILD_FAILED`·`appliedCommitSha null`·`PATCH_VERIFY` 단계 `ERROR`, 이벤트 `image build failed` 의 `data.tail` 이 보이고 토큰 모양은 `***` | `test_apply_run_detail_shows_build_failure_tail_without_secrets` |
| AC-3 | 뷰어·에이전트 토큰 200(상세·이벤트), 다른 조직 경로 404 `PRECHECK_NOT_FOUND`, 남의 조직 경로 404 `ORGANIZATION_NOT_FOUND` | `test_apply_run_detail_is_readable_by_viewer_and_agent_but_not_other_org` |
| AC-4 | 반영 실행 id 로 취소·승인·거절 404 `PRECHECK_NOT_FOUND`, 실행 상태 불변 | `test_apply_run_id_is_not_accepted_by_precheck_write_routes` |
| AC-5 | 사전 점검 id 회귀(`kind PRECHECK`·`apply null`·결과 칸·이벤트 그대로), `CHANGE_PROPOSAL` 404(기존 시험), OpenAPI 에 `kind`·`apply` 필수 칸·`PrecheckApplyRunResponse` 6칸 | `test_precheck_detail_keeps_shape_and_marks_kind`·`test_openapi_precheck_detail_documents_kind_and_apply`·`test_get_precheck_is_not_found_for_other_org_unknown_id_and_other_kinds` |
| AC-6 | 러너 계약·러너 경로 시험 불변(`test_apply_patch_runs.py`·`test_runner_api.py` 그대로 통과), 마이그레이션 0, ruff·format·mypy app·전체 pytest(건너뜀 0) | 전체 pytest |

## 6. 운영 영향

- 스위치 없음: 병합·배포 순간 운영 `GET …/prechecks/{반영 실행 id}` 가 404 → 200 으로 바뀐다. 읽기만이고 쓰기·러너 동작은 그대로다.
- 기존 사전 점검 응답에는 `kind: "PRECHECK"`·`apply: null` 두 칸이 더해진다(모르는 칸을 무시하는 화면이면 영향 0).
- 11/6 두 실행을 운영 API로 읽는 확인은 배포 뒤 할 일이다(로컬은 같은 모양의 시험 데이터로 확인).

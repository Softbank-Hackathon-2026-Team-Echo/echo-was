# API 서버 17: 이전 계획에 DB 쓰기 전환 단계(DB_CUTOVER) 넣기 — SPEC

- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/75 (X4, 데모 필수 ① 「가동 중 앱 클라우드 간 이전」 완결성)
- 저장소 경로: `specs/api-17-cutover-step.md`
- 기준: echo-was main `f9680b8`(#100 병합 뒤), echo-infra main `bebf7fc`. Alembic head `31bc0563b24b` → 새 리비전 `7c2e5a9d4b18`
- 승인: 지민 11/7 02:5x 「병렬로 전부 진행」. 이도현 세부 계획(#75 11/6 댓글 "회의 전까지 구상해서 다시 전달")은 아직 없음 → 7절 확인 필요 항목
- 상태: 구현(기본 꺼짐). 운영에서 켜는 것은 별도 PR

## 1. 왜

이전 계획은 트래픽만 옮긴다(PREFLIGHT → SHIFT 10 → SHIFT 50 → DATA_SYNC_CHECK → SHIFT 100). 계획이 `SUCCEEDED` 여도 DB 쓰기 담당은 출발(A)에 남고, 컷오버는 사람이 도현 컷오버 Job 을 따로 실행한다. 이 작업은 그 Job 을 계획의 마지막 단계로 부르고, 결과 표의 누락·중복으로 통과를 자동 판정한다.

## 2. 확인된 사실 (읽기만 함)

### 2-1. 도현 컷오버 Job (echo-infra `replication/cutover-job/`, origin/main `bebf7fc`)

| 항목 | 값 | 출처 |
|---|---|---|
| 도는 곳 | 서울 GKE(B) 네임스페이스 `echo-ops`, SA `echo-cutover`(Workload Identity → GSA `echo-dev-cutover`, `terraform/environments/gcp/dev/cutover.tf`) | `manifests/echo-ops.yaml`, `cutover.tf` |
| 이미지 | `asia-northeast3-docker.pkg.dev/team-echo-final/echo/cutover@sha256:cb668942…`(main `c3b4747`, #79 SCRAM 사전 확인 포함). digest 만 허용 | `deploy/values.env` |
| 진입점 | `cutover-entry.sh`(ENTRYPOINT, command·args 덮어쓰기 금지). B = in-cluster 토큰, A = Google ID 토큰 → AWS STS → EKS. A·B 주소는 각 클러스터 Service `echo-pg-external` | `cutover-entry.sh`, `Dockerfile` |
| 인자(환경 변수) | `KIND`=cutover·rollback·promote, `PLAN_ID`=소문자 uuid, `ALLOW_PROMOTE`=true·false. **이 셋 외 금지**(VAP) | `echo-ops.yaml` VAP `echo-cutover-job-template` |
| Job 모양 | 이름 `echo-cutover-<계획 앞 8자>-<시도>`, `backoffLimit 0`, `activeDeadlineSeconds 120`, `ttlSecondsAfterFinished 86400`, 컨테이너 `cutover` 하나, 볼륨 없음 | `job.example.yaml` |
| 종료 코드 | 0 끝남(이번에 했거나 이미 됨: `ALREADY_WRITER`·`ALREADY_DONE`) · 1 바뀐 것 없음(RESUME 함) · 2 부분 상태(PAUSE 로 남을 수 있음, 사람이 "부분 상태 복구"). SIGTERM(제한 시간)은 쓰기 정지 중이면 RESUME 후 1, 전환 중이면 2 | `common.sh` guard, README "Job 이 죽을 때" |
| 결과 기록 | RESUME 뒤 새 쓰기 담당(to)에 `echo_ops.cutover_runs` 한 줄: kind(CUTOVER·ROLLBACK), from/to, paused/resumed, write_pause_ms, drain/catchup/switch_ms, rows_total, missing_rows, duplicate_rows, max_n, **plan_id**, mode(NORMAL·PROMOTE), rpo_estimate_ms. `(plan_id, kind)` 부분 unique. 기록 실패는 경고만(컷오버 자체는 끝남 → 행 없을 수 있음) | `common.sh` `OPS_DDL`·`record_run` |
| 롤백 Job | 같은 이미지 `KIND=rollback`(rollback.sh, B→A 거울). A 가 살아 있어야 한다(A 주소 없으면 1) | `cutover-entry.sh` |
| 승격 | `KIND=cutover` + `ALLOW_PROMOTE=true` 이고 A 에 닿지 않으면 promote.sh(결과 mode=PROMOTE). 요청 저널 재생(promote-with-replay.sh, infra#85~#90)은 Job 진입점이 아직 부르지 않는다 | `cutover.sh`, `promote-with-replay.sh` |
| RBAC(echo-was) | Role `echo-was-cutover-jobs`(ns echo-ops): jobs create·get·list·watch, pods get·list, pods/log get → KSA `echo-was/echo-was`. 11/6 운영 can-i "echo-was jobs create yes", VAP dry-run 확인(README 운영 적용 기록) | `deploy/gcp-seoul/echo-ops.yaml` |
| design-infra §10 | "컷오버 실행이 이전 계획과 분리 — 비율 100% 뒤 컷오버 호출은 후속" · 시연 순서(§5) "이전 → DB 컷오버 → 복귀 → DB 롤백" | `docs/design-infra.md` |

### 2-2. echo-was

- 일꾼·API 파드는 서울 GKE `echo-was` 네임스페이스, `serviceAccountName: echo-was`(토큰 자동 마운트). `DATA_SYNC_DATABASES` 의 gcp 가 `echo-pg.echo-data.svc.cluster.local` → **echo-ops 와 같은 클러스터**다. 그래서 다른 클러스터 접속기(`ClusterAccessProvider`) 없이 파드 SA 토큰으로 `https://kubernetes.default.svc` 를 부르면 된다.
- 결과 읽기: `PostgresCutoverRunReader`(API 서버 16)는 시간 창으로만 찾는다. 계획 안에서 컷오버하면 창(`finished_at` 이후)에 걸리지 않으므로 계획 id 로 찾는 `find_by_plan` 을 더한다.
- 이슈 #75 본문은 FAILBACK 의 DB_CUTOVER 를 SHIFT 10 **앞**에 두는 안이었다. 이번 X4 지시는 "SHIFT 100 뒤", design-infra §5 시연 순서도 "복귀 → DB 롤백"이라 **모든 종류에서 SHIFT 100 뒤(index 5)** 로 둔다(7절 확인).

## 3. 범위

한다: 단계 종류 `DB_CUTOVER`, 요청 `includeCutover`(기본 false), 실행기 base/fake/kubernetes, 처리기 3 phase, 판정·실패 정책, 로그 코드, 감사 줄, simulate 4종, 결과를 단계·계획 `result` 에 기록, CHECK 넓히기 마이그레이션, 문서.

하지 않는다: 운영에서 켜기(`deploy/base.yaml` 은 그대로 — 별도 PR), 롤백 Job 자동 호출, 요청 저널 재생(infra 12.4)을 Job 에 넣기(infra 몫), 프론트 표시(인계), 보안 게이트 변경.

## 4. 설계

### 4-1. 계획 만들기

- 요청 `includeCutover: bool = false`. false 면 **지금과 같은 5단계**(단계 JSON·로그·감사 data 모양까지 같다). true 면 `steps` 끝에 `{index 5, kind DB_CUTOVER, maxWaitSeconds = MIGRATION_DB_CUTOVER_TIMEOUT_SECONDS, expectedSeconds = 여유 + 30}`.
- 서버 실행기 `DB_CUTOVER_EXECUTOR_MODE=disabled`(기본)면 `includeCutover=true` 는 409 `DB_CUTOVER_DISABLED` → **병합만으로는 운영 동작이 바뀌지 않는다.**
- 재난 트리거(EMERGENCY)는 `MIGRATION_DB_CUTOVER_ON_DISASTER=true` 이고 실행기가 켜져 있을 때만 넣는다(기본 false).
- 이미 만든 계획은 저장된 단계 그대로다(단계 수는 계획 행의 `steps` 길이로 판단).

### 4-2. Job 값

| 계획 종류 | KIND | ALLOW_PROMOTE | 결과 표 kind | 결과를 읽는 쪽 |
|---|---|---|---|---|
| PLANNED | cutover | false | CUTOVER | 도착(B) |
| EMERGENCY | cutover | true | CUTOVER | 도착(B) |
| FAILBACK | rollback | false | ROLLBACK | 도착(A) |

### 4-3. 처리기 (`migration_plan_processor`)

- APPLY: 요청 시각(`traffic_requested_at` 칸 재사용) 저장·커밋 → `start` → Job 이름·종류를 `step_results[5].cutover` 에 적고 OBSERVE(`observe_until = 지금 + 단계 maxWaitSeconds`). 만들기 실패가 retryable(5xx·연결)이면 요청 시각 + 상한까지 같은 이름으로 다시(409 면 PLAN_ID 확인 후 같은 Job).
- OBSERVE: `MIGRATION_DB_CUTOVER_POLL_SECONDS` 마다 `poll`. 끝남·사라짐·상한 초과면 JUDGE.
- JUDGE: 종료 1 이 아니면 `find_run`(이 계획 id·kind 행) → `judge_db_cutover`.
- Job 을 요청한 뒤(APPLY 요청 시각 이후·OBSERVE·JUDGE)에는 **중단 요청·임대 상실로 트래픽을 되돌리지 않고** 판정까지 간다.

### 4-4. 판정과 실패 정책 (롤백 Job 은 자동으로 부르지 않는다)

| 상황 | 결과 | rollbackStatus | 트래픽 | 이유 |
|---|---|---|---|---|
| 종료 0 + 행 + 누락 0·중복 0 · 종료 0 + ALREADY_WRITER | 통과 → SUCCEEDED | — | 도착 100% | |
| 종료 1 · Job 을 만들지 못함 | ROLLED_BACK(`DB_CUTOVER_FAILED`) | SUCCEEDED | 출발 100% | 쓰기 담당 그대로 → 기존 롤백이 안전 |
| 종료 2 · 알 수 없는 종료(143 등) · Job 사라짐, 행 없음 | FAILED(`DB_CUTOVER_PARTIAL`) | SKIPPED | 그대로 | PgBouncer 가 PAUSE 일 수 있고 rollback.sh 도 판단 불가(종료 2) → 사람이 절차서 |
| 상한 초과, 행 없음 | FAILED(`DB_CUTOVER_TIMEOUT`) | SKIPPED | 그대로 | 쓰기 담당 모름 |
| 누락·중복 > 0 · 종료 0 인데 행 없음/못 읽음 | FAILED(`DATA_INTEGRITY_FAILED`) | SKIPPED | 그대로 | 쓰기 담당 = 도착. 되돌리기는 사람 판단(12.0 결정 3) |

종료 코드를 모르거나 사라졌거나 상한을 넘었어도 이 계획 행이 있으면 전환은 끝난 것이라 행으로 판정한다. 트래픽 위치는 쓰기를 깨지 않는다(두 PgBouncer 가 늘 같은 쓰기 담당을 본다) — 그래서 "모름"일 때 트래픽을 건드리지 않는 쪽을 골랐다.

### 4-5. 기록

- 단계 결과 `cutover = {jobName, jobKind, allowPromote, exitCode, reason, jobFinished, jobLost, startFailure, run{kind, pausedAt, resumedAt, writePauseMs, drainMs, catchupMs, switchMs, rowsTotal, missingRows, duplicateRows, mode, rpoEstimateMs}}` → 응답 `steps[i].cutover`(그 밖 단계 null).
- 계획 `result`(API 서버 16 칸): DB_CUTOVER 단계가 있는 계획은 단계가 적은 행을 그대로 싣는다(고객 DB 를 다시 읽지 않는다).
- 로그 코드: `DB_CUTOVER_JOB_CREATED`·`DB_CUTOVER_JOB_FINISHED`·`DB_CUTOVER_OK`·`DB_CUTOVER_FAILED`(원인 문장)·`DB_CUTOVER_MANUAL_REQUIRED`. data 는 Job 이름·종류·종료 코드·이유·결과 숫자(로그·토큰 원문 없음).
- 감사: SYSTEM `migration-worker` `MIGRATION_DB_CUTOVER_STARTED`·`MIGRATION_DB_CUTOVER_FINISHED`(SUCCEEDED/FAILED, errorCode). 끝 상태 `MIGRATION_FINISHED` 는 그대로.
- 원인 코드 4개·`rollbackStatus SKIPPED` 추가, 단계 번호 CHECK 0~4 → 0~5(마이그레이션 `7c2e5a9d4b18`, CHECK 넓히기만. downgrade 는 새 값이 있으면 실패).

### 4-6. 보안 게이트

영향 없음. 게이트는 승인·실행 시작(APPROVED → RUNNING) 때 판정하고 단계 수와 무관하다. Job 권한 경계는 echo-infra VAP(이미지 digest·env 3개 고정)이고 echo-was 는 그 모양 그대로만 만든다.

### 4-7. simulate (가짜 모드)

`failAtStep=5`(includeCutover=true 만, 아니면 422): `DB_CUTOVER_FAIL`(종료 1) · `DB_CUTOVER_PARTIAL`(종료 2) · `DB_CUTOVER_TIMEOUT`(끝나지 않음) · `DATA_INTEGRITY`(종료 0, 누락 1). 실행기를 부르지 않는다.

## 5. 설정

`DB_CUTOVER_EXECUTOR_MODE`(disabled·fake·kubernetes, 기본 disabled) · `DB_CUTOVER_JOB_IMAGE`(kubernetes 필수, @sha256) · `DB_CUTOVER_NAMESPACE`(echo-ops) · `DB_CUTOVER_KUBE_API_URL` · `DB_CUTOVER_SERVICE_ACCOUNT_DIR` · `DB_CUTOVER_KUBE_TIMEOUT_SECONDS`(10) · `MIGRATION_DB_CUTOVER_TIMEOUT_SECONDS`(180) · `MIGRATION_DB_CUTOVER_POLL_SECONDS`(3) · `MIGRATION_DB_CUTOVER_ON_DISASTER`(false). kubernetes 는 `DATA_SYNC_CHECKER_MODE=postgres` + `CUTOVER_RUNS_DATABASE` 가 필요하다(결과 행 읽기, 시작 때 검사).

## 6. 완료 기준

- [x] (자동) includeCutover 없음/false → 지금과 같은 5단계·로그(시험)
- [x] (자동) true + Job 성공 → 6단계 SUCCEEDED, 단계·계획 result 에 writePauseMs·누락·중복·rowsTotal, 감사 2줄
- [x] (자동) 종료 1·만들기 실패 → 트래픽 롤백 / 종료 2·사라짐·누락·행 없음 → FAILED SKIPPED / 상한 초과 → TIMEOUT SKIPPED / 상한 초과지만 행 있음 → 통과
- [x] (자동) Job 실행 중 중단 → Job 결과로 판정(트래픽 되돌리지 않음)
- [ ] (운영) 켜기 PR 뒤 PLANNED 이전 → 자동 컷오버 → FAILBACK 자동 rollback 한 바퀴, 사람 명령 0
- [ ] (운영) EMERGENCY 승격 경로

## 7. 도현 확인 필요

1. **FAILBACK 위치**: 이슈 #75 본문은 SHIFT 10 앞, 이 구현은 SHIFT 100 뒤(X4 지시·design-infra §5 시연 순서). 앞이 필요하면 단계 정의만 바꾸면 된다.
2. **재난 → 승격 → A 복구 → FAILBACK**: A 가 돌아와도 `writer_side` PARTIAL → 종료 2 → `DB_CUTOVER_PARTIAL`(자동 처리 없음). resync 를 Job 이 품는지(11/6 댓글 "일정 시간 자동 시도") 정해지면 판정에 반영.
3. **요청 저널 재생**: Job 진입점은 promote.sh 를 부르고 promote-with-replay.sh 는 부르지 않는다. 재생을 Job 에 넣으면 env 가 늘어 VAP 와 echo-was Job 모양을 같이 바꿔야 한다.
4. **로그 표시 문자열**: 종료 0 의 이유를 파드 로그의 `ALREADY_WRITER`·`ALREADY_DONE`·`승격 완료` 로 읽는다. 문구를 바꾸면 알려 달라(결과 행 mode=PROMOTE 는 별도로 읽는다).
5. **상한 180초**: Job `activeDeadlineSeconds 120` + 종료 유예 30 + 파드 시작. 이미지 풀이 느리면 늘린다.
6. **이미지 digest 동기화**: `DB_CUTOVER_JOB_IMAGE` 는 `deploy/values.env` 의 `CUTOVER_IMAGE` 와 같아야 VAP 를 통과한다. 이미지를 다시 빌드하면 echo-was 설정 PR 도 같이.

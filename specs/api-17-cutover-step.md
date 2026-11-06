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
| EMERGENCY | cutover | true(만들 때 승격 허용 표시 + 승인 동의가 있을 때만) | CUTOVER | 도착(B) |
| FAILBACK | rollback | false | ROLLBACK | 도착(A) |

### 4-3. 처리기 (`migration_plan_processor`)

- APPLY: 요청 시각(`traffic_requested_at` 칸 재사용) 저장·커밋 → `start` → Job 이름·종류를 `step_results[5].cutover` 에 적고 OBSERVE(`observe_until = 지금 + 단계 maxWaitSeconds`). 만들기 실패가 retryable 이면 요청 시각 + 상한까지 같은 이름으로 다시(409 면 PLAN_ID 확인 후 같은 Job).
  - **생성 응답 유실(검증 P1-1)**: 실패를 셋으로 가른다. ① 분명한 거절(401·403·400·422·이름 충돌) = Job 없음. ② 보내기 전 실패(접속 실패·토큰 파일 읽기 실패·429, `not_sent`) = Job 없음. ③ 보낸 뒤 결과 불명(읽기 시간 초과·끊김·5xx, `maybe_created`) = 만들었을 수 있음 → 단계 결과에 `startUncertain` 을 적어 두고, 상한까지 끝내 생성 답을 못 받으면 **원복 전에** 결정적 이름(`echo-cutover-<계획 8자>-1`)으로 GET(`find_job`): 있음 → 그 Job 을 기다림(`startConfirmed`), 404 → 결과 표 `cutover_runs`(계획 id)도 없을 때만 `DB_CUTOVER_FAILED`·트래픽 원복(행이 있으면 판정으로), 확인 불가 → `DB_CUTOVER_UNKNOWN`·가중치 그대로·`SKIPPED`.
- OBSERVE: `MIGRATION_DB_CUTOVER_POLL_SECONDS` 마다 `poll`. 끝남·사라짐·상한 초과면 JUDGE. **poll 예외(검증 P1-4)**는 흡수해 연속 횟수(`pollErrors`)를 적고 시계 상한은 늘 확인한다. 연속 5번이면 상한을 기다리지 않고 JUDGE(행이 없으면 `DB_CUTOVER_UNKNOWN`). 일꾼이 바뀌어도 OBSERVE 는 저장된 Job 이름으로, APPLY 는 같은 이름 409 → GET 으로 다시 붙는다.
- JUDGE: `find_run`(이 계획 id·kind 행) → `judge_db_cutover`. **종료 1(검증 P1-2)**은 「바뀐 것 없음」의 증거가 아니다(이미 B 가 쓰기 담당인데 사전 확인이 실패해도, PAUSE 뒤 RESUME 이 실패해도 종료 1) → 행이 있으면 행으로 판정, 없으면 데이터 확인 실행기로 지금 쓰기 담당을 본다(출발 `pg_stat_replication` 에 도착 구독 walsender = 출발이 쓰기 담당 / `TARGET_IS_WRITER` = 도착). **출발이 쓰기 담당이거나 파드 로그에 `NO_CHANGE` 표식이 있을 때만** 트래픽 원복, 아니면 `DB_CUTOVER_UNKNOWN`.
- 데이터 확인(단계 3, 검증 P1-3): **EMERGENCY + DB 쓰기 전환** 계획에서 지연을 못 재면(원본 DB 단절) 실패 대신 건너뛰고 `DATA_SYNC_SKIPPED`(WARN, reason `EMERGENCY_SOURCE_UNREACHABLE`) 를 남긴 뒤 승격 Job 까지 간다. 승격은 승인 때 동의를 받았다. DB 쓰기 전환이 없는 EMERGENCY·그 밖의 계획은 그대로 `METRICS_UNAVAILABLE`.
- Job 을 요청한 뒤(APPLY 요청 시각 이후·OBSERVE·JUDGE)에는 **중단 요청·임대 상실로 트래픽을 되돌리지 않고** 판정까지 간다.

### 4-4. 판정과 실패 정책 (롤백 Job 은 자동으로 부르지 않는다)

| 상황 | 결과 | rollbackStatus | 트래픽 | 이유 |
|---|---|---|---|---|
| 종료 0 + 행 + 누락 0·중복 0 · 종료 0 + ALREADY_WRITER | 통과 → SUCCEEDED | — | 도착 100% | |
| 종료 1 + (출발이 쓰기 담당 확인 또는 로그 `NO_CHANGE`) · Job 을 분명히 못 만듦(거절·보내기 전 실패·같은 이름 404 + 행 없음) | ROLLED_BACK(`DB_CUTOVER_FAILED`) | SUCCEEDED | 출발 100% | 쓰기 담당 그대로 → 기존 롤백이 안전 |
| 종료 1 + 쓰기 담당 미확인·도착 · 생성 응답 유실 뒤 조회 불가 · poll 연속 실패(행 없음) | FAILED(`DB_CUTOVER_UNKNOWN`) | SKIPPED | 그대로 | **결과를 모르면 가중치를 되돌리지 않는다**(트래픽과 쓰기 담당이 갈리는 것이 최악) → 사람이 쓰기 담당 확인 |
| 종료 2 · 알 수 없는 종료(143 등) · Job 사라짐, 행 없음 | FAILED(`DB_CUTOVER_PARTIAL`) | SKIPPED | 그대로 | PgBouncer 가 PAUSE 일 수 있고 rollback.sh 도 판단 불가(종료 2) → 사람이 절차서 |
| 상한 초과, 행 없음 | FAILED(`DB_CUTOVER_TIMEOUT`) | SKIPPED | 그대로 | 쓰기 담당 모름 |
| 누락·중복 > 0 · 종료 0 인데 행 없음/못 읽음 | FAILED(`DATA_INTEGRITY_FAILED`) | SKIPPED | 그대로 | 쓰기 담당 = 도착. 되돌리기는 사람 판단(12.0 결정 3) |

종료 코드를 모르거나 사라졌거나 상한을 넘었어도 이 계획 행이 있으면 전환은 끝난 것이라 행으로 판정한다. 트래픽 위치는 쓰기를 깨지 않는다(두 PgBouncer 가 늘 같은 쓰기 담당을 본다) — 그래서 "모름"일 때 트래픽을 건드리지 않는 쪽을 골랐다.

### 4-5. 기록

- 단계 결과 `cutover = {jobName, jobKind, allowPromote, exitCode, reason, jobFinished, jobLost, startFailure, run{kind, pausedAt, resumedAt, writePauseMs, drainMs, catchupMs, switchMs, rowsTotal, missingRows, duplicateRows, mode, rpoEstimateMs}}` → 응답 `steps[i].cutover`(그 밖 단계 null).
- 계획 `result`(API 서버 16 칸): DB_CUTOVER 단계가 있는 계획은 단계가 적은 행을 그대로 싣는다(고객 DB 를 다시 읽지 않는다).
- 로그 코드: `DB_CUTOVER_JOB_CREATED`·`DB_CUTOVER_JOB_FINISHED`·`DB_CUTOVER_OK`·`DB_CUTOVER_FAILED`(원인 문장)·`DB_CUTOVER_MANUAL_REQUIRED`. data 는 Job 이름·종류·종료 코드·이유·결과 숫자(로그·토큰 원문 없음).
- 감사: SYSTEM `migration-worker` `MIGRATION_DB_CUTOVER_STARTED`·`MIGRATION_DB_CUTOVER_FINISHED`(SUCCEEDED/FAILED, errorCode). 끝 상태 `MIGRATION_FINISHED` 는 그대로.
- 단계 결과 cutover 에 검증 반영 칸: `startUncertain`·`startConfirmed`·`pollErrors`·`writer`(SOURCE·TARGET·UNKNOWN). 이유(reason)에 `NO_CHANGE`·`PRECHECK_FAILED`·`WRITER_UNCONFIRMED`·`TARGET_IS_WRITER`·`START_UNCONFIRMED`·`POLL_ERRORS`.
- 원인 코드 5개(`DB_CUTOVER_UNKNOWN` 포함)·`rollbackStatus SKIPPED` 추가, 단계 번호 CHECK 0~4 → 0~5(마이그레이션 `7c2e5a9d4b18`, CHECK 넓히기만. downgrade 는 새 값이 있으면 실패).

### 4-5a. 승격 동의 (검증 P2)

- 재난 긴급 이전 계획에 DB 쓰기 전환이 들어가면(`MIGRATION_DB_CUTOVER_ON_DISASTER=true`) 만들 때 그 단계에 승격 허용 표시(`allowPromote`)를 단다. 실행 때 `ALLOW_PROMOTE=true` 는 **EMERGENCY + 이 표시**일 때만이다.
- 상세·승인 응답 `promoteWarning`: 승격은 되돌릴 수 없고, 복제 안 된 쓰기를 잃을 수 있고(rpoEstimateMs), 원본이 단절만 됐을 수 있고, 복귀에는 resync 가 필요하다는 고지. 그 밖의 계획은 null.
- 승인 본문 `{"acknowledgePromote": true}` 가 없으면 422 `PROMOTE_ACK_REQUIRED`(계획 그대로). 그 밖의 계획은 본문 없이 승인(지금과 같음). 동의는 승인 감사 줄 `acknowledgePromote: true` 로 남는다.
- 재난 자동 승인(자기 승인 허용 조직)은 서버 설정을 켠 것을 운영자 사전 동의로 보고 동의를 함께 넘긴다(감사 줄에 남음) — 지민 확인 항목.

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
- [x] (자동, 검증 반영) 생성 응답 유실 → 같은 이름 Job 재부착 / 404 + 행 없음만 원복 / 확인 불가 UNKNOWN·SKIPPED · 종료 1 은 출발 쓰기 담당·`NO_CHANGE` 일 때만 원복 · 원본 단절 EMERGENCY 가 승격 Job 에 닿음 · poll 지속 예외 UNKNOWN · 승격 동의 없으면 422
- [ ] (운영) 켜기 PR 뒤 PLANNED 이전 → 자동 컷오버 → FAILBACK 자동 rollback 한 바퀴, 사람 명령 0
- [ ] (운영) EMERGENCY 승격 경로

## 7. 도현 확인 필요

1. **FAILBACK 위치**: 이슈 #75 본문은 SHIFT 10 앞, 이 구현은 SHIFT 100 뒤(X4 지시·design-infra §5 시연 순서). 앞이 필요하면 단계 정의만 바꾸면 된다.
2. **재난 → 승격 → A 복구 → FAILBACK**: A 가 돌아와도 `writer_side` PARTIAL → 종료 2 → `DB_CUTOVER_PARTIAL`(자동 처리 없음). resync 를 Job 이 품는지(11/6 댓글 "일정 시간 자동 시도") 정해지면 판정에 반영.
3. **요청 저널 재생**: Job 진입점은 promote.sh 를 부르고 promote-with-replay.sh 는 부르지 않는다. 재생을 Job 에 넣으면 env 가 늘어 VAP 와 echo-was Job 모양을 같이 바꿔야 한다.
4. **로그 표시 문자열**: 종료 0 의 이유를 파드 로그의 `ALREADY_WRITER`·`ALREADY_DONE`·`승격 완료` 로 읽는다. 문구를 바꾸면 알려 달라(결과 행 mode=PROMOTE 는 별도로 읽는다).
5. **상한 180초**: Job `activeDeadlineSeconds 120` + 종료 유예 30 + 파드 시작. 이미지 풀이 느리면 늘린다.
6. **이미지 digest 동기화**: `DB_CUTOVER_JOB_IMAGE` 는 `deploy/values.env` 의 `CUTOVER_IMAGE` 와 같아야 VAP 를 통과한다. 이미지를 다시 빌드하면 echo-was 설정 PR 도 같이.
7. **종료 1 의 뜻 구분(검증 P1-2, 필수)**: 지금 `common.sh` 는 사전 확인 실패(이미 B 가 쓰기 담당이어도)와 PAUSE 뒤 RESUME 실패(`|| true`)를 모두 종료 1 로 낸다. echo-was 는 이제 종료 1 만으로 원복하지 않고 쓰기 담당을 다시 확인한다. Job 이 로그에 **`NO_CHANGE`(출발 — cutover 는 A, rollback 은 B — 이 쓰기 담당 그대로 + RESUME 까지 끝남)** 와 **`PRECHECK_FAILED`(사전 확인 실패, 쓰기 담당 미확인)** 를 구분해 찍어 주고, RESUME 실패는 종료 2 로 바꿔 달라. 표식이 없으면 데이터 확인으로 출발이 쓰기 담당임이 보일 때만 원복하고, 아니면 `DB_CUTOVER_UNKNOWN`(사람 확인)이다.
8. **승격 fencing(검증 P2)**: `promote.sh` 는 A 에 닿지 않는 것만으로 승격한다. A 가 살아 있고 단절·권한 문제면 이중 쓰기가 될 수 있다. echo-was 는 승인 때 고지·동의만 받는다 — A 정지/fencing 확인을 Job 이 하는지 알려 달라.

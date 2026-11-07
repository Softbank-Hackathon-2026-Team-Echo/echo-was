# 명시 DB쌍 지원 능력 — db-pair/v1 (1단계)

관련: echo-was#116(지원쌍 사전판정·DB 바인딩), echo-infra#105(명시 DB쌍 Job).
이 문서는 안전 차단 기반이다. 임의 환경쌍의 DB 이전·재난 승격·복귀가 운영에서 완성됐다는 뜻이 아니다.

## 설정과 활성화 경계

`MIGRATION_PAIR_GUARD_ENABLED=false`, `MIGRATION_PAIR_REGISTRY={}`가 기본이다. 이 PR은 운영 설정을 바꾸지 않는다. OFF이고 snapshot이 없는 옛 계획은 기존 A/B 경로를 유지한다. snapshot이 있는 계획을 OFF로 바꾸어 legacy 경로로 내릴 수 없다(`DB_PAIR_GUARD_DISABLED`).

ON이면 생성 때 snapshot을 고정하고 승인·worker 시작·RUNNING 재개(아직 Job을 요청하지 않은 상태)·DB Job 요청 직전에 조직/서비스/환경/배포·등록표 해시·writer·증거 신선도를 재검사한다. 유효한 쌍도 현 이미지·VAP·엔진의 명시 DB쌍 계약이 없으므로 승인 및 실행은 `DB_PAIR_WORKER_NOT_READY`로 차단한다. API의 `executionReady`는 항상 false다. 이것을 true로 바꾸는 운영 설정은 없다.

worker 차단은 외부 TLS 시험·트래픽 변경·Job 송신보다 먼저 실행한다. 시작 전이면 FAILED, rollbackStatus=NOT_NEEDED. RUNNING 상태의 방어 차단이면 FAILED, rollbackStatus=SKIPPED이며 자동 트래픽/DB 롤백을 하지 않는다. 기존 errorCode `DB_CUTOVER_UNKNOWN`, 실행 로그 `FAILED.data.migrationPairReason`에 구체 사유를 남기고 기존 종료 감사 경로를 사용한다. 이미 요청했을 수 있는 Job은 기존 OBSERVE/JUDGE로 추적하며 쌍 증거 없는 결과를 성공으로 인정하지 않는다.

## 조회 API

`GET /api/v1/orgs/{org_id}/services/{service_id}/migration-capabilities?sourceEnvironmentId=UUID&targetEnvironmentId=UUID`

Viewer 이상·에이전트 읽기 허용. 조직 권한 검사 후 서비스와 두 환경을 조직 범위로 조회한다. 다른 조직 리소스는 기존 404다. 조회는 네트워크 증거 수집이나 DB 쓰기를 하지 않는다.

`ApiResponse.data`:

| 필드 | 의미 |
|---|---|
| contractVersion | `db-pair/v1` |
| registryRevision | 서버 등록표 버전, 비어 있으면 `unconfigured` |
| sourceEnvironmentId / targetEnvironmentId | 요청의 정확한 환경 UUID |
| sourceDbRegistryId / targetDbRegistryId | 잠재 지원 판정에 성공한 등록 이름, 없으면 null |
| creationGateEnforced / approvalGateEnforced / workerGateEnforced | 현재 서버의 guard ON 여부. 실행 준비 여부가 아님 |
| executionReady | 현재 항상 false |
| appTraffic / dbCutover / disasterPromote / failback | 각각 아래 판정 객체 |

판정 객체: supported(bool), status(SUPPORTED/UNSUPPORTED/UNKNOWN), reasons(코드 배열), evidenceFreshness(FRESH/STALE/UNKNOWN), evidenceIds(서버 증거 ID 배열).

실제 서비스 응답은 잠재 지원하는 fixture 쌍도 `supported=false`, `status=UNSUPPORTED`, `reasons=[DB_PAIR_WORKER_NOT_READY]`로 내린다. 등록 누락·stale의 구체 사유는 유지한다. 순수 registry 시험에서 true가 나오는 것은 등록표/adapter의 잠재 계약만 입증하며 API 실행 지원을 뜻하지 않는다. FE는 supported=false, executionReady=false, UNKNOWN 또는 조회 오류면 실행을 막는다. 종류명만으로 추정하지 않는다. FE 코드는 이 PR에서 수정하지 않았다.

미등록 기본 응답 예시(판정 객체 네 개 모두 같은 내용):

```json
{"contractVersion":"db-pair/v1","registryRevision":"unconfigured","creationGateEnforced":false,"approvalGateEnforced":false,"workerGateEnforced":false,"executionReady":false,"sourceEnvironmentId":"00000000-0000-0000-0000-000000000003","targetEnvironmentId":"00000000-0000-0000-0000-000000000004","sourceDbRegistryId":null,"targetDbRegistryId":null,"appTraffic":{"supported":false,"status":"UNKNOWN","reasons":["DB_BINDING_UNREGISTERED"],"evidenceFreshness":"UNKNOWN","evidenceIds":[]},"dbCutover":{"supported":false,"status":"UNKNOWN","reasons":["DB_BINDING_UNREGISTERED"],"evidenceFreshness":"UNKNOWN","evidenceIds":[]},"disasterPromote":{"supported":false,"status":"UNKNOWN","reasons":["DB_BINDING_UNREGISTERED"],"evidenceFreshness":"UNKNOWN","evidenceIds":[]},"failback":{"supported":false,"status":"UNKNOWN","reasons":["DB_BINDING_UNREGISTERED"],"evidenceFreshness":"UNKNOWN","evidenceIds":[]}}
```

## 등록표·snapshot·거절

서버 관리 등록표만 받는다. 요청자가 host, command, credentials, secret을 주입하는 API는 없다. 등록표는 extra=forbid, DB id·조직/서비스/환경 binding·방향 중복을 거부한다. 환경 종류는 DB lookup 키가 아니다. B=고객 GKE app DB이며 GCP_B2=온프레 demo DB의 전용 복제본이다. 둘을 대신 선택하지 않는다.

증거(route, writer_path, writer, DB 동작에는 data_sync/job_contract까지)는 VERIFIED·evidence_id·aware UTC observed_at이 필수다. 기본 유효기간 300초, 최대 3600초. 미래/naive/누락=UNKNOWN, 오래됨=STALE, FAILED=미지원. 현재 writer는 source DB여야 한다. 운영 증거 수집은 이번 범위 밖이고 fixture를 운영 증거로 쓰지 않는다.

생성 시 `plan.fingerprint.migrationPair`에 contract_version, registry_revision, registry_sha256, organization_id, service_id, source/target environment/deployment UUID, source_db_id/target_db_id, operation, job_kind를 저장한다. 해시 입력은 Pydantic 기본값을 포함한 JSON(mode=json)을 `sort_keys=True,separators=(',',':'),ensure_ascii=False`로 직렬화한 UTF-8이다. 증거를 포함한 등록표 전체가 바뀌면 새 계획을 만들어야 한다.

생성/승인 거절: HTTP 409 `MIGRATION_PAIR_UNSUPPORTED`, details=[{field:"migrationPair",reason:"CODE"}]. 주요 코드: DB_BINDING_UNREGISTERED, DB_DIRECTION_UNREGISTERED, DB_BINDING_SCOPE_MISMATCH, DB_PAIR_SAME, DEPLOYMENT_MISSING, DB_DEPLOYMENT_CHANGED, DB_ADAPTER_UNSUPPORTED, DB_WRITER_MISMATCH, DB_PAIR_SNAPSHOT_CHANGED, DB_PAIR_GUARD_DISABLED, DB_PAIR_WORKER_NOT_READY, ROUTE/WRITER_PATH/WRITER/DATA_SYNC/JOB_CONTRACT의 _UNKNOWN/_STALE/_FAILED. 접속 주소·자격·외부 원문은 반환하지 않는다.

기존 보안·자기 승인·승격 동의·이미지 검사 규칙을 대체하거나 우회하지 않는다.

## 6방향 현재 / 목표

아래 현재 칸은 이번 명시 DB쌍 경로 기준이다. 과거 legacy A/B 운영 동작의 검증 수준을 확대하지 않는다.

| 방향 | 등록 fixture의 잠재 앱/DB/재난승격/복귀 | 현재 API 실행 | 목표·필수 후속 |
|---|---|---|---|
| AWS→GCP | true/true/true/false (A/app→B/app에만) | 모두 false, ready=false | 명시쌍 엔진·VAP·receipt, 앱→writer 경로 실측 |
| GCP→AWS | true/true/false/true (B/app→A/app에만) | 모두 false, ready=false | 역방향 PLANNED도 rollback 선택, 역복제·재동기화·승격 설계 |
| AWS→onprem | 모두 false, 미등록/adapter 없음 | 모두 false | 정확한 app/demo DB 선택·복제·네트워크·writer 경로 |
| onprem→AWS | 모두 false, 미등록/adapter 없음 | 모두 false | 기존 AWS 온프레 사본 재사용·승격·앱 기동·재동기화 |
| GCP→onprem | 모두 false, 미등록/adapter 없음 | 모두 false | 복제·반대방향·온프레 안전 접속·writer 경로 |
| onprem→GCP | 모두 false, 미등록/adapter 없음 | 모두 false | 기존 GCP B2 사본 재사용·승격·앱 기동·재동기화 |

## Job·결과 인계 (실행 활성화 아님)

DbCutoverRequest.pair_snapshot과 정적 manifest는 같은 snapshot을 보존한다. KIND는 명시 source/target의 adapter 슬롯으로 정한다. GCP→AWS PLANNED도 rollback이다. env는 기존 KIND/PLAN_ID/ALLOW_PROMOTE에 SOURCE_DB, TARGET_DB, EXPECTED_WRITER_DB, DB_PAIR_CONTRACT_VERSION, DB_PAIR_REGISTRY_SHA256, ORGANIZATION_ID, SERVICE_ID, SOURCE_ENVIRONMENT_ID, TARGET_ENVIRONMENT_ID, SOURCE_DEPLOYMENT_ID, TARGET_DEPLOYMENT_ID를 추가한다. host/command/secret/volume은 추가하지 않는다. 재부착은 PLAN_ID/KIND만이 아니라 전체 env의 정확한 일치·중복 없음을 검사한다.

실제 KubernetesDbCutoverExecutor.start는 pair_snapshot이 있으면 송신 전에 NOT_READY로 거절한다. 독립 PairBoundJobExecutor는 합성 HTTP 전송 시험용 인계 경계이고 build_executor_set/worker에 실행기로 등록하지 않았다. 이를 실제 엔진 연결 완료로 세지 않는다. 실패 시 legacy 3-env 재송신은 없다.

결과 receipt: planId, sourceDbId, targetDbId, contractVersion, registrySha256, writerDbId(=target). actual worker JUDGE→judge_db_cutover가 snapshot과 receipt를 대조한다. ALREADY_WRITER/exit0/정합성 정상 행도 예외가 아니다. 누락·불일치는 DATA_INTEGRITY_FAILED / MANUAL / DB_PAIR_RESULT_MISMATCH이며 자동 되돌림 없음. 행수·누락·중복의 기존 검사도 그대로 수행한다. 실제 reader/엔진은 아직 receipt를 생산하지 않아 성공 경로는 fixture만 검증했다. 계획 상세의 pair/receipt 공개 필드와 Job/run ID의 묶음 표시는 2단계 미완이다.

2단계 소유권 제안(새 담당 지정·요청 아님): WAS 계약 소비는 지민, 엔진·결과표는 데이터 이전 담당 이도현, 이미지/VAP·기존 복제 기반 접점은 서동옥, 화면은 도영. GitHub 이슈의 빈 assignee를 이 문서가 대신 채우지 않는다. 기존 이슈를 재사용하고 운영 적용·새 자원·리허설은 별도 승인이다.

# SPEC — 배포 실패 시 컨테이너 마지막 로그·종료 코드·사유를 남기고, 이전 버전이 없으면 '실행 중인 버전 없음'으로 알린다 (echo-was, API 서버 4-1)

- 상태: **확정(Confirmed)** · 이슈 [#92](https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/92) (발견 이도현 11/6, 담당 김지민, P0)
- 기준: echo-was main `7c53d65`. 표·칸은 그대로(배포 작업 로그 줄 코드만 늘어난다) → **마이그레이션 0**, `alembic heads` 하나 그대로
- 앞 작업: [api-4-history-rollback-logs.md](api-4-history-rollback-logs.md)(배포 작업 로그), [api-3-2-real-executors.md](api-3-2-real-executors.md)(쿠버네티스 직접 반영 `poll`·`revert`)
- 받는 쪽: echo-ai-agent #22(배포 실패 에러 수정 에이전트) — 5절의 `failure` 칸을 입력으로 읽는다. echo-frontend 실패 화면
- 범위 밖: 실행 로그(`source = RUNTIME`) 보존(지금처럼 즉석 조회), 이벤트(`kubectl get events`) 수집, 헬스체크 실패 때의 컨테이너 로그(컨테이너는 살아 있음 — 실행 로그로 본다), 스위치

## 1. 문제

11/6 `shap-api` 배포가 `DATABASE_URL` 누락으로 `CONTAINER_START_FAILED` 3회. 화면·로그에는 "The container kept crashing after it started." 한 줄과 INFO 5줄뿐이고, 실패한 Deployment·ReplicaSet·Pod 는 되돌리기가 곧바로 지워 `kubectl logs --previous` 도 볼 수 없었다. 첫 배포 실패인데도 `REVERT_SUCCEEDED` 가 "이전 버전 유지"라고 안내했다.

## 2. 읽는 때와 대상 (실행기)

- `KubernetesDeployExecutor.poll` 이 `IMAGE_PULL_FAILED`·`CONTAINER_START_FAILED` 를 판정한 **그 호출 안에서**(처리기가 되돌리기를 부르기 전) 판정한 파드·컨테이너 하나를 읽는다.
- 상태: 이미 읽은 파드 목록의 `containerStatuses[]` 에서 `state.waiting.reason`, `state.terminated` 없으면 `lastState.terminated` 의 `reason`·`exitCode`, `restartCount`.
- 로그(`CONTAINER_START_FAILED` 만): `GET /api/v1/namespaces/{ns}/pods/{pod}/log?container=app&tailLines=51&limitBytes=262144`. 재시작했으면(`restartCount > 0` 또는 종료 기록 있음) `previous=true` 먼저, 비면(400·404·빈 글) 지금 컨테이너.
- 상한: 마지막 **50줄**(51번째 줄이 있으면 `log_truncated = true`), 한 줄 **400자**(넘으면 399자 + `…`). 줄 자르기는 실행기가 아니라 처리기가 **가린 뒤에** 한다(3절).
- 실패해도 계속: 로그 읽기가 401·403(권한)·5xx·연결 실패·그 밖 4xx 면 `log_tail = ()` 로 두고 경고 `CONTAINER_LOGS_UNAVAILABLE`. 실패 코드·되돌리기는 그대로.
- 진단 전체 시간 상한(검증 반영): 설정 `container_diagnostics_timeout_seconds`(기본 **10초**, 환경 변수 `CONTAINER_DIAGNOSTICS_TIMEOUT_SECONDS`)가 진단 전체(파드 상태 정리 + 로그 읽기 + 처리기의 환경 변수 재조회)의 절대 상한이다. HTTP 요청마다의 시간 제한은 바이트가 조금씩 오면 늘어나므로 따로 묶는다. 실행기는 시작할 때 마감 시각(`ContainerDiagnostics.deadline`, 이벤트 루프 시각)을 정해 진단에 담고, 처리기는 그 마감 안에서 환경 변수를 다시 조회한다(마감이 없으면 지금 + 설정값). 넘으면 경고 `CONTAINER_DIAGNOSTICS_TIMEOUT` 과 함께 **진단 없이**(`diagnostics = None`) 실패 처리를 계속한다.
- 예외 범위(검증 반영): 진단 읽기·환경 변수 재조회에서 예상하지 못한 예외(`Exception` 전부)도 경고 `CONTAINER_DIAGNOSTICS_FAILED`(예외 이름만, 문장은 비밀값을 담을 수 있어 쓰지 않음)와 함께 진단 없이 계속한다. 배포 실패 처리·되돌리기는 진단 때문에 멈추지 않는다.
- 계약 값: `DeployFailed(code, diagnostics: ContainerDiagnostics | None)` — `diagnostics` 는 비교에서 빠지는 선택 칸(기존 판정 시험 호환). `ContainerDiagnostics{container, reason, termination_reason, exit_code, restart_count, log_tail, log_truncated}`.

## 3. 비밀값 가리기

1. 실행기: `mask_secrets`(GitHub·AWS·`Bearer`·`sk-`·Echo 토큰, 단독 JWT, 개인 키 블록)를 줄로 나누기 전 글 전체에 건다. **줄 길이는 자르지 않는다.**
2. 처리기(`mask_container_log`, 한 곳에서 **가리기 → 자르기** 순서): 그 배포 기록의 앱 환경 변수 값을 다시 꺼내(`AppEnvResolver`) 아래 규칙의 값을 `***` 로 바꾸고(줄을 이어 붙인 글에서 바꿔 여러 줄 값도 맞춘다), 접속 문자열 비밀번호(`scheme://user:***@host`)와 `password|passwd|pwd|secret|token|api_key|access_key` `=`·`:` 뒤 값을 가린 다음 `mask_secrets` 를 한 번 더 걸고, **그 뒤에** 한 줄을 400자로 자른다. 먼저 자르면 잘린 줄 끝의 값 앞부분이 전체 값 비교에 안 맞아 남는다(검증 탐침에서 660자 값 누출 재현).
   - 가릴 환경 변수 값 규칙(검증 반영): 빈 값은 가리지 않는다. **6자 이상 값은 키와 상관없이 모두** 가린다. **6자 미만 값은 키 이름이 민감 키 모양일 때만** 가린다 — `password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|auth|session|cookie|salt|signing|dsn|database[_-]?url|conn(ection)?[_-]?str`(대소문자 무시, 이름 어디에 있어도). `true`·`false`·`8080`·`1` 같은 짧은 설정값까지 가리면 로그 낱말이 모두 지워지는 오탐이 심해서다. 따라서 민감 키가 아닌 이름(`X`·`MODE` 등)의 5자 이하 값은 가리지 않는다.
   - 바이트 상한(`limitBytes`)으로 받은 글의 끝이 잘려 마지막 줄 끝에 값 앞부분만 남을 수 있다. 마지막 줄 끝이 가릴 값의 앞 6자 이상과 같으면 그 부분도 가린다.
   - 실행기의 `mask_secrets` 로 값 일부가 이미 바뀐 꼴(예: 값 안의 GitHub 토큰)도 함께 찾는다.
3. 값을 못 꺼내면(`ENV_SECRET_UNAVAILABLE`·일시 오류) 가릴 수 없으므로 `log_tail` 을 버리고 상태 값만 남긴다(경고 `container log tail withheld`).
4. 응답: 배포 작업 로그·`failure` 칸 모두 응답 직전 `mask_secrets` 를 다시 건다(기존 방어). 단독 JWT(`eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*`)는 `app/core/log_masking.py` 에 더해 모든 로그 응답이 함께 가린다(검증 반영).

## 4. 배포 작업 로그 줄 (저장)

`FAILED` 줄과 같은 트랜잭션에서, 그 **앞에** 쓴다.

| 코드 | 수준 | 단계 | 문장 |
|---|---|---|---|
| `CONTAINER_DIAGNOSTICS` | ERROR | FAILED | `Container app: reason=CrashLoopBackOff, terminationReason=Error, exitCode=1, restartCount=3, logLines=7, truncated=false.` (모르는 값 `-`) |
| `CONTAINER_LOG` × N(≤50) | INFO | FAILED | 가린 로그 한 줄 |
| `FAILED` | ERROR | FAILED | `errorMessage` + ` Last error: <원인 한 줄>`(500자 상한) |
| `REVERT_SUCCEEDED` | INFO | REVERT | 이전 버전이 있었다: "…the previous version was kept." |
| `REVERT_SUCCEEDED_NO_PREVIOUS` | INFO | REVERT | 이전 배포 없음: "The failed version was withdrawn. No version is running in this environment because there was no previous deployment." |

원인 한 줄: 마지막 예외 줄(`…Error: …`·`…Exception: …`, 모듈 경로는 떼어 낸다) → pydantic 설정 오류(`ValidationError: N validation error(s) for …` 뒤 들여쓰지 않은 필드 이름 줄)는 `ValidationError: database_url` 로 줄인다 → 없으면 마지막 `panic:`·`fatal:`·`error:` 줄 → 없으면 마지막 빈 줄 아닌 줄 → 로그가 없으면 `<terminationReason> (exit code N)`(예 `OOMKilled (exit code 137)`). 200자 상한.

요청당 500줄 상한(`LOG_TRUNCATED`)은 그대로 — 진단 줄은 최대 51줄.

## 5. 응답 — `DeployRequestDetail.failure` (더하기만, 에이전트 입력 계약)

```text
failure: {
  code: DeployRequestErrorCode,          # = errorCode
  message: string,                       # errorMessage + " Last error: …" (진단 없으면 errorMessage)
  container: string | null,              # "app"
  containerReason: string | null,        # CrashLoopBackOff · ImagePullBackOff · ErrImagePull · InvalidImageName · ErrImageNeverPull
  terminationReason: string | null,      # OOMKilled · Error · Completed …
  exitCode: integer | null,
  restartCount: integer | null,
  logTail: string[],                     # ≤ 50줄, 가린 값, 진단 없거나 못 읽으면 []
  logTruncated: boolean,
  previousVersionRunning: boolean | null # true 이전 버전 유지 · false 실행 중인 버전 없음 · null 되돌리기 전/실패/빌드 실패
} | null                                 # status != FAILED 면 null
```

- OpenAPI(검증 반영): `DeployRequestDetail.failure` 와 로그 `items[].code` 는 나중에 더한 칸이라 **required 가 아닌 선택 칸**(모델 기본값 `None`)이다 — 옛 생성 클라이언트를 깨지 않는다. 응답 본문에는 `data` 안 칸 관례(`ApiResponse` 설명: 값이 없어도 null 로 늘 싣는다)대로 늘 실린다(`exclude_none` 쓰지 않음).

- 상세(`GET …/deploy-requests/{id}`)만 채운다. 목록 항목에는 없다(요청마다 로그 줄 조회를 피한다).
- 값은 배포 작업 로그 줄(`CONTAINER_DIAGNOSTICS`·`CONTAINER_LOG`·`FAILED`·`REVERT_*`)로 만든다 — 저장 칸 추가 없음.
- 기존 `errorCode`·`errorMessage`(고정 문장)·`revertStatus` 는 그대로.
- 로그 API `items[]` 에 `code`(배포 작업 로그 줄 코드, 빌드·실행 로그는 null)를 더한다 — 화면·에이전트가 `CONTAINER_LOG` 줄을 문장 모양에 기대지 않고 고른다.

에러 수정 에이전트(ai#22)는 `failure.code`·`failure.message`·`failure.logTail`·`failure.exitCode`·`failure.containerReason`·`failure.terminationReason` 을 읽는다. `logTail` 은 이미 가린 값이므로 원문 비밀값을 기대하지 않는다.

## 6. 가짜 실행기

- `FakeDeployExecutor(crash_log_tail=…)` 의 배포 실패는 `DeployFailed(DEPLOY_FAILED, fake_crash_diagnostics(tail))` — 실제와 같은 50줄·가리기 규칙. 기본 꼬리는 필수 변수 누락 7줄(`ValidationError: database_url`).
- `simulate.failAt = DEPLOYING` 도 같은 기본 진단을 쓴다(시연 "배포 실패 → AI 진단"의 입력, `simulated = true`).

## 7. 운영 영향

- 스위치 없음. 병합 = 배포 즉시 실제 실행기의 실패 경로에서 로그를 읽는다(성공 경로 호출 수는 그대로, 실패 판정 때 GET 1~2번 추가).
- 권한: 실행기 서비스 계정에 앱 네임스페이스 `pods/log` `get` 이 필요하다. 온프레 `echo-app-deployer` ClusterRole(echo-infra `onprem/manifests/echo-executor.yaml`)에는 이미 있다. aws(EKS access policy `AmazonEKSClusterAdminPolicy`)·gcp(`roles/container.developer`)도 포함한다. 권한이 없어도 2절대로 실패 처리는 그대로다.
- 마이그레이션 없음.

## 8. 완료 기준

- [x] 필수 변수가 빠진 앱의 실패에서 로그 API·실패 상세에 `ValidationError: database_url` 원인 줄과 종료 코드·사유가 보인다(가짜 실행기 통합 시험, 실제 실행기 단위 시험)
- [x] 남긴 로그에 환경 변수 값·토큰이 그대로 나오지 않는다(마스킹 시험)
- [x] 이전 배포가 없는 첫 배포 실패에서 `REVERT_SUCCEEDED_NO_PREVIOUS`·`previousVersionRunning = false`
- [x] 에이전트 입력 칸 이름·형태가 5절에 있다
- [x] 마이그레이션 없음
- [ ] 운영 클러스터에서 실제 실패 배포로 확인(병합·배포 뒤)

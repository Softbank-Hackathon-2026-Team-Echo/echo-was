# 실행기 계약 (빌드·배포·대표 주소·헬스체크·내리기)

원터치 배포의 흐름·상태·DB 기록은 echo-was 일꾼이 맡고, 외부 시스템을 실제로 움직이는 일은 **실행기**가 맡는다. 이번에는 모두 가짜 구현(`EXECUTOR_MODE=fake`)이고, 인프라 3(Argo CD + 빌드·배포 파이프라인)·인프라 5(고정 진입점 프록시)가 준비되면 **실행기 구현만** 바꾼다. 표·API 계약은 바꾸지 않는 것이 목표다.

- 인터페이스: `app/executors/base.py`(Protocol + 값 객체). 가짜 구현: `app/executors/fake.py`. 실제 HTTP 헬스체커: `app/executors/http_health_checker.py`. 고르는 곳: `app/executors/__init__.py::build_executor_set`.
- 공통 계약 시험: `tests/executor_contract.py`. 실제 구현도 같은 검사를 스텁 전송으로 통과해야 한다.
- 실행기 패키지는 `app.models` 를 import 하지 않는다. 일꾼이 값 객체(`BuildSpec`·`DeploymentSpec`·`DeployTarget`·`MainRoute`·`SourceAccess`)로 바꿔 넘긴다.

## 인터페이스

| 실행기 | 메서드 | 결과 |
|---|---|---|
| `BuildExecutor` | `start(build, source_access, idempotency_key) -> external_ref` | |
| | `poll(external_ref)` | `BuildRunning` · `BuildSucceeded(image_repository, image_digest)` · `BuildFailed(code)` |
| `DeployExecutor` | `apply(deployment, target, idempotency_key) -> external_ref` | |
| | `poll(external_ref)` | `DeployPending` · `DeployApplied` · `DeployFailed(code)` |
| | `revert(target, previous_deployment \| None, idempotency_key)` | `Reverted(external_ref)` · `RevertFailed(code)` |
| `MainRouteExecutor` | `route_main(route, target, idempotency_key)` | `RouteApplied(applied_at)` · `RouteFailed(code)` |
| `HealthChecker` | `probe(url, path, timeout_seconds)` | `HealthOk(status)` · `HealthHttpError(status)` · `HealthUnreachable` |
| `TeardownExecutor` | `remove(target, idempotency_key) -> external_ref` | |
| | `poll(external_ref)` | `TeardownPending` · `TeardownRemoved` · `TeardownFailed(code)` |

모든 실행기는 `simulated` 속성을 가진다(가짜면 true). 결과의 `simulated` 는 요청·빌드 행에 저장된다.

## 멱등 키와 호출 순서 (반드시 지킬 것)

| 외부 작업 | 멱등 키 |
|---|---|
| 빌드 시작 | `builds.id` |
| 배포 반영 | `deployments.id` |
| 되돌리기 | 실패한 `deployments.id` |
| 대표 주소 연결 | 성공한 `deployments.id` |
| 내리기 | `{service_environments.id}:{teardown_attempt}` (서비스 삭제를 다시 보내면 attempt 가 1 늘어 새 키) |

일꾼은 언제든 죽을 수 있다. 그래서 외부 호출은 항상 이 순서다.

1. **요청함 시각을 저장·커밋한다**(`builds.start_requested_at`, `deploy_requests.deploy_requested_at`, `service_environments.teardown_requested_at`).
2. **트랜잭션 밖에서** 실행기를 부른다(멱등 키 포함).
3. 돌아온 `external_ref` 를 저장한다(`builds.external_ref`, `deploy_requests.deploy_external_ref`, `service_environments.teardown_external_ref`).

다시 시작한 일꾼은 요청함 시각이 있고 `external_ref` 가 없으면 **같은 키로 다시 부른다.** 그래서 실제 구현은:

- **같은 멱등 키로 다시 불러도 같은 작업을 돌려줘야 한다**(새 빌드·새 배포를 만들지 않는다). 예: GitOps 커밋이라면 커밋 메시지·브랜치에 키를 넣고, push 전에 이미 있는지 확인한다.
- `external_ref` 는 다른 일꾼 프로세스가 `poll` 해도 같은 답을 받을 수 있는 값이어야 한다(프로세스 메모리에만 있는 번호 금지).
- `poll` 은 상태를 바꾸지 않는 조회다. 일꾼은 실행마다 한 번 부르고, 끝나지 않았으면 `WORKER_POLL_INTERVAL_SECONDS` 뒤 다시 부른다.

## 실패 코드

- 단계마다 정해진 코드로 실패를 돌려준다. **권한 오류를 시간 초과로 뭉개지 않는다**(예선에서 권한 오류가 시간 초과로만 보여 원인을 못 찾았다). 빌드 `BUILD_FAILED`, 배포 `DEPLOY_FAILED`, 내리기 `TEARDOWN_FAILED`. 시간 초과(`BUILD_TIMEOUT`·`DEPLOY_TIMEOUT`·`TEARDOWN_TIMEOUT`)는 일꾼이 단계 시작 시각으로 정한다(빌드 `builds.started_at`, 배포 `deploy_requests.deploying_at`, 내리기 `teardown_requested_at`).
- 실패 코드·`external_ref` 에 **비밀값을 넣지 않는다**(설치 토큰·kubeconfig·레지스트리 비밀번호 등). 응답의 `errorMessage` 는 코드별 고정 문장이라 외부 원문은 사용자에게 나가지 않는다.

## 실행기별 계약

### 빌드 (`BuildExecutor`)

- 입력: `BuildSpec(build_id, service_id, service_name, repo_url, commit_sha)`, `SourceAccess(repo_url, commit_sha, read_token)`. `read_token` 은 비공개 저장소일 때만 있는 GitHub App 설치 토큰(1시간)이며 **메모리로만** 넘긴다. 저장·로그 금지.
- 결과: 이미지 저장소 주소(tag 없음) + `sha256:` digest. 배포는 digest 로만 한다(`latest` 같은 바뀌는 tag 금지).
- 같은 서비스·저장소·커밋의 빌드는 일꾼이 재사용하므로 실행기는 한 커밋에 한 번만 불린다(실패 뒤에는 새 `builds.id` 로 다시).

### 배포 (`DeployExecutor`)

- 입력: `DeploymentSpec(deployment_id, image_repository, image_digest, config_snapshot)`, `DeployTarget(service, service_environment, environment kind, public_host, container_port)`.
- `apply` 는 그 연결(클러스터의 그 서비스)에 digest 이미지를 반영한다. `DeployApplied` 는 "클러스터가 새 버전을 받아들였다"이다. 사용자 트래픽 확인은 일꾼의 헬스체크가 환경별 퍼블릭 주소로 한다.
- `revert(target, previous, key)` 는 실패한 새 버전을 거두고 `previous`(연결의 현재 배포, 없으면 아무것도 없는 상태)를 유지한다. 실패해도 일꾼은 요청의 `errorCode` 를 바꾸지 않고 `revertStatus = FAILED` 만 적는다.
- 연결안(인프라 3): GitOps 저장소에 digest 를 커밋(커밋 SHA 를 push 전에 기록) → Argo CD 동기화 상태를 `poll`.

### 대표 주소 (`MainRouteExecutor`)

- 서비스에 서비스 중 연결이 없을 때, 성공한 연결로 대표 주소(`{service}-{suffix}.{domain}`, 저장한 `services.public_host`)를 보낸다. `RouteApplied(applied_at)` 일 때만 일꾼이 `traffic_weight = 100`, `traffic_applied_at = applied_at` 을 적는다("프록시에 마지막으로 적용한 비율"이라는 칸 뜻을 지킨다).
- 가짜 구현은 I/O 가 없어 성공 처리 트랜잭션 안에서 부른다. 실제 구현(echo-infra#5 고정 진입점 프록시)의 호출 위치(트랜잭션 밖 + 멱등 키)는 실제 구현 작업에서 정한다.

### 헬스체커 (`HealthChecker`)

- `GET {url}{path}` 한 번. 2xx 면 `HealthOk`, 그 밖의 응답(3xx 포함, **리다이렉트 따르지 않음**)은 `HealthHttpError(status)`, 연결 실패·시간 초과는 `HealthUnreachable`.
- `url` 은 **환경별 퍼블릭 주소**다. 대표 주소는 성공 처리 뒤에야 이 환경을 가리키므로 확인하지 않는다(실제 라우터 연결 뒤 추가).
- 실제 구현 `HttpHealthChecker` 는 이미 있다(`HEALTH_CHECKER_MODE=http`). 요청 하나 제한 시간은 `HEALTH_CHECK_REQUEST_TIMEOUT_SECONDS`(5).

### 내리기 (`TeardownExecutor`)

- 서비스 삭제 때 연결 하나를 클러스터에서 내린다(그 서비스의 Deployment·Service·Ingress 등). `TeardownRemoved` 면 일꾼이 연결을 소프트 삭제한다. 배포 기록은 남는다.

## 가짜 구현

| 항목 | 값 |
|---|---|
| 걸리는 시간 | `FAKE_EXECUTOR_STEP_SECONDS`(기본 2.0, 시험 0) 뒤 성공 |
| `external_ref` | `fake-<종류>\|<멱등 키>\|<시작 ms>\|<결과를 정하는 값>` — 일꾼 프로세스가 바뀌어도 같은 답 |
| 빌드 결과 | 이미지 `registry.echo.test/<서비스 이름>`, digest `sha256(서비스 id + 커밋)` |
| 대표 주소 | 늘 적용(`applied_at` = 그 시각) |
| 헬스체크 | 늘 2xx(`HEALTH_CHECKER_MODE=fake`) |
| 실패 주입 | 시험만: 인스턴스를 만들 때 실패·멈춤 대상 커밋·연결을 넣는다(운영 설정에는 없다). 사용자 요청 단위 실패는 `simulate.failAt` |
| 호출 기록 | `calls`(호출마다 멱등 키), `operation_keys`(만든 작업) — 시험이 "같은 키는 같은 작업"을 확인한다 |

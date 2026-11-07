# API 서버 3-2: 실제 빌드·배포·내리기 실행기 SPEC (변경분)

- 상태: Confirmed (2026-11-03, fresh-eyes 검토 10건 반영, AD-6 세부 사용자 확인)
- 작성일: 2026-11-03
- Depth: Deep
- 바꾸는 SPEC: `specs/api-3-one-touch-deploy.md`, `specs/api-3-1-public-host-format.md`, `docs/executors.md`. **다르면 이 문서가 우선한다.**
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/3 (API 서버 3 원터치 배포, In Progress, 담당 Saccharine1211)
- 관련: echo-infra 인프라 3(빌드·배포 파이프라인 계약, 서동옥) https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-infra/issues/3 · 인프라 2(온프레 k3s, 완료) · 인프라 5(고정 진입점)
- 기준: echo-was `main` = `0c6884f`(API 서버 8-2 병합 포함)
- 인터뷰 기록: `.deep-interview/api-3-2-real-executors.state.md`

## 1. 목표

가짜 빌드·배포·내리기 실행기를 실제로 바꿔, 원터치 배포 요청 한 번으로 고객 앱을 **GitHub Actions로 빌드**하고 **AWS EKS · GCP GKE · 온프레 k3s 중 한 곳에 직접 반영**하며, 환경별 퍼블릭 주소의 헬스체크를 통과해야 교체한다. 흐름·상태·표·응답 모양(API 서버 3)은 바꾸지 않는다. 이슈 완료 기준("실제 빌드 → 클라우드·온프레 배포 → 퍼블릭 URL")을 채우는 마지막 작업이다.

## 2. 확인된 사실 (11/3)

### 2.1 저장소
- `app/executors/__init__.py::build_executor_set` — `EXECUTOR_MODE=fake`만 허용(`config.py::_check_executor_mode`가 다른 값 거부). 실제 구현은 `HttpHealthChecker`, `Entrypoint{Traffic,MainRoute,Metrics}`(#17)뿐.
- 실행기 계약 `docs/executors.md`, 값 객체 `app/executors/base.py`. `DeployTarget`에는 환경 접속 정보(config·secret_ref)가 없다. 실행기 패키지는 `app.models`를 import하지 않는다.
- 처리기 `app/services/deploy_request_processor.py`: 빌드 `start(..., idempotency_key=str(build.id))` → `poll`, 배포 `apply(..., key=str(deployment.id))` → `poll`, 실패 뒤 `revert(target, previous, key)`, 헬스는 `config_snapshot["environmentUrl"]` + 경로. 빌드 시작 전 `source_client.issue_read_token`으로 비공개 저장소 토큰을 받아 `SourceAccess.read_token`에 넣는다.
- 내리기 `app/services/service_teardown_processor.py`: `teardown.remove(deploy_target(...), key)` → `poll`.
- `DeployRequestErrorCode`·`TeardownErrorCode`는 DB CHECK가 없다(앱 enum만). 새 코드 추가에 마이그레이션이 필요 없다.
- 환경 설정 양식 `app/adapters/{aws,gcp,onprem}.py`(`extra="forbid"`, frozen): aws `region, cluster_name` / gcp `project_id, region, cluster_name` / onprem `location, kube_context`. 환경 행에 `secret_ref`(선택)가 있다. 어댑터(접속 확인)는 가짜.
- 비밀값 조회 `app/clients/secret_client.py::EnvVarSecretClient` — `env:` 저장소만 읽는다.
- 환경별 주소 `deploy_support.py::connection_public_host` → `{서비스}-{환경}-{suffix}.{PUBLIC_BASE_DOMAIN}`, 연결을 만들 때 한 번 정해 `service_environments.public_host`(살아 있는 행 unique `uq_service_environments_public_host_live`)에 저장. 대표 주소 `{서비스}-{suffix}.{도메인}`.
- 서비스 칸: `repo_url, repo_private, health_check_path, health_check_timeout_seconds, container_port, public_host, host_suffix`. 환경 변수 칸 없음(API 서버 10, 2차).
- 의존성에 `httpx`만 있고 쿠버네티스·AWS·Google SDK는 없다.

### 2.2 인프라 (직접 조회)
- EKS `echo-dev-eks`(도쿄, 1.35): 공개 엔드포인트 `0.0.0.0/0`. 역할 `arn:aws:iam::<ACCOUNT_ID>:role/echo-dev-executor` 존재, Access Entry `AmazonEKSClusterAdminPolicy`(cluster). 신뢰 = 관제탑 GSA의 Google ID 토큰(`accounts.google.com:sub`). IngressClass `alb`(LBC 설치), 고객 앱 없음.
- GKE `echo-dev-gke`(서울 **존** `asia-northeast3-a`): 관제탑 GSA `echo-dev-echo-was@team-echo-final.iam.gserviceaccount.com`에 `roles/container.developer`. 공유 인그레스 컨트롤러 없음. Gateway API 꺼짐. 일꾼이 이 클러스터 `echo-was` 네임스페이스에서 돈다(Workload Identity).
- Artifact Registry `echo` 저장소 IAM: `echo-dev-ci-build`(writer)뿐 — 관제탑 GSA 읽기 권한 없음.
- 온프레: AWS 서울 EC2 `i-051fe56358de1235a` + k3s v1.36 + Cloudflare 터널 `echo-onprem-demo`(id `c446e0fe-…`, 모든 주소 → Traefik), 들어오는 포트 0.
- GitHub App `echoport-deploy`(설치 id `177714197`, selected): 권한 contents·members·metadata 읽기뿐.
- GitHub REST "Create a workflow dispatch event": 본문에 `return_run_details: true`를 넣으면 **200 + `workflow_run_id`**(changelog 2026-02-19). API 버전 `2026-03-10`은 항상 200, 그 전 버전은 파라미터가 없으면 204. 실행 객체의 `run-name`은 `name`이 아니라 **`display_title`** 칸이다.
- GKE 노드 SA `echo-dev-gke-node`에 프로젝트 `roles/artifactregistry.reader` 있음(11/3 확인) → GKE는 AR 이미지를 그대로 받는다.
- 진입점 Envoy: 업스트림 `aws`·`gcp` 두 갈래(`host:port`, HTTP), Host 헤더를 그대로 넘긴다.

### 2.3 스파이크 (11/3, Cloudflare 터널로 k3s 창구)
터널 설정에 `hostname: k3s-k2f6xje5.echoport.uk → https://kubernetes.default.svc:443` 추가 후 M1에서: 토큰 없음 401, 조회·서버 측 apply(Deployment·Service·Ingress)·재적용 멱등·rollout watch·로그(tail·follow·`timestamps`) 성공, 없는 이미지는 12초 안에 `ErrImagePull`, 다른 네임스페이스 Secret 403. 새 연결 1~3초(가끔 6~10초), 연결 재사용 0.5초. 시험 뒤 원복(DNS CNAME만 남김). 기록 `archive/api-3/spike-k3s-tunnel/`.

### 2.4 예선(iris) 근거
운영 검증: AWS 공유 ALB 그룹 + 닻 규칙, 온프레 배포 권한 표(네임스페이스 만들기 + 앱 자원 쓰기, 권한 변경·exec·Secret 쓰기 금지), 온프레 이미지 받기 자격 주기 갱신(첫 배포가 자격 누락으로 ImagePullBackOff), GitHub App 설치 토큰. 미검증: GCP Gateway, Sealed Secrets 변수.

## 3. 범위

### 3.1 이번에 한다 (echo-was)
1. 실제 빌드 실행기 `GithubActionsBuildExecutor` (5.3)
2. 실제 배포·내리기 실행기 `KubernetesDeployExecutor`·`KubernetesTeardownExecutor` + 클러스터 접속기 3종 (5.4~5.6)
3. 환경별 주소 형식 변경(클라우드는 환경별 하위 도메인) (5.2)
4. 온프레 앱 주소 DNS 레코드(Cloudflare) 만들기·지우기 (5.7)
5. 온프레 이미지 받기 자격 갱신 루프 (5.8)
6. 설정·모드 전환(`EXECUTOR_MODE=real`), 문서(`docs/executors.md`, `docs/api/*`), 시험

### 3.2 하지 않는다
- GitOps(Argo CD) — 2단계(11/3 결정).
- 진입점에 온프레 갈래 추가, 대표 주소가 온프레를 가리키는 일(시연 첫 배포는 AWS).
- 고객이 화면에서 환경 변수를 넣는 기능(API 서버 10, 2차).
- 고객 클라우드 계정 연결 안내·자동 공유 문 설치·고객별 터널 생성(2차). 단 환경 설정 양식은 고객 계정도 받을 수 있는 모양으로 둔다(5.1).
- 환경 접속 확인 어댑터(`check_connection`)의 실제 구현 — 별도(API 서버 12 등). 이번 접속기는 실행기 안에서만 쓴다.

### 3.3 이 저장소 밖 준비 (7절 표, 담당 따로)
빌드 워크플로 본문(서동옥), 공유 문·인증서·와일드카드 DNS, 온프레 설치 스크립트 갱신(echo-infra, 지민), AR 읽기 권한·진입점 업스트림(서동옥), GitHub App 권한·Cloudflare 토큰(지민 클릭), 환경별 DB·`*-env` Secret(데이터 파트·지민).

## 4. 결정

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | 고객 앱 빌드 워크플로는 **echo-was 저장소**에 둔다(인프라 변경 없이 기존 OIDC 신원 사용) | 사용자 R2 |
| D-2 | 워크플로 본문은 동옥님께 계약(5.3)을 인프라 3 이슈 댓글로 요청. 실행기는 계약대로 먼저 만들고 가짜 GitHub로 시험 | 사용자 R3 |
| D-3 | 고객 앱 비밀값 = 환경 담당이 앱 네임스페이스에 **미리 둔 Secret**을 실행기가 `envFrom`(optional)으로 읽게만 한다. 화면 입력은 2차 | 사용자 R3 |
| D-4 | 환경별 주소: **AWS·GCP는 환경별 하위 도메인**(`{서비스}-{suffix}.{환경 하위 도메인}`), **온프레는 지금 한 단계 형식**(`{서비스}-{환경}-{suffix}.{도메인}`). 유료 인증서 사지 않음. 대표 주소는 그대로(진입점 경유, 이전 중에도 같은 주소) | 사용자 R3·R5 |
| D-5 | 온프레 연결 = 기존 Cloudflare 터널에 k3s 창구 주소 추가(스파이크 통과) | 사용자 R4 |
| AD-1 | 클러스터마다 공유 문 하나: AWS ALB 그룹 + 닻 규칙(모르는 주소 404), GCP ingress-nginx 1개, 온프레 Traefik | 사용자 C0 |
| AD-2 | 고객 앱 네임스페이스 = `{서비스}-{suffix}`(대표 주소 레이블). 실행기는 이 네임스페이스 안 자원만 다룬다 | 사용자 C0 |
| AD-3 | 권한: 온프레는 예선 권한 표 + 앱 네임스페이스 안 Secret 쓰기, 읽기도 앱 네임스페이스로 좁힘(5.6). GKE·EKS는 기존 넓은 신원을 쓰되 코드에서 같은 범위로 제한하고 설계 문서에 밝힘 | 사용자 C0 |
| AD-4 | 토큰: k3s 만료 없는 서비스 계정 토큰, GKE·EKS는 만료 5분 전 자동 재발급 | 사용자 C0 |
| AD-5 | 반영 = 서버 측 apply, 이름표에 배포 기록 번호. 반영됨 = 새 파드 준비 완료. 이미지 못 받음 = `IMAGE_PULL_FAILED` 즉시. 되돌리기 = 직전 digest로 다시 apply | 사용자 C0 |
| AD-6 | 온프레 이미지 받기 자격: 배포 때 + 5분마다 확인해 남은 수명 30분 미만이면 갱신(5.8), 관제탑 GSA AR 읽기 권한은 동옥님께 요청 | 사용자 C0, 세부 사용자 R7 |
| AD-7 | 로그: 빌드 = Actions 실행 로그, 실행 = 네임스페이스 파드 로그 | 사용자 C0 |
| AD-8 | `EXECUTOR_MODE=real`이 빌드·배포·내리기를 함께 바꾼다. 운영 기본값은 AC-9 확인 전까지 `fake` | 사용자 C0 |
| AD-9 | GitOps·진입점 온프레 갈래는 범위 밖 | 사용자 C0 |
| AD-10 | 환경 설정 양식에 접속 정보를 **선택 칸**으로 더한다(5.1). 고객 계정도 같은 칸으로 받을 수 있는 모양(R3 설명에서 밝힘) | AI-default |
| AD-11 | 쿠버네티스 호출은 SDK 없이 `httpx`로 REST를 직접 부른다(서버 측 apply = `PATCH application/apply-patch+yaml`, field manager `echo-was`). 시험은 httpx MockTransport. 클라이언트는 환경마다 재사용(keep-alive, 스파이크 근거) | AI-default |
| AD-12 | AWS 서명(STS·EKS·presign)은 `botocore`만 더해 쓴다(`boto3` 불필요). Google 토큰은 메타데이터 서버를 `httpx`로 직접 부른다(`google-auth` 불필요) | AI-default |
| AD-13 | 이미지 정본 주소 = Artifact Registry. EKS 반영 때 설정의 미러 표로 ECR 주소로 바꾼다(같은 digest — 동옥 계약) | AI-default |
| AD-14 | 새 실패 코드: `BUILD_DISPATCH_FAILED`(워크플로를 시작하지 못함: 권한·워크플로 없음), `CLUSTER_UNAUTHORIZED`(창구 401·403·자격 발급 실패), `IMAGE_PULL_FAILED`, `CONTAINER_START_FAILED`(CrashLoopBackOff 등). 교훈 L1 | AI-default |
| AD-15 | 고객 앱 Secret 이름 = `{네임스페이스}-env`(R3 설명 예와 같음) | AI-default |
| AD-16 | 온프레 앱 DNS 레코드는 실행기가 첫 반영 때 Cloudflare API로 만든다(CNAME → `<터널 id>.cfargotunnel.com`, 프록시 켬). 클라우드는 환경별 와일드카드 레코드 1개를 사람이 미리 만든다(7절) | AI-default |

## 5. 계약 변경

### 5.1 환경 설정 양식 (API 서버 2 계약, 모두 선택 칸 — 기존 행·요청 그대로 유효)

| 종류 | 더하는 칸(API camelCase) | 뜻 | 데모 값 |
|---|---|---|---|
| aws | `roleArn` | 실행기가 넘겨받을 IAM 역할. 비면 설정 `EXECUTOR_AWS_ROLE_ARN` | (비움) |
| aws | `ingressDomain` | 이 환경의 하위 도메인. 있으면 환경별 주소가 `{서비스}-{suffix}.{ingressDomain}` | `aws-tokyo.echoport.uk` |
| aws | `ingressGroup` | ALB 그룹 이름 | `echo-apps` |
| aws | `certificateArn` | ALB HTTPS 인증서(ACM). 명시하지 않으면 LBC가 대표 주소용 인증서까지 찾다가 그룹 전체 반영이 실패한다 | `*.aws-tokyo.echoport.uk` ACM ARN |
| gcp | `clusterLocation` | GKE 위치(존 또는 리전). 비면 `region` | `asia-northeast3-a` |
| gcp | `ingressDomain` | 위와 같음 | `gcp-seoul.echoport.uk` |
| onprem | `apiUrl` | k3s 창구 공개 주소(터널) | `https://k3s-k2f6xje5.echoport.uk` |
| onprem | `tunnelTarget` | 앱 주소 CNAME 대상 | `c446e0fe-….cfargotunnel.com` |
| 모두 | `ingressClass` | 비면 aws `alb`, gcp `nginx`, onprem `traefik` | — |

- 온프레 창구 토큰은 환경 행의 기존 `secret_ref`(`env:ECHO_SECRET_…`)로 가리킨다. aws·gcp는 `secret_ref`를 쓰지 않는다(워크로드 신원).
- `ingressDomain` 검사: 양식은 도메인 모양만 본다. `PUBLIC_BASE_DOMAIN`의 **바로 아래 한 단계**인지는 `EnvironmentService`(생성·수정)가 설정값으로 검사한다(양식은 Settings에 닿지 않는다). 위반은 422 `ENVIRONMENT_CONFIG_INVALID`(`field=config.ingressDomain`). 온프레 종류에는 칸이 없다(D-4).
- **저장·응답은 `exclude_none=True`**: 환경 config를 저장하는 세 곳(`app/models/environment.py`의 before_insert, `environment_service.py` 생성·수정)과 응답(`app/schemas/environment.py`)에서 값이 없는 새 칸을 키로 남기지 않는다. 그래야 새 칸을 쓰지 않는 환경 행은 지금과 글자까지 같고, 롤링 중 옛 파드가 읽어도 `extra=forbid`에 걸리지 않는다. 스키마 키 집합을 비교하는 시험(`tests/test_adapters.py`, `tests/integration/test_environment_api.py`의 스키마 응답)은 새 선택 칸을 더해 고친다.
- 실제 모드에서 반영 때 필요한 칸이 비어 있으면(onprem `apiUrl`·`secret_ref`·`tunnelTarget`) `apply`가 `DeployApplyError(DEPLOY_FAILED)`를 던지고 경고 로그 `ENVIRONMENT_NOT_CONFIGURED`. 접수 단계에서 막지 않는다(가짜 모드 시험 유지).

### 5.1-b 값 객체 변경 (`app/executors/base.py`)

`DeployTarget`에 칸을 더한다(기존 호출부가 깨지지 않게 모두 기본값):

| 칸 | 타입 | 채우는 곳(`deploy_support.deploy_target`) |
|---|---|---|
| `environment_name` | `str = ""` | `environment.name` (`ECHO_ENVIRONMENT`) |
| `environment_config` | `EnvironmentConfig \| None = None` | `environment.parsed_config()` — `app.adapters`는 `app.models`를 import하지 않으므로 의존 방향 문제 없음 |
| `environment_secret_ref` | `str \| None = None` | `environment.secret_ref` |
| `main_public_host` | `str = ""` | `service.public_host` (Ingress 두 번째 규칙) |

- 기존 `public_host`는 그대로 **환경별 주소**. 네임스페이스 = `main_public_host`의 첫 레이블(`{서비스}-{suffix}`).
- 헬스 경로는 `DeploymentSpec.config_snapshot["healthCheckPath"]`에서 읽는다(되돌리기의 previous도 자기 사본을 쓴다).
- 새 예외(같은 파일): `BuildStartError(code: DeployRequestErrorCode)`, `DeployApplyError(code: DeployRequestErrorCode)`. 실행기가 "다시 불러도 소용없는 실패"를 알릴 때만 던진다.
- 공유 계약 모듈 `tests/executor_contract.py`의 `TARGET`·`DEPLOYMENT` 상수에 새 칸과 `healthCheckPath`를 더한다(가짜 실행기 시험도 같이 돈다).

### 5.2 환경별 주소 형식 (API 서버 3-1 변경)

`connection_public_host(service, environment, …)`:
- 환경 config에 `ingress_domain`이 있으면 → `{public_host_label(서비스 이름, suffix)}.{ingress_domain}` (예 `demo-ab12cd34.gcp-seoul.echoport.uk`)
- 없으면 → 지금 그대로 `{서비스}-{환경}-{suffix}.{PUBLIC_BASE_DOMAIN}` (온프레, 기존 환경)
- 대표 주소·`host_suffix`·63자 자르기·unique·409 `PUBLIC_HOST_CONFLICT` 규칙은 그대로.
- **이미 만든 연결 행은 바꾸지 않는다**(주소는 만들 때 한 번 정해 저장). 환경에 `ingressDomain`을 나중에 더하면 그 뒤 새 연결부터 적용.

### 5.3 빌드 실행기 — GitHub Actions 계약 (동옥님께 요청할 내용)

워크플로 `echo-was/.github/workflows/customer-build.yml`:

| 항목 | 값 |
|---|---|
| 시작 | `on: workflow_dispatch`, ref `main` |
| 입력 | `build_id`(uuid), `repo`(`owner/name`), `commit_sha`(40자), `private`(`"true"`/`"false"`) |
| 실행 이름 | `run-name: build ${{ inputs.build_id }}` — 일꾼이 재시작 뒤 같은 빌드를 찾는 열쇠 |
| 동시 실행 | `concurrency: customer-build-${{ inputs.build_id }}` |
| 소스 | 비공개면 워크플로 안에서 `actions/create-github-app-token`(App id·개인키 = Actions secret, `owner`·`repositories`=고객 저장소)로 1시간 읽기 토큰 → 그 커밋 checkout. **토큰을 입력값으로 받지 않는다**(실행 화면에 보임) |
| 빌드·게시 | 계약 §1 그대로(ECR `echo/app` + AR `echo/app`, 같은 digest). 태그 `b-<build_id 앞 12자>` |
| 결과 | 아티팩트 `build-result`(파일 `build-result.json`): `{"build_id","image_digest":"sha256:…","ar_repository","ecr_repository"}` |
| 실패 | 단계 이름을 고정(`checkout`, `build`, `push`)해 로그에서 단계가 보이게 |

실행기 동작:
- `start(build, source, key)`:
  1. GitHub App 설치 토큰(echo-was 저장소, `actions:write`) 발급 → 같은 키의 실행이 있나 찾는다: `GET /repos/{빌드 저장소}/actions/workflows/{워크플로}/runs?event=workflow_dispatch&branch=main&per_page=50`에서 **`display_title == "build {key}"`**. 있으면 그 run id.
  2. 없으면 `POST …/dispatches` 본문 `{"ref", "inputs", "return_run_details": true}` → 200 `workflow_run_id`. (204가 오면 1의 찾기를 5초 간격 3번 다시 해 run id를 얻고, 끝내 못 찾으면 예외.) 404·403·422 → `BuildStartError(BUILD_DISPATCH_FAILED)`(처리기가 빌드를 그 코드로 닫는다), 5xx·429·연결 실패 → 실행기 안에서 1·2·4초 간격 3번 재시도 뒤 예외.
  3. `external_ref = "gha:{run_id}"`.
  - **중복 실행 한계**: 일꾼이 dispatch 성공 직후 ref를 저장하기 전에 죽으면, 재시작 때 1의 목록에 새 실행이 아직 안 보여 두 번째 dispatch가 나갈 수 있다. 재시작 경로(`start_requested_at`이 있고 ref 없음)에서는 1의 찾기를 5초 간격 3번(`created ≥ start_requested_at − 60초`) 해 본 뒤에만 dispatch한다. 그래도 중복이 생기면 결과는 ref가 가리키는 실행만 쓰고 다른 실행은 버린다(같은 커밋·같은 태그라 해가 없다).
  - `SourceAccess.read_token`은 쓰지 않는다(워크플로가 직접 받는다). 처리기는 비공개 저장소 접근 확인(설치 여부)을 위해 지금처럼 `issue_read_token`을 부르되 실행기에 넘긴 토큰을 저장·로그하지 않는다.
- `poll(ref)`: `GET …/actions/runs/{id}` — `queued|in_progress|waiting|pending` → `BuildRunning`, `completed`+`success` → 아티팩트 `build-result` 내려받아 `BuildSucceeded(image_repository=ar_repository, image_digest)`, `completed`+그 밖 → `BuildFailed(BUILD_FAILED)`. 아티팩트가 없거나 모양이 다르면 `BuildFailed(BUILD_FAILED)` + 경고 로그 `BUILD_RESULT_INVALID`. digest는 `^sha256:[0-9a-f]{64}$` 검사.
- `read_logs(ref, cursor, limit)`: 실행의 잡 로그(`GET …/actions/jobs/{job_id}/logs`)를 줄 단위로 합쳐 시간 순, cursor = `"{job 순번}:{줄 번호}"`. 실행이 끝나기 전에는 빈 청크(`has_more=True`, 같은 cursor). 마스킹은 기존 `mask_secrets` + Actions가 이미 가린 값.

### 5.4 배포 실행기 — 쿠버네티스 직접 반영

반영 대상(한 연결 = 한 네임스페이스 `{서비스}-{suffix}`, 라벨 `app.kubernetes.io/managed-by=echo-was`, `echo.service-id`):

| 객체 | 이름 | 내용 |
|---|---|---|
| Namespace | `{서비스}-{suffix}` | 없으면 만든다 |
| Deployment | `app` | replicas 1, 이미지 `{환경별 저장소}@{digest}`, 포트 `containerPort`, env `PORT`·`ECHO_ENVIRONMENT=<환경 이름>`(덮어쓰기 불가 — `env`가 `envFrom`보다 우선), `envFrom: secretRef {ns}-env optional`, readinessProbe `GET {health_check_path}`, 어노테이션 `echo.deployment-id`, 온프레만 `imagePullSecrets: echo-pull`, 자원 요청 100m/128Mi·제한 512Mi |
| Service | `app` | 80 → containerPort |
| Ingress | `app` | 규칙 2개: **환경별 주소**와 **대표 주소** 둘 다 → `app:80`. class = 환경 `ingressClass`. AWS: `alb.ingress.kubernetes.io/group.name: <ingressGroup>`, `listen-ports [{"HTTP":80},{"HTTPS":443}]`, **`certificate-arn: <certificateArn>`(명시 — 자동 찾기 금지)**, `target-type: ip`, `healthcheck-path` = 헬스 경로(80→443 강제 이동 없음 — 진입점이 HTTP로 대표 주소를 보낸다). `certificateArn`이 비면 `DeployApplyError(DEPLOY_FAILED)` |

- 저장소 주소: `deployment.image_repository`(AR 정본). aws 환경이면 설정 `IMAGE_REGISTRY_MIRRORS`(`<AR 접두>=<ECR 접두>`)로 바꾼다. 표에 없으면 그대로.
- `apply(deployment, target, key)`: 위 4객체를 서버 측 apply(`fieldManager=echo-was`, `force=true`). 같은 키 다시 → 같은 내용이라 같은 결과. `external_ref = "k8s:{environment_id}:{namespace}:{deployment_id}"`. 401·403 → `DeployApplyError(CLUSTER_UNAUTHORIZED)`(처리기가 그 코드로 실패) — 단 온프레에서 RoleBinding을 막 만든 직후 10초 동안의 403은 2초 간격으로 다시 시도한다(권한 캐시 반영 지연). 연결 실패·5xx·429 → 실행기 안에서 3번 재시도 뒤 예외.
- `poll(ref)`: Deployment `app`을 읽어
  - 어노테이션의 배포 번호가 ref와 다르면 `DeployFailed(DEPLOY_FAILED)`(그사이 다른 반영이 덮음)
  - `observedGeneration ≥ generation` 그리고 `updatedReplicas == availableReplicas == replicas` → `DeployApplied`
  - 그 배포 번호 파드 중 대기 이유가 `ImagePullBackOff`이거나 `ErrImagePull`이 `DEPLOY_IMAGE_PULL_GRACE_SECONDS`(기본 30) 넘게 이어짐 → `DeployFailed(IMAGE_PULL_FAILED)`
  - `CrashLoopBackOff` → `DeployFailed(CONTAINER_START_FAILED)`
  - 그 밖 → `DeployPending`. 401·403 → `DeployFailed(CLUSTER_UNAUTHORIZED)`. 연결 실패 → `DeployPending`(일꾼의 `DEPLOY_TIMEOUT`이 끝낸다) + 경고 로그 `CLUSTER_UNREACHABLE`.
- `revert(target, previous, key)`: previous가 있으면 그 배포 기록으로 같은 apply(어노테이션 = previous 번호) → `Reverted("k8s-revert:…")`. 없으면 Deployment·Service·Ingress를 지운다(네임스페이스·Secret은 남긴다). 실패 → `RevertFailed(코드)`.
- `read_runtime_logs`: 네임스페이스에서 라벨 `echo.deployment-id=<번호>` 파드의 `app` 컨테이너 로그(`timestamps=true`). cursor = `"{파드 이름}|{마지막 줄 시각}|{같은 시각 줄 수}"`. 파드가 없으면 빈 청크.

### 5.5 내리기 실행기
- `remove(target, key)`: 네임스페이스 `{서비스}-{suffix}` 삭제 요청(없으면 성공) + 온프레면 앱 DNS 레코드 삭제(5.7). `external_ref = "k8s-teardown:{environment_id}:{namespace}:{key}"`(키를 넣어 attempt마다 다른 ref — 계약 시험 `other_attempt != first`). 설정 칸이 비어 있으면 호출 없이 `"k8s-teardown-invalid:{key}"`를 돌려주고 poll이 `TeardownFailed(TEARDOWN_FAILED)`(처리기 변경 없음).
- `poll`: 네임스페이스 404 → `TeardownRemoved`, 있음 → `TeardownPending`, 401·403 → `TeardownFailed(TEARDOWN_FAILED)` + 경고 로그 `CLUSTER_UNAUTHORIZED`, 연결 실패·5xx → `TeardownPending` + 경고(`TEARDOWN_TIMEOUT`이 끝낸다).

### 5.5-b 일시 오류 처리 원칙 (모든 실제 실행기)
- **poll 계열**(빌드·배포·내리기): 연결 실패·5xx·429 → Running/Pending + 경고 로그(코드 `*_UNREACHABLE`). 기존 단계 제한 시간이 끝낸다. 예외를 올리지 않는다.
- **start 계열**(start·apply·remove): 실행기 안에서 1·2·4초 3번 재시도 뒤에만 예외. 처리기 밖으로 나간 예외는 runner가 삼키고 임대 만료 뒤 다시 집히며 `lease_losses`가 쌓여 **3번이면 WORKER_LOST**로 끝난다(지금 규칙) — 그래서 poll에서는 예외를 올리지 않는다.

### 5.6 클러스터 접속기 (환경 종류별)

| 종류 | 창구 주소·CA | 토큰 | 캐시 |
|---|---|---|---|
| gcp | 메타데이터 서버 액세스 토큰으로 `GET container.googleapis.com/v1/projects/{p}/locations/{clusterLocation or region}/clusters/{name}` → `endpoint`, `masterAuth.clusterCaCertificate` | 메타데이터 서버 `…/service-accounts/default/token`(GSA, 1시간) | 만료 5분 전까지 재사용 |
| aws | STS 임시 키로 `eks:DescribeCluster` → endpoint·CA | ① 메타데이터 `…/identity?audience=sts.amazonaws.com&format=full` ② `AssumeRoleWithWebIdentity(roleArn, 세션 이름 echo-executor)` ③ presigned `GetCallerIdentity`(헤더 `x-k8s-aws-id: <cluster>`, 60초 유효 URL) → `k8s-aws-v1.` + base64url(패딩 없음) | 임시 키 1시간, 클러스터 토큰 10분 |
| onprem | config `apiUrl`(공개 인증서) | 환경 `secret_ref` → 서비스 계정 토큰(만료 없음) | 프로세스 동안 |

- 실행기는 앱 네임스페이스(`app.kubernetes.io/managed-by=echo-was` 라벨)가 아닌 곳을 쓰지 않는다(이름 규칙으로 보장 + 시험).
- 온프레 서비스 계정 권한(echo-infra 설치 스크립트, 7절): ClusterRole `echo-executor` = namespaces get·create·delete(라벨 붙은 것만 지우는 건 코드 몫), rolebindings create(앱 네임스페이스), **bind는 ClusterRole `echo-app-deployer`에만**(`resourceNames`). `echo-app-deployer` = deployments·services·ingresses·secrets·pods·pods/log·events·replicasets 읽기·쓰기. 실행기는 네임스페이스를 만든 직후 그 안에 RoleBinding(`echo-app-deployer` → 자기 SA)을 만든다. 권한 변경·exec 없음. 새 네임스페이스는 실행 중에 생기므로 RoleBinding 만들기 권한은 클러스터 전체에 줄 수밖에 없다 → 설치 스크립트에 **ValidatingAdmissionPolicy**(k3s 1.36)를 더해 이 SA의 rolebindings 생성·namespaces 삭제를 `app.kubernetes.io/managed-by=echo-was` 라벨이 붙은 네임스페이스로만 허용한다(교훈 L7을 RBAC 밖에서 보장, P5 범위). 정책이 없으면 "코드로 제한"이라고 설계 문서에 밝힌다.
- 요청 하나 제한 시간 `KUBE_REQUEST_TIMEOUT_SECONDS`(기본 15). 접속 실패 처리는 5.5-b 원칙을 따른다.

### 5.7 온프레 앱 DNS 레코드 (Cloudflare)
- `apply` 전에(온프레만) `{환경별 주소}` CNAME → `tunnelTarget`, proxied, comment `echo-was:{service_environment_id}`. 이미 같은 값이면 그대로(멱등), 다른 값의 레코드가 있으면 `apply`가 `DeployApplyError(DEPLOY_FAILED)` + 경고 `DNS_RECORD_CONFLICT`(남의 레코드를 덮지 않는다).
- 내리기 때 comment가 같은 레코드만 지운다.
- 설정: `CLOUDFLARE_API_TOKEN_REF`(`env:ECHO_SECRET_CLOUDFLARE_DNS_TOKEN`, 권한 = echoport.uk DNS 편집만), `CLOUDFLARE_ZONE_ID`.

### 5.8 온프레 이미지 받기 자격
- `apply` 때(온프레만) 메타데이터 서버 액세스 토큰(scope `cloud-platform`)으로 dockerconfigjson Secret `echo-pull`(사용자 `oauth2accesstoken`, 서버 = AR 호스트)을 그 네임스페이스에 apply.
- 일꾼 프로세스에 갱신 루프: `ONPREM_PULL_CHECK_SECONDS`(기본 300)마다 확인하고, 마지막으로 넣은 토큰의 남은 수명(토큰 응답 `expires_in` 기준, Secret 어노테이션 `echo.pull-expires-at`)이 `ONPREM_PULL_MIN_REMAINING_SECONDS`(기본 1800) 미만이면, 현재 배포가 있는 살아 있는 온프레 연결의 네임스페이스에 새 토큰으로 다시 apply. 메타데이터 서버가 주는 토큰은 이미 일부 수명을 쓴 캐시 토큰일 수 있어 고정 30분 주기로는 빈 구간이 생기기 때문(검토 반영 — AD-6의 "30분마다"를 "5분마다 확인, 남은 수명 30분 미만이면 갱신"으로 구체화). 실패는 경고 로그만. 일꾼이 여럿이면 중복 실행되어도 결과가 같다.

### 5.9 설정 (`app/core/config.py`)

| 이름 | 기본 | 뜻 |
|---|---|---|
| `EXECUTOR_MODE` | `fake` | `real` 허용. `real`이면 아래 필수값 검사 |
| `EXECUTOR_AWS_ROLE_ARN` | 없음 | aws 환경 기본 역할 |
| `BUILD_GITHUB_REPOSITORY` | `Softbank-Hackathon-2026-Team-Echo/echo-was` | 빌드 워크플로 저장소 |
| `BUILD_GITHUB_WORKFLOW` | `customer-build.yml` | |
| `BUILD_GITHUB_REF` | `main` | |
| `IMAGE_REGISTRY_MIRRORS` | 빈 목록 | `asia-northeast3-docker.pkg.dev/team-echo-final/echo=<ACCOUNT_ID>.dkr.ecr.ap-northeast-1.amazonaws.com/echo` |
| `CLOUDFLARE_API_TOKEN_REF`, `CLOUDFLARE_ZONE_ID` | 없음 | 5.7 |
| `ONPREM_PULL_CHECK_SECONDS` / `ONPREM_PULL_MIN_REMAINING_SECONDS` | 300 / 1800 | 5.8 |
| `DEPLOY_IMAGE_PULL_GRACE_SECONDS` | 30 | 5.4 |
| `KUBE_REQUEST_TIMEOUT_SECONDS` | 15 | 5.6 |
| `GOOGLE_METADATA_BASE_URL` | `http://metadata.google.internal` | 시험에서 바꾼다 |

- `real` 필수: GitHub App(`github_app_id`·개인키 참조), `SOURCE_CLIENT_MODE=github`, `HEALTH_CHECKER_MODE=http`. 하나라도 빠지면 시작 때 설정 오류.
- 가짜 모드의 `simulate`는 지금처럼 `EXECUTOR_MODE=fake`일 때만 받는다(실제 모드 422 그대로).
- 로그 API(`app/dependencies.py::get_executor_set`)는 `real`이면 실제 실행기를 쓴다(지금은 `fake`가 아니면 None). **실제 실행기 생성은 부작용이 없다**: 비밀값·토큰 조회와 필수값 확인은 처음 호출할 때 한다. API 프로세스의 실행기 묶음은 **프로세스당 하나**(앱 lifespan에서 만들어 `app.state`에 두고 종료 때 httpx 클라이언트를 닫음), 요청마다 새로 만들지 않는다. 일꾼도 프로세스당 하나.
- 바꿔야 할 기존 시험: `tests/test_fake_executors.py`의 real 모드 `NotImplementedError` 기대 시험(real 허용으로 바뀜), `tests/integration/test_deploy_logs.py`의 `test_simulated_comes_from_rows_not_settings`(real 실행기를 부작용 없이 만들 수 있어야 통과 — 기대값·주석 갱신).

### 5.10 실패 코드·로그 문구
- `DeployRequestErrorCode` += `BUILD_DISPATCH_FAILED`, `CLUSTER_UNAUTHORIZED`, `IMAGE_PULL_FAILED`, `CONTAINER_START_FAILED`. `deploy_log_messages.py`에 영어 고정 문장. `docs/api/services.md`(오류 코드 표)·프론트 계약 이슈에 새 코드 알림.
- 처리기(`deploy_request_processor.py`): `_start_build`의 except(지금 `RepoNotAccessibleError`·`SourceProviderUnavailableError`)에 `BuildStartError`를 더해 `_close_build(build.id, exc.code)` — 다음 실행에서 요청이 그 코드로 실패한다. `_deploy`의 `deploy.apply` 호출을 try로 감싸 `DeployApplyError`면 `return await self._fail(exc.code)`(배포 기록이 있으므로 되돌리기 호출). 그 밖의 흐름·상태 전환은 그대로. 두 예외를 던지는 가짜 실행기로 처리기 통합 시험을 더한다(AC-2·AC-3).

## 6. 호환성
- 롤링 배포 중 **옛 파드가 새 실패 코드 행을 읽으면** `DeployRequestErrorCode(value)`가 `ValueError` → 500. 새 코드는 `EXECUTOR_MODE=real`에서만 생기므로, 이 PR 배포가 끝난 뒤 모드를 바꾼다(운영 절차로 보장, 6번째 줄 참고).
- **옛 파드가 새 config 칸이 있는 환경을 읽으면** `extra=forbid`로 검증 실패. 저장을 `exclude_none`으로 하므로(5.1) 새 칸은 **값을 넣은 환경**에만 생기고, 그런 환경은 이 PR 배포가 끝난 뒤 등록·수정한다(같은 운영 절차).
- 기존 연결 행 주소는 바꾸지 않는다(5.2). 운영의 기존 서비스는 가짜 모드에서 만든 `simulated` 기록이다. 실제 모드 전환 전에 데모용 서비스·환경을 새로 만든다(옛 가짜 기록은 그대로 둔다).
- `simulated` 칸은 실행기 속성에서 오므로 실제 모드 기록은 `false`로 저장된다(지금 규칙 그대로). 로그 API의 "실행기와 행의 simulated가 다르면 빈 페이지" 규칙 그대로.
- 진행 중 요청이 있을 때 모드를 바꾸지 않는다(가짜 external_ref를 실제 실행기가 읽지 못함). 실제 실행기는 알아보지 못하는 ref(`fake-…`)에 `BuildFailed`/`DeployFailed(DEPLOY_FAILED)`를 돌려준다.
- 운영 전환 순서: ① 이 PR 병합·배포(모드 fake) ② 7절 준비 완료 ③ 환경 3개 등록 ④ `EXECUTOR_MODE=real` 설정 반영(SecretSync/ConfigMap) ⑤ AC-9.

## 7. 저장소 밖 준비 (담당·순서)

| # | 할 일 | 담당 | 비고 |
|---|---|---|---|
| P1 | 고객 빌드 워크플로 `customer-build.yml`(5.3 계약) + App 개인키 Actions secret | 서동옥(D-2) | 인프라 3 이슈 댓글로 요청 |
| P2 | Echoport Deploy App 권한에 **Actions 읽기·쓰기** 추가 + echo-was 저장소에 설치 확인 | 지민(클릭) | 조직 승인 화면 |
| P3 | AWS: ALB 그룹 `echo-apps` 닻 Ingress(모르는 주소 404) + ACM `*.aws-tokyo.echoport.uk`(DNS 검증 CNAME) + Cloudflare `*.aws-tokyo` CNAME → ALB(DNS only) | 지민(동옥 협의) | ALB 하루 약 $0.6 |
| P4 | GKE: ingress-nginx(L4 부하 분산기 1개) + cert-manager + Let's Encrypt DNS-01 와일드카드 `*.gcp-seoul.echoport.uk`를 기본 인증서로 + Cloudflare `*.gcp-seoul` A → LB IP(DNS only) | 지민(동옥 협의) | 부하 분산기 하루 약 $0.6, DNS-01용 Cloudflare 토큰 |
| P5 | 온프레 `install.sh`: 터널 설정에 k3s 창구 주소(caPool + originServerName, noTLSVerify 아님) + `echo-executor` SA·ClusterRole·토큰(5.6) | 지민(echo-infra PR) | 인프라 2 후속 |
| P6 | 관제탑 GSA에 AR `echo` 읽기 권한(온프레 받기 자격용). GKE 노드 SA는 이미 프로젝트 AR 읽기 있음(확인) | 서동옥(Terraform) | 5.8 |
| P7 | 진입점 업스트림 `aws=<ALB DNS>:80`, `gcp=<nginx LB IP>:80` | 서동옥 | |
| P8 | Cloudflare API 토큰(echoport.uk DNS 편집만) → Secret Manager → echo-was 환경 변수 | 지민(발급 클릭) | 5.7·P4 |
| P9 | 환경별 DB + 앱 네임스페이스의 `{ns}-env` Secret(`DATABASE_URL`) | 데이터 파트·지민(D-3) | 시연 앱 |

## 8. 인수 조건

| AC | 확인 | 어디서 |
|---|---|---|
| AC-1 | 세 실제 실행기가 `tests/executor_contract.py`의 해당 검사(새 칸을 더한 `TARGET`·`DEPLOYMENT`)를 스텁 전송(httpx MockTransport: GitHub·쿠버네티스·메타데이터·STS·EKS·Cloudflare)으로 통과. 가짜 실행기도 같은 계약 시험을 계속 통과 | 로컬 |
| AC-2 | 빌드: 같은 키로 두 번 `start` → dispatch 1번(두 번째는 `display_title`로 찾음 — 스텁의 `name`과 `display_title`을 다른 값으로 둔다). dispatch 본문에 `return_run_details: true`. 재시작 경로에서 목록에 보이면 dispatch 0번. 404/403/422 → `BUILD_DISPATCH_FAILED`(처리기 통합 시험: 요청이 그 코드로 FAILED), 실행 실패 → `BUILD_FAILED`, 아티팩트 없음·digest 모양 다름 → `BUILD_FAILED`, 5xx는 poll에서 Running. 성공 → AR 저장소 + digest | 로컬 |
| AC-3 | 배포: 만든 매니페스트 4개가 5.4 표와 같음(스냅샷 시험, aws는 ECR 주소·ALB 어노테이션, 온프레는 `imagePullSecrets`). 같은 키 재적용 멱등. poll 판정: 준비 완료 / `ImagePullBackOff`·유예 넘은 `ErrImagePull` / `CrashLoopBackOff` / 어노테이션 불일치 / 401·403 각각 기대 결과 | 로컬 |
| AC-4 | 되돌리기: previous 있음 → 그 digest로 apply, 없음 → 세 객체 삭제. 내리기: 네임스페이스 삭제 → 404면 Removed, 온프레 DNS 레코드는 comment가 같은 것만 삭제 | 로컬 |
| AC-5 | 접속기: EKS 토큰이 `k8s-aws-v1.`로 시작하고 디코드하면 `x-k8s-aws-id` 서명 헤더가 든 presigned URL, GKE·EKS 토큰 캐시가 만료 5분 전 재발급, 온프레 토큰은 secret_ref에서 | 로컬 |
| AC-6 | 주소: `ingressDomain` 있는 환경의 새 연결 = `{서비스}-{suffix}.{도메인}`, 없는 환경 = 기존 형식, 기존 행 불변, 잘못된 `ingressDomain`(두 단계·다른 도메인) 422 | 로컬 DB |
| AC-7 | 설정: `EXECUTOR_MODE=real`에서 필수값이 빠지면 시작 실패, real 실행기 생성은 네트워크·비밀값 조회 없음, API 프로세스 실행기 묶음 1개 재사용. `fake` 기본 동작 그대로. 5.9에 적은 시험 2개와 5.1의 스키마 키 시험만 고치고 나머지 기존 시험은 그대로 통과. 새 칸을 쓰지 않는 환경의 저장 config가 지금과 같음(`exclude_none`) | 로컬 |
| AC-8 | 품질: ruff·format·mypy(`app`)·pytest 전체 통과, 건너뜀 0, `alembic check` 변경 0(마이그레이션 없음), 키 패턴 `git grep` 0 | 로컬 |
| AC-9 | **실환경**(7절 준비 뒤): 시연 앱을 (a) AWS 도쿄에 원터치 배포 → 환경별 주소 `/healthz` 200 + 대표 주소 200(진입점 경유) (b) GCP 서울 배포 → 예비(비율 0) + 환경별 주소 200 (c) 코드 한 줄 바꿔 AWS 재배포 → GCP 자동 따라가기 성공 (d) 온프레 배포 → 환경별 주소 200 (e) 없는 커밋 이미지 등 실패 주입 → 직전 버전 유지 (f) 서비스 삭제 → 세 곳 네임스페이스 사라짐. 요청 id·주소·응답 코드를 이슈 댓글에 기록 | 운영 |

AC-1~8은 PR 병합 조건, AC-9는 이슈 완료 조건이다.

## 9. 사전 부검 (데모 날 기준)
- P-a 첫 실배포가 이미지 받기 실패로 멈춤 → AC-3 `IMAGE_PULL_FAILED` + 5.8 + P6 (예선 L2·L3)
- P-b 권한 오류가 시간 초과로만 보여 원인을 못 찾음 → AD-14 코드 + 경고 로그 (L1)
- P-c 빌드가 5분 시연 시간을 넘김 → 시연 대본: 빌드는 미리 해 두고 장면에서는 같은 커밋 재배포(빌드 재사용, 수 초)
- P-d 터널이 끊겨 온프레 반영이 멈춤 → 일꾼 재시도 + `DEPLOY_TIMEOUT` 300초, 시연 전 `kubectl get --server <apiUrl>` 점검을 리허설 체크리스트에

## 10. 위임 세부 (구현자가 정하고 보고)
- 파일 배치(`app/executors/github_actions.py`, `kubernetes/…` 등), 매니페스트 생성 방식, httpx 클라이언트 재사용 구조, 로그 cursor 내부 형식, 아티팩트 zip 해제, 재시도 간격, 시험 픽스처 모양.

## 11. 완료 뒤 알릴 것
- 인프라 3 이슈: 5.3 계약 + P6·P7 요청(D-2)
- 프론트 계약 이슈: 새 실패 코드 4개, 환경별 주소 형식(클라우드 하위 도메인)
- 설계 문서(팀 ADR): 데모는 직접 반영·실제 서비스는 GitOps, 실행기 권한 위험, 온프레 터널 창구, 환경별 하위 도메인(예선 방식)

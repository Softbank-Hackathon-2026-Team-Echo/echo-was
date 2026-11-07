# 운영 배포

`main`에 push하면 `.github/workflows/deploy.yml`이 테스트 → 이미지 빌드·양쪽 레지스트리 사전 배치 → 마이그레이션 → 롤아웃을 자동으로 한다.
대상은 해커톤 GCP 서울(`asia-northeast3`)의 GKE `echo-dev-gke`, 네임스페이스 `echo-was`이고 주소는 `https://api.echoport.uk`다.
관제탑을 고객 앱 출발지(AWS 도쿄)와 다른 클라우드에 두어, 도쿄 재난 장면에서도 관리 API가 살아 있게 한다(2026-11-03 AWS 도쿄 EKS에서 옮김).

이미지는 한 번 빌드해 같은 digest를 Artifact Registry(운영)와 ECR(사본)에 넣는다. ECR 사본은 서울 장애 때 도쿄에서 관제탑을 다시 세우기 위한 사전 배치이지 Active-Active가 아니다. 운영 트래픽과 자동 롤아웃 대상은 GKE 하나다.

| 파일 | 내용 |
|---|---|
| `base.yaml` | KSA·비밀 연결(SecretProviderClass·SecretSync)·네트워크 울타리(NetworkPolicy)·PostgreSQL(StatefulSet)·설정(ConfigMap)·Service·Ingress |
| `migrate-job.yaml` | 배포마다 새 이름으로 한 번 도는 `alembic upgrade head` |
| `app.yaml` | API·일꾼 Deployment (이미지는 digest) |
| `lb.yaml` | 부하 분산기 헬스체크(BackendConfig, `/livez`)·HTTP→HTTPS(FrontendConfig). 롤아웃 뒤에 적용 |

## 흐름

1. `test`: ruff·mypy·pytest(PostgreSQL 17 서비스 컨테이너). PR에서도 돈다.
2. `deploy`(main push만): GitHub OIDC → AWS 역할 `echo-dev-ci-build`(ECR push만) + GCP Workload Identity 연합 → 서비스 계정 `echo-dev-ci-build` → 이미지 `echo/app:was-<커밋 7자>-<실행 번호>`를 한 번 빌드해 Artifact Registry와 ECR에 푸시하고 두 digest가 같은지 확인(다르면 배포하지 않는다) → `base.yaml` 적용 → 마이그레이션 Job 완료 대기 → `app.yaml` 적용(Artifact Registry digest) → 롤아웃 → `lb.yaml` 적용 → `/readyz` 확인.
3. 마이그레이션이 실패하면 롤아웃하지 않고 기존 파드를 그대로 둔다. 롤아웃이 실패하면 `rollout undo`로 직전 버전으로 되돌린다(마이그레이션은 앞으로만 가므로 되돌리지 않는다).
4. 배포는 한 번에 하나만 돈다(`concurrency`). 저장소에 클라우드 키·비밀값은 없다(AWS OIDC·GCP Workload Identity 연합).
5. **PR 없이 main 에 들어온 커밋은 배포하지 않는다.** 배포 job 첫 단계가 커밋이 병합된 PR 과 연결되어 있는지 보고, 아니면 GCP 에 접근하기 전에 실패한다. (이 조직은 무료 요금제라 비공개 저장소에 보호 브랜치·ruleset 을 걸 수 없어 대신 둔 장치다. 직접 push 자체를 막지는 못한다.)

## 비밀값

- DB 접속 주소·GitHub App 개인키·client secret은 Secret Manager(`echo-dev-echo-was-database-url`·`-github-app-private-key`·`-github-app-client-secret`)에 있다. 비밀 껍데기와 파드 서비스 계정(GSA `echo-dev-echo-was`, 이 세 비밀과 아래 진입점 토큰만 읽기)은 echo-infra `terraform/environments/gcp/dev/control-plane.tf`가 만든다.
- 고정 진입점 토큰은 진입점 VM이 만든 비밀 `echo-dev-entry-weights-token`에 있다. GSA `echo-dev-echo-was`의 읽기 권한은 echo-infra `control-plane.tf`가 준다(이 권한이 없으면 SecretSync가 실패한다).
- Cloudflare DNS 토큰은 `echo-dev-echo-was-cloudflare-dns-token`에 있다(echoport.uk 영역의 DNS 편집·영역 읽기만, 온프레 앱 주소 레코드용). 비밀과 GSA `echo-dev-echo-was` 읽기 권한(이 비밀 하나에만)은 11/3 gcloud로 손으로 만들었다 — echo-infra `control-plane.tf`에는 아직 없다.
- 시연 조직 `echo-demo` 온프레 환경의 k3s 실행기 토큰은 `echo-dev-echo-was-onprem-executor-token`에 있다(SA `echo-system/echo-executor`, 권한·라벨 정책은 echo-infra `onprem/manifests/echo-executor.yaml`). 환경 `secretRef`는 `env:ECHO_SECRET_ECHO_DEMO__ONPREM_EXECUTOR_TOKEN`. SecretSync 키는 밑줄 두 개를 받지 않아(`targetKey` 형식) `ECHO_DEMO_ONPREM_EXECUTOR_TOKEN`으로 옮긴 뒤 `app.yaml`의 `env`(`secretKeyRef`)가 조직 접두사 이름으로 바꿔 넣는다. 비밀과 읽기 권한(이 비밀 하나에만)은 11/4 gcloud로 손으로 만들었다.
- 데이터 확인(이전 계획의 복제 지연 판정)은 `DATA_SYNC_CHECKER_MODE: postgres`다. 읽기 전용 계정 `echo_monitor`(`pg_monitor`만)의 비밀번호는 `echo-dev-echo-was-pg-monitor-password`에 있고, GSA `echo-dev-echo-was` 읽기 권한은 이 비밀 하나에만 있다(11/4 echo-infra `replication/k8s/scripts/monitor-user.sh`로 만듦). A(도쿄 NLB)·B(서울 `echo-pg`) 둘 다 TLS(`require`)로 붙는다. 매핑에 없는 환경(온프레 등)이 낀 이전은 데이터 확인 단계에서 `METRICS_UNAVAILABLE`로 실패하므로, 되돌리려면 `fake`로 바꾼다.
- DB 쓰기 전환 단계(API 서버 17): 운영은 `DB_CUTOVER_EXECUTOR_MODE` 를 설정하지 않아 기본 `disabled` 다(`includeCutover=true` 는 409). 켜려면 별도 PR 로 `DB_CUTOVER_EXECUTOR_MODE: kubernetes` · `DB_CUTOVER_JOB_IMAGE: <echo-infra deploy/values.env 의 CUTOVER_IMAGE 그대로>` 두 줄을 더한다. 일꾼 파드 KSA `echo-was/echo-was` 가 같은 클러스터 `echo-ops` 에 Job 을 만든다(권한은 echo-infra `replication/cutover-job/deploy/gcp-seoul/echo-ops.yaml` Role `echo-was-cutover-jobs`).
- 컷오버 결과(API 서버 16): API 파드가 끝난 이전 계획 상세를 조회할 때 도착 환경 PostgreSQL 의 `CUTOVER_RUNS_DATABASE: app` 에 같은 계정 `echo_monitor` 로 붙어 `echo_ops.cutover_runs` 를 읽는다(읽기만, 실패해도 응답은 200 이고 `result` 칸만 null). `CUTOVER_RESULTS_NOT_BEFORE: "2026-11-04T13:30:00Z"` 는 표가 생긴 때(11/4 22:30 KST)이고, 이보다 먼저 끝난 계획에는 11/4 22:52 행이 붙지 않게 한다. 되돌리려면 `CUTOVER_RUNS_DATABASE: ""` 로 바꾼다. 캐시 초(`CUTOVER_RESULT_CACHE_SECONDS`)는 기본값 5를 쓴다.
- 서비스 대시보드 지표 API(운영 2)는 진입점 Prometheus(`ENTRYPOINT_PROMETHEUS_URL`)를 basic auth(사용자 `echo-otel`)로 읽는다. 비밀번호는 진입점 VM이 쓰는 비밀 `echo-dev-entry-prometheus-remote-write`(값 = 비밀번호만)에 있고, GSA `echo-dev-echo-was` 읽기 권한은 이 비밀 하나에만 11/5 gcloud로 손으로 줬다(echo-infra `control-plane.tf`에는 아직 없다). ConfigMap `ENTRYPOINT_PROMETHEUS_BASIC_AUTH_PASSWORD_REF`를 지우면 인증 없이 읽어 응답이 `UNAVAILABLE`/`PROMETHEUS_UNAUTHORIZED`가 된다.
- 환경 접속 확인 어댑터는 `ENVIRONMENT_ADAPTER_MODE: real`(API 서버 12)이다. aws·gcp는 배포 실행기와 같은 워크로드 신원으로 붙고(저장 비밀 없음), 온프레는 위 k3s 실행기 토큰으로 붙는다. 온프레 노드 수를 읽으려면 `echo-executor` ClusterRole에 `nodes get,list`가 필요하다(없으면 환경은 `CONNECTED`, 클러스터 정보만 `AUTH_FAILED`). 되돌리려면 이 값을 `simulated`로 바꾼다.
- `SecretSync`가 이 값을 Kubernetes Secret `echo-was-env`(`DATABASE_URL`·`ECHO_SECRET_GITHUB_APP_PRIVATE_KEY`·`ECHO_SECRET_GITHUB_APP_CLIENT_SECRET`·`ECHO_SECRET_ENTRYPOINT_TOKEN`·`ECHO_SECRET_CLOUDFLARE_DNS_TOKEN`·`ECHO_DEMO_ONPREM_EXECUTOR_TOKEN`·`ECHO_SECRET_DATA_SYNC_PASSWORD`·`ECHO_SECRET_ENTRYPOINT_PROMETHEUS_PASSWORD`)로 옮기고 API·일꾼·마이그레이션이 `envFrom`으로 읽는다. 값을 바꾸면 새 version을 추가한다(`gcloud secrets versions add <id> --data-file=-`). 동기화는 약 2분 간격이다.

## 고객 앱 환경 변수 보관함 (API 서버 10)

고객 앱의 환경 변수·API 키는 관리 평면 DB 가 아니라 GCP Secret Manager(이름 접두어 `echo-app-`)에 둔다. 실행기가 배포 때 꺼내 앱 네임스페이스 Secret `{ns}-env` 에 쓴다([docs/executors.md](../docs/executors.md#앱-환경-변수-ns-env-api-서버-10)).

| 설정 | 기본 | 뜻 |
|---|---|---|
| `APP_SECRET_STORE_MODE` | `fake` | `gcp` 면 Secret Manager. `fake` + `EXECUTOR_MODE=real`(지금 운영)이면 쓰기 API 가 409 `SECRET_STORE_NOT_CONFIGURED` 이고 실행기는 `{ns}-env` 를 건드리지 않는다 |
| `APP_SECRET_PROJECT_ID` | 없음 | 보관함 프로젝트 id. `gcp` 면 필수 |
| `APP_SECRET_NAME_PREFIX` | `echo-app-` | IAM 조건의 접두어와 같아야 한다 |

- **`base.yaml` 은 `gcp` 다(11/4).** GSA `echo-dev-echo-was` 에 `roles/secretmanager.admin` + 조건 `resource.name.startsWith("projects/<GCP_PROJECT_NUMBER>/secrets/echo-app-")`(제목 `echo-app-secrets-only`)을 gcloud 로 주고, 운영 API 파드에서 만들기·버전 추가·읽기 200, 접두어 밖 만들기 403 을 확인했다. Terraform 반영은 echo-infra #42.
- 되돌리려면 `fake` 로 바꾼다. 쓰기 API 가 409 가 되고, 이미 쓴 `{ns}-env` 는 그대로 남는다.
- 지금 손으로 만든 시연 앱 `demo-y6cz3jrc-env` 는 값을 API 로 넣고 다시 배포하기 전까지 그대로 쓰인다(변수가 없는 연결은 손 Secret 을 건드리지 않는다). 넣고 배포하면 통째로 echo-was 소유로 바뀐다.
- echo-was 는 보관함 비밀을 지우지 않는다. 정리는 라벨(`managed-by=echo-was`, `echo-service`)로 찾는 후속 일이다.

## 손으로 만들어 두는 것 (저장소에 넣지 않는다)

- 네임스페이스 `echo-was`, Secret `echo-was-db`(DB 비밀번호, `DATABASE_URL`과 같은 값)
- CI 신원은 echo-infra Terraform(`terraform/environments/gcp/dev/ci.tf`·`aws/dev/ci.tf`)이 만든다: Workload Identity pool `echo-dev-github`(이 저장소 `main`만 `echo-dev-ci-build`를 받는다), 서비스 계정 `echo-dev-ci-build`(Artifact Registry `echo` 쓰기·`roles/container.developer`), AWS 역할 `echo-dev-ci-build`(ECR `echo/app` push만). 손으로 만들었던 pool `echo-was-github`·서비스 계정 `echo-was-deploy`·`rbac.yaml`(Role·RoleBinding)·AWS 역할 `echo-was-deploy`(정책·EKS Access Entry 포함)는 #18 첫 배포 성공 뒤 11/3에 지웠다.
- 고정 IP `echo-dev-echo-was`·관리형 인증서는 echo-infra가 만든다. Cloudflare `api` A 레코드(DNS only)가 그 IP를 가리킨다.

## GKE 에서 알아 둘 것

- GCP 부하 분산기 헬스체크는 **200만 성공**으로 본다. `/healthz`·`/readyz`는 204라서 부하 분산기에는 `/livez`(200, 같은 의미)를 쓴다.
- 이 클러스터는 고객 앱의 대피처이기도 하다. `base.yaml`의 NetworkPolicy가 다른 네임스페이스에서 echo-was API·DB로 들어오는 연결을 막는다(부하 분산기 대역 `35.191.0.0/16`·`130.211.0.0/22`만 API 8000 허용).

## 수동으로 되돌리기

```bash
kubectl -n echo-was rollout undo deploy/echo-was-api deploy/echo-was-worker
```

특정 버전으로 돌리려면 그 커밋의 이미지 digest(Artifact Registry `echo/app:was-<7자>-<실행 번호>`)로 `app.yaml`을 적용한다.

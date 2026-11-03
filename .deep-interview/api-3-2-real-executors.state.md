# state: API 서버 3-2 — 실제 빌드·배포·내리기 실행기 (가짜 → 실제 교체)
status: confirmed
depth: Deep (외부 계약 + 세 클러스터 연결 + 다른 파트(인프라) 계약 + 비밀값)
이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/3 (In Progress, 담당 지민). 11/2 코드(가짜 실행기) 병합 → 남은 완료 기준 = 실제 빌드·배포·퍼블릭 URL
저장소: echo-was main=06cd5fe (11/3 15:2x 확인)

## goal
가짜 빌드·배포·내리기 실행기를 실제로 바꿔, 한 번의 요청으로 고객 앱(echo-demo-app)을 GKE·EKS·온프레 k3s 중 한 곳에 빌드·배포하고 환경별·대표 퍼블릭 URL이 응답하게 한다. 표·API 계약은 바꾸지 않는다(docs/executors.md).

## repo / infra evidence (11/3 확인)
- echo-was: `build_executor_set`는 `EXECUTOR_MODE=fake`만 허용(config validator가 다른 값 거부). 실제 구현 있음 = HttpHealthChecker, Entrypoint{Traffic,MainRoute,Metrics}(#17, read_counts 자리값)
- 실행기 계약 docs/executors.md: BuildExecutor(start/poll/read_logs), DeployExecutor(apply/poll/revert/read_runtime_logs), TeardownExecutor(remove/poll). 멱등 키 builds.id / deployments.id / `{연결 id}:{attempt}`
- 서비스 칸: repo_url·repo_private·health_check_path·timeout·container_port·public_host·host_suffix. 환경 변수 칸 없음(API 10, 2차). config_snapshot = 헬스 경로·제한·포트·대표/환경별 URL
- 주소: 대표 `{svc}-{8}.echoport.uk`, 환경별 `{svc}-{env}-{8}.echoport.uk`. DNS `*.echoport.uk` → 진입점 Envoy VM(가중치로 aws/gcp 분배, Host 무관)
- echo-demo-app: DATABASE_URL 필수(기본값 없음), ECHO_ENVIRONMENT, PORT 8080, /healthz(DB 안 봄)
- 인프라 3 계약(동옥 11/2 댓글): 빌드 GitHub Actions → ECR echo/app + AR echo/app 같은 digest, 1단계 echo-was 실행기가 kubeconfig로 직접 apply, Argo CD는 2단계. 역할: 실행기 코드 = 지민, 접근 설정·레지스트리·IAM = 동옥. Slack 11/3 동옥 "Actions 제가 구현하겠습니다"(빌드 워크플로 본문)
- echo-infra contracts/executor-credentials.md: 빌드 = echo-was 저장소 main Actions(OIDC ci_build_role / ci-build SA), 배포 = 관제탑 GSA. GKE: GSA에 roles/container.developer(확인), EKS: echo-dev-executor 역할(존재 확인, Access Entry ClusterAdmin, cluster scope 확인), STS web identity
- Terraform github-repos build_repositories = [echo-was] 만
- 고객 앱 빌드 워크플로: 아직 없음(echo-was PR #18은 echo-was 자기 이미지 = 흐름 ①)
- GitHub App `echoport-deploy` 존재, 권한 contents/members/metadata read, 설치 selected
- EKS: 공개 엔드포인트 0.0.0.0/0, ingressclass alb(LBC 설치), 고객 앱 없음, ns echo-data(도현 PgBouncer)
- GKE: ingress는 echo-was-api(GCE)뿐, 공유 인그레스 컨트롤러 없음, 고객 앱 없음
- 온프레: AWS 서울 EC2 + k3s + Cloudflare Tunnel(모든 주소 → Traefik), 들어오는 포트 0. k3s API 외부 도달 경로 없음. 시연 앱 이미지 = GHCR 공개
- 진입점 Envoy 업스트림 aws/gcp 두 갈래(온프레 없음)

## 넘겨받은 결정
- API 3 D1~D12, AD-1~28 (api-3-one-touch-deploy.state.md). D3(GitOps)는 2단계로 미룸(11/3 지민), 1단계 직접 반영
- D7: 헬스 = 환경별 퍼블릭 주소 + 경로 2xx
- 11/3 지민: 관제탑 GKE, 실행기 권한은 고객 네임스페이스로 좁히라는 제안(설계 문서)

## decisions
- D-1(R2): 고객 앱 빌드 워크플로는 echo-was 저장소에 둔다(3A) [사용자]
- D-2(R3): 빌드 워크플로 본문은 동옥님께 계약(입력·실행 이름·결과 파일)을 인프라 3 이슈 댓글로 요청, 실행기는 계약대로 먼저 만들고 가짜 빌드로 시험(3b1) [사용자]
- D-3(R3): 고객 앱 비밀값 = 환경 담당이 고객 앱 네임스페이스에 미리 둔 Secret을 실행기가 envFrom으로 읽게만 함(4A). DB 생성·주소 넣기는 환경 담당(온프레 install.sh 방식과 같음). 화면 입력은 2차(API 서버 10) [사용자]
- D-4(R3): 환경별 주소 = 예선처럼 환경별 하위 도메인 와일드카드(2D). 예: demo-xxxxxxxx.gcp.echoport.uk. API 서버 3-1 환경별 주소 형식 변경, 대표 주소({svc}-{8}.echoport.uk, Envoy 경유)는 그대로. 인증서: AWS ACM 와일드카드(무료), GCP cert-manager+Let's Encrypt(무료, Cloudflare DNS 토큰 필요), 온프레 터널 두 단계 주소는 Cloudflare Advanced Certificate Manager($10/월) 필요 → 비용 승인 별도 [사용자, 비용 항목은 승인 대기]
- D-5(R3): 온프레 연결은 결정 전에 스파이크(Cloudflare 터널로 k3s 창구) 지금 실행 승인 → 결과로 1A/1C 결정 [사용자]
- 원칙(R2, 사용자): 고객 앱은 하위 도메인 하나(대표 주소)로 어느 클라우드에 있든 접근, 이전 중에도 같은 주소 → 기존 D8 대표 주소(Envoy 경유)와 같음, 확인만. 2번 질문(환경별 주소 처리)은 이 원칙과 별개라 재질문 [사용자 + AI 해석]

## unresolved (impact 순)
1. 온프레 k3s에 echo-was가 닿는 길
2. 환경별 주소를 그 환경으로 보내는 길(헬스체크 대상) + 클러스터별 공유 문(인그레스)
3. 고객 앱 빌드 워크플로 위치·시작·digest 회수, 작성 담당(동옥 약속)
4. 고객 앱 설정·비밀값(DATABASE_URL) 주입
5. 온프레 이미지 pull 자격(ECR/AR은 클라우드 자격 필요)

## round log
- R1 (11/3): 설명 페이지 api-3/api32-r1-real-executors-explainer.html 로 1~4 묶음 질문 + AD 목록. 답 대기
- R1 사용자 질문 "예선 때 어떻게 했나" → 예선(iris) 조사 결과(출처: iris-infra runbooks onprem-gateway·onprem-server-registration·gcp-workload, ADR 0004·0007, architecture.md, runtime/onprem-auth-renewal, iris-was control-plane-build-deploy-flow·onprem-server-registration-contract):
  - 온프레: Tailscale(태그 ACL, 관리 EKS의 Tailscale operator 프록시로 k3s 6443·Traefik 80), install.sh가 재사용 auth key로 가입, Argo CD가 SA 토큰으로 반영. 겪은 일: 정책 병합·태그 400 거절, 토큰 만료(48h·24h) 갱신 CronJob, 관리 Argo가 서버 Secret 전체 읽기 위험
  - 주소: 환경마다 하위 도메인 와일드카드(*.likelion.uk→workload ALB, *.gcp.likelion.uk→GCP Gateway, *.internal.likelion.uk→관리 ALB→nginx→Tailscale). 앱마다 DNS 레코드 없음. 서비스당 타깃 1곳이라 대표/환경별 구분 없음. 인증서는 DNS only + ACM·GCP Certificate Manager
  - 문: 클러스터마다 하나(ALB 그룹 앵커, GCP는 Gateway API + 고정 IP + 와일드카드 인증서)
  - 빌드: 플랫폼 인프라의 CodeBuild를 Build Worker가 StartBuild, ID 먼저 기록, 로그 CloudWatch. AWS 전용이라 GCP 배포도 AWS에 의존
  - 변수: 사용자 변수 암호화 저장 → Sealed Secrets 봉인 → envFrom, 릴리스마다 Secret 이름 새로, PORT·IRIS_* 덮어쓰기 금지 (ADR 0004 상태 '제안', 배포 후 검증 전)
  - 이미지 pull: GKE·온프레가 ECR을 못 받아 단기 자격 갱신기(1분·만료 15분 전) 필요 → AD-6 '배포 때만 갱신'은 파드 재시작 시 실패 위험, 주기 갱신으로 보강 필요
  - 우리 GKE: Gateway API 꺼져 있음(gatewayApiConfig 없음, 11/3 확인). 시연 앱 /healthz 200
- 판단: 1A·2A·3A·4A 추천 유지, AD-1(GCP 문)·AD-6(온프레 pull 주기 갱신)을 예선 근거로 고침 제안. 답 대기
- R2 (11/3) 사용자 "예선 교훈·검증된 방안 참고해 다시 인터뷰" → 예선 검증 여부 재분류(운영 검증: AWS 경로, 온프레 Tailscale 경로·권한 표·이미지 자격 갱신, GitHub App 토큰 / 미검증: GCP Gateway, Sealed Secrets 변수). 교훈 L1~L9를 요구사항으로 변환. 페이지 api-3/api32-r2-prelim-lessons.html (R1 페이지 대체). 선택지 변경: 1A=터널+30분 스파이크, 실패 시 C(Tailscale), 2D(예선식 환경별 하위 도메인) 추가, AD-3 권한 = 예선 온프레 권한 표 + 앱 ns Secret 쓰기, AD-4 토큰, AD-5 IMAGE_PULL_FAILED, AD-6 30분 갱신. 답 대기
- R2 답(11/3): 3A. 1·4는 설명 요청(스파이크 뜻·구현 첫 단계, 미리 둔 Secret 뜻·A/B 차이), 2는 원칙 제시 → 대표 주소와 환경별 주소 구분 설명 후 재질문. 3b 미답
- R3 답(11/3): 4A, 1=지금 스파이크, 2D, 3b1, 비밀값 A. C(AI 기본값) 미답. 추가 질문: 시연용 vs 실제 고객 배포 흐름
- 스파이크(11/3 16:55~17:05 KST, 사용자 승인): Cloudflare 터널로 k3s 창구. 터널 설정에 hostname k3s-k2f6xje5.echoport.uk → https://kubernetes.default.svc:443(noTLSVerify) 추가, 시험 ns spike-k3s + SA(admin RoleBinding, 1h 토큰). M1에서: 토큰 없음 401 / 조회 OK / 서버 측 apply(Deployment·Service·Ingress) OK·재적용 멱등 / rollout status(watch) OK / 로그 tail·follow OK / 로그 API(timestamps) OK / 이미지 없는 digest → 12초 안에 ErrImagePull 보임 / 다른 ns secret list 403. 지연: 새 연결 1~3초(가끔 6~10초 이상치), 연결 재사용 시 0.5초 — 같은 터널 시연 앱 /healthz도 1.1~2.6초라 터널 경로 특성. kubectl 첫 실행은 디스커버리로 10~15초(실행기는 직접 API 호출이라 무관). cloudflared 2개 순차 재시작으로 시연 앱 200 유지. 정리: ns 삭제, 터널 설정 원래대로(백업 /root/cloudflared-config.backup.yaml), 토큰·kubeconfig 삭제. DNS CNAME k3s-k2f6xje5는 남김(지금은 Traefik 404). 기록 api-3/spike-k3s-tunnel/
  → 결론: 1A 통과(검증됨). 실 구현 메모: noTLSVerify 대신 originServerName kubernetes.default.svc + caPool(kube-root-ca.crt 마운트), 실행기는 HTTP keep-alive 클라이언트 재사용, 호출 제한 시간 15초+재시도
- R4 답(11/3): 1A 터널 확정(스파이크 통과) [사용자] → D-6. C0 AI 기본값 1~9 모두 그대로 [사용자] → AD-1~AD-9 확정. 온프레 두 단계 주소 인증서 필요 여부 설명 요청(답 대기)
- R5 답(11/3): 2A — 온프레만 한 단계 주소({svc}-{환경}-{8}.echoport.uk, 지금 형식), AWS·GCP는 환경별 하위 도메인. 유료 인증서 안 삼 [사용자] → D-4 수정. 모든 결정 완료 → SPEC 작성

## R6 — SPEC 초안·검토 (11/3) [AI]
- SPEC 초안 `api-3-2-real-executors.md` 작성 → fresh-eyes 검토 10건(high 3, medium 7) 전부 반영(r2).
  - high: run-name은 `display_title` 칸 + dispatch에 `return_run_details: true` 필요(GitHub changelog 2026-02-19 확인) / ALB HTTPS 인증서 자동 찾기가 대표 주소 때문에 그룹 전체 실패 → aws `certificateArn` 칸 명시 / `DeployTarget` 확장 칸 명시(5.1-b)
  - medium: teardown ref에 키 포함, `BuildStartError`·`DeployApplyError` 처리기 연결 위치, poll 계열 일시 오류 = Pending(예외 3번 = WORKER_LOST 방지), 온프레 RoleBinding 직후 403 재시도 + ValidatingAdmissionPolicy, config `exclude_none` 저장, real 실행기 생성 부작용 없음·프로세스당 1개, 고칠 기존 시험 목록
  - 확인: GKE 노드 SA `echo-dev-gke-node`에 `roles/artifactregistry.reader` 있음 → GKE 이미지 받기 문제 없음
- 사용자 확인 필요 1건: AD-6 "30분마다 갱신" → "5분마다 확인, 남은 수명 30분 미만이면 갱신"(메타데이터 토큰이 캐시라 남은 수명이 1시간보다 짧을 수 있음). [AI 추천]

## R7 — AD-6 세부 확정 (11/3) [사용자]
- 사용자 답 "A": 온프레 이미지 받기 자격은 5분마다 확인, 남은 수명 30분 미만이면 갱신(SPEC 5.8). SPEC Confirmed, 기준 main 0c6884f(8-2 병합 포함, SPEC 영향 없음 — entrypoint.py·docs/executors.md 카운터 부분만).
- 다음: m5claude `echo-api-3-2` 구현(AC-1~8).

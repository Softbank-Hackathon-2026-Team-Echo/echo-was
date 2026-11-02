# 운영 배포

`main`에 push하면 `.github/workflows/deploy.yml`이 테스트 → 이미지 빌드·양쪽 레지스트리 사전 배치 → 마이그레이션 → 롤아웃을 자동으로 한다.
대상은 해커톤 AWS 도쿄(`ap-northeast-1`)의 EKS `echo-dev-eks`, 네임스페이스 `echo-was`이고 주소는 `https://api.echoport.uk`다.

애플리케이션 실행 구조는 **AWS Primary / GCP Warm Standby**다. GitHub Actions가 이미지를 한 번만 빌드한 뒤 같은 digest를 ECR과 Artifact Registry에 미리 넣지만, 이것은 Active-Active 운영을 뜻하지 않는다. 현재 운영 트래픽과 자동 롤아웃 대상은 AWS EKS이며 GCP 이미지는 도쿄 장애 전에 준비해 두는 배포 산출물이다.

| 파일 | 내용 |
|---|---|
| `base.yaml` | PostgreSQL(StatefulSet)·설정(ConfigMap)·Service·Ingress(ALB) |
| `migrate-job.yaml` | 배포마다 새 이름으로 한 번 도는 `alembic upgrade head` |
| `app.yaml` | API·일꾼 Deployment (이미지는 digest) |

## 흐름

1. `test`: ruff·mypy·pytest(PostgreSQL 17 서비스 컨테이너). PR에서도 돈다.
2. `deploy`(main push만): GitHub OIDC로 AWS 빌드 역할과 GCP 빌드 서비스 계정을 받는다. 이미지 `echo/app:was-<커밋 7자>-<실행 번호>`를 한 번 빌드해 ECR과 Artifact Registry에 같은 digest로 푸시하고 두 digest가 일치하는지 확인한다.
3. AWS 배포 역할 `echo-was-deploy`를 다시 받은 뒤 `base.yaml` 적용 → 마이그레이션 Job 완료 대기 → `app.yaml` 적용 → EKS 롤아웃 → `/readyz` 확인을 수행한다.
4. 마이그레이션이 실패하면 롤아웃하지 않고 기존 파드를 그대로 둔다. 롤아웃이 실패하면 `rollout undo`로 직전 버전으로 되돌린다(마이그레이션은 앞으로만 가므로 되돌리지 않는다).
5. 배포는 한 번에 하나만 돈다(`concurrency`). 저장소에 AWS·GCP 장기 키나 비밀값은 없다(OIDC·Workload Identity Federation).
6. **PR 없이 main 에 들어온 커밋은 배포하지 않는다.** 배포 job 첫 단계가 커밋이 병합된 PR 과 연결되어 있는지 보고, 아니면 클라우드에 접근하기 전에 실패한다. (이 조직은 무료 요금제라 비공개 저장소에 보호 브랜치·ruleset 을 걸 수 없어 대신 둔 장치다. 직접 push 자체를 막지는 못한다.)

## 손으로 만들어 두는 것 (저장소에 넣지 않는다)

- 네임스페이스 `echo-was`
- Secret `echo-was-db`(DB 비밀번호), `echo-was-env`(`DATABASE_URL`), `echo-was-github`(GitHub App 개인키·client secret)
- IAM 역할 `echo-was-deploy`와 EKS Access Entry(네임스페이스 `echo-was` 범위 편집 권한). echo-infra에 Terraform으로 옮긴다.

빌드용 AWS 역할과 GCP 서비스 계정·Workload Identity Federation은 `echo-infra` Terraform이 관리한다. 이 리소스가 적용되기 전에는 양쪽 레지스트리 푸시 단계가 실패한다.

## 수동으로 되돌리기

```bash
kubectl -n echo-was rollout undo deploy/echo-was-api deploy/echo-was-worker
```

특정 버전으로 돌리려면 그 커밋의 이미지 digest(ECR `echo/app:was-<7자>`)로 `app.yaml`을 적용한다.

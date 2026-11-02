# 운영 배포

`main`에 push하면 `.github/workflows/deploy.yml`이 테스트 → 이미지 빌드 → 마이그레이션 → 롤아웃을 자동으로 한다.
대상은 해커톤 AWS 도쿄(`ap-northeast-1`)의 EKS `echo-dev-eks`, 네임스페이스 `echo-was`이고 주소는 `https://api.echoport.uk`다.

| 파일 | 내용 |
|---|---|
| `base.yaml` | PostgreSQL(StatefulSet)·설정(ConfigMap)·Service·Ingress(ALB) |
| `migrate-job.yaml` | 배포마다 새 이름으로 한 번 도는 `alembic upgrade head` |
| `app.yaml` | API·일꾼 Deployment (이미지는 digest) |

## 흐름

1. `test`: ruff·mypy·pytest(PostgreSQL 17 서비스 컨테이너). PR에서도 돈다.
2. `deploy`(main push만): GitHub OIDC 역할 `echo-was-deploy`로 AWS에 접근 → 이미지 `echo/app:was-<커밋 7자>` 빌드·푸시(digest 기록) → `base.yaml` 적용 → 마이그레이션 Job 완료 대기 → `app.yaml` 적용 → 롤아웃 → `/readyz` 확인.
3. 마이그레이션이 실패하면 롤아웃하지 않고 기존 파드를 그대로 둔다. 롤아웃이 실패하면 `rollout undo`로 직전 버전으로 되돌린다(마이그레이션은 앞으로만 가므로 되돌리지 않는다).
4. 배포는 한 번에 하나만 돈다(`concurrency`). 저장소에 AWS 키·비밀값은 없다(OIDC).

## 손으로 만들어 두는 것 (저장소에 넣지 않는다)

- 네임스페이스 `echo-was`
- Secret `echo-was-db`(DB 비밀번호), `echo-was-env`(`DATABASE_URL`), `echo-was-github`(GitHub App 개인키·client secret)
- IAM 역할 `echo-was-deploy`와 EKS Access Entry(네임스페이스 `echo-was` 범위 편집 권한). echo-infra에 Terraform으로 옮긴다.

## 수동으로 되돌리기

```bash
kubectl -n echo-was rollout undo deploy/echo-was-api deploy/echo-was-worker
```

특정 버전으로 돌리려면 그 커밋의 이미지 digest(ECR `echo/app:was-<7자>`)로 `app.yaml`을 적용한다.

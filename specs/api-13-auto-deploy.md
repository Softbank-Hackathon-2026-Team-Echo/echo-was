# API 서버 13: main push 자동 배포 (GitHub Actions → Artifact Registry·ECR → GKE)

- 상태: 구현됨 (11/2)
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/14
- 범위 결정: 인프라 3(Argo CD)이 정해지기 전까지 쓰는 임시 파이프라인. 실행기(빌드·배포 실행기 코드)와는 별개다.

## 목표

`main` 에 push 하면 사람 손 없이 운영(`https://api.echoport.uk`, 해커톤 AWS 도쿄 `echo-dev-eks`, 네임스페이스 `echo-was`)에 반영된다.

## 흐름

1. `test`: ruff·format 검사·mypy·pytest(PostgreSQL 17). PR 에서도 돈다.
2. `deploy`: GitHub OIDC 로 AWS 역할 `echo-dev-ci-build` 와 GCP 서비스 계정 `echo-dev-ci-build` 를 받는다(저장소에 키 없음, echo-infra Terraform). 이미지를 한 번 빌드해 Artifact Registry 와 ECR 에 `echo/app:was-<7자>-<실행 번호>` 로 푸시하고 양쪽 digest 가 같은지 확인한다.
3. GCP 서울 GKE `echo-dev-gke` 에 Artifact Registry 의 **digest 로만** 배포한다. ECR 사본은 서울 장애 때 도쿄에서 관제탑을 다시 세우기 위한 사전 배치이고, 이 워크플로가 도쿄에 배포하지는 않는다.
4. `deploy/base.yaml` 적용 → 마이그레이션 Job 완료 대기 → `deploy/app.yaml` 적용 → 롤아웃 → `deploy/lb.yaml` 적용 → `/readyz` 204 확인.

## 안전장치

| 상황 | 처리 |
|---|---|
| 테스트 실패 | 이미지를 만들지 않는다. 운영 그대로 |
| 한쪽 레지스트리 푸시·digest 검증 실패 | 배포하지 않는다. 기존 운영 그대로 |
| 마이그레이션 실패 | 롤아웃하지 않는다. 기존 파드 그대로 |
| 롤아웃 실패 | `rollout undo` 로 직전 버전 복귀(마이그레이션은 되돌리지 않음) |
| 배포 겹침 | `concurrency` 로 한 번에 하나, 진행 중 배포는 취소하지 않음 |
| 권한 | AWS 역할 `echo-dev-ci-build` 는 ECR `echo/app` push 만, GCP 서비스 계정 `echo-dev-ci-build` 는 Artifact Registry `echo` 쓰기 + `roles/container.developer`. 신뢰 조건은 이 저장소 `main` 한 곳 |

이미지를 두 레지스트리에 두는 것은 Active-Active 가 아니다. 운영은 GCP 서울 GKE 하나이고, 동일 산출물을 미리 두어 서울 장애 뒤 도쿄가 Artifact Registry 에 의존하지 않게 하는 것이다.

## 손으로 만든 것

없음. 처음에 손으로 만든 AWS 역할 `echo-was-deploy(-policy)`·EKS Access Entry와 GCP pool `echo-was-github`·서비스 계정 `echo-was-deploy`·`deploy/rbac.yaml`은 CI 신원을 echo-infra Terraform의 `echo-dev-ci-build`로 바꾼 뒤(#18) 11/3에 지웠다.

## 되돌릴 때 주의

마이그레이션은 앞으로만 간다. 칸을 지우는 마이그레이션은 이전 코드와 함께 쓰이지 않게 두 번에 나눠 낸다.

# API 서버 13: main push 자동 배포 (GitHub Actions → ECR·Artifact Registry → EKS)

- 상태: 구현됨 (11/2)
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/14
- 범위 결정: 인프라 3(Argo CD)이 정해지기 전까지 쓰는 임시 파이프라인. 실행기(빌드·배포 실행기 코드)와는 별개다.

## 목표

`main` 에 push 하면 사람 손 없이 운영(`https://api.echoport.uk`, 해커톤 AWS 도쿄 `echo-dev-eks`, 네임스페이스 `echo-was`)에 반영된다.

## 흐름

1. `test`: ruff·format 검사·mypy·pytest(PostgreSQL 17). PR 에서도 돈다.
2. `deploy`: GitHub OIDC·GCP Workload Identity Federation으로 단기 권한을 받는다. 이미지를 한 번 빌드해 ECR과 Artifact Registry에 푸시하고 양쪽 digest가 같은지 확인한다.
3. AWS EKS에는 ECR의 **digest**로만 배포한다. GCP는 Warm Standby이므로 Artifact Registry에 이미지를 사전 배치하되 이 워크플로에서 고객 트래픽을 보내지 않는다.
4. `deploy/base.yaml` 적용 → 마이그레이션 Job 완료 대기 → `deploy/app.yaml` 적용 → 롤아웃 → `/readyz` 204 확인.

## 안전장치

| 상황 | 처리 |
|---|---|
| 테스트 실패 | 이미지를 만들지 않는다. 운영 그대로 |
| 한쪽 레지스트리 푸시·digest 검증 실패 | 배포하지 않는다. 기존 운영 그대로 |
| 마이그레이션 실패 | 롤아웃하지 않는다. 기존 파드 그대로 |
| 롤아웃 실패 | `rollout undo` 로 직전 버전 복귀(마이그레이션은 되돌리지 않음) |
| 배포 겹침 | `concurrency` 로 한 번에 하나, 진행 중 배포는 취소하지 않음 |
| 권한 | 빌드는 `echo-dev-ci-build` AWS 역할과 GCP 서비스 계정, 배포는 `echo-was-deploy` 역할로 분리. 신뢰 조건은 이 저장소 `main` 한 곳 |

이미지를 두 레지스트리에 두는 것은 Active-Active가 아니다. 실행 구조는 AWS Primary / GCP Warm Standby이며, 도쿄 장애 뒤 GKE가 ECR에 의존하지 않도록 동일 산출물을 미리 준비하는 것이다.

## 손으로 만든 것

IAM 정책·역할 `echo-was-deploy(-policy)`, EKS Access Entry(AmazonEKSEditPolicy, 네임스페이스 `echo-was`). 태그 `managed-by=jimin-manual`. echo-infra 에서 Terraform 으로 옮긴다.

## 되돌릴 때 주의

마이그레이션은 앞으로만 간다. 칸을 지우는 마이그레이션은 이전 코드와 함께 쓰이지 않게 두 번에 나눠 낸다.

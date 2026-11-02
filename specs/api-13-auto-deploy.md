# API 서버 13: main push 자동 배포 (GitHub Actions → ECR → EKS)

- 상태: 구현됨 (11/2)
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/14
- 범위 결정: 인프라 3(Argo CD)이 정해지기 전까지 쓰는 임시 파이프라인. 실행기(빌드·배포 실행기 코드)와는 별개다.

## 목표

`main` 에 push 하면 사람 손 없이 운영(`https://api.echoport.uk`, 해커톤 AWS 도쿄 `echo-dev-eks`, 네임스페이스 `echo-was`)에 반영된다.

## 흐름

1. `test`: ruff·format 검사·mypy·pytest(PostgreSQL 17). PR 에서도 돈다.
2. `deploy`: GitHub OIDC 로 역할 `echo-was-deploy` 를 받는다(저장소에 AWS 키 없음). 이미지를 `echo/app:was-<7자>-<실행 번호>` 로 푸시하고 **digest 로만** 배포한다.
3. `deploy/base.yaml` 적용 → 마이그레이션 Job 완료 대기 → `deploy/app.yaml` 적용 → 롤아웃 → `/readyz` 204 확인.

## 안전장치

| 상황 | 처리 |
|---|---|
| 테스트 실패 | 이미지를 만들지 않는다. 운영 그대로 |
| 마이그레이션 실패 | 롤아웃하지 않는다. 기존 파드 그대로 |
| 롤아웃 실패 | `rollout undo` 로 직전 버전 복귀(마이그레이션은 되돌리지 않음) |
| 배포 겹침 | `concurrency` 로 한 번에 하나, 진행 중 배포는 취소하지 않음 |
| 권한 | 역할은 `echo/app` ECR 푸시와 EKS `echo-was` 네임스페이스 편집만. 신뢰 조건은 이 저장소 `main` 한 곳 |

## 손으로 만든 것

IAM 정책·역할 `echo-was-deploy(-policy)`, EKS Access Entry(AmazonEKSEditPolicy, 네임스페이스 `echo-was`). 태그 `managed-by=jimin-manual`. echo-infra 에서 Terraform 으로 옮긴다.

## 되돌릴 때 주의

마이그레이션은 앞으로만 간다. 칸을 지우는 마이그레이션은 이전 코드와 함께 쓰이지 않게 두 번에 나눠 낸다.

# 보안 매트릭스·증거 API (보안·거버넌스 4, 계약 4-7)

조직의 환경마다 **권한·암호화·감사 기록·정책** 4칸의 보안 상태와 그 근거(통제·사유·증거)를 돌려준다. 판정은 공통 보안 규칙표(`app/services/security_rules.py`, 정책 버전 `POLICY_VERSION`)가 하고, 이 API는 읽기만 한다.

- 권한: 뷰어 이상(에이전트 토큰 가능). 다른 조직은 404 `ORGANIZATION_NOT_FOUND`.
- 비밀값 원문은 응답에 없다(검사 원문을 증거로 바꿀 때 이미 버렸다).
- 결과 값: `PASS` · `WARN` · `FAIL` · `UNKNOWN`(확인 못 함: 증거 없음·만료·범위 다름) · `NA`(그 환경에 해당 없음). **확인하지 못한 것은 PASS가 아니다.**

## GET `/api/v1/orgs/{orgId}/security/matrix`

```json
{
  "policyVersion": "sec-rules-2026.11.04",
  "phase": "PREPARATION",
  "checkedAt": "2026-11-05T01:00:00Z",
  "rows": [
    {
      "environmentId": "…", "environmentName": "aws-tokyo", "environmentKind": "aws",
      "services": [{"serviceId": "…", "name": "demo", "imageDigest": "sha256:…"}],
      "cells": {
        "iam":        {"result": "UNKNOWN", "findingCount": 0, "hasException": false, "mode": null, "checkedAt": null, "expiresAt": null, "controls": [ … ]},
        "encryption": { … },
        "auditLog":   { … },
        "policy": {
          "result": "FAIL", "findingCount": 2, "hasException": false, "mode": "REAL",
          "checkedAt": "…", "expiresAt": "…",
          "controls": [
            {"controlId": "C-IMG-1", "title": "이미지 취약점", "result": "FAIL", "isMandatory": true,
             "reasons": ["CONTROL_FAILED"], "findingIds": ["…"], "mode": "REAL",
             "checkedAt": "…", "expiresAt": "…", "serviceId": "…"}
          ]
        }
      }
    }
  ]
}
```

- 행 = 조직의 살아 있는 환경(만든 순, 최대 200), 칸은 늘 4개(빈칸 없음, NFR-09).
- **판정 시점은 준비(PREPARATION)**다. 실행 직전에만 보는 통제(이미지 일치·DB 쓰기·목적지·사전 점검)는 매트릭스에 없고 이전 게이트가 본다.
- 칸 결과 = 그 칸 통제들의 가장 나쁜 값: FAIL > 필수 UNKNOWN > WARN(예외 포함) > 선택 UNKNOWN > PASS. 칸의 통제가 모두 NA면 NA.
- **환경 단위 통제**(범위가 환경·WAS 버전·없음)는 환경마다 한 줄, **서비스 단위 통제**(범위에 서비스·이미지 digest·설정 커밋, 예: C-IMG-1·C-IMG-2·C-TLS-1)는 그 환경에 현재 배포가 있는 서비스마다 한 줄(`serviceId`)이다. 서비스가 없는 환경은 서비스 단위 통제를 세지 않는다.
- 증거는 그 서비스의 **지금 도는 이미지 digest**와 같을 때만 쓴다. 다른 digest의 증거는 `SCOPE_MISMATCH`로 버리고 UNKNOWN이다.
- `findingCount` = 그 칸 판정에 쓴 증거 수. `mode` = 쓴 증거 중 가장 약한 모드(`REAL` < `CACHE`), `checkedAt` = 가장 오래된 수집 시각, `expiresAt` = 가장 이른 만료.
- `reasons` 값은 [용어 사전](../../.claude/rules/term-dictionary.md) "보안 판정 값"의 판정 사유(`EVIDENCE_MISSING` 등)다.
- 기한부 예외 표는 아직 없다(`hasException`은 늘 false).

## GET `/api/v1/orgs/{orgId}/security/findings`

칸의 근거 드로어용 증거 목록(최신순, 페이지).

| 쿼리 | 뜻 |
|---|---|
| `page`·`size` | 공통 페이지(기본 0·20, 최대 100) |
| `environmentId` · `serviceId` · `controlId` | 그 값과 같은 증거만(`controlId`는 `C-IMG-1` 표기) |
| `findingId` (여러 번) | 매트릭스 `findingIds`로 고르기, 최대 100개 |
| `includeExpired` | 기본 `false`(만료되지 않은 것만) |

항목: `id`·`controlId`·`result`·`reason`(정규화 이유, 예: `FIXABLE_HIGH_VULNERABILITY`)·`source`(`TRIVY_IMAGE`·`TRIVY_SECRET`·`GITLEAKS`·`TLS_PROBE`·`MANUAL_REVIEW`·`AI_REVIEW` …)·`origin`(`RULE` 검사 도구 · `HUMAN` 사람 점검 · `AI`)·`mode`·`confirmation`(AI만)·`policyVersion`·`toolVersion`·`serviceId`·`environmentId`·`imageDigest`·`configCommit`·`wasVersion`·`collectedAt`·`expiresAt`·`evidenceRef`(원문 위치, 예: 검사 실행)·`reviewerUserId`·`basisUrl`·`items[]`(`itemId`·`severity`·`target`·`installedVersion`·`fixedVersion`·`line`·`commit`·`reason`, 최대 50)·`totalItems`.

- AI 결과(`origin: AI`)는 PASS가 없고, 사람이 `ESCALATED`로 확인해야 차단 사유가 된다.

## 다음 (같은 이슈)

- `GET …/security/compare?source=&target=`(이전 전후 비교, 통제마다 양쪽 값 + 막는 사유) — 이전 게이트(#29)와 같은 판정을 쓴다
- WAS 버전 범위 통제(C-IAM-2·C-AUD-1)의 증거, TLS 주기 시험(C-TLS-1) — 지금은 UNKNOWN
- (11/5 됨) C-SEC-1(비밀 전달 경로, 조직 범위)은 빌드 비밀 검사에서 C-IMG-2 와 함께 생긴다: 원문 없음 PASS, 발견 WARN(그 이미지는 C-IMG-2 FAIL), 원문을 못 읽으면 만들지 않음. 새 빌드부터 보인다

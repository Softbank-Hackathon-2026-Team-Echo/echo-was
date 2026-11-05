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
        "iam": {
          "result": "UNKNOWN", "findingCount": 0, "hasException": true, "gateStatus": "WAIVED",
          "exception": {"id": "…", "controlId": "C-IAM-2", "reason": "…", "expiresAt": "…", "approvedBy": {"id": "…", "login": "…"}},
          "mode": null, "checkedAt": null, "expiresAt": "…",
          "controls": [ … ]
        },
        "encryption": { … },
        "auditLog":   { … },
        "policy": {
          "result": "FAIL", "findingCount": 2, "hasException": false, "gateStatus": "BLOCKED", "exception": null,
          "mode": "REAL", "checkedAt": "…", "expiresAt": "…",
          "controls": [
            {"controlId": "C-IMG-1", "title": "이미지 취약점", "result": "FAIL", "isMandatory": true,
             "reasons": ["CONTROL_FAILED"], "findingIds": ["…"], "mode": "REAL",
             "checkedAt": "…", "expiresAt": "…", "serviceId": "…",
             "gateStatus": "BLOCKED", "exception": null}
          ]
        }
      }
    }
  ]
}
```

- 행 = 조직의 살아 있는 환경(만든 순, 최대 200), 칸은 늘 4개(빈칸 없음, NFR-09).
- **판정 시점은 준비(PREPARATION)**다. 실행 직전에만 보는 통제(이미지 일치 C-IMG-3·DB 쓰기 C-DB-1·목적지 C-DST-1·사전 점검 C-AI-1)는 매트릭스에 없고 이전 게이트가 본다. 이 통제들의 기한부 예외도 매트릭스에 나타나지 않는다(예외 API에서 본다).
- `result`(칸·통제) = **예외 반영 전 증거 값**이다. 칸은 그 칸 통제들의 가장 나쁜 값: FAIL > 필수 UNKNOWN > WARN > 선택 UNKNOWN > PASS. 칸의 통제가 모두 NA면 NA. 게이트가 막는지는 `gateStatus`로 따로 본다.
- **환경 단위 통제**(범위가 환경·WAS 버전·없음)는 환경마다 한 줄, **서비스 단위 통제**(범위에 서비스·이미지 digest·설정 커밋, 예: C-IMG-1·C-IMG-2·C-TLS-1)는 그 환경에 현재 배포가 있는 서비스마다 한 줄(`serviceId`)이다. 서비스가 없는 환경은 서비스 단위 통제를 세지 않는다.
- 증거는 그 서비스의 **지금 도는 이미지 digest**와 같을 때만 쓴다. 다른 digest의 증거는 `SCOPE_MISMATCH`로 버리고 UNKNOWN이다.
- `findingCount` = 그 칸 판정에 쓴 증거 수. `mode` = 쓴 증거 중 가장 약한 모드(`REAL` < `CACHE`), `checkedAt` = 가장 오래된 수집 시각, `expiresAt` = 쓴 증거·적용된 예외 중 가장 이른 만료.
- `reasons` 값은 [용어 사전](../../.claude/rules/term-dictionary.md) "보안 판정 값"의 판정 사유(`EVIDENCE_MISSING` 등)다. 예외가 걸리면 `EXCEPTION_APPLIED`, 만료된 예외만 있으면 `EXCEPTION_EXPIRED`가 붙는다.

### 기한부 예외와 게이트 관점

입력은 이전 게이트와 같다: 조직의 유효 증거, 승인·미철회 예외(최근 7일 안에 만료된 것 포함), 대상 범위의 서버 `WAS_VERSION`, 조직 `allowSelfApproval`. 판정도 같은 규칙표 함수(`evaluate_control`)가 한다. 예외가 유효한지는 [기한부 예외 API](security-exceptions.md)의 범위·자기 승인 규칙을 따른다(`WAS_VERSION`이 없거나 다르면 WAS 버전 범위 예외는 적용되지 않는다).

`gateStatus`: 게이트가 이 통제·칸을 막는가(먼저 맞는 것).

| 대상 | `BLOCKED` | `WAIVED` | `PASS` |
|---|---|---|---|
| 통제 줄 | 필수 + 증거 FAIL·UNKNOWN + 유효 예외 없음 | FAIL·UNKNOWN에 유효 예외 적용(필수·선택 모두) | 그 밖(PASS·WARN·NA, 선택 통제의 FAIL·UNKNOWN) |
| 칸 | 통제 줄 중 하나라도 `BLOCKED` | 아니고 하나라도 `WAIVED` | 그 밖 |

- `hasException` = 칸 통제 중 하나라도 유효 예외가 적용되면 true.
- `exception` = `{id, controlId, reason, expiresAt, approvedBy{id, login}}` 또는 null. 통제 줄은 그 통제에 적용된 예외, 칸은 적용된 예외 중 **가장 먼저 만료되는 것**. `approvedBy`는 사용자 행을 못 찾으면 null. 범위·요청자는 예외 API에서 본다.
- PASS인 통제에는 예외를 적용하지 않는다(`exception` null, `gateStatus` `PASS`).
- 준비 시점 판정이다. 실행 전용 통제와 실행 직전 C-TLS-1 재확인이 있어 `gateStatus`가 모두 막지 않아도 이전 게이트가 막을 수 있다.

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

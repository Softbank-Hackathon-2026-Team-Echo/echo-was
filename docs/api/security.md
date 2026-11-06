# 보안 매트릭스·증거·이전 전후 비교 API (보안·거버넌스 4·5-2, 계약 4-7)

조직의 환경마다 **권한·암호화·감사 기록·정책** 4칸의 보안 상태와 그 근거(통제·사유·증거)를 돌려준다. 판정은 공통 보안 규칙표(`app/services/security_rules.py`, 정책 버전 `POLICY_VERSION`)가 하고, 이 API는 읽기만 한다.

- 권한: 뷰어 이상(에이전트 토큰 가능). 다른 조직은 404 `ORGANIZATION_NOT_FOUND`.
- 비밀값 원문은 응답에 없다(검사 원문을 증거로 바꿀 때 이미 버렸다).
- 결과 값: `PASS` · `WARN` · `FAIL` · `UNKNOWN`(확인 못 함: 증거 없음·만료·범위 다름) · `NA`(그 환경에 해당 없음). **확인하지 못한 것은 PASS가 아니다.**

## GET `/api/v1/orgs/{orgId}/security/matrix`

```json
{
  "policyVersion": "sec-rules-2026.11.06",
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
- **C-SEC-1(비밀 전달 경로, `encryption` 칸, 서비스 단위)은 조회 때 계산한다.** 서비스마다 서비스 중 연결(비율이 가장 큰 연결)의 앱 환경 변수 **키**가 그 환경 연결에 모두 있으면 PASS, 빠지면 FAIL(필수 → BLOCKED). 서비스 중 연결 자신은 PASS 다. 값은 읽지 않는다. 이 증거는 저장하지 않아 `findingIds` 의 id(조회마다 같음)로 증거 목록을 불러도 나오지 않는다 — 빠진 키는 이전 게이트가 남긴 증거(`evidenceRef = migration-plan:…`) 또는 변수 API 로 본다. 현재 배포가 있는 연결만 본다 — 첫 배포 전 변수만 넣은 연결(API 서버 10-4)은 행의 `services`·C-SEC-1 기준·판정 대상에 들어가지 않는다. 값이 맞는지·도착 배포에 반영됐는지(`pendingRedeploy`)·Secret 저장 암호화는 보지 않는다([1-2 SPEC](../../specs/sec-1-2-secret-delivery.md)).
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

## GET `/api/v1/orgs/{orgId}/security/compare?source=&target=[&serviceId=]`

이전 전후 보안 비교(보안 5-2, SPEC `specs/sec-5-2-security-compare.md`). 두 환경의 매트릭스 칸을 나란히 놓고, 칸마다 **도착 환경이 이전을 막는지**를 준다. SEC Compare 탭과 이전 계획 승인 화면(E01-3) 비교표의 원천이다.

| 쿼리 | 뜻 |
|---|---|
| `source` · `target` | 출발·도착 환경 id(필수). 이 조직의 살아 있는 환경만 — 아니면 404 `ENVIRONMENT_NOT_FOUND` |
| `serviceId` | 선택. 서비스 단위 통제를 그 서비스의 현재 배포로만 센다. 이 조직의 살아 있는 서비스가 아니면 404 `SERVICE_NOT_FOUND` |

- 권한: 뷰어 이상(에이전트 토큰 가능). 같은 환경을 두 번 주면 422 `VALIDATION_ERROR`(`details=[{field: "target", reason: "SAME_ENVIRONMENT"}]`, DB를 읽기 전에 거절). 쿼리가 빠지거나 UUID가 아니면 422 `VALIDATION_ERROR`.
- 캐시 없음. 매트릭스와 같은 요청 시각의 값이다(두 화면이 다르게 보이지 않게).

```json
{
  "policyVersion": "sec-rules-2026.11.06",
  "phase": "PREPARATION",
  "checkedAt": "2026-11-05T14:00:00Z",
  "simulated": false,
  "serviceId": null,
  "source": {"environmentId": "…", "environmentName": "aws-tokyo", "environmentKind": "aws",
             "services": [{"serviceId": "…", "name": "demo", "imageDigest": "sha256:…"}]},
  "target": {"environmentId": "…", "environmentName": "gcp-seoul", "environmentKind": "gcp", "services": []},
  "summary": {"blocking": 1, "worseOnTarget": 0, "same": 3, "better": 1},
  "rows": [
    {"control": "iam",
     "source": {"result": "UNKNOWN", "findingCount": 0, "hasException": true, "gateStatus": "WAIVED", "exception": {"…": "…"}, "mode": null, "checkedAt": null, "expiresAt": "…", "controls": [ … ]},
     "target": {"result": "UNKNOWN", "findingCount": 0, "hasException": true, "gateStatus": "WAIVED", "…": "…"},
     "blocksMigration": false, "blockingControlIds": [], "change": "SAME"},
    {"control": "encryption",
     "source": {"result": "UNKNOWN", "gateStatus": "BLOCKED", "…": "…"},
     "target": {"result": "UNKNOWN", "gateStatus": "BLOCKED", "…": "…"},
     "blocksMigration": true, "blockingControlIds": ["C-TLS-1"], "change": "SAME"},
    {"control": "auditLog", "…": "…", "blocksMigration": false, "change": "SAME"},
    {"control": "policy",
     "source": {"result": "FAIL", "gateStatus": "BLOCKED", "…": "…"},
     "target": {"result": "UNKNOWN", "gateStatus": "PASS", "…": "…"},
     "blocksMigration": false, "blockingControlIds": [], "change": "BETTER"}
  ]
}
```

- `rows`: 늘 4줄(`iam`·`encryption`·`auditLog`·`policy` 순). `control` = 매트릭스 칸 이름. 줄의 `source`·`target`은 **매트릭스 칸 응답 그대로**(`result`·`findingCount`·`hasException`·`gateStatus`·`exception`·`mode`·`checkedAt`·`expiresAt`·`controls[]`) — `serviceId` 없이 부르면 같은 시각 매트릭스의 그 두 행 칸과 같다.
- `blocksMigration` = 도착 칸 `gateStatus`가 `BLOCKED`. 곧 도착 칸 통제 중 하나라도 **필수 + 증거 FAIL·UNKNOWN + 유효 예외 없음**(규칙표 `evaluate_control`의 `is_blocking`, 이전 게이트와 같은 함수). 출발 칸 값은 보지 않는다. 예외로 풀린 칸(`WAIVED`)과 선택 통제의 FAIL은 막지 않는다.
- `blockingControlIds` = 도착 칸에서 막은 통제 ID(중복 없음). 막지 않으면 빈 목록.
- `change` = 도착이 출발보다 `WORSE`·`SAME`·`BETTER`. 비교는 칸 `result` 심각도(NA < PASS < WARN < UNKNOWN < FAIL), 같으면 `gateStatus`(PASS < WAIVED < BLOCKED).
- `summary` = 4줄을 센 값: `blocking`(막는 줄 수) · `worseOnTarget` · `same` · `better`(셋의 합 4).
- 맨 위 `source`·`target` = 비교한 환경과 그 비교에 센 서비스(지금 도는 이미지). `simulated`는 늘 false(서버 판정).
- `serviceId`를 주면 환경 단위 통제는 그대로, 서비스 단위 통제(C-IMG-1·C-IMG-2·C-CFG-1·C-TLS-1)는 **그 서비스의** 현재 배포로만 한 줄이다. 그 환경에 배포가 없으면 매트릭스처럼 세지 않는다. 주지 않으면 그 환경의 모든 서비스를 센다 — 도착 환경의 다른 서비스 때문에 막힘이 보일 수 있으므로 서비스가 정해진 화면(E01-3)은 `serviceId`를 주는 것이 게이트에 가깝다.
- 준비 시점 비교다. 실행 전용 통제(C-IMG-3·C-DB-1·C-DST-1·C-AI-1)와 실행 직전 C-TLS-1 재확인은 없으므로, `blocksMigration`이 모두 false여도 이전 게이트가 막을 수 있다. 막는지의 최종 판정은 게이트(승인 409 `SECURITY_GATE_BLOCKED`)다.

## GET `/api/v1/orgs/{orgId}/security/findings`

칸의 근거 드로어용 증거 목록(최신순, 페이지).

| 쿼리 | 뜻 |
|---|---|
| `page`·`size` | 공통 페이지(기본 0·20, 최대 100) |
| `environmentId` · `serviceId` · `controlId` | 그 값과 같은 증거만(`controlId`는 `C-IMG-1` 표기) |
| `findingId` (여러 번) | 매트릭스 `findingIds`로 고르기, 최대 100개 |
| `includeExpired` | 기본 `false`(만료되지 않은 것만) |

항목: `id`·`controlId`·`result`·`reason`(정규화 이유, 예: `FIXABLE_HIGH_VULNERABILITY`)·`source`(`TRIVY_IMAGE`·`TRIVY_SECRET`·`GITLEAKS`·`TLS_PROBE`·`MANUAL_REVIEW`·`AI_REVIEW`·`CVE_WATCH` …)·`origin`(`RULE` 검사 도구 · `HUMAN` 사람 점검 · `AI`)·`mode`·`confirmation`(AI만)·`policyVersion`·`toolVersion`·`serviceId`·`environmentId`·`imageDigest`·`configCommit`·`wasVersion`·`collectedAt`·`expiresAt`·`evidenceRef`(원문 위치, 예: 검사 실행)·`reviewerUserId`·`basisUrl`·`items[]`(`itemId`·`severity`·`target`·`installedVersion`·`fixedVersion`·`line`·`commit`·`reason`, 최대 50)·`totalItems`·`remediation`(고칠 방법, CVE 감시 증거만 — `{kind: DEPENDENCY_BUMP, ecosystem, package, fromVersion, toVersion, advisoryId, aliases, severity, summary}`, 그 밖 null. [cve-watch.md](cve-watch.md)).

C-IMG-1 은 빌드 검사(`TRIVY_IMAGE`)와 CVE 감시(`CVE_WATCH`, 배포된 이미지 의존성에 새로 나온 HIGH·CRITICAL 권고) 증거를 함께 받는다. 범위는 이미지 digest 라 같은 digest 의 두 출처 중 나쁜 값이 칸 값이다.

- AI 결과(`origin: AI`)는 PASS가 없고, 사람이 `ESCALATED`로 확인해야 차단 사유가 된다.

## 다음 (같은 이슈)

- (11/5 됨) `GET …/security/compare?source=&target=` 이전 전후 비교 — 위 절. 게이트와 같은 판정(`is_blocking`)을 쓴다
- (11/7 됨) WAS 버전 범위 통제(C-IAM-2·C-AUD-1)의 증거: 보안 게이트가 켜진 일꾼이 시작 때와 `SECURITY_PLATFORM_SELF_TEST_INTERVAL_SECONDS`(기본 21600, 300~28800)마다 플랫폼 자기 시험을 돌려 조직 + WAS 버전 범위 증거(`sourceRef = self-test:platform`, 유효 24시간)를 남긴다. C-IAM-2(`WAS_TEST`) = 일회용 에이전트 토큰으로 쓰기 경로 35개(승인 3개·배포 실패 진단 1개 포함) 호출 → 모두 403 `AGENT_READ_ONLY` 면 PASS, C-AUD-1(`AUDIT_LOG`) = 쓰기 경로 이름표 전수 + 최근 24시간 승인·게이트 판정·AI 실행·예외·거부의 감사 줄 대조. 환경 칸이 없어 모든 환경 행이 같은 증거를 쓴다. PASS 면 예외는 적용되지 않는다(그대로 만료) — [5-3 SPEC](../../specs/sec-5-platform-evidence.md)
- (11/5 됨) C-TLS-1 주기 시험: 보안 게이트가 켜진 일꾼이 `SECURITY_TLS_PROBE_INTERVAL_SECONDS`(기본 1800, 최대 3000)마다 현재 배포가 있는 연결의 대표 주소·환경별 주소를 게이트와 같은 시험기로 시험해 서비스 + 환경 범위 증거(`sourceRef = tls-probe:periodic`, 유효 1시간)
- (11/6 바뀜) C-SEC-1 은 빌드 비밀 검사에서 만들지 않는다(11/5 의 파생 증거는 C-IMG-2 복사라 지움). 서비스 × 환경의 앱 환경 변수 키 비교로 다시 정의했다 — 위 매트릭스 절·[1-2 SPEC](../../specs/sec-1-2-secret-delivery.md). `policyVersion` 이 `sec-rules-2026.11.06` 으로 바뀌어 그 전에 저장된 빌드 검사 증거는 다시 빌드해야 쓰인다

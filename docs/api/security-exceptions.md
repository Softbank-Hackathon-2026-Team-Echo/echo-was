# 기한부 예외 API — 요청 · 승인 · 철회 · 조회 (보안·거버넌스 3)

**기한부 예외**는 보안 통제 하나를 정해진 범위 안에서 최대 7일 동안 막지 않게 하는 기록이다. 규칙표 판정에서 예외가 걸린 FAIL·UNKNOWN 은 PASS 가 아니라 **WARN("예외 적용 중")** 이고 이전 게이트를 막지 않는다. 보안 매트릭스는 증거 값(`result`)을 그대로 두고 `gateStatus: WAIVED`·`hasException`·`exception` 으로 보인다([보안 매트릭스 API](security.md)). 사람만 요청·승인하고, 사유·만료가 있어야 하며, 철회할 수 있고 지우지 않는다. 근거: SPEC [specs/sec-3-2-security-exceptions-api.md](../../specs/sec-3-2-security-exceptions-api.md), 판정 규칙 `app/services/security_rules.py`.

모든 경로는 `/api/v1/orgs/{orgId}` 아래, 응답은 공통 봉투 `ApiResponse`(성공 `data`, 실패 `code`·`message`·`details`)다.

## 엔드포인트

| 메서드 · 경로 | 성공 | 주요 실패 | 역할 |
|---|---|---|---|
| `POST /security/exceptions` | 201 `SecurityException`(`PENDING`) | 422 `VALIDATION_ERROR`, 404 `SERVICE_NOT_FOUND`·`ENVIRONMENT_NOT_FOUND` | 개발자(사람) |
| `GET /security/exceptions?controlId&serviceId&environmentId&status&page&size` | 200 `Page<SecurityException>` | 422 | 뷰어 |
| `GET /security/exceptions/{exceptionId}` | 200 `SecurityException` | 404 `SECURITY_EXCEPTION_NOT_FOUND` | 뷰어 |
| `POST /security/exceptions/{exceptionId}/approve` | 200 (`ACTIVE`) | 403 `SELF_APPROVAL_FORBIDDEN`, 409 `SECURITY_EXCEPTION_NOT_PENDING`, 404 | 개발자(사람) |
| `POST /security/exceptions/{exceptionId}/revoke` | 200 (`REVOKED`) | 409 `SECURITY_EXCEPTION_CLOSED`, 404 | 개발자(사람) |

에이전트 토큰은 조회만 한다(쓰기 403 `AGENT_READ_ONLY`). 다른 조직의 예외는 404 다. 코드: `app/routers/security_exception_router.py`, `app/services/security_exception_service.py`.

## 만들기 `POST /security/exceptions`

```json
{
  "controlId": "C-DB-1",
  "serviceId": "7c1e…",
  "environmentId": "2b9d…",
  "reason": "D04~D07 결정 전, 시연은 쓰기 정지 구간에서 수동 확인",
  "expiresAt": "2026-11-08T03:00:00Z"
}
```

- `reason`: 앞뒤 공백을 빼고 1~1000자.
- `expiresAt`: 시간대가 있는 시각. **지금보다 뒤이고, 지금부터 7일 이하**(정확히 7일 포함). 넘으면 422 `details[{field: "expiresAt", reason: "MAX_7_DAYS"}]`, 지금 이전이면 `NOT_AFTER_NOW`. 만든 시각(`createdAt`)은 서버 시각이다.
- **범위 칸은 그 통제가 정한 것만, 모두 채운다**(나머지는 생략 또는 null). 빠지면 `REQUIRED_FOR_CONTROL`, 더 넣으면 `NOT_ALLOWED_FOR_CONTROL`(`details[].field` = 칸 이름). 서비스·환경은 이 조직의 살아 있는 것이어야 한다(아니면 404).

| 통제 | 범위 칸 |
|---|---|
| C-DB-1 · C-DST-1 · C-AI-1 · C-TLS-1 · C-POST-1 | `serviceId` + `environmentId` |
| C-IMG-3 | `serviceId` |
| C-IMG-1 · C-IMG-2 | `imageDigest`(`sha256:` + 16진 64자) |
| C-CFG-1 | `configCommit`(16진 40자) |
| C-IAM-2 · C-AUD-1 | `wasVersion`(1~64자, 서버 설정 `WAS_VERSION` 과 같은 값) |
| C-TLS-2 · C-NET-1 · C-NET-2 · C-IAM-1 · C-AUD-2 | `environmentId` |
| C-SEC-1 · C-CI-1 | 없음(조직 전체) |

- **C-DB-1 · C-DST-1 · C-AI-1 · C-POST-1 의 `environmentId` = 이전 도착 환경.** 이전 게이트는 판정 범위를 도착 환경(`target_environment_id`)으로 만든다(`app/services/security_gate.py`). 출발 환경으로 만든 예외는 `ACTIVE` 로 보여도 범위가 맞지 않아 이전이 막힌다.

만든 예외는 `PENDING`(승인 대기)이고 판정에 쓰이지 않는다.

## 감사 기록

요청·승인·철회는 그 작업과 같은 트랜잭션에서 감사 줄을 남긴다(`docs/api/audit-logs.md`): `SECURITY_EXCEPTION_REQUESTED`·`SECURITY_EXCEPTION_APPROVED`·`SECURITY_EXCEPTION_REVOKED`, 대상 `security_exception`(이름표 = 통제 ID), 요청 줄 `details` 는 `{expiresAt}` 만. 막힌 자기 승인은 거부 줄(403)이다. 만료는 따로 줄을 남기지 않는다(조회 때 `EXPIRED`).

## 승인 · 철회

- **승인**: `PENDING` 일 때만(아니면 409 `SECURITY_EXCEPTION_NOT_PENDING`). 기본은 **요청한 사람이 아닌** 개발자다. 조직 설정 `allowSelfApproval = false`(기본)이면 요청한 사람의 승인은 403 `SELF_APPROVAL_FORBIDDEN`(거부 기록 남음). `true` 면 허용한다(판정도 같은 스위치를 본다).
- **철회**: 철회 전·만료 전이면 승인 전이어도 된다(아니면 409 `SECURITY_EXCEPTION_CLOSED`). 철회한 예외는 판정에 다시 쓰지 않는다.
- 두 동작은 조건부 UPDATE 라 동시에 눌러도 한 번만 반영된다.

## 상태 `status`

저장하지 않고 조회할 때 정한다(위에서부터 먼저 맞는 것).

| 값 | 조건 | 판정에 쓰나 |
|---|---|---|
| `REVOKED` | 철회함 | 아니오 |
| `EXPIRED` | `expiresAt <= 지금`. 승인 대기(`PENDING`)인 채로 기한이 지난 것도 `EXPIRED` | 아니오(판정 사유에 "예외 만료"가 붙는다) |
| `ACTIVE` | 승인됨 | 예(범위가 맞는 FAIL·UNKNOWN 을 막지 않는다. 매트릭스는 `gateStatus: WAIVED`) |
| `PENDING` | 승인 전 | 아니오 |

## 응답 `SecurityException`

`id`, `controlId`, `status`, `serviceId`, `environmentId`, `imageDigest`, `configCommit`, `wasVersion`(범위 칸, 없으면 null), `reason`, `requestedBy{id, login}`, `approvedBy`·`approvedAt`, `revokedBy`·`revokedAt`, `createdAt`, `expiresAt`. 목록은 `createdAt` 역순, `size` 기본 20·최대 100, `status`·`controlId` 는 여러 번 줄 수 있다.

## 매트릭스에 보이는 범위

- 매트릭스는 준비 시점 통제만 줄이 있다. 실행 전용 통제(C-DB-1·C-DST-1·C-AI-1·C-IMG-3)의 예외는 이전 게이트에만 쓰이고 매트릭스에는 나타나지 않는다. 이 API 로 본다.

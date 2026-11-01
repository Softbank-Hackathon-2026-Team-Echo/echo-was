# 조직·멤버·초대·에이전트 토큰 API (API 서버 6)

고객 회사 단위의 **조직**을 만들고, 멤버를 초대하고 역할을 바꾸고, AI 에이전트용 토큰을 발급한다. 서비스·환경·배포 요청은 조직에 속하고 다른 조직에는 보이지 않는다. 근거: SPEC [specs/api-6-github-login-roles.md](../../specs/api-6-github-login-roles.md). 로그인·세션·권한 검사 순서·역할 표는 [auth.md](auth.md).

- 공통 봉투·camelCase·null 규칙·`X-Request-ID` 는 [environments.md](environments.md#공통-규칙)와 같다. 목록은 `Page`(`items`, `total`, `page` 0부터, `size` 1~100 기본 20).
- 경로의 `{orgId}` 는 조직 `id`(uuid)다. slug 가 아니다.
- 멤버가 아닌 조직은 없는 것과 같다(404 `ORGANIZATION_NOT_FOUND`).

## 엔드포인트

| 메서드 · 경로 | 누가 | 성공 | 실패 |
|---|---|---|---|
| `POST /api/v1/orgs` `{slug, name}` | 로그인한 사람 | 201 `Organization`(`role = ADMIN`) | 422 `VALIDATION_ERROR`, 409 `ORGANIZATION_SLUG_CONFLICT`·`ORGANIZATION_SLUG_RESERVED` |
| `GET /api/v1/orgs?page&size` | 로그인한 사람 | 200 `Page[Organization]`(내가 멤버인 것, 개인 조직 먼저) | — |
| `GET /api/v1/orgs/{orgId}` | 뷰어 이상(에이전트는 자기 조직) | 200 `Organization` | 404 |
| `GET /api/v1/orgs/{orgId}/members?page&size` | 뷰어 이상, 사람 | 200 `Page[Member]`(들어온 순서) | 404 |
| `PATCH /api/v1/orgs/{orgId}/members/{userId}` `{role}` | 어드민 | 200 `Member` | 403 `CANNOT_CHANGE_OWN_ROLE`·`ROLE_FORBIDDEN`, 404 `MEMBER_NOT_FOUND`, 409 `LAST_ADMIN` |
| `DELETE /api/v1/orgs/{orgId}/members/{userId}` | 어드민(본인 탈퇴는 누구나) | 204 | 403 `ROLE_FORBIDDEN`, 404 `MEMBER_NOT_FOUND`, 409 `LAST_ADMIN` |
| `POST /api/v1/orgs/{orgId}/invitations` `{githubLogin, role}` | 어드민 | 201 `Invitation`(`PENDING`) | 422 `GITHUB_USER_NOT_FOUND`·`VALIDATION_ERROR`, 409 `ALREADY_MEMBER`, 502 `GITHUB_UNAVAILABLE` |
| `GET /api/v1/orgs/{orgId}/invitations?page&size` | 어드민 | 200 `Page[Invitation]`(최신순) | — |
| `DELETE /api/v1/orgs/{orgId}/invitations/{invitationId}` | 어드민 | 204(`REVOKED`) | 404 `INVITATION_NOT_FOUND`, 409 `INVITATION_NOT_PENDING` |
| `POST /api/v1/orgs/{orgId}/agent-tokens` `{name, expiresInDays?}` | 개발자 이상, 사람 | 201 `IssuedAgentToken`(**원문 한 번**) | 422 `VALIDATION_ERROR` |
| `GET /api/v1/orgs/{orgId}/agent-tokens?page&size` | 개발자 이상, 사람 | 200 `Page[AgentToken]`(최신순, 원문 없음) | — |
| `DELETE /api/v1/orgs/{orgId}/agent-tokens/{tokenId}` | 개발자 이상, 사람 | 204(이미 폐기여도) | 404 `AGENT_TOKEN_NOT_FOUND` |

모든 조직 경로는 그 밖에도 401 `UNAUTHENTICATED`·404 `ORGANIZATION_NOT_FOUND`·403(역할·에이전트·Origin)을 낼 수 있다([auth.md](auth.md#오류-코드)).

## 조직

- `slug`: 1~39자, 소문자·숫자·하이픈. 하이픈은 처음·끝·연속 금지(`^[a-z0-9](-?[a-z0-9]){0,38}$`). 전체에서 유일하고 **만든 뒤 바꿀 수 없다**(환경 열쇠 이름이 slug 에서 나온다). `name`: 1~100자.
- 예약 slug(`RESERVED_ORG_SLUGS`, 기본 `team-echo`)는 API 로 만들 수 없다(409 `ORGANIZATION_SLUG_RESERVED`). 데모 준비 명령만 만든다.
- 처음 로그인하면 개인 조직(`personal = true`)이 생기고 본인이 어드민이다. 다른 조직과 똑같이 초대할 수 있다.
- 조직 삭제 API 는 없다(2차 후보).

```json
{"id": "6f0c…", "slug": "acme", "name": "ACME 주식회사", "personal": false, "role": "ADMIN", "createdAt": "2026-11-02T03:00:00Z"}
```

`role` 은 요청한 사람의 역할이다. 에이전트 토큰으로 읽으면 `null`.

## 멤버

```json
{"id": "<사용자 id>", "githubLogin": "bob-ops", "name": "Bob", "avatarUrl": null, "role": "DEVELOPER", "joinedAt": "2026-11-02T03:00:00Z"}
```

- 경로의 `{userId}` 는 멤버 응답의 `id`(사용자 id)다.
- 자기 역할은 바꿀 수 없다(403 `CANNOT_CHANGE_OWN_ROLE`, 거부 기록 남음).
- 변경 뒤 어드민이 0명이 되면 409 `LAST_ADMIN`(역할 변경·내보내기·탈퇴 모두). 마지막 어드민은 탈퇴할 수 없다.
- 멤버 변경은 조직 행을 잠근 뒤 **잠금 안에서 요청한 사람의 역할을 다시 읽는다.** 두 어드민이 동시에 서로를 내리거나 내보내도 어드민은 1명 이상 남고, 진 쪽은 403 `ROLE_FORBIDDEN` 또는 409 `LAST_ADMIN` 이다.
- 탈퇴·내보내진 사람의 세션은 살아 있지만 그 조직 경로는 404 다.

## 초대

```json
{"id": "…", "githubLogin": "Bob-Ops", "githubId": 7007, "role": "DEVELOPER", "status": "PENDING",
 "invitedByUserId": "…", "expiresAt": "2026-11-09T03:00:00Z", "acceptedAt": null, "createdAt": "…"}
```

- GitHub 아이디로 받아 GitHub 공개 API(`GET /users/{login}`)로 **숫자 번호**를 얻어 저장한다(아이디는 바뀔 수 있다). 없는 아이디는 422 `GITHUB_USER_NOT_FOUND`.
- 그 번호의 사람이 이미 멤버면 409 `ALREADY_MEMBER`. 같은 사람의 대기 초대가 있으면 그것을 `REVOKED` 로 닫고 새로 만든다.
- **적용**: 그 사람이 로그인하거나(콜백) `GET /api/v1/me` 를 부르면, 만료 전 `PENDING` 초대가 정한 역할의 멤버가 되고 `ACCEPTED` 다. 이미 멤버면 역할은 그대로 두고 `ACCEPTED`. 만료된 것은 `EXPIRED`. 이미 로그인해 있던 사람은 프론트가 `/me` 를 부르면 바로 들어온다. 동시에 두 번 불러도 한 번만 적용된다.
- 유효 기간 7일(`INVITATION_TTL_SECONDS`). 목록을 읽을 때 기한 지난 `PENDING` 은 `EXPIRED` 로 닫는다.
- 상태: `PENDING` → `ACCEPTED` | `EXPIRED` | `REVOKED`(끝 상태).

## 에이전트 토큰

```json
{"id": "…", "name": "triage-bot", "last4": "x9Qa", "createdBy": "<사용자 id>", "createdAt": "…",
 "expiresAt": "…", "revokedAt": null, "lastUsedAt": null, "token": "echo_agt_…"}
```

- `token`(원문, `echo_agt_` + base64url 32바이트)은 **발급 응답에만 한 번** 싣는다. 다시 볼 수 없다. DB 에는 SHA-256 과 끝 4자리(`last4`)만 둔다.
- `expiresInDays` 1~90, 빼면 30(`AGENT_TOKEN_DEFAULT_DAYS`). `AGENT_TOKEN_MAX_DAYS` 를 넘으면 422 `VALIDATION_ERROR`(`details[{field: "expiresInDays", reason: "EXPIRES_IN_DAYS_TOO_LONG"}]`).
- 토큰은 조직 하나에 묶이고 그 조직을 **읽기만** 한다([auth.md](auth.md#에이전트-토큰-인증)). 폐기·만료되면 401.
- 발급한 개발자가 조직을 떠나도 토큰은 남는다(조직의 개발자가 폐기한다).

## 조직 범위가 생긴 API 2·3

서비스·환경·배포 요청 경로는 모두 `/api/v1/orgs/{orgId}` 아래다([services.md](services.md)·[environments.md](environments.md)·[deploy-requests.md](deploy-requests.md)).

- 환경 이름은 **조직 안에서** 유일하다(두 조직에 `aws-seoul` 이 각각 있을 수 있다).
- 서비스 이름은 대표 주소(`{서비스}.{도메인}`) 때문에 **Echo 전체에서** 유일하다.
- 환경 열쇠 이름: `env:` 비밀 참조는 `ECHO_SECRET_<조직 키>__` 로 시작해야 한다. 조직 키는 slug 의 `-` 를 `_` 로 바꾸고 대문자(`team-echo` → `TEAM_ECHO`). 예 `env:ECHO_SECRET_TEAM_ECHO__AWS_SEOUL_KUBECONFIG`. 아니면 422 `SECRET_REF_NOT_IN_ORGANIZATION`. slug 에 하이픈 연속이 없어 키에 `__` 가 생기지 않으므로 `a-corp` 와 `a-corp-x` 는 서로의 열쇠를 가리킬 수 없다.
- 다른 조직의 서비스·환경·배포 요청은 각 리소스의 404 다. 배포 요청 접수에서 환경이 서비스와 다른 조직이면 404 `ENVIRONMENT_NOT_FOUND`.

## 데모 준비 명령

```bash
uv run --env-file .env python -m app.scripts.demo_seed --org team-echo --admin <GitHub 아이디> --viewer <GitHub 아이디>
```

두 사람이 한 번씩 로그인해 둔 뒤 실행한다. 조직이 없으면 만들고(예약 slug 도 이 명령은 만든다), `--admin` 은 ADMIN, `--viewer` 는 VIEWER 로 맞추고, 에이전트 토큰 하나를 발급해 원문을 표준 출력에 한 번 쓴다. 두 번 돌려도 조직·멤버는 늘지 않는다(토큰은 돌릴 때마다 새로). 로그인한 적 없는 아이디, 남이 먼저 만든 같은 slug 조직이면 분명한 메시지로 실패한다(종료 코드 1).

출력 예:

```text
organization: team-echo (6f0c…) created
admin: kim-admin ADMIN
viewer: lee-viewer VIEWER
agent token 2b1d… (expires 2026-12-02T03:00:00+00:00), shown once:
echo_agt_…
```

출력된 토큰으로 배포 요청(`POST …/services/{id}/deploy-requests`)을 보내면 403 `AGENT_READ_ONLY` 이고 `audit_logs` 에 `actor_type = AGENT` 한 줄이 남는다(데모 거부 장면).

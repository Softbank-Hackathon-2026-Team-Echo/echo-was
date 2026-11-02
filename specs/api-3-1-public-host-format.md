# API 서버 3-1: 고객 앱 주소 형식 변경 SPEC

- 상태: Confirmed
- 확인일: 2026-11-02
- Depth: Standard
- 근거: 지민 결정(11/2) — 다른 서비스 조사 결과 "이름 + 무작위 8자, 기본 도메인 바로 아래 한 단계" 추천안 채택, 도메인은 추가 구매 없이 `echoport.uk`
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/3 (원터치 배포, In Progress) · 관련 https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/6 (조직 권한)
- 저장소: `echo-was` · 브랜치 `feat/random-public-host` (`main` = `04578c0`에서)

## 1. 목표

고객 앱 주소를 다른 PaaS처럼 "이름 + 무작위 값"으로, 기본 도메인 바로 아래 한 단계에 만든다.

| 주소 | 지금 | 바꾼 뒤 |
|---|---|---|
| 대표 | `{서비스}.{도메인}` | `{서비스}-{suffix}.{도메인}` |
| 환경별 | `{서비스}.{환경}.{도메인}` | `{서비스}-{환경}-{suffix}.{도메인}` |

예(데모, `PUBLIC_BASE_DOMAIN=echoport.uk`): `shop-a1b2c3d4.echoport.uk`, `shop-aws-seoul-a1b2c3d4.echoport.uk`.

이유:
- **무작위 값**: 지운 서비스와 같은 이름으로 새 서비스를 만들어도 옛 주소를 이어받지 못하게 한다(하위 도메인 탈취 방지, Heroku 12자 무작위 값과 같은 이유).
- **한 단계**: 와일드카드 인증서·DNS 레코드 하나(`*.도메인`)로 모든 고객 앱을 덮는다(Vercel `-git-`, Netlify `--`, Northflank `--` 방식). Cloudflare 무료 인증서도 한 단계까지만 덮는다.
- **서비스 이름은 조직 안에서만 유일**: 주소 유일성은 무작위 값이 맡으므로, 다른 조직과 이름이 겹쳐도 된다(API 6 SPEC의 "서비스 이름은 Echo 전체 유일"을 바꾼다).

## 2. 규칙

- `suffix`: 소문자·숫자 8자(`^[a-z0-9]{8}$`, `secrets` 모듈). 서비스를 만들 때 한 번 정해 `services.host_suffix`에 저장하고 바꾸지 않는다.
- 대표 주소 레이블 = `{name}-{suffix}`. 환경별 주소 레이블 = `{name}-{environment.name}-{suffix}`(같은 서비스의 suffix를 쓴다).
- 레이블이 63자를 넘으면 suffix 앞부분(`{name}` 또는 `{name}-{env}`)을 54자로 자르고 끝의 `-`를 뗀 뒤 `-{suffix}`를 붙인다.
- 대표 주소가 이미 쓰이면(살아 있는 행의 unique 위반) 새 suffix로 최대 3번 다시 시도한다. 그래도 겹치면 500이 아니라 409 `PUBLIC_HOST_CONFLICT`.
- 환경별 주소 충돌(잘림으로 생긴 경우)은 지금처럼 기존 충돌 처리 경로를 따른다(500 금지).
- 서비스 이름 유일성: `uq_services_name_live`(전체) → `uq_services_org_name_live`(`organization_id`, `name`, 살아 있는 행). 겹치면 기존 409 `SERVICE_NAME_CONFLICT`(이제 조직 안에서만).
- `public_host` 칸(대표·환경별)과 그 전체 unique 인덱스는 그대로 둔다. 응답의 `url` 칸도 그대로(값의 형식만 바뀜).
- 예약 이름은 두지 않는다: 고객 앱 레이블에는 항상 `-{suffix}`가 붙으므로 `app`·`api`·`www`와 겹칠 수 없다.
- `PUBLIC_BASE_DOMAIN` 기본값(`apps.echo.test`)은 그대로 둔다(로컬·시험용). 데모 배포 값은 `echoport.uk`.

## 3. 범위

- `app/services/deploy_support.py`: suffix 생성 함수(주입 가능, 시험에서 고정 값), 대표·환경별 주소 함수에 suffix·자르기 규칙
- `app/models/service.py`: `host_suffix` 칸(char(8), NOT NULL, CHECK 형식), 인덱스 교체. `service_service.py`의 `NAME_UNIQUE_INDEX` 상수, 생성 시 suffix·재시도
- `app/services/deploy_request_service.py`: 환경별 주소에 suffix
- 새 Alembic revision 1개: `services`·`service_environments`에 행이 있으면 분명한 메시지로 실패(API 6과 같은 방식), 칸 추가, 인덱스 교체. downgrade 왕복. `db-schema.sql` 다시 뽑기, `test_migrations.py` 기대 목록 갱신
- 시험: 고정 URL을 쓰는 기존 시험을 형식 검사나 주입한 suffix로 바꾼다(지우거나 건너뛰지 않는다). 새 시험 — 주소 형식 정규식, 63자 자르기(이름 63자 + 환경 63자), suffix 충돌 재시도(주입 생성기), 다른 조직 같은 서비스 이름 둘 다 201, 같은 조직 409, 같은 이름을 지우고 다시 만들면 대표 주소가 달라짐
- 문서: `docs/api/services.md`·`docs/api/deploy-requests.md` 예시, `docs/schema.md`(칸·인덱스·주소 설명), 용어 사전(퍼블릭 주소 줄), `README.md`("데모 배포 주소" 표: 고객 앱 `{서비스}-{무작위 8자}.echoport.uk`, 환경별 `{서비스}-{환경}-{무작위 8자}.echoport.uk`, `PUBLIC_BASE_DOMAIN=echoport.uk`, DNS·인증서는 `*.echoport.uk` 하나), `.env.example` 데모 블록, `specs/api-6-github-login-roles.md`는 고치지 않고 이 SPEC이 그 부분을 바꾼다고 README·schema.md에 적는다

### 데모 세션 쿠키 이름 (AI 기본값 ★, 거부 가능)
고객 앱이 같은 등록 도메인(`echoport.uk`) 아래에 있으므로, 고객 앱 JavaScript가 `Domain=echoport.uk` 쿠키로 우리 세션 쿠키를 덮어쓰지 못하게 데모 배포 값으로 `SESSION_COOKIE_NAME=__Host-echo_session`을 README·`.env.example` 데모 블록에 적는다(`__Host-` 접두어 쿠키는 Secure·Path=/·Domain 없음이어야만 저장된다). 코드가 세션 쿠키에 Domain을 붙이지 않고 Path=/인지 확인하고, 이 이름으로도 로그인 흐름 시험이 통과하는지 시험 하나를 더한다. 로컬 기본값(`echo_session`)은 그대로.

## 4. 제외
- 별도 고객 앱 도메인 구매, PSL 등록
- DNS 레코드 등록(M1이 Cloudflare에서), 실제 실행기 연결
- 상태 쿠키(`echo_oauth_state`) 이름·경로 변경

## 5. Acceptance criteria
- `AC-1` 서비스 등록 응답의 대표 `url`이 `^https://{name}-[a-z0-9]{8}\.{도메인}$`이고, DB `host_suffix`와 일치한다.
- `AC-2` 배포 요청으로 만든 환경별 주소가 `{name}-{env}-{같은 suffix}.{도메인}`이다.
- `AC-3` 63자 이름 + 63자 환경 이름에서도 레이블이 63자 이하이고 끝이 `-{suffix}`다.
- `AC-4` 주입한 생성기가 이미 쓰인 suffix를 두 번 낸 뒤 새 값을 내면 201, 계속 같은 값을 내면 409 `PUBLIC_HOST_CONFLICT`(500 아님).
- `AC-5` 조직 A·B에 같은 서비스 이름 → 둘 다 201, 주소는 다름. 같은 조직에 같은 이름 → 409 `SERVICE_NAME_CONFLICT`. 지우고 같은 이름으로 다시 만들면 새 주소.
- `AC-6` (★) `SESSION_COOKIE_NAME=__Host-echo_session`, `SESSION_COOKIE_SECURE=true`로 로그인 → 세션 쿠키에 Domain 없음·Path=/·Secure, `/me` 200.
- `AC-7` 마이그레이션 왕복, `alembic check` 0, `dump_schema.sh` 일치, `test_migrations.py` 통과, 행이 있으면 upgrade가 분명히 실패.
- `AC-8` ruff·format·`mypy app`·pytest 통과, 통합 시험 건너뜀 0, 기존 시험 수가 줄지 않음.

## 6. 결정 기록
| ID | 결정 | 출처 |
|---|---|---|
| D1 | 주소 = 이름 + 무작위 8자, 기본 도메인 바로 아래 한 단계 | 지민 11/2 (추천안 채택) |
| D2 | 도메인 추가 구매 없이 `echoport.uk` | 지민 11/2 |
| D3 | 서비스 이름은 조직 안에서만 유일 | 지민 11/2 (추천안에 포함) |
| AD-1 | 환경별 주소는 서비스의 suffix를 재사용 | AI-default |
| AD-2 | 63자 초과 시 앞부분 54자로 자르고 suffix 유지 | AI-default |
| AD-3 | suffix 충돌 3회 재시도 후 409 `PUBLIC_HOST_CONFLICT` | AI-default |
| AD-4 | 행이 있으면 마이그레이션 실패(백필 없음) | AI-default (API 6과 같음) |
| AD-5 ★ | 데모 세션 쿠키 이름 `__Host-echo_session` | AI-default |

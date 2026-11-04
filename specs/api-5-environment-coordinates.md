# API 서버 5: 환경 좌표 (환경별 상태 조회의 남은 범위) — SPEC (확정)

- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/5 (Project #4)
- 저장소 경로: `specs/api-5-environment-coordinates.md`
- 기준: echo-was main `6300a43087cdbbd2d5dabf85d2bad771978a14d3`(11/5 02:03, #67 API 16 병합 뒤), echo-frontend main `b2b2c96`(`src/lib/geo.ts` SHA-256 `589453ac6b3c197efbd86de55549f728c2ee83e8b373d4dc74111695576fc442`)
- 상태: **확정**(11/5 지민 결정 ①=A, ②=A, ③=A, AI 기본값 AD-1~9 거부 없음)
- 마이그레이션: **0**. 좌표는 저장하지 않고 `config` 안의 기존 글자(`region`·`location`)로 코드 상수 표에서 찾는다
- 관련: echo-frontend `docs/api-contract.md` 4-1(좌표)·4-3(재난), echo-frontend #3(환경 카드, CLOSED), echo-was #9 [API 서버 9] 재난 → 이전 트리거

## 1. 완료 기준

### 1-1. 이슈 원문

한 응답에 두 클라우드와 온프레의 상태가 함께 나온다.

### 1-2. 이번 범위의 읽기

- "상태" = 원장 F3 "환경 카드마다 클라우드·버전·트래픽 비율·위치".
- 버전·트래픽 비율·역할은 **이미 있다**(API 3·8이 서비스 상세에 넣음, 2절). 이번에 더하는 것은 위치의 좌표(계약 4-1)뿐이다.
- 위치·접속 상태는 환경 목록 응답, 버전·비율은 서비스 상세 응답에 있고 프론트가 환경 id로 합친다(프론트 #3 CLOSED). 둘을 한 API로 합치는 새 API는 만들지 않는다(D-1).

### 1-3. 완료 기준 재작성 (이슈 본문 수정은 M1이 병합 뒤 한다)

| 지금 문구 | 바꿀 문구 | 메모 |
|---|---|---|
| 한 응답에 두 클라우드와 온프레의 상태가 함께 나온다. | ① (이미 충족, API 3·8) 서비스 상세 한 응답에 두 클라우드와 온프레 연결의 역할·트래픽 비율·현재 버전(commitSha·imageDigest)·예비 버전 일치 여부가 함께 나온다.<br>② (이미 충족, API 2) 환경 목록 한 응답에 세 환경의 종류·위치·접속 상태가 함께 나온다.<br>③ (이번에 더함) 환경 목록·단건 응답에 좌표 `coordinates {lat, lng} \| null` 이 나온다. 클라우드는 리전으로, 온프레는 `location` 도시 코드로 서버 표에서 찾는다. 모르는 위치는 null.<br>④ (이번에 기록) 자동 주기 접속 확인은 1차에서 하지 않는다. | 11/5 지민 결정: 범위 = 좌표만(①A), 상태 요약 새 API 없음, 주기 확인 1차 제외. 서버 재난 판정은 API 서버 9 |

## 2. 확인된 사실 (main `6300a43` 기준으로 다시 확인)

- 서비스 상세 `ServiceDetailResponse`(`app/schemas/service.py` 206행)의 `environments[]` = `ServiceEnvironmentResponse`(117행): `serviceEnvironmentId`, `environment {id, name, kind}`(`EnvironmentRefResponse`, 100행), `role`, `trafficWeight`, `url`, `currentDeployment {id, commitSha, imageDigest, finishedAt}`, `versionStatus` 등. 이번에 바꾸지 않는다.
- `EnvironmentRefResponse`는 배포 요청 응답에서도 쓴다 — `app/schemas/deploy_request.py` 27·129·197행. 바꾸지 않는다(AD-5).
- 환경 목록 `EnvironmentSummaryResponse` — `app/schemas/environment.py` 90~111행: `id, name, kind, location, config, secretRef, connectionStatus, connectionCheckedAt, connectionError, createdAt, updatedAt`. 단건 `EnvironmentDetailResponse`(114~133행) = 위 + `clusterInfo`, `clusterInfoError`. 둘 다 `_summary_fields(view)`(149~167행)로 채우고, `location` = `view.config.display_location() if view.config is not None else None`(155행). config를 지금 양식으로 못 읽으면 `view.config`가 None.
- 설정 양식 `app/adapters/base.py` `EnvironmentConfig`(84행): `extra="forbid"`, frozen, 추상 `display_location()`(116행), 기본 구현 메서드 `environment_host_domain() -> None`(120행 — 추상이 아닌 기본 메서드의 선례). aws `region`(패턴 `^[a-z]{2}(?:-[a-z]+)+-\d{1,2}$` — `tokyo` 같은 도시 글자는 등록 단계에서 이미 422), gcp `region`(+선택 `cluster_location`), onprem `location`(`NonEmptyStr` 자유 문구, docstring 예 `team-laptop`) — `aws.py`·`gcp.py`·`onprem.py`.
- 모델 `app/models/environment.py`: 종류별 설정은 `config`(jsonb) 한 칸(84행), `parsed_config()`(112행)로 양식 객체로 되돌린다. 위치·좌표 칸은 없고 이번에도 더하지 않는다.
- 라우터 `app/routers/environment_router.py`·서비스 `app/services/environment_service.py`는 def6052 뒤 감사 로그(#66)로 `access.user.id`를 넘기게 바뀌었다. 응답 모양은 그대로이고 이번에 고칠 필요 없다(응답은 스키마에서만 바뀜).
- `app.adapters.registry.discover()`는 `app.adapters` 패키지 안 모든 모듈을 import한다. 표 모듈은 `app/core/places.py`에 둔다(어댑터 패키지 밖).
- 시험용 가짜 양식: `tests/conftest.py` 32행 `AzureEnvironmentConfig`, `tests/test_adapters.py` 155·301·351행. 새 메서드를 추상으로 만들지 않으면 고칠 필요 없다.
- 통합 시험이 응답 키 집합을 정확히 검사한다 — `tests/integration/test_environment_api.py` 57~69행 `SUMMARY_KEYS`, 70행 `DETAIL_KEYS`, 비교 190·235·351·506·704·962행. 깨진 config 행 시험 `broken-aws` 950·962~963행(`location`·`config`가 null).
- 문서 `docs/api/environments.md`: 등록 표 52~56행(onprem `location` 예 `team-laptop`), 수정 104~111행(config 전체 교체), 다시 확인 120행 "자동 주기 확인은 없다(API 서버 5 에서 정한다).", 응답 스키마 133~151행, 예 160~177행.
- 같은 "API 서버 5" 주기 확인 문구가 `README.md` 404행, `docs/schema.md` 584·678행에도 있다 — 이번 PR에서는 고치지 않는다(AD-8, 후속).
- 프론트 `src/lib/geo.ts`(echo-frontend main `b2b2c96`, 원격 main과 같은 파일): `PLACES` 8~57행 14줄, `environmentCoordinates()` 65~69행 = `environment.coordinates ?? PLACES[location.toLowerCase()]?.coordinates ?? null` — 서버 값을 먼저 쓴다.
- 열린 PR은 #64(보안·거버넌스 3, 다른 세션이 고치는 중) 하나. #64가 만지는 파일: `app/main.py`, `app/audit/actions.py`, `app/enums.py`, `app/dependencies.py`, `app/core/exceptions.py`, `docs/api/auth.md`, `docs/api/audit-logs.md`, `tests/integration/test_authorization.py`, `tests/integration/test_audit_logs.py`, `tests/test_audit_recorder.py`, `CLAUDE.md`, `.claude/rules/term-dictionary.md`, 보안 예외 새 파일들. 이번 범위와 겹침 0.
- 운영 시연 온프레 행의 `location` 값은 미확인(단서 `aws-seoul-ec2`, 실제 기기 AWS 서울 EC2) — 6절에서 M1이 확인·PATCH.

## 3. 결정

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | 범위 = 환경 응답에 좌표만 보탬. 상태 요약 새 API·새 라우트 없음. 완료 기준을 1-3처럼 고침 | 11/5 지민 ①=A |
| D-2 | 자동 주기 접속 확인은 1차에서 하지 않음(API 2 D10에서 넘겨받은 결정의 답) | 11/5 지민 ①=A |
| D-3 | 온프레 좌표 = `config.location` 글자를 서버 도시 표에서 찾음(새 config 칸 없음). 직접 좌표 입력은 2차 | 11/5 지민 ②=A |
| D-4 | 시연 온프레(k3s) 행은 병합·배포 뒤 M1이 `location: "seoul"`로 PATCH(마이그레이션 없음, config 전체 교체) | 11/5 지민 ②=A |
| D-5 | 표는 코드 상수 `app/core/places.py`, 값은 프론트 `geo.ts` PLACES와 같은 14줄 | 11/5 지민 ③=A |
| D-6 | 역할 표·라우트·`main.py`·`dependencies.py`·`enums.py`·감사 이름표·`CLAUDE.md`·용어 사전은 건드리지 않는다(열린 PR #64와 겹침 피함). 적을 것이 있으면 PR 본문 "후속"에 남긴다 | 오케스트레이터 지시 |

AI 기본값(거부 없음, 채택): AD-1 칸 모양 `coordinates: {lat, lng} | null`(항상 있음), AD-2 표 값 = 프론트 PLACES 14줄 그대로, AD-3 찾는 규칙(aws·gcp `region`, onprem `location` 소문자), AD-4 양식 메서드 `coordinates()`(기본 None, 추상 아님), AD-5 서비스 상세·배포 요청 `environment` 참조 불변, AD-6 config 못 읽으면 null, AD-7 DB·마이그레이션 변경 0, AD-8 겹치는 문서(`README.md`·`docs/schema.md`·용어 사전)는 나중, AD-9 프론트 계약 문서는 요청만.

## 4. 범위

한다:
1. 좌표 표 `app/core/places.py`: 종류별 표 3개(AWS 리전 6, GCP 리전 3, 온프레 도시 5 = 14줄, 5-3)와 좌표 타입·찾기 함수.
2. 설정 양식 메서드 `coordinates() -> Coordinates | None`: `EnvironmentConfig` 기본 None(추상 아님), aws·gcp는 `region`, onprem은 `location.lower()`(AD-3·AD-4).
3. 응답 칸 `coordinates`를 `EnvironmentSummaryResponse`에 더함(단건은 상속). 응답 모델 `CoordinatesResponse {lat, lng}`. `_summary_fields`에서 채움.
4. onprem `location` 칸에 JSON Schema `description`·`examples`(도시 코드면 지도에 표시된다는 안내)만 더함 — 검사 규칙(`NonEmptyStr`)은 그대로.
5. 문서 `docs/api/environments.md`: 응답 표에 `coordinates`, 찾는 규칙과 표, 등록 표 onprem `location` 안내, 예 JSON에 `coordinates`, 120행 "자동 주기 확인은 없다(API 서버 5 에서 정한다)" → "자동 주기 확인은 하지 않는다(API 서버 5, 11/5 결정)".
6. 시험(7절).

하지 않는다:
- 상태 요약 새 API·새 라우트(① B), 서비스 상세·배포 요청의 `environment` 참조 변경(AD-5), PATCH 규칙 변경(config 전체 교체 그대로 — `location` 글자만 바꾸면 좌표가 바뀐다).
- 온프레 직접 좌표·`city` 칸(② B·C), DB 표(③ B), 마이그레이션.
- 서버 재난 판정·반경 계산(API 서버 9, 계약 4-3), 자동 주기 접속 확인(D-2).
- 열린 PR #64 파일(2절 목록)·`README.md`·`docs/schema.md`·`CLAUDE.md`·용어 사전 — 병합 뒤 작은 문서 PR(AD-8, D-6).
- echo-frontend 변경(타입 다시 만들기·계약 문서는 김도영님 몫, AD-9).
- 운영 온프레 행 PATCH — M1이 병합·배포 뒤 따로 한다(6절).

## 5. 계약

### 5-1. 데이터

변경 없음. 표·칸·마이그레이션 0. `environments.config` 안 키도 그대로다(새 키 없음).

### 5-2. 응답

`EnvironmentSummaryResponse`(목록 항목)와 `EnvironmentDetailResponse`(등록·단건·수정·다시 확인)에 한 칸을 더한다.

| 칸 | 타입 | 설명 |
|---|---|---|
| `coordinates` | `{lat: number, lng: number}` \| null | 지도·재난 판정용 위치. aws·gcp = `config.region`의 표 값, onprem = `config.location`(소문자)의 도시 표 값. 표에 없거나 config를 못 읽으면 null |

예:

```json
{"id": "…", "name": "aws-tokyo", "kind": "aws", "location": "ap-northeast-1",
 "coordinates": {"lat": 35.68, "lng": 139.69}, "config": {"region": "ap-northeast-1", "clusterName": "echo-dev-eks"}, "…": "…"}
{"id": "…", "name": "onprem", "kind": "onprem", "location": "team-laptop", "coordinates": null, "…": "…"}
```

### 5-3. 좌표 표 (프론트 `geo.ts` PLACES 8~57행과 같은 값)

| 종류 | 키 | lat | lng |
|---|---|---|---|
| aws | `ap-northeast-1` | 35.68 | 139.69 |
| aws | `ap-northeast-2` | 37.57 | 126.98 |
| aws | `ap-northeast-3` | 34.69 | 135.5 |
| aws | `us-east-1` | 38.9 | -77.4 |
| aws | `us-west-2` | 45.6 | -121.2 |
| aws | `eu-west-1` | 53.3 | -6.3 |
| gcp | `asia-northeast1` | 35.68 | 139.69 |
| gcp | `asia-northeast2` | 34.69 | 135.5 |
| gcp | `asia-northeast3` | 37.57 | 126.98 |
| onprem | `tokyo` | 35.68 | 139.69 |
| onprem | `osaka` | 34.69 | 135.5 |
| onprem | `fukuoka` | 33.59 | 130.4 |
| onprem | `seoul` | 37.57 | 126.98 |
| onprem | `busan` | 35.18 | 129.08 |

- 표는 종류별로 나눈다(aws 리전 / gcp 리전 / 온프레 도시). aws 설정이 `tokyo` 같은 도시 키를, onprem 설정이 리전 키를 찾지 않게 하기 위함이다.
- 새 리전·도시는 이 표에 한 줄 더하면 된다. 프론트 표와 어긋나면 서버 값이 이긴다(`geo.ts`가 서버 값을 먼저 씀).

### 5-4. 설정 양식 메서드

```text
class EnvironmentConfig:
    def coordinates(self) -> Coordinates | None: return None   # 기본, 추상 아님
AwsEnvironmentConfig.coordinates()    -> AWS 표.get(region)
GcpEnvironmentConfig.coordinates()    -> GCP 표.get(region)
OnpremEnvironmentConfig.coordinates() -> 온프레 도시 표.get(location.lower())
```

- `_summary_fields`에서 `view.config.coordinates() if view.config is not None else None`을 응답 모델로 바꿔 넣는다.
- 시험용 가짜 종류(2절)는 기본 None을 물려받아 고칠 필요가 없다(추상 메서드로 만들지 않는 이유).

### 5-5. 호환성 (롤링 배포)

- DB·config 키 변경이 없어 옛 파드·새 파드가 같은 행을 그대로 읽는다(`extra="forbid"` 문제 없음).
- 롤링 중 옛 파드 응답에는 `coordinates` 칸이 없다. 프론트는 `environment.coordinates ?? 표`라서 칸이 없어도 지금처럼 돈다.
- 응답에 칸만 더해지므로 다른 파트 응답 파싱에 영향 없음. 응답 키 집합을 정확히 비교하는 소비자는 저장소 안 우리 시험뿐이다.
- OpenAPI에 `coordinates`가 생긴다. 프론트 타입은 다시 만들 때 반영된다.

## 6. 이 PR 밖에서 M1이 할 일 (병합·배포 뒤)

1. 운영 `GET /api/v1/orgs/{시연 조직}/environments`로 세 환경의 `location`·`coordinates`를 확인한다(aws-tokyo = 35.68/139.69, gcp-seoul = 37.57/126.98 기대).
2. 온프레 `coordinates`가 null이면 `PATCH …/environments/{온프레 id}`에 지금 config 전체 + `location: "seoul"`(D-4) — 터널이 살아 있을 때, 응답 `connectionStatus = CONNECTED`·`coordinates = {37.57, 126.98}` 확인.
3. echo-was #5에 진행 기록 댓글, 이슈 본문 완료 기준을 1-3으로 고침.
4. 김도영님께 댓글: 계약 4-1 반영 완료, 타입 다시 만들기, 열린 질문 3 정리 요청.
5. #64 병합 뒤 문서 PR: `README.md` 404행·`docs/schema.md` 584·678행 "API 서버 5" 문구, 용어 사전 "좌표" 한 줄, 필요하면 `CLAUDE.md`.

## 7. 완료 판정 (AC)

| AC | 내용 | 확인 |
|---|---|---|
| AC-1 | (기준 ①, 이미 충족 확인) 서비스 상세에 aws·gcp·onprem 연결마다 `role`·`trafficWeight`·`currentDeployment.commitSha/imageDigest`·`versionStatus` 칸이 있다 | 기존 통합 시험 그대로 통과 |
| AC-2 | 목록·단건·등록·수정·다시 확인 응답 키 집합 = 기존 + `coordinates`(`SUMMARY_KEYS`에 추가) | 통합 시험(기존 집합 비교 6곳이 새 집합으로 통과) |
| AC-3 | aws `ap-northeast-1` → 35.68/139.69, gcp `asia-northeast3`(+`clusterLocation: asia-northeast3-a`) → 37.57/126.98, onprem `location: "Seoul"` → 37.57/126.98(대소문자 무시) | 통합 시험 1건(세 종류 등록 → 목록) |
| AC-4 | 모르는 값 → null: aws `ap-southeast-1`, onprem `team-laptop` | 통합 시험 1건 |
| AC-5 | 저장된 config를 못 읽는 행 → `location`·`config`·`coordinates` 모두 null, 목록 200 | 기존 `broken-aws` 시험(962~963행)에 한 줄 |
| AC-6 | 종류 섞임 없음: aws 설정은 도시 키(`tokyo`)를 찾지 않는다(양식 `region` 패턴이 이미 거부함을 확인), onprem `location: "ap-northeast-1"`은 null(도시 표만 봄) | 단위 시험(`tests/test_places.py`) |
| AC-7 | 표 14줄이 5-3과 같다(값 고정 시험) | 단위 시험 |
| AC-8 | 서비스 상세·배포 요청 응답의 `environment` 키 집합은 그대로 `{id, name, kind}` | 통합 시험 1줄(기존 시험에 있으면 그것으로) |
| AC-9 | DB·마이그레이션 변경 0: `alembic/versions` 새 파일 없음, `alembic heads` 하나, `db-schema.sql` 변경 없음, pytest 안 `test_migrations` 통과. **DB를 바꾸는 alembic 명령(upgrade·downgrade·stamp)은 돌리지 않는다**(worktree `.env`가 공용 개발 DB를 가리킴) | 검사 |
| AC-10 | `docs/api/environments.md`에 칸·표·찾는 규칙·"주기 확인 하지 않음"이 있다 | 문서 확인 |
| AC-11 | 열린 PR #64 파일(2절)을 하나도 고치지 않는다 | `git diff --name-only` |
| AC-12 | (운영, 6절 뒤) 시연 조직 세 환경의 `coordinates`가 모두 null이 아니다 | M1 운영 확인(이 PR 밖) |

## 8. 위임한 세부사항 (워커가 정하고 보고)

- 파일 안 타입 이름·모양(`Coordinates`를 NamedTuple·dataclass·pydantic 중 무엇으로 할지), 표 모듈 안 배치.
- onprem `location` 힌트 문구(영어, JSON Schema `description`·`examples`).
- 단위 시험 파일 나누기.

## 9. 예상 작업량

- 파일: 새 2(`app/core/places.py`, `tests/test_places.py`) + 고침 7(`app/adapters/base.py`·`aws.py`·`gcp.py`·`onprem.py`, `app/schemas/environment.py`, `docs/api/environments.md`, `tests/integration/test_environment_api.py`) + SPEC 1. 열린 PR #64와 겹침 0.
- 시험: 새 6~8건(단위 3~4, 통합 3~4) + 기존 키 집합 상수 1곳 수정.

## 10. 결정 기록

- 11/4 조사(main `def6052`): `facts.md`, 결정 질문 `decisions.md` ①~③ + AD-1~9.
- 11/5 00:0x 지민(채팅): ①=A(좌표만 + 완료 기준 문구 고침 + 주기 접속 확인 1차 제외), ②=A(location 글자를 도시 표에서 찾음, 시연 k3s 행 `location='seoul'` PATCH), ③=A(코드 상수, AD 거부 없음).
- 11/5 M1: 기준 main `6300a43`으로 사실을 다시 확인(줄 번호·열린 PR #64 파일 목록·라우터/서비스 감사 로그 변경), AC-9를 alembic 쓰기 금지에 맞게 고치고 AC-11(#64 파일 비접촉)을 더해 확정.

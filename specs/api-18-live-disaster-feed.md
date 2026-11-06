# API 서버 18: 실제 재난 피드 연동(R2a) — SPEC

- 저장소 경로: `specs/api-18-live-disaster-feed.md`
- 상태: 구현 PR(지민 지시 11/7, 아이디어 「실제 태풍·지진 알림을 받아 이전시키기」). 병합·운영 스위치 켜기는 별도 승인.
- 기준: echo-was main `f9680b8`(#100 병합 뒤) · Alembic head `31bc0563b24b` → 새 리비전 `8748e995387a`(#101 병합 뒤 main 을 합치며 down_revision 을 #101 의 `9d8e1f2a3b4c` 로 옮겼다)
- 관련: API 서버 9 재난 API(`specs/api-9-disaster-trigger.md` — D-3 "지진 피드 2차"를 여기서 한다) · #30 SYSTEM 사전 승인(형래) · echo-frontend `src/lib/api/disasters.ts` `LIVE_FEED = {name, ready:false}` 자리

## 1. 완료 기준

1. 서버 설정 스위치 `LIVE_DISASTER_FEED_ENABLED`(기본 false)를 켜면 일꾼이 60초마다 외부 지진 피드(USGS)를 읽는다. 꺼져 있으면 아무것도 하지 않는다(HTTP 도 부르지 않음).
2. 새 지진(이미 본 id 제외)이 조직 기준(규모 ≥ M6.0 ∧ 조직 환경이 반경 300km 안, 조직 설정값)에 맞으면 그 조직에 재난(`source: LIVE_FEED`, `simulated: false`, 원본 USGS id·위치·규모·시각)을 만들고 SYSTEM 감사 줄을 남긴다.
3. 긴급 대피 계획은 **제안만** 한다. 사람이 `POST /disasters/{id}/evacuate` 로 기존 재난 선언과 같은 흐름(피해 서비스마다 EMERGENCY 계획, 자기 승인 허용 조직은 바로 승인)을 시작한다.
4. `GET /disasters/feed` 가 피드 상태(마지막 조회 시각·지진 수·임계·연결 상태)를 프론트 `LIVE_FEED` 모양(`name`·`ready`)을 포함해 돌려준다.
5. 시연용 `POST /disasters/feed:inject`(개발자 이상 사람)가 가짜 지진 한 건을 같은 경로로 흘린다(`source: FEED_INJECTED`, `simulated: true` — 목업은 목업이라고 밝힌다).

## 2. 피드 조사 (11/7 M1 실호출)

| 피드 | 접근 | 결과 | 판단 |
|---|---|---|---|
| **USGS** `earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson` | 무료·인증 없음·`cache-control: max-age=60`(1분 갱신)·CORS `*` | HTTP 200 GeoJSON FeatureCollection. feature: `id`, `properties.{mag, place, time(ms), updated, url, type, ids, status, tsunami}`, `geometry.coordinates=[경도, 위도, 깊이km]`. 같은 지진의 여러 관측망 id 가 `ids`(",us7000abcd,ci123,")에 있다. 11/6 10:39Z 실제 `us7000twg9` M6.1 "144 km SE of Hasaki, Japan"(도쿄 환경 좌표에서 234km, 서울 1386km)이 있었다 — 이 SPEC 기준이면 도쿄 환경 피해 | **구현**(기본 URL) |
| 기상청 지진정보 조회서비스(공공데이터포털 `apis.data.go.kr/1360000/EqkInfoService/getEqkMsg`) | 서비스 키 필요 | 키 없이 401 `SERVICE_KEY_IS_NULL` | 설계만: 같은 `FeedEvent` 로 바꾸는 파서 하나 + 키는 `env:ECHO_SECRET_…` 참조. 이번 PR 구현 안 함 |
| 일본 JMA | 공식 공개 API 없음 | 홈페이지가 쓰는 JSON `www.jma.go.jp/bosai/quake/data/list.json` 은 200(최근 189건, `cod: "+34.2+134.8-10000/"`, `mag`, `at`)이나 약관상 공개 API 가 아님 | 조사만. 쓰지 않는다 |
| P2P地震情報 `api.p2pquake.net/v2/history?codes=551` | 무료·인증 없음(JMA 재배포) | 200, `earthquake.hypocenter.{latitude, longitude, magnitude, depth}`·`time`(JST) | 프론트 `LIVE_FEED.name` 이 가리키던 후보. 일본 국내 진도 정보가 장점. 이번엔 전 세계·공식 기관인 USGS 로 시작(파서 하나로 추가 가능) |

## 3. 결정

| ID | 결정 |
|---|---|
| D-1 | 피드는 USGS `all_hour` 하나(설정 `LIVE_DISASTER_FEED_URL` 로 바꿀 수 있음). `properties.type == earthquake` 이고 규모 0~10 인 지진만(소수 한 자리 반올림, 0.05 올림). 태풍 피드는 없다 |
| D-2 | 주기 워커: 기존 `PeriodicTlsProbe` 관례(일꾼 프로세스의 TaskGroup 루프, 실패는 로그만 남기고 다음 주기). 간격 `LIVE_DISASTER_FEED_INTERVAL_SECONDS` 기본 60(30~3600), 제한 시간 10초. 스위치 `LIVE_DISASTER_FEED_ENABLED` 기본 false — 꺼지면 루프를 띄우지 않고, `poll_once` 도 바로 None |
| D-3 | 중복 제거: 처리한 지진의 모든 id(별칭 포함)를 `disaster_feed_states.seen_event_ids` 에 기억(최근 2000개). 같은 조직·같은 지진의 재난은 `disasters(organization_id, feed_event_id)` 부분 유니크 인덱스가 최종 보장(일꾼 여럿·기억 저장 전 죽음) |
| D-4 | 조직 기준: `organizations.live_feed_min_magnitude`(기본 6.0)·`live_feed_radius_km`(기본 300). 어드민이 `PATCH /settings` 의 선택 칸으로 바꾼다. 판정 = 규모 ≥ 기준 ∧ 조직 환경(좌표는 API 서버 5 `coordinates`) 중 하나 이상이 하버사인 거리 ≤ 반경 |
| D-5 | 재난 행: `type=EARTHQUAKE`, `radius_km` = 조직 반경(버튼 재난의 규모 식이 아님), `meets_threshold=true`, `created_by_user_id=NULL`, `feed_event_id="usgs:<id>"`, `feed_event={provider, eventId, place, occurredAt, depthKm, url, nearestDistanceKm}`. 피해·안전·모름 분류는 기존 `classify_environments` 를 조직 반경으로. 기준 미달 지진은 저장하지 않는다(로그만) |
| D-6 | 대피 계획은 제안만(기본이자 유일). 사람의 `POST /disasters/{id}/evacuate` 가 기존 `_trigger_plans` 를 그 사람으로 돈다(멱등: 열린 계획 SKIPPED). 자동 실행은 SYSTEM 이 계획을 만들고 승인해야 하는데 계획의 만든 사람·승인자가 users FK 라 #30 범위다 — 조직 스위치를 만들지 않았다(`evacuation: "SUGGESTED"` 고정) |
| D-7 | 감사: SYSTEM `live-disaster-feed` 의 `DISASTER_DECLARED`, USER `DISASTER_EVACUATION_REQUESTED`·`DISASTER_FEED_INJECTED`(쓰기 경로 34 → 36) |
| D-8 | 상태 API `GET /disasters/feed`(뷰어·에이전트): `{name, provider, ready, enabled, connectionStatus(DISABLED·WAITING·CONNECTED·ERROR), feedUrl, pollIntervalSeconds, lastPolledAt, lastSuccessAt, lastErrorCode, lastFeedCount, lastNewEventCount, threshold{minMagnitude, radiusKm}, evacuation}`. 마지막 성공이 3주기보다 오래되면 ERROR(일꾼 멈춤 감지) |
| D-9 | 시연 주입 `POST /disasters/feed:inject`(개발자 이상 사람, 에이전트 403): `{epicenter, magnitude, place?}` → 같은 `process_feed_event` 를 그 조직에만, `source=FEED_INJECTED`(simulated=true), `feed_event_id="inject:<uuid hex>"`. 스위치와 상관없고 피드 상태를 바꾸지 않는다 |
| D-10 | 응답 바뀜(호환): `Disaster.createdBy` 가 null 가능, `feedEvent` 칸 추가, `simulated = (source != LIVE_FEED)` |

## 4. 데이터 — 마이그레이션 1개 `8748e995387a`(down_revision `9d8e1f2a3b4c`)

- 새 표 `disaster_feed_states`(provider PK, 시각 2·오류 코드·수 2·`seen_event_ids` jsonb 배열).
- `disasters`: `feed_event_id varchar(128)`·`feed_event jsonb`(NULL, 맨 뒤), `created_by_user_id` NULL 허용, source CHECK 에 `LIVE_FEED`·`FEED_INJECTED`, CHECK `creator_matches_source`·`feed_event_matches_source`·`feed_event_is_object`, 부분 유니크 인덱스.
- `organizations`: `live_feed_min_magnitude numeric(3,1) DEFAULT 6.0`·`live_feed_radius_km int DEFAULT 300` + 범위 CHECK.
- 순수 더하기가 아닌 것: `created_by_user_id` NOT NULL 풀기(느슨하게 함)와 source CHECK 넓히기 — 둘 다 옛 파드가 쓰는 값을 그대로 받는다. downgrade 는 피드 재난 행이 있으면 실패(재난은 지울 수 없음).
- **다른 서브와 충돌 가능**: 같은 시점에 다른 PR 이 head `31bc0563b24b` 에 리비전을 이으면 head 가 둘이 된다. 병합 순서에 따라 뒤 PR 이 `down_revision` 을 앞 PR 리비전으로 바꾸고 `test_migrations.py` 의 `HEAD_REVISION`·사슬을 맞춘다.

## 5. 시험

- 단위 `tests/test_live_disaster_feed_rules.py`: USGS 파싱(발파·규모 없음·음수 규모 제외, 별칭, 시각), 형식 오류, 규모 반올림, 하버사인(도쿄↔실제 M6.1 = 234km), 임계(규모 미달·반경 밖·경계 포함·모름), 중복 제거(별칭·같은 응답 안), 기억 목록 자르기.
- 통합 `tests/integration/test_live_disaster_feed.py`: `httpx.MockTransport` 가짜 피드 → LIVE_FEED 재난·SYSTEM 감사·계획 0개·상태 CONNECTED / 스위치 꺼짐 무동작(HTTP 0회·상태 행 없음) / 같은 지진 두 번·id 바뀜 무시 / 유니크 인덱스 DUPLICATE / 먼·작은 지진 무시 / HTTP 503 → ERROR / 3주기 지나면 ERROR / 조직 기준 변경 / evacuate → EMERGENCY 계획(승인 대기·자기 승인 시 APPROVED·두 번째 SKIPPED) / 끝난 재난 evacuate 409 / inject 성공·기준 미달·다른 지역·검증 422.
- 고친 시험: 마이그레이션(head·CHECK·인덱스·표 목록·왕복 2개), 권한 표 3경로, 감사 쓰기 경로 36개(전체 경로 순회 시험에 evacuate·inject 추가), 재난 응답 모양(`feedEvent`).

## 6. 시연 방법

1. (운영 스위치를 켜지 않고) 재난 다이얼로그 대신 `POST /api/v1/orgs/{org}/disasters/feed:inject` `{"epicenter":{"lat":35.2,"lng":139.4},"magnitude":7.2,"place":"demo near Tokyo"}` → `source: FEED_INJECTED`·`simulated: true` 재난 생성(도쿄 환경 피해, 서울 안전).
2. `POST /disasters/{id}/evacuate` → 피해 서비스 EMERGENCY 계획(자기 승인 허용 조직이면 바로 실행).
3. 실제 피드: `LIVE_DISASTER_FEED_ENABLED=true` 배포 PR(별도 승인) 뒤 `GET /disasters/feed` 가 `CONNECTED`·`lastFeedCount` 를 보이면 연결 증거. 실제 M6.0+ 지진이 반경 안에 오면 LIVE_FEED 재난이 생긴다(보장 없음 — 그래서 주입이 필수).

## 7. 하지 않음

- 자동 실행(SYSTEM 계획·승인, #30), 기상청·P2P 파서, 태풍 피드, 알림(SSE·Slack), 피드 재난 자동 종료, 운영 스위치 켜기(별도 `deploy/base.yaml` PR), 프론트 화면(echo-frontend 이슈로 인계).

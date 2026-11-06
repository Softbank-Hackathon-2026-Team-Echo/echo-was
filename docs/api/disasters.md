# 재난 API — 미리보기 · 만들기 · 조회 · 종료 (API 서버 9)

사람이 재난 버튼을 한 번 누르면(`POST /disasters`) 서버가 반경과 피해·안전·모름 환경을 계산해 저장하고, 피해 환경에서 **서비스 중인** 서비스마다 `mode = EMERGENCY` 이전 계획을 만든다. 조직이 자기 승인을 허용하면 같은 요청에서 승인까지 해서 실행을 시작한다. 실행은 기존 일꾼이 그대로 한다(사전 확인 → 10·50% → 데이터 확인 → 100%, 실패하면 롤백). 근거: SPEC [specs/api-9-disaster-trigger.md](../../specs/api-9-disaster-trigger.md).

- 버튼 재난은 시뮬레이션이다(`source: SIMULATED`, `simulated: true`). 실제 지진 피드(USGS)가 만든 재난은 `source: LIVE_FEED`, `simulated: false` 이고, 시연용으로 피드에 넣은 가짜 지진은 `source: FEED_INJECTED`, `simulated: true` 다 — 아래 「실제 재난 피드」(API 서버 18). 주기 헬스 감시는 없다. 이전 계획의 `simulated` 는 지금처럼 실행기 모드로 정해서 "재난은 시뮬레이션, 이전은 실제"가 구분된다.
- 켜는 스위치는 없다. 버튼을 누르는 개발자의 동의로 조직의 피해 서비스 전부가 대상이다.
- SYSTEM 승인·사전 승인(#30)·DB 쓰기 전환 자동화(`cutover.sh`)는 범위 밖이다. 자동 이전은 트래픽까지이고, 쓰기 전환은 지금처럼 사람 단계다.
- 경로는 `/api/v1/orgs/{orgId}` 아래, 공통 봉투 `ApiResponse`, camelCase, 모든 응답 칸은 항상 있고 값이 없으면 null.

## 엔드포인트

| 메서드 · 경로 | 성공 | 대표 실패 | 최소 역할 |
|---|---|---|---|
| `GET /disasters/preview?type&lat&lng&magnitude` | 200 `DisasterPreview`(저장 안 함) | 422 `VALIDATION_ERROR` | 뷰어·에이전트 |
| `POST /disasters` | 201 `Disaster` | 422 `VALIDATION_ERROR`, 403 `ROLE_FORBIDDEN`·`AGENT_READ_ONLY` | 개발자(사람) |
| `GET /disasters?status=ACTIVE\|ENDED&page&size` | 200 `Page[Disaster]`(최신순) | 422 | 뷰어·에이전트 |
| `GET /disasters/{disasterId}` | 200 `Disaster` | 404 `DISASTER_NOT_FOUND` | 뷰어·에이전트 |
| `POST /disasters/{disasterId}/end` | 200 `Disaster`(`ENDED`) | 404 `DISASTER_NOT_FOUND`, 409 `DISASTER_NOT_ACTIVE`, 403 | 개발자(사람) |
| `POST /disasters/{disasterId}/evacuate` | 200 `Disaster`(+ 그 순간 `plans[]`) | 404, 409 `DISASTER_NOT_ACTIVE`, 403 | 개발자(사람) |
| `GET /disasters/feed` | 200 `LiveFeedStatus` | — | 뷰어·에이전트 |
| `POST /disasters/feed:inject` | 201 `InjectFeedEventResult` | 422 `VALIDATION_ERROR`, 403 | 개발자(사람) |

다른 조직의 재난은 404 `DISASTER_NOT_FOUND` 다. 에이전트 토큰은 조회·미리보기만 한다(재난을 만들거나 끝낼 수 없다, 403 `AGENT_READ_ONLY`). 재난 행은 지우지 않는다(`BEFORE DELETE` 트리거). 코드: `app/routers/disaster_router.py`, `app/services/disaster_service.py`(트리거), `app/services/disaster_rules.py`(순수 규칙).

미리보기는 프론트 계약 초안의 POST 가 아니라 **GET** 이다. 저장하지 않는 조회라 감사 이름이 필요 없다.

## 입력

| 칸 | 규칙 |
|---|---|
| `type` | `EARTHQUAKE` · `TYPHOON` |
| `epicenter` (`lat`·`lng`) | 위도 −90~90, 경도 −180~180. 미리보기는 쿼리 `lat`·`lng` |
| `magnitude` | 0~10, 소수 한 자리까지(`7.25` 는 422) |

## 판정 규칙 (서버 코드 상수, 환경 변수 아님)

| 항목 | 규칙 |
|---|---|
| 이전 대상 기준 `meetsThreshold` | 지진은 규모 7.0 이상, 태풍은 규모와 상관없이 true |
| 반경 `radiusKm` | `round(50 · e^(0.9 · (M − 5.2)))` km, 정수 km 로 반올림하고 0.5 는 올린다(M7.0 → 253, M7.2 → 302). 태풍도 같은 식이다 |
| 거리 | 하버사인, 지구 반지름 6371km(프론트 `geo.ts` 의 `distanceKm` 와 같은 식) |
| 피해 · 안전 · 모름 | 거리 ≤ 반경이면 피해(같으면 피해), 그보다 멀면 안전, 좌표를 모르면(`coordinates` 가 null, 설정을 읽지 못하는 환경 포함) 모름. 모름은 도착 후보에서 빠진다 |

반경은 프론트 `radiusForMagnitude`(`Math.round`)와 같은 값이다. 그래서 화면 원과 서버 판정이 같다. SPEC AC-1 의 "M7.2 → 300km" 는 조사 때의 계산 착오다(식 값 302.48).

기준 미달 지진도 재난 행은 `ACTIVE` 로 저장하지만(`meetsThreshold: false`, `plans: []`) 이전을 일으키지 않으므로 복귀 차단·`failback`·도착 후보 제외에 쓰지 않는다(아래 "복귀 차단"). 환경 id 목록은 만든 순간의 스냅숏이라 환경이 나중에 바뀌어도 고치지 않는다.

## `POST /disasters` 한 요청의 흐름

1. (트랜잭션) 조직의 살아 있는 환경 → 반경·분류 → 재난 행 `ACTIVE` + 감사 `DISASTER_DECLARED`. 커밋한다(뒤 단계가 실패해도 재난은 남는다).
2. 기준 미달이면 `plans: []` 로 201.
3. 피해 환경에 서비스 중 연결(비율 > 0)이 있는 살아 있는 서비스를 id 오름차순으로 읽는다. **출발 환경의 접속 상태는 보지 않는다**(재난이면 `FAILED` 일 수 있다).
4. 서비스마다 서로 독립으로(한 서비스의 실패는 그 서비스의 결과만 바꾼다):
   1. 도착을 고른다(아래). 열린 계획이 있으면 `SKIPPED MIGRATION_PLAN_OPEN`, 후보가 없으면 `SKIPPED NO_SAFE_TARGET`.
   2. 기존 계획 만들기 + `mode = EMERGENCY`, `disasterId`. 버튼 누른 사람이 만든 사람이다. 로그는 `DISASTER_TRIGGERED` → `PLAN_CREATED`. 기존 거절(`TARGET_NOT_READY`·`TARGET_SYNCING`·`DEPLOY_IN_PROGRESS`·`SOURCE_NOT_SINGLE`·`SERVICE_DELETING`·`ENVIRONMENT_UNHEALTHY` 등)은 `SKIPPED` + 그 코드.
   3. `allowSelfApproval` 이 켜져 있으면 기존 승인을 그 사람으로 부른다(`SELF_APPROVED`, 할 일 넣음). 꺼져 있으면 승인을 시도하지 않고 `PENDING_APPROVAL` + `SELF_APPROVAL_DISABLED`(거부 줄 없음). 다른 개발자가 기존 승인 API 로 시작할 수 있다.
5. 건너뛴 서비스가 있으면 감사 줄을 하나 더 쓴다(아래 "감사"). 201.

계획 기본값(필요 건수·최대 대기·롤백 조건)은 설정 기본값 그대로다. 재난이라고 단계를 빨리 넘기지 않는다. 보안 게이트도 같은 판정이다(`SECURITY_GATE_ENABLED`): 막히면 계획은 승인 대기로 남고 `PENDING_APPROVAL` + `SECURITY_GATE_BLOCKED` 다.

### 도착 고르기

후보 = 그 서비스의 예비 연결(비율 0, 현재 배포 있음) 중 환경이 다음을 모두 만족하는 것.

- 이 재난의 안전 환경이고, **복귀를 막는 다른 재난(아래 "복귀 차단")의 피해 환경이 아니다**.
- 마지막 접속 확인이 `FAILED` 가 아니다.
- 진입점 이름이 있다: 환경 종류가 `aws`·`gcp` 이거나, 환경 id 또는 종류가 `ENTRYPOINT_ENVIRONMENT_MAP` 에 있다(진입점은 그 밖의 이름을 모른다).

후보 중 출발과 **같은 이미지**인 것에서 진원지에서 가장 먼 곳을 고른다(같으면 환경 id 가 작은 쪽). 같은 이미지가 하나도 없으면 가장 먼 후보로 계획 만들기를 불러 이유(`TARGET_NOT_READY`·`TARGET_SYNCING`)를 그대로 돌려준다. 후보가 없으면 `NO_SAFE_TARGET`.

## 응답

`DisasterPreview`:

```json
{"radiusKm": 302, "meetsThreshold": true, "affected": ["e1…"], "safe": ["e2…"], "unknown": ["e3…"]}
```

`Disaster`:

```json
{
  "id": "d1…",
  "type": "EARTHQUAKE",
  "source": "SIMULATED",
  "simulated": true,
  "magnitude": 7.2,
  "epicenter": {"lat": 35.2, "lng": 139.4},
  "radiusKm": 302,
  "meetsThreshold": true,
  "status": "ACTIVE",
  "affectedEnvironmentIds": ["e1…"],
  "safeEnvironmentIds": ["e2…"],
  "unknownEnvironmentIds": ["e3…"],
  "createdBy": {"id": "u1…", "login": "dev-a"},
  "endedBy": null,
  "startedAt": "2026-11-07T03:05:40Z",
  "endedAt": null,
  "feedEvent": null,
  "plans": [
    {"serviceId": "s1…", "planId": "p1…", "outcome": "APPROVED", "reason": null, "targetEnvironmentId": "e2…", "planStatus": "APPROVED"},
    {"serviceId": "s2…", "planId": null, "outcome": "SKIPPED", "reason": "NO_SAFE_TARGET", "targetEnvironmentId": null, "planStatus": null}
  ]
}
```

### `plans[]`

| 칸 | 뜻 |
|---|---|
| `outcome` | `APPROVED`(승인까지 됨) · `PENDING_APPROVAL`(승인 대기) · `SKIPPED`(계획을 만들지 않았거나 승인 없이 닫힘) |
| `reason` | 아래 표. 없으면 null |
| `planId` | 만든 계획. 열린 계획 때문에 건너뛰었으면 그 계획, 그 밖의 건너뜀은 null |
| `planStatus` | 그 계획의 실제 상태(`MigrationPlanStatus`), 계획이 없으면 null. 진행 상황은 계획 API 를 폴링한다(SSE 없음) |

| `reason` | 뜻 |
|---|---|
| `NO_SAFE_TARGET` | 도착 후보가 없다 |
| `SELF_APPROVAL_DISABLED` | 조직이 자기 승인을 허용하지 않아 승인 대기다 |
| `SOURCE_NOT_AFFECTED` | 서비스 목록을 읽은 뒤 트래픽이 피해 환경 밖으로 옮겨 갔다 |
| `INTERNAL_ERROR` | 그 서비스 처리 중 예상 못 한 오류(로그에 스택). 다른 서비스는 계속한다 |
| 그 밖 | 계획 API 의 오류 코드 그대로: `MIGRATION_PLAN_OPEN`·`TARGET_NOT_READY`·`TARGET_SYNCING`·`DEPLOY_IN_PROGRESS`·`SOURCE_NOT_SINGLE`·`SERVICE_DELETING`·`ENVIRONMENT_UNHEALTHY`·`DISASTER_ACTIVE`·`SECURITY_GATE_BLOCKED`·`PLAN_STALE`·`PLAN_EXPIRED` |

**만들기 응답**은 그 순간의 결과이고 건너뛴 서비스도 싣는다. **조회 응답**은 `migration_plans.disaster_id` 로 찾은 계획만 싣는다(건너뛴 서비스는 저장되지 않는다. 감사 줄 `details.skippedPlans` 로 본다). 조회의 요약은 계획에서 계산한다: `PENDING_APPROVAL` 은 그대로, 승인된 적 있으면(`approvedAt` 있음) 지금 상태와 상관없이 `APPROVED`, 승인 없이 닫혔으면(`REJECTED`·`CANCELLED`·`EXPIRED`·`STALE`) `SKIPPED` + `reason` = 그 상태.

## 복귀 차단

재난이 끝나기 전에는 피해 환경으로 트래픽을 옮기지 않는다. 복귀는 자동이 아니고 사람이 계획을 만든다. 막는 재난은 `status = ACTIVE` 이고 `meetsThreshold = true` 인 것이다. 기준 미달 재난은 이전을 일으키지 않으므로 그 지역으로 가는 것도 막지 않는다.

- `POST /services/{id}/migration-plans` 의 도착 환경이 막는 재난의 피해 환경이면 409 `DISASTER_ACTIVE`, `details = [{field: "disasterId", reason: "DISASTER_ACTIVE", value: <최신 재난 id>}]`. 재난이 여럿이면 전부 끝나야 풀린다.
- 계획 상세의 `failback {allowed, reason, disasterId}` 가 "이 계획을 거꾸로 하는 복귀(도착 = 이 계획의 출발 환경)를 지금 만들 수 있는가"를 미리 보여 준다.
- `POST /disasters/{id}/end` 로 재난을 끝내면 풀린다. 진행 중인 이전 계획은 건드리지 않는다.

승인은 이 검사를 다시 하지 않는다. 재난 전에 만든 승인 대기 계획은 그대로 승인할 수 있다.

## 멱등 · 멈추기

- 다시 눌러도 열린 계획이 있는 서비스는 `SKIPPED MIGRATION_PLAN_OPEN` 이다(쿨다운 없음). 재난 행은 누를 때마다 새로 생기고, ACTIVE 재난은 조직당 여러 개일 수 있다.
- 계획을 멈추는 것은 기존 `cancel`(시작 전)·`abort`(실행 중, 출발로 되돌림) 그대로다.

## 감사

| 이름 | 대상 | `details` |
|---|---|---|
| `DISASTER_DECLARED` | `disaster`(이름표 `EARTHQUAKE M7.2`) | `{type, magnitude, radiusKm, meetsThreshold, affectedEnvironmentIds}` |
| `DISASTER_DECLARED`(건너뛴 서비스가 있을 때만 둘째 줄) | 같은 대상 | `{skippedPlans: [{serviceId, planId, reason}]}` |
| `DISASTER_ENDED` | `disaster` | `{}` |
| `MIGRATION_PLAN_CREATED`·`MIGRATION_PLAN_APPROVED`(재난이 만든 계획) | `migration_plan` | 기존 칸 + `{disasterId, trigger: "DISASTER"}` |

감사 줄은 고칠 수 없고 건너뜀은 재난 행을 커밋한 뒤에야 알 수 있어서 둘째 줄을 쓴다. 전체 표는 [audit-logs.md](audit-logs.md).

## 실행 로그

`DISASTER_TRIGGERED`(WARN)는 재난이 만든 계획의 첫 줄이고 `PLAN_CREATED` 앞에 온다. `data = {disasterId, magnitude, distanceKm}`, `distanceKm` 은 진원지에서 출발 환경까지다(소수 한 자리).

## 롤링 배포 호환

새 표와 NULL 칸만 더한다. 옛 파드는 `disaster_id` 를 모른 채 동작하고(그 파드가 만든 계획은 NULL), 새 경로는 옛 파드에서 404 다. 옛 파드는 복귀 차단을 검사하지 않는다.

## 실제 재난 피드 (API 서버 18)

근거: SPEC [specs/api-18-live-disaster-feed.md](../../specs/api-18-live-disaster-feed.md).

- **출처**: USGS Earthquake Hazards Program 요약 피드 `https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson`(GeoJSON, 인증 없음, 1분 갱신, 지난 1시간 전 세계 지진). `properties.type = earthquake` 이고 규모가 있는 지진만 읽는다.
- **스위치**: 서버 설정 `LIVE_DISASTER_FEED_ENABLED`(기본 **false**). 켜면 일꾼 프로세스가 `LIVE_DISASTER_FEED_INTERVAL_SECONDS`(기본 60, 30~3600)마다 `LIVE_DISASTER_FEED_URL` 을 읽는다(제한 시간 `LIVE_DISASTER_FEED_TIMEOUT_SECONDS`, 기본 10초, 본문 상한 `LIVE_DISASTER_FEED_MAX_BYTES`, 기본 5MB — 넘는 순간 읽기를 멈추고 그 주기를 버린다). 꺼져 있으면 피드를 부르지 않는다.
- **조직 기준**: `organizations.live_feed_min_magnitude`(기본 6.0)·`live_feed_radius_km`(기본 300). 어드민이 `PATCH /orgs/{orgId}/settings` 의 `liveFeedMinMagnitude`(0~10, 소수 한 자리)·`liveFeedRadiusKm`(1~2000)로 바꾼다(생략하면 그대로, 바뀌면 `ORGANIZATION_SETTINGS_CHANGED` details 에 from·to).
- **판정**: 지진 규모 ≥ 조직 기준 규모 ∧ 그 조직 환경 하나 이상이 진원지에서 조직 기준 반경 안(하버사인, 경계 포함 — 부동소수 오차로 반경 위 환경이 빠지지 않게 1m 여유를 둔다, 좌표는 버튼 재난과 같은 `coordinates`). 맞으면 재난을 만든다: `type = EARTHQUAKE`, `radiusKm` = 조직 기준 반경(규모 식이 아님), `meetsThreshold = true`, `createdBy = null`, `feedEvent` 채움. 버튼 재난의 규모 7.0 기준·반경 식은 쓰지 않는다.
- **중복 제거**: 처리한 지진 id(관측망 별칭 `properties.ids` 포함)를 `disaster_feed_states.seen_event_ids` 에 기억해(최근 2000개) 다시 처리하지 않는다. 같은 조직·같은 피드 지진의 재난은 부분 유니크 인덱스가 하나로 막는다(일꾼이 여럿이어도).
- **대피 계획은 제안만**: 피드 재난은 만든 사람이 없어 계획을 만들지 않는다(`plans = []`). 사람이 `POST /disasters/{id}/evacuate` 를 부르면 버튼 재난과 **같은 트리거**(피해 서비스마다 EMERGENCY 계획, 조직이 자기 승인을 허용하면 바로 승인)를 그 사람 이름으로 돈다. 열린 계획이 있는 서비스는 `SKIPPED MIGRATION_PLAN_OPEN` 이라 여러 번 불러도 겹치지 않는다. 피해·안전 환경은 재난을 만든 순간의 스냅숏이다. 버튼 재난에도 쓸 수 있다. 자동 실행(사람 없이 계획 만들기·승인)은 SYSTEM 승인이 필요해 #30 범위로 남긴다 — `GET /disasters/feed` 의 `evacuation` 은 늘 `SUGGESTED` 다.

`GET /disasters/feed` → `LiveFeedStatus`(프론트 `LIVE_FEED = {name, ready}` 를 그대로 바꿀 수 있는 모양):

```json
{
  "name": "USGS Earthquake Feed (all_hour)",
  "provider": "USGS",
  "ready": true,
  "enabled": true,
  "connectionStatus": "CONNECTED",
  "feedUrl": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
  "pollIntervalSeconds": 60,
  "lastPolledAt": "2026-11-07T03:05:40Z",
  "lastSuccessAt": "2026-11-07T03:05:40Z",
  "lastErrorCode": null,
  "lastFeedCount": 12,
  "lastNewEventCount": 1,
  "lastSkippedCount": 0,
  "threshold": {"minMagnitude": 6.0, "radiusKm": 300},
  "evacuation": "SUGGESTED"
}
```

| `connectionStatus` | 뜻 |
|---|---|
| `DISABLED` | 스위치가 꺼져 있다(`ready=false`) |
| `WAITING` | 켜졌지만 아직 한 번도 읽지 않았다 |
| `CONNECTED` | 마지막 조회가 성공했고 3주기 안이다(`ready=true`) |
| `ERROR` | 마지막 조회 실패(`lastErrorCode` = `FEED_UNREACHABLE`·`FEED_HTTP_ERROR`·`FEED_INVALID`·`FEED_TOO_LARGE`(본문 상한 초과)·`FEED_PROCESSING_FAILED`(주기 처리 중 예상 밖 실패)) 또는 마지막 성공이 3주기보다 오래됨(일꾼 멈춤 포함). 실패한 주기에 바로 바뀐다 |

`lastFeedCount` 는 마지막 응답의 지진 수(발파 등 제외), `lastNewEventCount` 는 그중 처음 본 지진 수다. `lastSkippedCount` 는 이상해서 건너뛴 지진 수다 — 시각이 1970~2100 밖·규모가 숫자 아님/0~10 밖·id 가 `usgs:` 포함 128자 초과·좌표가 범위 밖이거나 숫자 아님, 또는 그 지진의 저장이 예상 밖으로 실패한 경우(기억하지 않아 다음 주기에 다시 한다). 그 건만 건너뛰고 같은 응답의 정상 지진은 그대로 처리한다. 응답 하나의 features 가 10000개를 넘으면 `FEED_INVALID` 로 그 주기를 버린다.

피드 재난의 `feedEvent`:

```json
{"provider": "usgs", "eventId": "us7000twg9", "place": "144 km SE of Hasaki, Japan",
 "occurredAt": "2026-11-06T10:39:27.869000+00:00", "depthKm": 29.0,
 "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000twg9", "nearestDistanceKm": 234.0}
```

`nearestDistanceKm` 은 진원지에서 그 조직의 가장 가까운 환경까지(소수 한 자리). 버튼 재난은 `feedEvent = null`.

### 시연 주입 `POST /disasters/feed:inject`

가짜 지진 한 건을 피드와 **같은 판정·생성 경로**로 이 조직에만 흘린다. 피드 스위치와 상관없이 동작하고 피드 상태(`GET /disasters/feed`)는 바꾸지 않는다.

- 요청: `{"epicenter": {"lat": 35.2, "lng": 139.4}, "magnitude": 7.2, "place": "demo near Tokyo"}`(`place` 선택, 1~120자). 검증은 버튼 재난과 같다.
- 응답 201 `{feedEventId: "inject:<hex>", simulated: true, matched, reason, disaster}` — 기준에 맞으면 `matched=true` 와 재난(`source: FEED_INJECTED`, `simulated: true`, `createdBy: null`, `feedEvent.provider = "inject"`), 아니면 `matched=false`, `reason` = `BELOW_THRESHOLD`·`NO_AFFECTED_ENVIRONMENT`, `disaster = null`.

### 피드 감사

| 이름 | 행위자 | 대상 | `details` |
|---|---|---|---|
| `DISASTER_DECLARED` | SYSTEM `live-disaster-feed` | `disaster` | `{source, feedEventId, magnitude, place, radiusKm, minMagnitude, nearestDistanceKm, affectedEnvironmentIds, evacuation: "SUGGESTED"}` |
| `DISASTER_EVACUATION_REQUESTED` | USER | `disaster` | `{source, plans: [{serviceId, planId, outcome, reason}]}` |
| `DISASTER_FEED_INJECTED` | USER | 만든 재난(`disaster`) 또는 조직(`organization`, 못 만들었을 때) | `{feedEventId, magnitude, epicenter, matched, reason}` |

주입은 USER 줄(`DISASTER_FEED_INJECTED`)과 피드 경로의 SYSTEM 줄(`DISASTER_DECLARED`)을 함께 남긴다.

### 롤링 배포 호환(API 서버 18)

revision `8748e995387a`: 새 표 `disaster_feed_states`, `disasters.feed_event_id`·`feed_event`(NULL), `disasters.created_by_user_id` NULL 허용, source CHECK 확장 + 출처 일관성 CHECK 2개, `organizations.live_feed_*` 2칸(서버 기본값). 옛 파드는 SIMULATED 재난만 만들어 새 CHECK 를 만족한다. 옛 파드가 피드 재난을 읽으면 `createdBy` 를 채우지 못해 실패할 수 있으나, 스위치가 기본 꺼짐이고 주입은 새 경로라 롤아웃 동안 피드 재난이 생기지 않는다.

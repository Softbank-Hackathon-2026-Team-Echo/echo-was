# 재난 자동 이전·과거 시연 FE 계약 v1

구현 계약 정본. OpenAPI와 동일한 endpoint/schema 이름. FE 코드 수정·Slack 발송은 하지 않았다. 별도 writer가 이 문서로 요청한다. 기존 재난 목록·상세·사람 evacuate는 유지하며 칸은 추가만 한다.

## 화면·지도
재난 지도/목록에서 live와 demo 탭을 분리한다. demo에는 '과거 실제 지진 + 가상 서비스 이전 시연' 고정 배너. 실서비스 KPI·실제 이전 성공 건수·현재 이전 타임라인과 합산하지 않는다.

실제 feed: source=LIVE_FEED, simulated=false, originSource=LIVE_USGS. 수동 가상: source=SIMULATED 또는 FEED_INJECTED, simulated=true, originSource=SYNTHETIC. 과거 API의 지진만 originSource=REAL_HISTORICAL, 앱 이전은 source=DEMO/isSimulated=true. source라는 이름만 보고 실제 이전으로 해석하지 않는다.

circle: live 및 FEED_INJECTED의 radiusKm는 조직 정책 반경, radiusSource=ORGANIZATION_POLICY. 수동 재난 버튼(SIMULATED)·과거 시연은 DEMO_ASSUMPTION. 실제 피해·진도·도달시간 경계라는 표현 금지. 규모는 지진 자체 크기이고 장소별 흔들림과 동일하지 않다는 보수적 표시 규칙이다. 지정 USGS 공식 설명 페이지 https://www.usgs.gov/programs/earthquake-hazards/earthquake-magnitude-energy-release-and-shaking-intensity 는 이번 환경에서 curl·브라우저 403/아카이브 복구 실패: 원문 직접 확인 완료라고 주장하지 않는다(미검증). 과거 2건의 실제 ShakeMap product URL·원자료는 별도 확보했다. ShakeMap은 circle과 독립 링크/레이어로 표시한다.

색상: 주황=정책 반경 안, 청록=현재 정책 반경 밖 후보(안전 보장 아님), 회색=좌표 모름·비활성, 보라=시연, 빨강=정책 차단. 규모만으로 빨강 물리 피해 원형을 만들지 않는다.

## GET /api/v1/orgs/{org_id}/disasters/history
권한 ViewerAccess(사람 뷰어 이상·에이전트 읽기), 200 ApiResponse[DisasterHistoryResponse]. DB 요청 기록 재생 WAS #121(DB 승격 후 요청 기록 재생 API)와 별개. POST/재생 실행 API 없음.

기본 반환: data={enabled:false,isSimulated:true,source:"DEMO",items:[]}.
DISASTER_HISTORY_DEMO_ENABLED=true를 테스트 설정에서 명시해야 고정 2개 시연 dataset을 반환한다. 읽기만으로 현재 jobs·plans·disasters·이전 실행 감사/로그 생성 0. 기존 AGENT 읽기 감사는 유지되며 가상 이전 실행 기록이 아니다.

DisasterHistoryItemResponse:
- id: demo-* 화면 식별자(실제 plan UUID 아님)
- isSimulated:true, source:"DEMO", liveState:"DEMO", enabled:true
- collectedAt/occurredAt: ISO8601 UTC. 조회시각이 아니라 원본 수신/발생 시각
- scenarioLabel: '시연:'으로 시작하는 한국어 label
- blockReason: null 또는 NO_SAFE_TARGET
- radiusKm: integer, radiusSource:"DEMO_ASSUMPTION"
- mockMigrationSeconds: integer 또는 null(실측 시간 아님)
- event: HistoricalEarthquakeResponse 아래
- environments: DemoEnvironmentResponse[] {id,label,lat,lng,role}; 좌표는 시연 배치, 실제 조직 등록환경 아님
- timeline: DemoTimelineResponse[] {stage,offsetSeconds,sourceWeight,targetWeight}; 사용자는 이 고정 배열을 재생한다. 시연 재생은 현재 실행 API를 부르지 않는다.

HistoricalEarthquakeResponse:
eventId(USGS 원본 그대로), lat/lng, magnitude, originSource:"REAL_HISTORICAL", source:"USGS_FDSN", occurredAt,collectedAt, sourceUrl(한정 FDSN query), rawSha256(원본 바이트 SHA-256), shakeMapUrl(nullable·공식 상세 product가 제공한 URL), detailSourceUrl, detailRawSha256.

2개 시나리오: 2011-03-11 도호쿠 공식 지진 + 가상 도쿄→서울 42초 성공; 2016-04-15 UTC 구마모토 공식 지진 + 가상 구마모토→후쿠오카 같은 정책 반경으로 차단. 지진 원본과 이전 단계/비율/시간은 명확히 다른 provenance다. SHA 및 전체 원본은 app/data/disaster_history/, 조회 영수증은 archive evidence/historical-receipts.json.

## 기존 GET /disasters·상세·/feed 추가 칸
DisasterResponse 추가: originSource, radiusSource. 기존 id/source/simulated/epicenter{lat,lng}/magnitude/radiusKm/환경 id 배열/plans 유지.
FeedEventResponse 추가: collectedAt(nullable), autoDrAttempts={service UUID 또는 status:{status?,reason?,planId?}}. 차단 이유를 화면에서 그대로 표시하되 알아볼 한국어 설명을 붙인다. RESERVED/RESERVATION_RETAINED는 '이미 처리 예약됨: 자동 재실행하지 않음'이다.
LiveFeedStatusResponse 추가: automaticEnabled. enabled는 수집 스위치이며 자동 이전 스위치와 다르다. evacuation은 OFF시 기존 SUGGESTED, 자동 정책 판정 경로가 켜지면 PREAUTHORIZED이다. 이것은 사전동의가 조직 전체를 승인했다는 뜻이 아니다. 서비스별 autoDrAttempts가 실제 판정이다.
실제 환경 지도 좌표는 기존 GET /api/v1/orgs/{org_id}/environments의 coordinates{lat,lng}|null을 환경 UUID로 join한다. null이면 회색·모름, 안전 후보로 색칠하지 않는다. 실제 계획 timeline은 기존 /migration-plans/{plan_id}·/logs만 사용하고 history timeline을 섞지 않는다.

## 명시 사전동의 API (ADMIN 사람 전용)
POST /api/v1/orgs/{org_id}/disasters/auto-dr/consents
CreateAutoDrConsentRequest: {serviceId,sourceEnvironmentId,targetEnvironmentId,expiresAt,acknowledgeAutomaticExecution:true}. extra 필드 거부. 최대 7일, 미래의 timezone-aware 시각. 정확 한 조직·서비스·출발·대상, 현재 이미지/설정/변수 버전·조직 threshold·보안 정책 version/digest를 서버가 고정한다.
201 ApiResponse[AutoDrConsentResponse], data={consentId:UUID}. 저장만으로 실행 스위치를 켜지 않는다. 생성 POST에는 idempotency key 계약이 없으며 같은 본문 재전송도 별도 동의 id·감사 1줄을 만든다. 생성 응답을 잃었을 때 자동 재전송하지 않는다. 새 id는 별도 설정 allowlist에 명시하지 않으면 실행 권한이 없다. 철회만 같은 consentId에 대한 멱등 연산이다.
POST /api/v1/orgs/{org_id}/disasters/auto-dr/consents/{consent_id}/revoke
본문 없음, 200 ApiResponse[None]. 반복 철회는 멱등(감사 중복0). AGENT/개발자/뷰어는 403, 타 조직 404 조직 가드.
409 AUTO_DR_REFUSED는 details=[{field:"automatic",reason:코드}]. 자동 소비의 거절은 feedEvent.autoDrAttempts[].reason으로 조회한다.

주요 reason: AUTO_DR_DISABLED, AUTOMATION_DISABLED, CONSENT_MISSING, CONSENT_NOT_ALLOWLISTED, CONSENT_EXPIRED, CONSENT_REVOKED, CONSENT_AUTHORITY_LOST, SCOPE_MISMATCH, SCOPE_CHANGED, POLICY_CHANGED, THRESHOLD_POLICY_CHANGED, EVENT_STALE, LIVE_EVENT_REQUIRED, NO_SAFE_TARGET, TARGET_AT_RISK, MIGRATION_PLAN_OPEN, SECURITY_GATE_BLOCKED, DB_PAIR_NOT_READY, SERVICE_SCOPED_ROUTING_NOT_READY, ROLLBACK_NOT_PREAUTHORIZED.

## 활성화 경계
LIVE_DISASTER_FEED_ENABLED=false, AUTO_DR_ENABLED=false, AUTO_DR_CONSENT_IDS=[], DISASTER_HISTORY_DEMO_ENABLED=false 기본. 실제 운영 값은 인증 거절로 미확인, 소스 기본 OFF를 운영 OFF로 쓰지 않는다.
일반 자기승인 허용을 SYSTEM 무제한 승인으로 바꾸지 않는다. SYSTEM 실행 감사와 사람 사전동의 감사는 분리한다. DB는 WAS #118(DB쌍 안전 기반) 미병합으로 NOT_READY를 보존하며 자동승격 ON 안 함. HTTP weights는 WAS #117(서비스 라우팅 격리) 미병합으로 하드 차단: 이 Draft의 자동 실행은 격리 fake trafficexecutor까지만 검증했다. 해당 의존성 통합·운영 인수 전 차단 해제 금지.

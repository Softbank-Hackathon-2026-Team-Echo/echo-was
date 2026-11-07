# 실제 지진 자동 소비와 과거 시연 dataset

Refs #30(사전 승인 자동 DR 공통 계약). 형래의 기존 목표·담당·본문은 보존한다. 독립 clone에서 즉시 구현 승인 범위만 구현한다.

## 이미 있는 경로와 이번 차이
- live_disaster_feed: 실제 USGS 수집·조직 규모/반경 판정·재난 저장은 기존 구현. 수집기 재작성 없음.
- DisasterService 도착 선택·MigrationPlanService 생성/승인·SecurityGate·queue·Worker 실행은 기존 구현. 자동화는 선택적 내부 검증 접점만 추가.
- WAS #120(보안 통제 시간 예산)·#121(DB 승격 후 요청 기록 재생)은 별도 소유, 변경 없음.
- WAS #117(서비스 라우팅 격리)·#118(DB쌍 안전 기반) 미병합. 이 후보는 실제 HTTP 트래픽 모드에 SERVICE_SCOPED_ROUTING_NOT_READY, DB 단계에는 DB_PAIR_NOT_READY를 반환한다. 117을 복제하거나 실제 전역 weights에 연결하지 않는다. 지원쌍/실효 Job image/VAP 미검증인 상태를 완료로 확대하지 않는다.

## 동의·정책 설계
POST /disasters/auto-dr/consents: 조직 ADMIN 사람만, 정확한 서비스·출발·대상·기한(최대 7일)과 acknowledgeAutomaticExecution=true를 받는다. 현재 상태·설정/변수 버전 digest·보안 규칙표 version/digest·조직 규모/반경을 서버가 묶어 기존 append-only 감사 행에 저장한다. 동의 저장은 자동 실행 스위치를 켜지 않는다. AUTO_DR_CONSENT_IDS의 정확 id allowlist에 포함되고 수집·자동화가 각각 ON인 경우에만 소비 가능. 기본 []/OFF. 철회는 별도 append-only 감사 행이며 같은 id 재활용해도 차단된다.
SYSTEM 계획 생성·승인 감사와 사람이 과거에 남긴 동의 행을 구분한다. plan의 기존 FK에는 동의한 사람 id를 보존하되 SYSTEM 감사 details에 consentId/consentedByUserId를 적는다. 일반 approve API 자기승인 규칙을 변경하지 않는다. 내부 automatic 능력은 재난 id·조직·서비스·대상·동의 행으로 재검증해야만 자기승인 대신 사용된다. HTTP 요청 본문은 automatic 능력을 받지 않는다. AI/AGENT는 사전동의·철회·재난 쓰기 불가.

## 소비·동시성·재시작
USGS 응답 안의 지진 하나·조직 하나 커밋 직후 소비한다. 다른 조직/다음 지진 수집 완료를 기다리지 않는다. threshold는 기존 조직 설정만 사용한다. source가 영향 밖이면 아무 계획 없음. 사전허용 대상 한 곳으로 기존 안전 후보 선택을 한정하고 동시 다른 재난·미등록·이미지/설정/변수 변화·열린 계획을 차단한다.
조직 행 잠금 안에서 대표 id와 별칭을 기존 재난의 eventKeys와 대조한다. 대표 id 변경·수정 수신·동시 poller도 재난/실행을 중복 생성하지 않는다. 재난 feed_event.autoDrAttempts에 서비스별 RESERVED를 커밋한 뒤 계획을 생성한다. 예약 뒤 crash/실패도 소모를 보존한다(자동 retry 없음, RESERVED/RESERVATION_RETAINED로 가시화). callback 장애 시 다음 수집에서 이미 저장한 재난을 중복 생성하지 않는다. 안전을 위해 crash 후 새 계획을 재생성하지 않는다.
승인 전·잠금 안·worker 매 동작·트래픽 요청 직전 동의/철회·기한·정책·설정/이미지 범위·현재 위험을 재검증한다. 비율 단계 직전 보안 게이트를 다시 통과해야 하므로 예외 만료도 차단한다. 승인 전 900초(default)의 freshness를 검사하며 오래된 공식 과거 원본과 미래 이벤트를 현재 자동화 입력으로 쓰지 않는다. 시작한 작업의 시간 경과 자체를 새 지진으로 재실행하지 않는다.
실행 중 동의가 무효가 되면 다음 트래픽 dispatch를 중지하고 FAILED/SECURITY_GATE_BLOCKED로 종료한다. 일부 비율 적용 후이면 rollbackStatus=SKIPPED, 자동 복귀하지 않는다. 순방향 동의는 위험한 출발지 역방향 복귀 권한이 아니므로 ROLLING_BACK 재개도 SKIPPED로 차단한다. 이 상태의 수동 복구는 별도 사람 판단이다.

## 과거 dataset
GET /disasters/history는 DB·queue와 독립된 읽기 전용 test dataset. 기본 enabled=false/items=[], 명시 DISASTER_HISTORY_DEMO_ENABLED=true에서만 고정 2개 반환. 공식 FDSN 시간 범위·limit=1 조회 GeoJSON 원본(2011 도호쿠·2016 구마모토), 실제 파일 수신시각·eventID·SHA를 보존한다. 상세 GeoJSON의 실제 ShakeMap product URL과 별도 원본 SHA도 제공한다. REAL_HISTORICAL은 지진 원본만, 앱 이전은 DEMO/isSimulated=true·한국어 시연 label. 현재 jobs/계획/재난/실행 감사 생성 0. 일반 AGENT GET의 기존 읽기 감사는 시연 실행 기록이 아니다.
지도 circle은 DEMO_ASSUMPTION/ORGANIZATION_POLICY이며 실제 피해·진도라고 표현하지 않는다. 지정 USGS 설명 페이지는 curl/공식 브라우저/아카이브 모두 접근 실패하여 원문 직접 확인은 미검증. 규모와 장소별 흔들림을 동일시하지 않는 보수적 표시 규칙을 적용한다. 실제 ShakeMap은 circle과 독립 자료다.

## 시험 경계
실제 로컬 PostgreSQL 17(이 작업 전용 scratch cluster, loopback 55537)·SQLAlchemy 트랜잭션·서비스·큐·Worker.run_once/run_until_idle 사용. USGS 송수신과 DNS/TLS/실제 트래픽 executor만 test transport/fake로 치환한다. 공식 외부 raw 읽기 수집은 별도 GET 결과를 보존한다. 실제 손님 앱/DB·전역 weights·운영 API 쓰기 0. 기본 OFF 회귀, SHA 원본 일치, history 부작용0 포함. 최초 RED와 후속 GREEN 로그는 archive evidence에 보존한다.

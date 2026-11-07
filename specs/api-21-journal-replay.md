# SPEC — 승격한 DB 쓰기 전환 뒤 요청 기록 재생 버튼 (echo-was, API 서버 21)

- 상태: 구현(기본 꺼짐). 운영에서 켜는 것은 별도 PR(`deploy/base.yaml`)
- 기준: echo-was main `e4d755f`, echo-infra 인프라 12.4 요청 기록(`specs/infra-12.4-request-journal.md`, 11/6 운영 시험 누락 2 → 0)
- 마이그레이션 **0개**(단계 결과 JSON `cutover` 에 `replay` 칸을 덧붙인다)
- API 문서: [docs/api/migration-plans.md](../docs/api/migration-plans.md) 「요청 기록 재생」, 실행기: [docs/executors.md](../docs/executors.md) 「요청 기록 재생」
- 작성: 이도현

## 1. 목표

도쿄 A 가 응답 없이 죽어 DB 쓰기 전환이 서울 B 를 **승격**하면(결과 행 `mode = PROMOTE`), A 에만 들어가고 B 로 복제되지 않은 쓰기를 잃는다. 진입점이 쓰기 요청을 15분 기록하므로(인프라 12.4) 그 기록을 B 에 다시 보내면 살릴 수 있다. 지금은 운영자 PC 에서 IAP 터널·port-forward·토큰을 들고 `replay.sh` 를 돌려야 한다. 이것을 **이전 계획 상세의 버튼 하나**로 바꾼다. 프론트는 버튼과 결과 표시만 더하면 된다.

## 2. 결정

| ID | 결정 | 근거 |
|---|---|---|
| D-1 | `POST /migration-plans/{planId}/replay`, 개발자 이상 사람, 본문 없음, 200 `MigrationPlanDetail` | 승인·중단과 같은 모양. 에이전트는 쓰기 불가 |
| D-2 | 받는 계획: DB 쓰기 전환 단계 `PASSED` + 결과 행 `mode = PROMOTE` + 같은 서비스에 그 뒤 계획 없음 + 도착 연결 살아 있음. 아니면 409 `REPLAY_NOT_AVAILABLE`(reason) | 정상 컷오버(NORMAL)는 원본에서 따라잡아 잃은 쓰기가 없다. 그 뒤 계획(복귀)이 있으면 쓰기 담당이 이미 다른 곳일 수 있다 |
| D-3 | 화면 표시용 `steps[i].cutover.replayAvailable`(앞의 두 조건). 뒤 두 조건은 API 가 409 로 알린다 | 응답 조립은 저장소를 읽지 않는다 |
| D-4 | 실행기는 echo-was 일꾼이 아니라 **API 프로세스**가 트랜잭션 밖에서 부른다. 결과는 잠금(서비스 → 연결 → 계획) 안에서 적는다 | 사람이 누른 즉시 결과를 보여 준다. 끝난 계획에 결과만 더한다(상태 그대로) |
| D-5 | 기록은 진입점 journal `GET /entries`(JSON 에 host 있음)에서 읽고 **서비스 대표 주소 Host 만** 골라 보낸다 | 진입점 하나가 모든 서비스를 기록한다. `replay.sh`(TSV, host 없음)는 서비스를 가르지 못한다 |
| D-6 | 보낼 곳 = 도착 연결의 환경별 주소(`https://…`, 헬스체크와 같은 길). 진입점을 다시 거치지 않는다 | VPC 내부에서 닿는다. 진입점 Host 가 지금 어느 환경으로 가는지와 무관하다 |
| D-7 | 시작점 = 결과 행 `pausedAt − rpoEstimateMs − JOURNAL_REPLAY_MARGIN_SECONDS`(기본 30) | `promote.sh` 가 남긴 「B 가 A 에서 마지막으로 받은 시각」. 겹친 쓰기는 앱 멱등으로 200 |
| D-8 | **진입점 쓰기를 막지 않는다(hold 없음)** | hold 는 진입점 전체(모든 서비스)를 막고, 만료가 없어(인프라 12.4) 일꾼·API 가 죽으면 풀리지 않는다. 버튼은 승격 뒤에 누르므로 순서 보장은 어차피 못 한다. 데이터는 잃지 않는다(순서만 뒤) |
| D-9 | 결과는 `cutover.replay` 에 덮어쓰고 `attempts` 를 센다. 실패가 있어도 계획 상태는 그대로(PARTIAL) | 멱등이라 다시 눌러도 안전. 승격은 이미 끝났다 |
| D-10 | 기록을 못 읽으면 502 `JOURNAL_UNAVAILABLE`, 아무것도 적지 않는다(로그·감사 없음) | 바뀐 것이 없다 |
| D-11 | 스위치 `JOURNAL_REPLAY_MODE`(기본 `disabled` → 409 `JOURNAL_REPLAY_DISABLED`) | 병합만으로 운영 동작이 바뀌지 않는다 |

## 3. 하지 않는 것

- 승격 직후 자동 재생(hold → 승격 → 재생 → release, 이전 계획 단계 안). echo-infra 쪽 hold 만료·필터 상시 켬이 먼저다(이슈 초안 12.5)
- 역방향(B 가 쓰기 담당일 때 서울 장애). 기록이 서울 VM 에 있어 함께 잃는다
- 멱등이 아닌 앱 거르기(서비스 `replayable` 설정). 지금 대상 앱(echo-demo-app·echo-shop-app)은 멱등이다
- 운영 켜기(`deploy/base.yaml` 의 `JOURNAL_*`, Secret Manager `echo-dev-entry-journal-token` 접근 권한)

## 4. 운영에서 켜기 (별도 PR)

1. echo-infra: 관제탑 GSA `echo-dev-echo-was` 에 `echo-dev-entry-journal-token` `secretAccessor`(이 비밀만)
2. `deploy/base.yaml`: 비밀 마운트 `journal-token` → `ECHO_SECRET_JOURNAL_TOKEN`, `JOURNAL_REPLAY_MODE: http`, `JOURNAL_BASE_URL: http://10.60.0.6:8082`, `JOURNAL_TOKEN_REF: env:ECHO_SECRET_JOURNAL_TOKEN`
3. 진입점 a 에서 기록 필터 켬(`journal.enabled=100`) — 꺼져 있으면 기록이 없어 재생이 `sent 0` 이다
4. 리허설: 11/6 runbook 시나리오(B 구독 멈춤 → 쓰기 2건 → A 내림 → 재난 긴급 이전 + DB 쓰기 전환 승인) 뒤 버튼 → `created 2`

## 5. 완료 기준

- [x] 스위치 기본값에서 기존 시험·응답 모양 그대로(`cutover.replayAvailable=false`·`replay=null` 칸만 더해짐)
- [x] 승격 계획에서 버튼 → 서비스 Host 기록만 도착 주소로, 시작점 D-7, 결과·로그 `JOURNAL_REPLAYED`·감사 줄
- [x] 다시 누르면 attempts 2, 실패 있으면 `JOURNAL_REPLAY_PARTIAL`
- [x] 정상 컷오버·DB 쓰기 전환 없음·그 뒤 계획 → 409(reason), 꺼짐 → 409, 기록 못 읽음 → 502 + 아무것도 안 적음
- [x] HTTP 실행기: 순서·메서드·본문·멱등 키, Host 거르기, 보내지 않을 기록, 토큰은 journal 에만, 실패 코드
- [ ] 운영 리허설(4절)

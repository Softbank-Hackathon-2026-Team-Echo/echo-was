# API 서버 8-4: 로컬 리허설에서 찾은 문제 수정 SPEC (변경분)

- 상태: Confirmed (11/3 지민 "전부 추천대로": Q1 A · Q2 A · Q3 A)
- 확인일: 2026-11-03
- Depth: Light
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/8 (In Progress)
- 근거: 로컬 리허설 보고(M5 `echo-was-rehearsal/.deep-interview/rehearsal/report.md`, 사본 `~/archive/softbank-hackathon-2026/api-8/rehearsal/`) — 실제 Envoy 진입점 + 데모 앱 2개 + 50 rps 로 S1~S5 통과, 문제 4건.
- 바꾸는 SPEC: `specs/api-8-migration-plans.md`, `specs/api-8-1-request-count-judgement.md`. 다르면 이 문서가 우선한다.
- 기준: echo-was `main` = API 서버 8-3(`mode` 칸) 병합 뒤 커밋.

## 1. 고칠 것

### 1.1 (중) `simulate ERROR_RATE` 가 롤백을 일으키지 않는다
- 재현(리허설 X1 진입점 http, X2 순수 가짜): ERROR_RATE 로 만든 계획이 `SUCCEEDED` 로 끝난다.
- 원인: `app/services/migration_plan_processor.py::_simulated_counts` 가 세기 시작 뒤 첫 읽기부터 도착 5xx 를 `allowed_errors_5xx + 1` 로 돌려준다. 첫 읽기는 `StepCounting.observe` 의 기준값이 되므로 센 5xx 가 계속 0 이다.
- 고침: 가짜 카운터는 **기준값 읽기에서 5xx 0**, 기준값이 잡힌 뒤의 읽기부터 `allowed_errors_5xx + 1` 을 더한다(누적값이라 이후 읽기에서 줄지 않는다). 그 결과 첫 비율 단계에서 `THRESHOLD_EXCEEDED` 로 실패 → 롤백.
- 같은 함수의 다른 주입(`TARGET_NOT_RECEIVING`·`LOW_TRAFFIC`)도 기준값 규칙 아래에서 의도대로 끝나는지 시험으로 확인한다(필요하면 같은 방식으로 고침).
- 시험: 실제 `StepCounting` 을 쓰는 처리기 수준 시험으로, 주입 시계로 1초씩 여러 번 읽어 ① ERROR_RATE → 첫 단계 `THRESHOLD_EXCEEDED`·롤백 ② TARGET_NOT_RECEIVING → `TARGET_NOT_RECEIVING` ③ LOW_TRAFFIC → `lowTraffic: true` 통과. 기존 시험이 이 버그를 왜 놓쳤는지 보고.

### 1.2 (하) p95 를 못 읽으면 `p95Ms` 가 null 이 아니라 0.0
- 원인: `app/executors/entrypoint.py::_as_float`(·`EntrypointMetricsProvider.read`) 가 `/summary` 의 `p95_ms: null` 을 0.0 으로 바꾼다. 문서(`docs/api/migration-plans.md`)는 null.
- 고침: p95 를 못 읽으면 단계 결과·로그의 `p95Ms` 를 null 로 남긴다. `Metrics.p95_ms` 가 `float` 이면 `float | None` 으로 넓히고 쓰는 곳(가짜·처리기·스키마)을 맞춘다. 판정에는 쓰지 않으므로(p95Advisory) 판정 결과는 바뀌지 않는다. 요청 수·오류율 같은 다른 값의 null 처리 규칙은 바꾸지 않는다.
- 시험: `/summary` 가 `p95_ms: null` 이면 `p95Ms` null, 숫자면 그 값.

## 2. 문서로 정할 것 (코드 동작은 바꾸지 않는다)

- 2.1 `simulated` 의 뜻(Q2 A): 계획을 실행할 실행기 묶음에 가짜가 하나라도 있으면 true 다(실제 계산 규칙을 코드에서 확인해 그대로 적는다). 지금은 데이터 복제 확인·헬스체크 등이 가짜라 진입점이 실제여도 true 이고, 이것이 정직한 표시다. `docs/api/migration-plans.md` 필드 표·`docs/executors.md` 에 적는다.
- 2.2 `simulate` 와 실제 진입점(Q3 A): 실제 진입점(http) 모드에서도 `simulate` 를 받는다(지금 동작). 이때 **트래픽 비율은 실제로 진입점에 적용되고**, 주입은 카운터 읽기(판정 입력)만 바꾼다. 롤백하면 실제 가중치도 출발 100 으로 돌아간다. 시연에서 "실제 트래픽이 옮겨지다가 실패 → 자동 롤백"을 보여 주는 용도. api-8 SPEC 의 "가짜 실행기에서만 받는다" 문장 옆에 이 SPEC 링크로 바뀜을 적는다(원문은 지우지 않음). 실제 받는 조건을 코드에서 확인해 그대로 적는다.
- 2.3 진행 숫자 주의(리허설 관찰): 기준값을 읽는 순간 처리 중이던 요청 때문에 도착 `requestCount` 가 `errors5xx` 보다 잠깐 작게 나올 수 있다(예: 2 < 3). 판정은 각 값의 증가분만 쓰므로 영향 없음. 50 rps 에서 단계별 실제 시간(10% 3.5~6.7초, 50% 2.3~3.5초, 100% 약 1.2초)과 "1초 폴링이면 100% 단계 진행 숫자는 0~1번만 보인다"를 `docs/api/migration-plans.md` 에 프론트 참고로 적는다.
- 2.4 인프라 쪽 관찰(고치지 않음, 기록만): 처음 요청을 받는 도착 환경은 진입점 Prometheus 가 쓰인 지표만 모으고(`usedonly`) 5초마다 긁어서 첫 이전 내내 p95 가 null 일 수 있다. `docs/executors.md` 고정 진입점 절에 한 줄.

## 3. 범위 밖

판정 규칙·기본값 변경, `expectedSeconds` 계산(17초, 실제 약 10초 — 보수적으로 둔다), 사전 확인의 업스트림 없음 감지(실제 헬스체커가 붙으면 잡힌다), 진입점 Prometheus 설정(echo-infra).

## 4. Acceptance criteria

- `AC-8-4-1` ERROR_RATE 주입 계획이 첫 비율 단계에서 `THRESHOLD_EXCEEDED` 로 실패하고 롤백 결과·로그가 남는다(가짜 묶음, http 진입점 + MockTransport 묶음 둘 다).
- `AC-8-4-2` TARGET_NOT_RECEIVING·LOW_TRAFFIC 주입이 각각 의도한 결과로 끝난다.
- `AC-8-4-3` `/summary` p95 null → `p95Ms` null, 숫자 → 그 값. 판정 결과는 같다.
- `AC-8-4-4` 문서 2.1~2.4 반영.
- 품질: ruff·format·mypy app·pytest 전부 통과(건너뜀 0, DB 포함), 마이그레이션 변경 없음.

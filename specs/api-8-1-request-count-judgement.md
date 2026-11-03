# API 서버 8-1: 이전 판정을 "요청 N건 처리 확인"으로 바꾸기 SPEC

- 상태: Confirmed (fresh-eyes 검토 10건 반영, 11/3 지민 확정)
- 확인일: 2026-11-03
- Depth: Standard (기존 이전 계획 판정 로직·응답 계약 변경)
- Interview rounds: R4 (Q1~Q8) · 상태 파일 `.deep-interview/api-8-migration-plan.state.md`
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/8 (In Progress)
- 근거: [#17 리뷰](https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/pull/17#pullrequestreview-5741386178) + 서동옥 동의 댓글(11/3 10:13 KST), 프론트 예고 [echo-frontend#6 댓글](https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-frontend/issues/6#issuecomment-6593705988)
- 바꾸는 SPEC: `specs/api-8-migration-plans.md`(이하 "원 SPEC"). 이 문서와 다르면 이 문서가 우선한다.
- 저장소: `echo-was` · 브랜치 `feat/migration-count-judgement` (`main` = `b364486`에서) · **PR로 올린다**(PR 없는 main 커밋은 배포되지 않음, main 병합 = GKE 운영 배포)

## 1. 목표

비율 단계(10%·50%·100%)의 판정을 "N초 지켜본 뒤 구간 지표 판정"에서 "도착 환경이 요청 N건을 오류 기준 이하로 처리했는지 확인"으로 바꾼다. 진행 숫자("GCP 처리 14/50건 · 오류 0/허용 1")를 1초 단위로 응답에 내려 화면에 보이게 한다.

이유(리뷰 요약): 진입점 Prometheus 수집 간격이 5초라 5초 구간 지표는 비어 `METRICS_UNAVAILABLE` 롤백이 나고, 구간을 늘려도 요청 0건이면 오류율 0%로 통과해 의미가 없으며, 시간 기반은 시연 시간을 많이 쓴다. 판정 품질은 "몇 건을 봤느냐"로 정해진다.

## 2. 확인된 사실 (`main` = `b364486`)

- 판정: `app/services/migration_plan_processor.py::_judge_shift` — 헬스체크 + `metrics.read(target, since, until)` → 오류율·p95를 `rollback_conditions`와 비교. 비율 단계 위치는 `APPLY → OBSERVE(observe_until까지) → JUDGE`.
- 실행기: `app/executors/base.py::MetricsProvider.read(target, since, until) -> Metrics | MetricsUnavailable`, 가짜 `app/executors/fake.py::FakeMetricsProvider`(연결 비율 `weight_of`로 요청 수 계산). PR #17이 `EntrypointMetricsProvider`(`GET /summary?window=…s`)를 구현 중이고, 이 SPEC의 `read_counts`를 같은 클래스에 더하기로 함.
- 진입점(동옥 담당, 범위 밖): `GET /counters` → `{"ts": "...", "envs": {"aws": {"requests": 12345, "errors_5xx": 3}, "gcp": {...}}}`. Envoy 시작 이후 누적값이라 재시작하면 줄어든다. 가중치는 진입점 전체에 하나(시연 = 고객 앱 1개).
- 계획 칸: `migration_plans.observe_seconds` NOT NULL, CHECK 1~600. `steps`·`step_results`·`simulate`는 JSONB. `error_code` CHECK = `MigrationErrorCode` 값 목록. 로그 코드는 DB CHECK 없음.
- 기본값: 오류율 1.0%, p95 800ms(`migration_plan_support.py`), `MIGRATION_OBSERVE_SECONDS_DEFAULT=5`, `MIGRATION_STEP_OVERHEAD_SECONDS=2`.
- 요청 스키마 `ApiRequestModel`은 모르는 칸을 거부(422)한다.
- 응답 실시간 전달은 폴링뿐(SSE 없음).

## 3. 범위

포함
- 실행기 인터페이스 `read_counts` + 가짜 구현 + 계약 시험
- 판정 로직(건수·오류 허용·트래픽 적음·도착 미수신·카운터 재시작·읽기 실패)
- 진행 숫자 저장·응답(`progress`), 단계 결과 칸 변경
- 계획 요청·응답 칸 변경(`observeSeconds` 제거, `requiredRequests`·`maxWaitSeconds` 추가), 예상 시간 계산 변경
- `simulate` 이유 변경, 새 오류 코드 `TARGET_NOT_RECEIVING`
- 문서: `docs/api/migration-plans.md`, `docs/executors.md`(`read_counts` 계약, #17이 맞출 기준), 용어 사전, 원 SPEC 머리에 "판정은 8-1로 바뀜" 한 줄, `.env.example`

제외
- 실제 진입점 연결(`GET /counters` 호출 구현, `ENTRYPOINT_*` 설정·토큰): PR #17(동옥) 이후 별도
- 앱별 라우트(Host별 가중치): 동옥 후속
- SSE 스트림, 화면 구현(프론트)
- 데이터 확인 단계(단계 3) 로직: 그대로
- 어드민 동시 삭제 시험(echo-was #20), PR #18

## 4. 결정 (R4)

| ID | 결정 | 출처 |
|---|---|---|
| R4-Q1 | 필요 건수 10% 단계 20 · 50% 단계 50 · 100% 단계 50, 단계 최대 대기 30초 | [사용자] |
| R4-Q2 | 허용 5xx 건수 = max(1, floor(필요 건수 × 기준 오류율 / 100)). 센 5xx가 허용을 넘는 순간 실패 | [사용자] |
| R4-Q3 | p95는 판정에서 뺀다. 단계가 끝날 때 `metrics.read(도착, 끝−30초, 끝)` 값이 있으면 `p95Ms`에 참고로 기록, 없거나 실패해도 판정에 영향 없음. `rollbackConditions.maxP95Ms`는 남기고 `p95Advisory: true`를 함께 내려 참고용임을 표시 | [사용자] |
| R4-Q4 | API에서 `observeSeconds` 제거(요청에 보내면 422). 요청 선택 칸 `requiredRequests`(정수 3개, 각 1~10000, 10·50·100% 순)·`maxWaitSeconds`(5~300, 기본 30). DB `observe_seconds` 칸은 지우지 않고 최대 대기 초를 적는다. 예상 시간은 건수 ÷ (예상 rps × 비율) | [사용자] |
| R4-Q5 | 일꾼이 1초마다 카운터를 읽어 DB에 적고, 단건 조회에 `progress`를 더한다. 프론트는 1초 폴링 | [사용자] |
| R4-Q6 | `simulate.reason`: `ERROR_RATE`(5xx 증가 → 즉시 실패)·`HEALTH`·`TRAFFIC`·`DATA_LAG` 유지, `LATENCY` 제거(요청 422), `TARGET_NOT_RECEIVING`·`LOW_TRAFFIC` 추가 | [사용자] |
| R4-Q7 | 카운터가 줄면(진입점 재시작) 기준값을 지금 값으로 다시 잡고 이미 센 건수는 이어 간다. 로그 `COUNTERS_RESET` | [사용자] |
| R4-Q8 | 최대 대기 안에 건수를 못 채우면: 도착에 갔어야 할 만큼 출발에 요청이 왔는데 도착이 거의 못 받았으면 `TARGET_NOT_RECEIVING` 실패·롤백, 아니면(서비스 전체가 조용함) "트래픽 적음"으로 통과 | [사용자] (넘겨받은 "INSUFFICIENT_TRAFFIC 실패"를 뒤집음) |
| AD-8-1-1 | 카운터 읽기 실패(`CountsUnavailable`)는 일시 오류로 보고 계속 다시 시도한다. 출발·도착은 따로 갱신한다(한쪽만 실패하면 다른 쪽은 반영). 건수 채움·5xx 판정은 도착 값만 본다. 마감 처리는 §5.4 표. **기준값을 잡은 뒤라도 마감 시각에 도착의 마지막 성공 읽기가 `MIGRATION_COUNTS_STALE_SECONDS`(기본 5)보다 오래됐으면 `METRICS_UNAVAILABLE` 롤백**(아무것도 보지 못한 채 다음 비율로 넘어가지 않게, 원 SPEC "판단 불가 = 롤백" 원칙) | [AI-default] fresh-eyes 검토 반영 |
| AD-8-1-2 | 헬스체크는 필요 건수를 채웠을 때 또는 트래픽 적음 통과 직전에 한 번 한다(JUDGE 위치). 실패면 `HEALTH_CHECK_FAILED` | [AI-default] 원 SPEC 판정 유지 |
| AD-8-1-3 | `read_counts`는 `MetricsProvider` 프로토콜의 두 번째 메서드로 둔다(#17의 `EntrypointMetricsProvider`가 같은 클래스에 구현) | [AI-default] 실행기 묶음 변경 최소 |
| AD-8-1-4 | "도착 미수신" 판정(정수식, 부동소수 경계 오류 방지). src = 출발 센 요청, tgt = 도착 센 요청, N = 필요 건수, w = 도착 비율. w < 100: `src × w ≥ N × (100 − w)`(도착에 N건 이상 갔어야 함) **그리고** `4 × tgt × (100 − w) < src × w`(실제가 기대의 4분의 1 미만). w = 100: `src ≥ N` **그리고** `4 × tgt < src` — 출발 비율이 0인데 출발로 요청이 계속 간다 = 진입점이 비율을 적용하지 않은 상황을 잡는다 | [AI-default] 예: 10% 단계 출발 180건 → 도착 4건이면 실패, 5건이면 트래픽 적음 통과 |
| AD-8-1-5 | 트래픽 적음 통과는 단계 결과 `PASSED` + `metrics.lowTraffic = true`, 로그 코드는 `JUDGE_PASSED` 그대로(data에 `lowTraffic: true`, 고정 문장 뒤에 "(트래픽 적음, 헬스체크로 통과)") | [AI-default] 로그 순서 계약(원 AC-2) 유지 |
| AD-8-1-6 | 카운터 읽기 주기 `MIGRATION_COUNT_POLL_SECONDS`(기본 1). 세는 동안 이 주기로 중단 요청·임대 상실도 확인한다 | [AI-default] |
| AD-8-1-7 | 저장된 옛 `simulate.reason = LATENCY` 계획은 읽을 수 있게 enum 값은 남기되, 처리기는 실패를 만들지 않는다(요청만 422) | [AI-default] 운영 기존 행 안전 |
| AD-8-1-8 | 카운터 재시작 감지 시 "재시작 뒤 0부터 다시 쌓였다"로 본다: carried += last − baseline, baseline = 0, last = 지금 값. 재시작 뒤 쌓인 요청·5xx도 센다(R4-Q7 "센 건수 이어 감"의 계산 방식) | [AI-default] fresh-eyes 검토 반영. Envoy 카운터는 재시작하면 0부터 다시 센다 |
| AD-8-1-9 | 허용 5xx는 필요 건수 기준으로 고정한다(센 요청이 N을 넘어도 다시 계산하지 않음). 계산은 `Decimal(str(기준 오류율))`. `errorRatePercent` = min(100, 5xx ÷ 요청 × 100), 요청 0이면 null | [AI-default] |
| AD-8-1-10 | 카운터는 환경(aws/gcp) 단위라, 같은 환경을 쓰는 다른 서비스 요청이 섞인다. 가중치도 진입점 전체에 하나이므로 "환경당 고객 앱 1개" 전제를 `docs/executors.md`·§9에 적고 코드로 막지는 않는다(앱별 라우트·카운터는 동옥 후속) | [AI-default] 시연 전제와 같음 |
| AD-8-1-11 | 롤링 배포 중 옛 파드 호환: `steps[i]`에 `observeSeconds` 키를 계속 함께 적는다(값 = maxWaitSeconds, 응답에는 내보내지 않음). 진행 중인 옛 계획(SHIFT 단계 OBSERVE·JUDGE인데 센 값 JSON이 없음)은 새 일꾼이 그 자리에서 OBSERVE를 다시 시작한다(`observe_until = now + 그 계획의 observe_seconds 칸`, 필요 건수는 설정 기본값). 옛 파드가 남아 있는 1~2분 동안 새 오류 코드·새 simulate 이유를 옛 파드가 읽으면 500이 날 수 있음은 수용한 위험(§9) | [AI-default] fresh-eyes 검토 반영, 운영 실행 중 계획 가능성 낮음 |

## 5. 흐름

### 5.1 계획 만들기 (원 SPEC §5.1 변경분)
- 요청 `requiredRequests`(선택, 길이 3, 각 1~10000) — 빼면 설정 `MIGRATION_REQUIRED_REQUESTS_DEFAULT`(`20,50,50`). `maxWaitSeconds`(선택, 5~300) — 빼면 `MIGRATION_MAX_WAIT_SECONDS_DEFAULT`(30). `observeSeconds`를 보내면 422 `VALIDATION_ERROR`.
- 요구 건수와 최대 대기는 각 SHIFT 단계 JSON(`steps[i]`)에 `requiredRequests`·`maxWaitSeconds`로 저장한다. DB `observe_seconds` = `maxWaitSeconds`(CHECK 1~600 안).
- 단계 예상 초: SHIFT = `MIGRATION_STEP_OVERHEAD_SECONDS` + min(maxWait, ceil(requiredRequests ÷ (MIGRATION_EXPECTED_RPS × w ÷ 100))). 나머지 = overhead. 기본값이면 2 + 6 + 4 + 2 + 3 = **17초**(10%: 20 ÷ 5 = 4, 50%: 50 ÷ 25 = 2, 100%: 50 ÷ 50 = 1).
- `simulate.reason`에 `LATENCY` → 422. `LOW_TRAFFIC`·`ERROR_RATE`·`HEALTH`·`TRAFFIC`는 SHIFT 단계(1·2·4)만, `TARGET_NOT_RECEIVING`은 단계 1·2만(단계 4는 출발 비율 0이라 가짜로 만들 수 없음), `DATA_LAG`는 단계 3만. 어긋나면 기존 이유 코드 `SIMULATE_REASON_STEP_MISMATCH`로 422.

### 5.2 비율 단계 진행 (원 SPEC §5.4 표의 SHIFT 행 교체)
위치는 그대로 `APPLY → OBSERVE → JUDGE`. `OBSERVE`가 "건수 세기", `observe_until` = 최대 대기 끝 시각(= 비율 적용 시각 + maxWaitSeconds).

1. **APPLY**: 기존대로 비율 적용. 적용됐으면 같은 트랜잭션에서 `OBSERVE`로 바꾸고 `observe_until`을 적는다. 그 뒤(트랜잭션 밖) 출발·도착 `read_counts`를 한 번 읽어 기준값을 저장한다(읽기 실패면 기준값 없음 → 다음 읽기에서 잡는다).
2. **OBSERVE (한 번 실행 = 한 번 읽기)**: 중단·임대 상실 확인 → 출발·도착 `read_counts`(트랜잭션 밖) → 한 트랜잭션에서 센 값 갱신(§5.3) + 판정:
   - 도착 5xx > 허용 → 즉시 `THRESHOLD_EXCEEDED` 실패·롤백(로그 `JUDGE_FAILED`).
   - 도착 센 요청 ≥ 필요 건수 → `JUDGE`로 전환, 같은 트랜잭션에서 최종 센 값과 `judgeReason = COUNT_REACHED`를 단계 결과에 저장·커밋.
   - `now ≥ observe_until` → §5.4 마감 판정(실패면 OBSERVE에서 바로 롤백, 통과 경로면 `JUDGE` 전환 + `judgeReason = LOW_TRAFFIC` 저장·커밋).
   - 그 밖 → 진행 숫자를 적고 `run_after = min(now + MIGRATION_COUNT_POLL_SECONDS, observe_until)`.
3. **JUDGE**: 카운터를 다시 읽지 않는다. 저장된 센 값·`judgeReason`으로 헬스체크(트랜잭션 밖) → p95 참고 읽기 → 통과면 `PASSED`(`lowTraffic = judgeReason == LOW_TRAFFIC`) + 다음 단계 첫 동작까지(원 SPEC 규칙), 헬스 실패면 `HEALTH_CHECK_FAILED` 롤백. JUDGE 중 일꾼이 죽으면 다시 시작한 일꾼이 같은 저장값으로 같은 경로를 밟는다(로그 줄은 반영 때만 쓰므로 겹치지 않음).
4. 각 OBSERVE 실행은 센 값 JSON(§5.3)과 `updated_at`을 조건부 UPDATE로 쓴다(위치가 기대와 다르면 0행 → 다시 읽기). 진행 숫자 갱신은 로그 줄을 남기지 않는다. 임대가 넘어가 다른 일꾼이 이어 받아도 저장된 센 값에서 이어 센다.
5. p95 참고: 단계를 끝낼 때(통과·실패 모두) `metrics.read(도착, 끝 − MIGRATION_P95_REFERENCE_WINDOW_SECONDS, 끝)`을 한 번 불러 `p95Ms`를 채운다(R4-Q3, 실패·없음은 null). 판정에 영향 없음.

### 5.3 센 값 (단계 결과 JSON, 환경마다 `source`·`target`)
`{baseline: {requests, errors5xx} | null, last: {requests, errors5xx} | null, carried: {requests, errors5xx}}`
- 센 값 = carried + (last − baseline). 기준값이 없으면 0.
- 지금 읽은 값이 last보다 작으면(요청·5xx 중 하나라도) 재시작으로 본다(AD-8-1-8): carried += (last − baseline), baseline = 0, last = 지금 값, 로그 `COUNTERS_RESET`(data: 환경 `source|target`, 이전 last, 지금 값). 화면 숫자는 줄지 않는다(R4-Q7).
- 예: 기준 12,000 → last 12,014(14건) → 다음 읽기 3 → 재시작. carried 14, baseline 0, last 3 → 센 값 17.
- 읽기가 실패하는 동안 재시작해 새 누적값이 이미 last보다 커지면 감지하지 못한다(수용, §9).
- 단계 결과에 마지막 성공 읽기 시각(`lastReadAt`, 환경별)을 함께 둔다.

### 5.4 마감 판정 (최대 대기 끝, 필요 건수 못 채움)
| 상태 | 결과 |
|---|---|
| 도착 기준값 없음(단계 내내 도착 읽기 실패) | `METRICS_UNAVAILABLE` 롤백 |
| 도착 마지막 성공 읽기가 `MIGRATION_COUNTS_STALE_SECONDS`보다 오래됨 | `METRICS_UNAVAILABLE` 롤백 |
| 도착·출발 기준값 모두 있음, AD-8-1-4 도착 미수신 | `TARGET_NOT_RECEIVING` 롤백(로그 `JUDGE_FAILED`) |
| 도착 기준값만 있음(출발 읽기 실패) | 미수신 판정 생략 → 트래픽 적음 경로 |
| 그 밖 | `JUDGE`(`judgeReason = LOW_TRAFFIC`) → 헬스체크 통과면 `PASSED` + `lowTraffic: true`(로그 `JUDGE_PASSED`, data `lowTraffic: true`), 실패면 `HEALTH_CHECK_FAILED` |

### 5.5 simulate (가짜 모드, 처리기가 카운터를 만들어 낸다)
- 처리기는 `simulate` 단계에서 실행기를 부르지 않고 "설정 rps(`MIGRATION_EXPECTED_RPS`) × 비율 × 지난 시간"으로 센 값을 만든다(시계 주입으로 결정적).
- `ERROR_RATE`: 첫 읽기에서 도착 5xx = 허용 + 1 → 즉시 실패. `TARGET_NOT_RECEIVING`(단계 1·2): 도착 카운터 고정, 출발은 "rps × 100% × 지난 시간"으로 증가(비율이 적용되지 않은 상황 흉내) → 마감에 실패. `LOW_TRAFFIC`: 양쪽 고정 → 마감에 트래픽 적음 통과. `HEALTH`: 건수는 정상, 헬스체크 실패. `TRAFFIC`·`DATA_LAG`: 기존 그대로.

## 6. 계약

### 6.1 실행기 (`app/executors/base.py`)
```python
@dataclass(frozen=True, slots=True)
class RequestCounts:
    requests: int      # 진입점이 켜진 뒤 그 환경으로 보낸 누적 요청 수(≥ 0)
    errors_5xx: int    # 그중 5xx 응답 수(≥ 0, ≤ requests 보장 안 함)

@dataclass(frozen=True, slots=True)
class CountsUnavailable:
    code: str | None = None   # 실제 구현의 실패 코드(예: ENTRYPOINT_UNREACHABLE), 가짜는 None

type CountsResult = RequestCounts | CountsUnavailable

class MetricsProvider(Protocol):
    async def read(self, target, since, until) -> MetricsResult: ...        # 그대로(p95 참고용)
    async def read_counts(self, target: DeployTarget) -> CountsResult: ...  # 새로
```
- 누적값은 단조 증가가 원칙이고, 줄면 재시작으로 처리한다(처리기 몫). 실제 구현은 `GET /counters`의 `envs[<환경 이름>]`을 매핑한다(#17, 범위 밖). `docs/executors.md`에 이 계약과 `/counters` 매핑 표를 적는다.
- 가짜 `FakeMetricsProvider.read_counts`: 주입 시계로 "rps × 그 연결 비율(`weight_of`, 모르면 100) × 지난 초"를 누적(비율이 바뀌어도 단조 증가), 5xx 0. rps는 설정 `MIGRATION_EXPECTED_RPS` 한 값을 주입해 `read`(p95 참고용)의 요청 수 계산과 통일한다(지금 `read`의 고정 10 rps 상수는 이 값으로 바꾼다). 시험용 손잡이: 고정(`freeze`), 5xx 더하기, 줄이기(재시작 흉내), 읽기 실패(환경별).
- `tests/executor_contract.py`에 `read_counts` 계약 시험(단조 증가·음수 없음·실패 형태)을 더한다.

### 6.2 HTTP 응답 (원 SPEC §8.2 변경분)
- 요청 `CreateMigrationPlanRequest`: `observeSeconds` 삭제, `requiredRequests: int[3] | null`, `maxWaitSeconds: int | null` 추가.
- `MigrationPlanDetail`: `observeSeconds` 삭제 → `maxWaitSeconds`. `rollbackConditions`에 `p95Advisory: true`. **`progress`** 추가:
  ```json
  "progress": {
    "step": 2, "targetWeight": 50,
    "requests": 14, "requiredRequests": 50,
    "errors5xx": 0, "allowedErrors5xx": 1,
    "sourceRequests": 15,
    "startedAt": "…", "deadlineAt": "…", "updatedAt": "…"
  }
  ```
  `status = RUNNING`이고 현재 단계가 SHIFT의 `OBSERVE`·`JUDGE`일 때만 값, 그 밖은 `null`. `updatedAt` = 마지막 카운터 읽기 시각. 목록(Summary)에는 넣지 않는다.
- `steps[i]`: `observeSeconds` 삭제 → SHIFT면 `requiredRequests`·`maxWaitSeconds`, 아니면 null.
- `steps[i].result.metrics`: `requestCount`(도착 센 요청), `errors5xx`, `allowedErrors5xx`, `errorRatePercent`(= 5xx ÷ 요청 × 100, 요청 0이면 null), `sourceRequestCount`, `lowTraffic`(bool), `p95Ms`(참고), `healthStatus`, `replicationLagSeconds`(단계 3만).
- 새 오류 코드 `TARGET_NOT_RECEIVING`(계획 `errorCode`, 단계 `failureCode`). 고정 문장: "도착 환경이 요청을 받지 못했습니다(진입점 비율 확인 필요)". 원 SPEC AC-12(원인 코드별 문장이 모두 다름) 유지.
- 새 로그 코드 `COUNTERS_RESET`(INFO). `JUDGE_PASSED`·`JUDGE_FAILED` data에 `requestCount`·`errors5xx`·`allowedErrors5xx`·`sourceRequestCount`·`lowTraffic`.
- 계약 문서 `docs/api/migration-plans.md`의 예시 JSON·표·로그 표를 고친다.

### 6.3 데이터
- Alembic revision 하나: `migration_plans.error_code` CHECK에 `TARGET_NOT_RECEIVING` 추가(넓히기만). 다른 칸 변경 없음(새 값은 JSONB 안). downgrade는 그 값이 있는 행이 있으면 분명한 오류로 멈춘다. `db-schema.sql`·changelog 갱신.

### 6.4 설정 (`app/core/config.py`, `.env.example`)
- 제거: `MIGRATION_OBSERVE_SECONDS_DEFAULT`(배포 매니페스트·문서에 남은 참조도 정리).
- 추가: `MIGRATION_REQUIRED_REQUESTS_DEFAULT`(기본 `20,50,50`, 각 1~10000), `MIGRATION_MAX_WAIT_SECONDS_DEFAULT`(30, 5~300), `MIGRATION_EXPECTED_RPS`(50, 1~10000, 예상 시간·가짜 카운터·simulate에 씀), `MIGRATION_COUNT_POLL_SECONDS`(1, 1~10), `MIGRATION_COUNTS_STALE_SECONDS`(5, 1~60), `MIGRATION_P95_REFERENCE_WINDOW_SECONDS`(30, 5~300).

## 7. 호환성·배포

- main 병합 = GKE 운영 배포. 마이그레이션은 CHECK 넓히기만이다. 롤링 배포 중 옛 파드 호환은 AD-8-1-11.
- 응답의 `maxWaitSeconds` 출처는 계획 행의 `observe_seconds` 칸 하나로 고정한다(새 계획은 둘이 같고, 옛 계획은 그 계획의 지켜보기 초). 옛 계획의 `steps[i].requiredRequests`는 null로 응답한다.
- 프론트는 아직 목업이라 `observeSeconds` 제거로 깨질 화면이 없다. 응답 모양은 PR을 올릴 때 echo-frontend#6 댓글로 알린다(지민 몫, 구현 범위 밖).
- #17은 `read_counts`를 이 계약대로 구현한다(병합 순서: 이 PR 먼저).

## 8. Acceptance criteria

모든 시험은 로컬 DB에서 건너뜀 0, 주입 시계로 결정적. 시험은 `run_until_idle` 헬퍼(시계를 `run_after`까지 진행)로 끝 상태를 보고, 경계(19→20건, 29→30초)만 따로 정확히 본다.

원 SPEC AC 처리표:

| 원 AC | 처리 |
|---|---|
| AC-1 | 변경 → AC-8-1-1 (예상 17초, 건수 기준) |
| AC-2 | 변경: 로그 순서는 그대로, "지켜보기 간격 ≥ observeSeconds" 검사를 "각 `JUDGE_PASSED` data `requestCount ≥ requiredRequests`"로 바꿈 |
| AC-3 | 폐기 → AC-8-1-2(건수 경계)·AC-8-1-9(요청 검증)로 대체 |
| AC-7 | 변경: `LATENCY` 경우 삭제. `ERROR_RATE` 기대값은 `errors5xx = allowedErrors5xx + 1`·`THRESHOLD_EXCEEDED`. "지표 MetricsUnavailable이면 롤백"은 "카운터 CountsUnavailable 단계 내내 → METRICS_UNAVAILABLE 롤백"으로(AC-8-1-7). `TRAFFIC`·`HEALTH`·`DATA_LAG`는 그대로 |
| AC-8 | 변경: "단계 1 지켜보기 중" → "단계 1 세는 중" |
| AC-11 | 변경: 재시작 이어 가기를 "OBSERVE 중 임대 만료 → 다른 일꾼이 저장된 센 값에서 이어 셈, 로그 중복 없음"과 "JUDGE 전환 직후 임대 만료 → 같은 `judgeReason`으로 통과"로 확인 |
| AC-20 | 변경: README·문서의 "약 25초" → "약 17초(합성 트래픽 50 rps 기준)" |
| 나머지(AC-4~6, 9, 10, 12~19, 21) | 그대로 통과 |

- `AC-8-1-1` 성공 경로: 기본 계획(가짜 50 rps) → `expectedSeconds = 17`, 비율 (90,10) → (50,50) → (0,100), `SUCCEEDED`. 각 SHIFT 단계 결과 `requestCount ≥ requiredRequests`, `lowTraffic = false`. 로그 순서는 원 AC-2와 같다(`COUNTERS_RESET` 없음).
- `AC-8-1-2` 건수 경계: 10% 단계에서 도착 19건이면 다음 단계로 가지 않고, 20건이 되는 실행에서 헬스체크 후 통과.
- `AC-8-1-3` 오류 허용: 필요 20건·기준 1%면 `allowedErrors5xx = 1`. 5xx 1건이면 계속 세고, 2건이 되는 실행에서 건수를 채우기 전이라도 `THRESHOLD_EXCEEDED`로 롤백, 비율 (100, 0). 기준 10%·필요 50건이면 허용 5.
- `AC-8-1-4` 트래픽 적음: 양쪽 카운터 고정 → 29초 시점까지 판정 안 함, 30초에 `PASSED`·`lowTraffic = true`, 로그 `JUDGE_PASSED`(data `lowTraffic: true`), 다음 단계 진행.
- `AC-8-1-5` 도착 미수신(정수식): 10% 단계(N = 20)에서 출발 180건·도착 4건 → 30초에 `TARGET_NOT_RECEIVING` 롤백, 비율 (100, 0). 출발 180건·도착 5건 → 트래픽 적음 통과. 출발 179건(179 × 10 < 20 × 90)·도착 0건 → 트래픽 적음 통과. 100% 단계(N = 50)에서 출발 60건·도착 14건 → 실패, 출발 60건·도착 15건 → 통과.
- `AC-8-1-6` 카운터 재시작: 도착 14건 센 뒤 도착 카운터 읽기가 3이면 `COUNTERS_RESET` 1줄, 그 실행 직후 `progress.requests = 17`(줄지 않음), 이어 세어 20에서 통과.
- `AC-8-1-7` 읽기 실패: (a) 단계 내내 도착 `CountsUnavailable` → 30초에 `METRICS_UNAVAILABLE` 롤백. (b) 중간 몇 번만 실패 → 계속 세고 통과. (c) 기준값을 잡은 뒤 24초부터 도착 읽기가 계속 실패하고 건수 미달 → 30초에 `METRICS_UNAVAILABLE`(마지막 성공 6초 전 > 5). (d) 출발 읽기만 단계 내내 실패, 도착 0건 → 트래픽 적음 통과(미수신 판정 생략). (e) 도착 읽기만 실패하는 동안 출발은 갱신됨.
- `AC-8-1-8` p95 참고: 지표 공급자가 p95 5000ms를 줘도 통과, 단계 결과 `p95Ms = 5000`. 지표가 `MetricsUnavailable`이어도 통과, `p95Ms = null`. 응답 `rollbackConditions.p95Advisory = true`.
- `AC-8-1-9` 요청 검증: `observeSeconds` → 422. `requiredRequests` 길이 2·4, 값 0·10001 → 422. `maxWaitSeconds` 4·301 → 422. `simulate.reason = LATENCY` → 422. 값을 주면 `steps[i]`·`expectedSeconds`에 그대로 반영.
- `AC-8-1-10` 진행 응답: 50% 단계 세는 중 단건 조회 → `progress.step = 2`, `requests`가 다음 실행 뒤 커지고 `updatedAt`이 바뀜, `deadlineAt = 적용 시각 + 30초`. 대기·끝난 계획·단계 3 중에는 `progress = null`. 목록 응답에는 `progress` 칸 없음.
- `AC-8-1-11` 중단 반영: 세는 중 `abort` → 다음 실행(1초 안)에서 롤백 시작.
- `AC-8-1-12` simulate: `ERROR_RATE`(단계 1) → 첫 읽기에서 `THRESHOLD_EXCEEDED`. `TARGET_NOT_RECEIVING`(단계 2) → 30초에 실패, 단계 4에 주면 422 `SIMULATE_REASON_STEP_MISMATCH`. `LOW_TRAFFIC`(단계 4) → 30초에 트래픽 적음 통과 후 `SUCCEEDED`. `HEALTH` → `HEALTH_CHECK_FAILED`. 저장된 옛 `LATENCY` 계획은 실패 없이 진행.
- `AC-8-1-16` 옛 계획 호환: SHIFT 단계 OBSERVE 위치·센 값 없음·`observe_seconds = 5`인 계획 행을 새 일꾼이 집으면 그 자리에서 세기를 다시 시작하고(`observe_until = now + 5`) 정상 진행. 새 계획의 `steps[i]` JSON에 `observeSeconds` 키가 있다(값 = maxWaitSeconds), 응답에는 없다.
- `AC-8-1-17` 오류율 계산: 기준 0.57%·N 10000이면 허용 57(부동소수로 곱하면 56이 나오는 경계). 5xx > 요청인 가짜 값이면 `errorRatePercent = 100`.
- `AC-8-1-13` 실행기 계약: 가짜 `read_counts`가 단조 증가·음수 없음·비율에 비례(10% 연결은 100% 연결의 약 10%), 고정·5xx·줄이기·실패 손잡이 동작. `executor_contract` 통과.
- `AC-8-1-14` 마이그레이션: upgrade 후 `TARGET_NOT_RECEIVING` 저장 가능, downgrade 시 그 값이 있으면 분명한 오류, 없으면 성공. `alembic check` 변화 없음, `db-schema.sql` 일치.
- `AC-8-1-15` 문서: `docs/api/migration-plans.md`(요청·응답·progress·로그·오류 코드), `docs/executors.md`(`read_counts` 계약 + `/counters` 매핑), `.env.example`, 원 SPEC 머리 한 줄이 이 SPEC과 맞다.
- 품질: ruff·format·mypy app·pytest 전부 통과(건너뜀 0).

## 9. 리스크
- 시연 중 합성 트래픽이 멈추면 단계마다 30초 대기 후 "트래픽 적음 통과"로 넘어간다(실패는 아님). 시연 전 합성 트래픽 확인을 리허설 체크리스트에 둔다(동옥 몫).
- 1초마다 계획 행을 쓴다(단계당 최대 30번). 데모 규모에서는 문제없다.
- 진입점이 환경 하나에만 카운터를 주는 등 `/counters` 모양이 다르면 #17에서 맞춘다(계약은 `docs/executors.md`).
- 카운터는 환경 단위라 환경당 고객 앱이 2개 이상이면 다른 앱 요청이 섞여 판정이 틀린다(AD-8-1-10, 앱별 라우트·카운터는 동옥 후속).
- 읽기 실패 중 진입점이 재시작하고 새 누적값이 이미 last보다 크면 재시작을 감지하지 못한다. 시연 규모에서는 드물어 수용한다.
- 롤링 배포 1~2분 동안 옛 파드가 새 오류 코드·새 simulate 이유를 가진 행을 읽으면 500이 날 수 있다(AD-8-1-11, 수용).

## 10. 구현 계획으로 위임한 세부사항
- 센 값·진행 칸의 JSON 키 이름(응답 칸 이름은 §6.2 고정), 시험 헬퍼 구성, 가짜 카운터 손잡이 이름.
- 로그 고정 문장의 정확한 문구(원인 코드마다 다르게).

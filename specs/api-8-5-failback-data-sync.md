# API 서버 8-5: 도착 DB가 이미 쓰기 담당이면 복귀 데이터 확인을 통과시킨다

- 상태: Confirmed (지민 결정 C, 2026-11-05 12:00)
- 기준: main `1e303dd005df52092423d52dc52e03be9a9acb5b` (#70)
- 관련: GitHub 이슈 https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/9 후속 (API 9 AC-12 운영 시험에서 발견)

## 1. 목표

트래픽만 이전하고(DB 컷오버 없음, 쓰기는 출발 A 그대로) 복귀(gcp→aws)를 실행하면 데이터 확인 단계가 `METRICS_UNAVAILABLE`로 롤백된다. 원인: `PostgresDataSyncChecker.replication_lag`(`app/executors/postgres_data_sync.py:129-158`)가 출발 DB를 항상 발행자로 보고 그 `pg_stat_replication`에서 도착 구독 walsender를 찾는데, 컷오버를 안 했으면 출발(B)에 walsender가 없다.

검사기가 **"쓰기 담당 DB가 어디인가"**를 보고 판단하게 한다. 도착 DB가 이미 쓰기 담당(발행자)이면 데이터는 이미 도착에 있으므로 지연 0으로 통과하고 이유 코드 `TARGET_IS_WRITER`를 남긴다. 그 밖은 지금 동작 그대로.

## 2. 판정 규칙 표

이름: `S` = 출발 매핑의 구독 이름(출발이 구독자일 때 이름), `T` = 도착 매핑의 구독 이름.

| # | 확인(순서대로) | 결과 |
|---|---|---|
| 0 | 출발·도착 매핑 없음 / 비밀값 없음 | 지금 그대로 `DataSyncUnavailable` (`DATA_SYNC_ENV_UNMAPPED`·`DATA_SYNC_SECRET_MISSING`) |
| 1 | 출발 DB 접속 실패·시간 초과 | 지금 그대로 `DataSyncUnavailable` (`DATA_SYNC_UNREACHABLE`) |
| 2 | 출발 DB `pg_stat_replication`에 `application_name = T` 행이 **있음** (출발 = 발행자) | 지금 그대로: streaming이면 lag 값(`DataSyncLag(seconds)`, 이유 없음), streaming 아님·따라잡지 못함이면 `DATA_SYNC_NOT_STREAMING` |
| 3 | 2의 행이 **없음** + 같은 출발 연결에서 `pg_subscription`에 `subname = S AND subenabled` 행이 있음 (출발이 구독 중) | `DataSyncLag(seconds=0.0, reason="TARGET_IS_WRITER")` |
| 4 | 2·3 아님 + 도착 DB `pg_stat_replication`에 `application_name = S` 행이 있음(상태 무관, 도착 = 발행자) | `DataSyncLag(seconds=0.0, reason="TARGET_IS_WRITER")` |
| 5 | 2의 행 없음 + 3·4 모두 아님(4의 도착 접속 실패·시간 초과 포함) | 지금 그대로 `DataSyncUnavailable` (`DATA_SYNC_NOT_STREAMING`) → 계획 `METRICS_UNAVAILABLE` |

- 3의 질의는 2와 같은 출발 연결에서 닫기 전에 한다(연결을 더 열지 않는다). 4는 도착 DB 연결 하나(같은 계정·비밀값·TLS·제한 시간, 연결 → `fetchrow` → `close`). 접속은 최대 2번(제한 3초 × 2 < 일꾼 임대 30초). 11:54 배치(B `echo_sub` enabled)는 3에서 끝난다.
- 규칙 2가 있으면 3·4를 보지 않는다(양쪽 다 발행 중이면 지금처럼 lag 검사).
- 3·4 통과 때 `logger.info("data sync target is writer", extra={action, source/target environment id, reason, evidence: "SOURCE_SUBSCRIPTION" | "TARGET_WALSENDER"})`(주소·비밀값 없음).

## 3. 바뀌는 파일

| 파일 | 변경 |
|---|---|
| `app/executors/base.py` | `DataSyncLag`에 `reason: str \| None = None` 추가(기존 생성 호출 그대로 동작) |
| `app/executors/postgres_data_sync.py` | 규칙 3·4 + 질의 상수 2개 + `DATA_SYNC_TARGET_IS_WRITER = "TARGET_IS_WRITER"` + 모듈 독스트링 |
| `app/services/migration_plan_processor.py` | `_check_data_sync`: `DataSyncLag.reason`이 있으면 `DATA_SYNC_OK` 로그 data에 `"reason"` 칸을 더한다(`{**_numbers(metrics_json), "reason": …}`). 단계 metrics 칸·응답 스키마는 그대로 |
| `tests/test_postgres_data_sync.py` | 새 시험(4절) — 기존 시험 삭제·약화 금지 |
| `tests/integration/…`(기존 이전 실행 시험 파일 중 알맞은 곳) | 처리기 수준 새 시험 1개 이상 |
| `docs/executors.md` 173-179행, `README.md` 328행, `CLAUDE.md` 84행 | 규칙 한두 줄 |
| `specs/api-8-5-failback-data-sync.md` | 이 SPEC |

마이그레이션 0, 새 환경 변수 0, 새 의존성 0, API 경로·요청·응답 모양 변경 0, 가짜 실행기 기본 동작 변경 0.

## 4. 완료 기준 (AC)

- **AC-1 출발 발행자(지금 동작 유지)**: 기존 `tests/test_postgres_data_sync.py` 시험 전부 그대로 통과. 새 시험: 출발에 T 행 streaming → `DataSyncLag(0.07)`·`reason is None`·접속 1번(도착 접속 없음); 출발에 T 행 있으나 streaming 아님 → `DataSyncUnavailable`이고 도착 접속 없음.
- **AC-2 도착 발행자**: 새 시험 — (a) 출발 행 없음 + 출발 `pg_subscription` S enabled → `DataSyncLag(0.0, reason="TARGET_IS_WRITER")`, 접속 1번(출발만), 구독 질의 인자는 S; (b) 출발 행 없음 + 구독 없음 + 도착 DB에 S 행 → 같은 결과, 도착 접속 host·port는 도착 매핑, 질의 인자는 S. 11:54 실측 배치(출발 gcp/B `echo_sub`, 도착 aws/A `echo_sub_rev`, A에 `echo_sub` walsender)를 그대로 재현한 시험 하나 포함. 처리기 수준 시험: 검사기가 `DataSyncLag(0.0, reason="TARGET_IS_WRITER")`를 주면 데이터 확인 단계 PASSED, 단계 metrics `replicationLagSeconds` 0.0, `DATA_SYNC_OK` 로그 data에 `reason: "TARGET_IS_WRITER"`, 계획이 다음 단계로 진행.
- **AC-3 둘 다 없음**: 새 시험 — 출발 행 없음 + 구독 없음 또는 disabled + (도착 행 없음 / 도착 접속 실패) → 모두 `DataSyncUnavailable`(경고 코드 `DATA_SYNC_NOT_STREAMING`). 기존 처리기 시험(`test_unavailable_counts_or_replication_lag_rolls_back`)대로 계획은 ROLLED_BACK `METRICS_UNAVAILABLE`.
- **AC-4 품질**: `uv run ruff check .`·`uv run ruff format --check .`·`uv run mypy app` 통과, `TEST_DATABASE_URL`을 준 전체 `uv run pytest -q -rs` 실패 0·건너뜀 0, `uv run alembic heads` 하나·새 리비전 0, 기존 시험 삭제 0(`git diff --stat`에서 시험 파일은 추가 줄만 또는 가짜 connect 확장), `parametrize`에 `uuid4()`·시각 금지(`ids=` 사용).

## 5. 위임한 세부사항

질의 상수 이름, 가짜 connect를 호스트별 행으로 확장하는 방법, 처리기 시험을 둘 파일, 로그 data 칸 순서, `evidence` 문자열 이름(위 예시 권장).

# API 서버 24 — 이전 비율 6단계 + 단계별 최소 관찰 시간

- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/133 (Project #4)
- 짝 프론트 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-frontend/issues/109
- 기준: echo-was main `69d17109d952573186dd934d31c754d3ce0dda76` (2026-11-07 19:08 KST)
- 상태: Confirmed (지민 결정 A1a, 2026-11-07 19:45 KST)

## 왜

비율 단계가 10 → 50 → 100% 3단계이고, 통과 기준이 "도착 요청 N건"뿐이라 비율이 클수록 더 짧게 지나간다(50 rps에서 10% 약 6초, 50% 약 4초, 100% 약 3초). 시연에서 부자연스럽다는 피드백. 작게 시작해 점점 넓히고, 단계마다 같은 리듬으로 지켜보게 바꾼다. 시연 시간 제약 때문에 이전 전체는 약 25~30초 안에 끝나야 한다.

## 결정 (다시 묻지 말 것)

- D1 단계 구성(모든 계획 종류 공통: 일반 이전·복귀·재난 긴급 이전):
  `0 PREFLIGHT · 1 SHIFT 5 · 2 SHIFT 10 · 3 SHIFT 25 · 4 SHIFT 50 · 5 DATA_SYNC_CHECK · 6 SHIFT 75 · 7 SHIFT 100 · (8 DB_CUTOVER, includeCutover일 때만)`.
- D2 비율 단계 통과 = 기존 "필요 건수 채움(또는 기존 마감 판정 LOW_TRAFFIC 통과)" **그리고** 그 단계 비율 적용 시각부터 최소 관찰 시간 경과. 실패 판정(5xx 초과, TARGET_NOT_RECEIVING, 헬스, 가중치 적용 실패, 판단 불가)은 최소 관찰 시간을 기다리지 않고 지금처럼 즉시 롤백한다.
- D3 최소 관찰 시간 설정 `MIGRATION_MIN_OBSERVE_SECONDS`(config `migration_min_observe_seconds`, 기본 4, 0~60). 계획을 만들 때 각 비율 단계 JSON에 `minObserveSeconds`로 적어 둔다(실행 중 설정이 바뀌어도 그 계획은 같은 값). 단계 응답에도 `minObserveSeconds`(비율 단계만, 나머지 null)를 더한다(추가 칸).
- D4 필요 건수 기본값 `(10, 10, 20, 30, 30, 30)`. `MIGRATION_SHIFT_STEP_COUNT = 6`. 단계 최대 대기 기본 30초 그대로.
- D5 예상 시간: 비율 단계 예상 = 여유(overhead) + max(최소 관찰 시간, min(최대 대기, ceil(N ÷ (rps × 비율/100)))).
- D6 하위 호환(서버 먼저 배포, 옛 프론트가 그대로 요청):
  - 요청 `requiredRequests`는 길이 6(새) 또는 3(옛)을 받는다. 3개 `[a, b, c]`는 `[a, a, b, b, c, c]`로 펼친다. 그 밖의 길이는 지금과 같은 422.
  - 요청에 `requiredRequests` 3개가 왔을 때만 `simulate.failAtStep`을 옛 번호로 보고 새 번호로 바꾼다: 1→2, 2→4, 3→5, 4→7, 5→8. 바꾼 뒤 기존 사유별 허용 단계 검사를 한다. 6개이거나 생략이면 새 번호로 본다.
  - 새 번호의 사유별 허용 단계: ERROR_RATE·HEALTH·TRAFFIC·LOW_TRAFFIC = 모든 비율 단계(1,2,3,4,6,7), TARGET_NOT_RECEIVING = 앞 두 비율 단계(1,2), DATA_LAG = 5, DB_CUTOVER_* · DATA_INTEGRITY = 8.
- D7 이미 저장된 계획(옛 5단계·6단계 + DB 전환)은 **저장된 steps를 정본으로** 읽고 실행·롤백·응답한다. 저장 계획을 상수 번호(`SHIFT_STEPS`, `DATA_SYNC_STEP`, `DB_CUTOVER_STEP`)로 짚는 코드는 저장된 steps의 `kind`로 찾게 바꾼다. 특히:
  - `migration_plan_service.py` 약 346행: 계획 단위 requiredRequests를 `SHIFT_STEPS`로 뽑는 곳.
  - `migration_plan_support.step_required_requests`: 8-1 이전 계획(값 없음)의 기본값을 `SHIFT_STEPS.index(index)`로 찾는 곳 — 옛 계획은 비율 단계 순서(몇 번째 SHIFT인지)로 옛 기본값 `(20, 50, 50)`을 쓴다.
  - 옛 계획에는 `minObserveSeconds`가 없으므로 0(지금 동작)으로 본다.
- D8 DB 검사 범위 `LAST_STEP_INDEX` 5 → 8: `migration_plans.current_step_range`, `migration_plan_logs.step_range` CHECK를 넓히는 Alembic 리비전 1개(drop + create, 넓히기만). `.claude/rules/db-schema.sql` 갱신. 현재 head 위에 잇는다(`alembic heads`로 확인).
- D9 운영 설정 파일(`deploy/*.yaml`)은 바꾸지 않는다(기본값으로 충분).

## 범위 밖

- 진입점(Envoy) 쪽 변경, 단계 안의 연속 증가(1초마다 조금씩), 롤백 방식 변경, 프론트 코드(짝 이슈에서 따로).

## 완료 기준

1. 새 계획(일반·복귀·재난 긴급, includeCutover 유무)이 D1 순서로 만들어지고 응답 steps에 `targetWeight`·`requiredRequests`·`minObserveSeconds`·`expectedSeconds`가 D4·D5대로 나온다.
2. 비율 단계는 건수를 채워도 최소 관찰 시간 전에는 다음 단계로 가지 않는다(가짜 시계 시험). 실패는 최소 관찰 시간 전이라도 즉시 롤백한다.
3. 옛 요청(`requiredRequests` 3개 + 옛 `failAtStep`)이 422 없이 받아지고 D6대로 펼쳐지고 바뀐다. 사유별 잘못된 단계는 지금처럼 422.
4. DB에 직접 넣은 옛 5단계 계획(8-1 이전 값 없는 것 포함)과 옛 6단계(DB 전환 포함) 계획을 읽고, 실행·롤백·응답이 지금과 같다.
5. Alembic upgrade → downgrade -1 → upgrade 왕복, `alembic check`, 스키마 덤프 일치.
6. `uv run ruff check .`, `ruff format --check .`, `mypy app`, 전체 `pytest`(건너뜀 0) 통과. 기존 시험의 단계 번호 기대값은 새 번호로 고치되, 시험이 검사하던 의미는 유지한다.
7. `docs/api/migration-plans.md`의 단계 표·예시·호환 규칙을 갱신한다.

## 위임한 세부사항 (스스로 정하고 보고)

- 최소 관찰 시간 판정 위치(건수 판정 뒤 헬스체크 전/후), 로그 코드를 새로 둘지(예: 관찰 대기), 시험 파일 구성.

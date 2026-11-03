# API 서버 8-2: 고정 진입점 `read_counts` 실제 구현 SPEC

- 상태: Confirmed (11/3 지민 결정 "내가 진행")
- 확인일: 2026-11-03
- Depth: Light (기존 실행기에 메서드 하나)
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/8 (In Progress)
- 앞 SPEC: `specs/api-8-1-request-count-judgement.md` §6.1(`read_counts` 계약). 이 SPEC은 그 계약의 고정 진입점 구현만 다룬다.
- 저장소: `echo-was` · 브랜치 `feat/entrypoint-read-counts` (`main` = `4c0170f`에서) · PR로 올린다(main 병합 = GKE 운영 배포)
- 담당 협의: PR #22 댓글(11/3 15:20 질문, 15:3x "제가 진행" 알림). 진입점 쪽 `GET /counters`는 서동옥 구현·배포 완료(echo-infra `c26addf`).

## 1. 목표

`EntrypointMetricsProvider.read_counts(target)`의 자리값(늘 `CountsUnavailable("ENTRYPOINT_COUNTS_NOT_IMPLEMENTED")`)을 진입점 `GET /counters` 호출로 바꾼다. 그러면 `ENTRYPOINT_EXECUTOR_MODE=http`일 때 8-1 판정이 실제 요청 수로 돈다.

## 2. 확인된 사실

- echo-was `app/executors/entrypoint.py`(main `4c0170f`): `EntrypointClient._request`(토큰을 비밀 조회기에서 읽음, 실패 코드 `ENTRYPOINT_UNAUTHORIZED`·`ENTRYPOINT_TOKEN_MISSING`·`ENTRYPOINT_REJECTED`·`ENTRYPOINT_UNREACHABLE`·`ENTRYPOINT_ERROR`), `environment_name(target)`(환경 id 매핑 → 종류 매핑 → 종류), `get_summary` 형식 검사 패턴, `ENTRYPOINT_ENV_UNMAPPED`.
- 진입점 `GET /counters`(Bearer, echo-infra `entrypoint/weights-api/app.py`): 200 `{"ts": "...", "envs": {"<env>": {"requests": int, "errors_5xx": int, "hosts": int}}}`, Envoy admin 접속 실패면 503. 값은 Envoy 시작 이후 누적이고 호스트 교체·재시작 때 줄 수 있다(재시작 처리는 일꾼 몫, 8-1 §5.3).
- 11/3 15:17 운영 API 파드에서 `http://10.60.0.6:8080/counters` 조회 → 200, `aws`·`gcp` 모두 `requests 0, errors_5xx 0, hosts 0`(업스트림 미연결).

## 3. 범위

포함: `EntrypointClient.get_counters()`, `EntrypointMetricsProvider.read_counts` 실제 구현, 자리값 상수·시험 정리, 시험, `docs/executors.md`·용어 사전·모듈 docstring 갱신.
제외: 운영 설정(`ENTRYPOINT_EXECUTOR_MODE`·주소·토큰 SecretSync), 진입점 업스트림 연결(서동옥), 판정 로직 변경, `read`(p95 참고) 변경.

## 4. 동작

`get_counters()`: `GET /counters` → 응답이 dict이고 `envs`가 dict가 아니면 `EntrypointCallError(ENTRYPOINT_ERROR)`. 호출 실패는 기존 `_request` 코드 그대로.

`read_counts(target)` → `CountsResult`(예외를 밖으로 내지 않는다):
1. `name = environment_name(target)`, `envs = get_counters()["envs"]`. `EntrypointCallError(code)` → `CountsUnavailable(code)`.
2. `envs`에 `name`이 없거나 값이 dict가 아님 → `CountsUnavailable(ENTRYPOINT_ENV_UNMAPPED)`.
3. `requests`·`errors_5xx`가 0 이상 정수가 아님(bool 제외) → `CountsUnavailable(ENTRYPOINT_ERROR)`.
4. `hosts` 키가 있고 0 이상 정수인데 0 → `CountsUnavailable(ENTRYPOINT_NO_HOSTS)`(새 코드). 이유: 그 환경으로 갈 업스트림이 없으면 0건은 "조용한 서비스"가 아니라 "길이 없음"이라, 8-1 판정이 트래픽 적음 통과로 잘못 넘어가지 않게 한다(마감에 `METRICS_UNAVAILABLE` 롤백). `hosts` 키가 없으면 검사하지 않는다(옛 진입점 호환). 정수가 아니면 3번과 같이 `ENTRYPOINT_ERROR`.
5. 그 밖 → `RequestCounts(requests, errors_5xx)`. 값을 보정하지 않는다.
- 실패는 `logger.warning`으로 코드와 환경 이름만 남긴다(토큰·응답 원문 금지). 요청 한 번에 `/counters` 한 번(출발·도착을 각각 부르면 두 번 — 묶음 최적화는 하지 않는다).
- 자리값 상수 `ENTRYPOINT_COUNTS_NOT_IMPLEMENTED`와 그 시험은 지운다(다른 곳에서 쓰지 않음을 확인).

## 5. Acceptance criteria

- `AC-8-2-1` 정상: `httpx.MockTransport`가 `{"envs": {"aws": {"requests": 120, "errors_5xx": 2, "hosts": 1}, "gcp": {...}}}`를 주면 aws 연결 `read_counts` = `RequestCounts(120, 2)`, 요청에 `Authorization: Bearer <토큰>`·경로 `/counters`.
- `AC-8-2-2` 매핑: `entrypoint_environment_map`으로 환경 id를 다른 이름에 붙이면 그 이름 항목을 읽는다. 없는 이름 → `ENTRYPOINT_ENV_UNMAPPED`.
- `AC-8-2-3` 실패 코드: 401 → `ENTRYPOINT_UNAUTHORIZED`, 503 → 기존 매핑 그대로(`ENTRYPOINT_TOKEN_MISSING`. `/counters`의 503은 "진입점 토큰 미설정" 또는 "Envoy admin 접속 실패" 두 경우라 문서 실패 코드 표에 둘 다 적는다. `_request` 공통 매핑은 바꾸지 않는다), 연결 실패·시간 초과 → `ENTRYPOINT_UNREACHABLE`, 비밀 조회 실패 → `ENTRYPOINT_TOKEN_MISSING`, `envs` 없음·리스트·음수·문자열·bool 값 → `ENTRYPOINT_ERROR`. 모두 예외 없이 `CountsUnavailable`.
- `AC-8-2-4` hosts: `hosts: 0` → `ENTRYPOINT_NO_HOSTS`, `hosts` 키 없음 → 값 그대로 `RequestCounts`.
- `AC-8-2-5` 계약: `tests/executor_contract.py`의 카운터 계약(`assert_counts_contract`)을 실제 구현(MockTransport, 호출마다 누적값 증가)에 돌려 통과.
- `AC-8-2-6` 판정 연결: http 모드 실행기 묶음 + MockTransport 카운터로 이전 계획 한 단계가 건수 기준으로 통과(통합 시험 하나, 8-1 처리기 그대로).
- `AC-8-2-7` 문서: `docs/executors.md` 고정 진입점 절의 자리값 문구를 실제 동작·실패 코드 표(`ENTRYPOINT_NO_HOSTS` 포함)로 바꾸고, `GET /counters` 응답 예와 "hosts 0이면 판단 불가" 이유를 적는다. 용어 사전·모듈 docstring도 맞춘다.
- 품질: ruff·format·mypy app·pytest 전부 통과(건너뜀 0).

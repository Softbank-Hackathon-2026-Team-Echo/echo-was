# 보안·AI 기반 스키마: 기한부 예외 + AI 실행 표 SPEC

- 상태: Draft (2026-11-04, 김형래)
- 작성일: 2026-11-04
- Depth: Standard (새 표 5개 + Alembic 리비전 1개, 기존 표 변경 없음)
- GitHub 이슈: echo-was #27(기한부 예외)·#29(게이트 입력) · echo-ai-agent #2(실행·로그)·#4(사전 점검)·#6(패치 승인)
- 근거: echo-ai-agent `specs/ai-1-appendix-a-was-runner-contract.md`(WAS ↔ 러너 계약 **v1, 11/4 승인**) §2·§3·§4 / 보안·거버넌스 1 SPEC §5.1 C-AI-1·C-DB-1·C-DST-1(P0는 기한부 예외) / 규칙표 `SecurityException`
- 저장소: `echo-was` · 브랜치 `feat/sec-ai-base-schema` (`main` = `a936eff`에서) · PR로 올린다

## 1. 목표

이전 게이트(#29)가 막았다가 통과하는 장면과, AI 러너 결과를 WAS에 남기는 데 필요한 표를 한 번에 만든다. 기존 표·데이터는 바꾸지 않는다.

## 2. 확인된 사실 (11/4)

- 게이트를 켜면 실행 시점 필수 통제 중 C-DB-1·C-DST-1은 P0에서 확인 수단이 없어 **기한부 예외**로만 통과한다. C-AI-1은 **최신 사전 점검 기록**이 없으면 FAIL이다.
- 규칙표의 예외 판정(`_is_valid_exception`)은 요청·승인이 사람(USER)이고, 승인자가 있고, 7일 이하이고, 범위가 맞을 때만 받아 준다.
- 계약 v1의 실행 칸(§2.3)·패치 승인(§4)·러너 토큰(§3.2, 해시만 저장)을 따른다.
- 원격 브랜치 중 다른 Alembic 리비전을 더하는 곳은 없다(11/4 20:40 확인). head는 `eee7a6c8229f`.

## 3. 범위

### 3.1 이번에 한다
- 모델 5개: `security_exceptions`, `agent_runners`, `agent_runs`, `agent_run_events`, `agent_patch_approvals`
- enum: `AgentRunKind`·`AgentRunStatus`·`AgentRunStep`·`AgentPatchDecision`
- 리비전 `201cf5419f4d`(표 추가만), `db-schema.sql`, 스키마 변경 로그, `docs/schema.md`(표 설명 + ④-6 상태 흐름), 용어 사전(§2 다섯 줄, §3 AI 실행 값)
- 마이그레이션 시험: 기대 제약·인덱스, head, 표 목록, 리비전 왕복

### 3.2 다음 PR
- 게이트 #29(승인·`_start`에서 판정, 409 `SECURITY_GATE_BLOCKED`, 실행 직전 TLS 시험) — 이 표의 예외·사전 점검을 읽는다
- 러너 API(가져가기·하트비트·이벤트·결과, 검사기 `require_runner`)와 사람 쪽 사전 점검·패치 승인 API, 예외 요청·승인 API
- 러너 토큰 발급 명령(`python -m app.scripts.issue_runner_token`)

### 3.3 하지 않는다
- **감사 기록의 SYSTEM 행위자.** `audit_logs`(API 서버 6·7) 표를 바꿔야 해서 이 PR에 넣지 않는다. 러너 행동의 감사는 감사 확대(#32·API 서버 7)에서 계약 v1 결정(SYSTEM + `component=ai-runner`)대로 더한다

## 4. 결정

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | **새 표만 만든다.** 기존 표·CHECK·인덱스는 그대로다(병합 = 운영 마이그레이션이지만 기존 데이터 영향 없음) | 운영 안전 |
| D-2 | `security_exceptions`: 범위 칸은 `findings`와 같다. 요청·승인·철회는 `users` FK(사람만). DB가 막는 것: 사유 공백, 승인 칸 짝, 철회 칸 짝, `expires_at > created_at`, **7일 이하**, digest 형식 | 규칙표·보안 1 |
| D-3 | `agent_runners`: 조직에 묶이지 않는 플랫폼 신원. `token_hash`(16진 64자) unique, 이름 RFC 1123 | 계약 §3.2 |
| D-4 | `agent_runs`: 계약 §2.3 칸 + `parent_run_id`(APPLY_PATCH → 사전 점검)·`claimed_at`. DB가 막는 것: 종류·상태 값, `CLAIMED`·`RUNNING`이면 러너·토큰 해시·임대, 끝 상태 ⇔ `finished_at`, 실패 코드는 실패일 때만, 결과는 `SUCCEEDED`·`FAILED`일 때만, PRECHECK은 도착 환경, APPLY_PATCH은 부모 실행, 마감 > 만든 시각, 입력 해시·토큰 해시 형식 | 계약 §2 |
| D-5 | 서비스당 열린 실행 하나(`uq_agent_runs_service_id_open`, `QUEUED`·`CLAIMED`·`RUNNING`). 실행 토큰 해시 unique. 가져가기용 `QUEUED` 부분 인덱스 | 계약 §2.2·§3.2 |
| D-6 | `agent_run_events`: `(run_id, seq)` unique(같은 seq 무시), 단계·수준 값 CHECK, message 200자 | 계약 §3.2 |
| D-7 | `agent_patch_approvals`: `(run_id, patch_id)` unique(패치마다 한 번), 해시 형식, 승인했을 때만 `apply_run_id` | 계약 §4 |
| AD-1 | 지우는 경로가 없어 FK는 모두 `ON DELETE RESTRICT`. 이력 표 삭제 금지 트리거는 이번에 넣지 않는다(쓰는 API가 생길 때) | AI-default |

## 5. Acceptance criteria

| AC | 확인 | 시험 |
|---|---|---|
| AC-1 | 리비전이 표 5개를 만들고 되돌리면 지운다. head 하나 | `test_agent_runs_revision_downgrade_removes_tables_and_upgrade_restores`, `test_migration_round_trip_upgrade_downgrade_upgrade` |
| AC-2 | 제약·인덱스가 기대 목록과 같고 모델로 만든 스키마와 같다 | `test_migrated_schema_matches_expected_catalog`, `test_model_metadata_matches_migrated_schema`, `test_alembic_check_reports_no_differences` |
| AC-3 | 로컬 `upgrade → downgrade -1 → upgrade → alembic check` | 수동(PR에 결과) |

## 6. 리스크

- **R-a 병합 = 운영 마이그레이션 Job** → 표 추가만이라 기존 데이터 영향은 없다. 병합 시간은 진행 중인 배포·이전이 없을 때로 맞춘다.
- **R-b 그사이 다른 리비전이 main에 들어오면** head가 갈라진다 → 병합 직전에 다시 확인하고, 갈라지면 이 리비전의 `down_revision`을 고친다(CI의 마이그레이션 시험이 먼저 잡는다).
- **R-c 사용하는 코드가 아직 없다** → 다음 PR(게이트·러너 API)이 이 표를 쓴다. 이름·칸이 리뷰에서 바뀌면 계약 문서도 같이 고친다.

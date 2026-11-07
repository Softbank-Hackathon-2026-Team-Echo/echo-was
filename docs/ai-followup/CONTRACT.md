# AI 반영 후속 읽기 계약 v1 — 검토 초안

상태: 작업② 구현 계약. 형래와의 합의/승인을 주장하지 않는다. 도영의 소비 검토와 형래의 WAS/AI 계약 검토 뒤 병합을 판단한다. 현재 운영 지원은 기존 ID·거절 코드뿐이며 아래 followUp은 이 Draft PR 후보의 지원이다. ③ 자동복구·스케줄러는 예정/미구현이다.

## 불변식과 공개 위치

`GET /api/v1/orgs/{orgId}/prechecks/{id}`의 `patches[].apply.followUp` 및 APPLY_PATCH 상세 `apply.followUp`에 같은 객체를 더한다. 승인 응답의 apply에도 같은 투영을 쓴다. 기존 칸 삭제/뜻 변경 없음. `status=SUCCEEDED`·`finishedAt`은 샌드박스 결과 수락/종료이지 저장소·배포 성공이 아니다. 기존 저장소 실패의 run FAILED 전환도 유지한다. commitState DONE은 저장소 처리 완료일 뿐이다. CREATED 뒤 실제 빌드/배포/검증/성공·실패는 기존 deploy GET과 로그만 정본이다. 주 환경 성공과 예비 동기화 완료도 별개다.

객체: `{schemaVersion:1, applicable:bool|null, repository:{state,reasonCode,startedAt,finishedAt}, redeploy:{state,reasonCode,finishedAt}, updatedAt, deadlineAt:null}`. 연결 ID·SHA·PR은 기존 칸을 재사용하고 중복 칸을 만들지 않는다. 단독 apply 상세에는 기존에 없던 연결 칸을 additive로 더한다. reasonCode는 아래 고정 코드 또는 기존 REPO_*/접수 오류 코드이며 외부 오류 본문·토큰·stack·result 전체를 노출하지 않는다. retryable/자동재시도 지원을 암시하는 칸은 만들지 않는다.

## 대상 판정과 상태 정의

부모 상세에서는 이미 읽은 부모 kind가 우선이다. 단독 실행은 고정 input_snapshot.diagnosis를 쓴다. 정상 v1 입력 사본에 diagnosis가 없으면 일반 PRECHECK 반영이고 미대상이다. 과거 빈/불명 입력은 applicable=null, UNKNOWN이며 무조건 미대상/성공으로 만들지 않는다. 일반 PRECHECK·미승인 제안에는 후속 배포를 만들지 않는다(apply 자체가 없으면 기존대로 null).

| 단계/상태 | 뜻 / reasonCode |
|---|---|
| repository NOT_REQUIRED | 일반 PRECHECK `NOT_DIAGNOSIS`; 샌드박스 실패/취소/만료 `APPLY_NOT_SUCCEEDED`; 모의 결과 `SIMULATED`; callback이 실제로 저장한 스위치 OFF `COMMIT_DISABLED` |
| repository PENDING | 대상의 샌드박스 미완료 `WAITING_FOR_APPLY` 또는 성공 후 callback 관측 전 `AWAITING_CALLBACK`. worker 예약/살아 있음 보장 아님 |
| repository IN_PROGRESS | 영속 commitState IN_PROGRESS + 유효 claim 시각이 TTL 안. `CLAIMED`; 실제 요청 생존 보장 아님 |
| repository SUCCEEDED | repoCommitted=true와 실제 저장 SHA가 함께 있음. `COMMITTED`; DONE만으로 판정하지 않음 |
| repository FAILED | 기존 확정 REPO_* 오류. 코드 유지; run FAILED/finishedAt 보존 |
| repository UNKNOWN | legacy/null/모순 기록 `STATE_UNAVAILABLE`; 잘못된/오래된 claim `CLAIM_UNCONFIRMED`; release 뒤 남은 claim `CALLBACK_INTERRUPTED`; REPO_WRITE_FAILED `REPO_WRITE_FAILED`는 원격 성공 응답 손실 가능성을 포함하므로 확인 필요 |
| redeploy NOT_REQUIRED | 일반 PRECHECK `NOT_DIAGNOSIS`; sandbox 실패 `APPLY_NOT_SUCCEEDED`; 저장소 실패/불명 원격결과 `REPOSITORY_NOT_CONFIRMED` — 새 배포 생성 근거 없음 |
| redeploy PENDING | 대상이며 아직 연결·거절 없음. `WAITING_FOR_APPLY` 또는 `AWAITING_LINK`. 예약/자동복구 보장 아님 |
| redeploy CREATED | 유효 UUID redeployRequestId 있음. `REQUEST_CREATED`; 실제 배포 성공이 아님 |
| redeploy REJECTED | 기존 거절 코드가 있음(INTERNAL_ERROR 포함). 자동 재시도 없음, ID null 키 멱등 종료 유지 |
| redeploy UNKNOWN | null ID 키만 있고 거절도 없거나, 모순/불명 입력. `STATE_UNAVAILABLE` |

simulated=true는 값을 지어냈다는 기존 뜻이다. 저장소 반영만 NOT_REQUIRED이고 재배포는 여전히 원래 실패 SHA로 CREATED/REJECTED가 될 수 있다. OFF도 동일하다. GET 시 현재 설정을 읽어 과거 기록을 재해석하지 않는다. callback 전 OFF 여부는 모르므로 PENDING이지 SKIPPED가 아니다.

판정 우선순위: 대상/legacy → 저장소 영속 성공·실패 → sandbox 종료/진행 → 모의·영속 OFF → claim → 미관측. 재배포는 미대상 → 유효 ID/거절(둘 다면 UNKNOWN) → sandbox·저장소 실패 → null-key 불명 → 대기. 읽기 실패/취소가 이미 생성된 ID를 지우지 않는다.

## 시각·기한·이벤트 순서

repository.startedAt은 기존 commitClaimedAt(현재 claim 시작); finishedAt은 새 `repositoryFinishedAt`(확정 성공/실패 또는 명시적 skip 관측). redeploy.finishedAt은 새 `redeployFinishedAt`(접수/거절 확정). updatedAt은 이 후속 전용 영속 시각들의 최댓값이며 없으면 null. 샌드박스 finishedAt/updatedAt/lease/deadline을 대신 쓰지 않는다. 오래된 데이터의 완료시각은 null이다. release·다시 claim의 시각을 단조 revision으로 쓰지 않는다.

실효 후속 SLA/총 예산은 현재 없다. 그러므로 deadlineAt은 항상 null이다. 기존 10분 COMMIT_CLAIM_TTL은 claim 중복 진입 억제일 뿐 SLA·재시도 시각·사용자 실패 판정이 아니다. TTL이 지나면 GET 투영만 UNKNOWN으로 바뀌며 DB 쓰기/재개 호출은 없다.

기존 순서 보존: 사람 승인·지문 검사 → 러너 결과 검사/DB 커밋(+기존 종료 감사) → 짧은 claim 부분 갱신 커밋 → 잠금 밖 GitHub → 행 잠금 아래 repo 필드/완료시각 부분 갱신 → savepoint 접수 → ID/거절/접수시각 부분 갱신 → 동일 트랜잭션 커밋 → 기존 job worker. 신규 이벤트/감사 타입 없음. GET은 관측만 하며 복구/승인/배포를 호출하지 않는다.

## 권한·조직·조회 오류·구버전

기존 로그인 → 조직 멤버 → 최소 역할 → Origin 검사 순서와 401/403/404, 다른 조직 리소스 숨김, 에이전트 GET 감사 순서 그대로다. 조회와 로그는 뷰어/조직 에이전트 읽기, 승인·배포는 개발자 이상 사람만. 새 권한/route/토큰 없음. 401/403/404면 자동 폴링 중단하고 권한/없음을 표시한다. 네트워크/429/5xx는 마지막 확인값과 조회 오류를 분리하며 서버 FAILED로 바꾸지 않는다.

followUp 없거나 null인 구서버/구클라이언트는 기존 ID/거절/apply 실패를 먼저 쓴다. 실제 CHANGE_PROPOSAL 승인 apply에만 유한 GET을 지속하고, finishedAt만으로 종료하지 않는다. 클라이언트 대기 예산 종료는 '결과 확인 지연'이지 서버 실패가 아니다. 조회 재시도가 승인/재배포 POST를 반복하면 안 된다. 늦은 응답은 org/route/parent/patch/apply 연결 단위로 막고, 같은 대상의 GET을 직렬화/이전 요청 취소한다. schemaVersion은 계약 버전이지 상태 revision이 아니다. 별도 revision/열/마이그레이션을 강제로 더하지 않는다: 기존 JSONB 부분 갱신과 ID의 영속성으로 이 범위를 만족하며, monotonic ordering 보장은 이번 계약에 없다.

## 불명 저장값의 읽기 경계 (I-1/I-2 보완)

- `redeployRequestId`는 저장된 표준 `8-4-4-4-12` 16진수 UUID 문자열만 공유 순수 파서로 읽는다. 유효 ID만 CREATED이며 불법/빈/다른 형식·타입은 공개 연결 칸 null, 후속 UNKNOWN이다. 공백 제거·하이픈 보정·ID 발명·DB 원본 수정은 하지 않는다. 유효 ID와 거절 증거가 함께 있으면 연결 칸은 보존하되 UNKNOWN이다.
- 공개 `redeployRejectedCode`와 후속 reason은 기존 접수 writer가 사용하는 `app.core.exceptions.AppError`의 정의된 고정 코드를 공유한다(INTERNAL_ERROR 포함). 기존 유효 코드의 REJECTED 뜻은 그대로다. 불명/개행/NUL/원시 본문·잘못된 타입은 공개 코드 UNKNOWN, 후속 UNKNOWN/STATE_UNAVAILABLE이며 원문은 복사하지 않는다. 거절 없음은 기존대로 null이다.
- 시각은 ISO 파싱뿐 아니라 UTC 정규화의 표현 범위까지 확인한다. 불법·naive·UTC 범위 초과는 해당 시각 null이며 다른 유효 시각을 덮지 않는다. claim 시작/관측 시각·TTL 만료 연산을 안전하게 판정할 수 없거나 claim이 미래/기한 밖이면 UNKNOWN/CLAIM_UNCONFIRMED이다. 표현 가능한 미래 원시 시각은 관측값으로 유지하며 실제 현재 상태/단조 revision을 보장하지 않는다. 클램프·가짜 시각·임의 deadline은 만들지 않는다.
- 이 방어는 검수의 synthetic legacy/오염 저장값에서 확인한 기존 GET500 조회 신뢰성 결함 보완이다. 정상 writer 생성이나 공개 runner 공격 가능성을 확인했다는 주장이 아니다. GET은 result·updatedAt·원래 종료 상태·Job·이벤트/사람 조회 감사 기록을 바꾸지 않는다. 승인 응답에도 같은 안전 직렬화를 쓴다. 승인 동작의 기존 권한·검사·callback을 바꾸거나 GET에서 호출하지 않는다.

## 기존 worker·③ 인계

`DiagnosisService.redeploy_after_apply(apply_run_id: UUID) -> AgentRun | None` 인터페이스 유지. 열린 transaction 없이 호출. `ApplyCommitService.prepare_redeploy_commit`은 잠금 밖 GitHub/고정 branch-parent-tree 검증과 기존 claim을 그대로 쓴다. terminal 결과 재제출은 409이며 재개 인터페이스가 아니다. INTERNAL_ERROR/null ID 키는 자동 재개되지 않는다.

기존 `app/worker/runner.py`·JobRepository·DeployRequestProcessor는 생성된 배포의 lease/저장단계 재개를 담당한다. ③은 새 outbox/배포 worker를 복제하지 말고 미연결 성공 apply의 bounded 재검색/권한·승인 재검사/기한·fencing/원격 읽기 reconciliation을 먼저 설계한다. REPO_WRITE_FAILED·stale claim은 맹목 재송신 금지. 이 PR은 자동복구 worker·활성 설정·새 유료자원·실제 GitHub App 쓰기·운영 배포 0이다. ③은 수정 PR 정확 최종 head의 독립 재검수 PASS와 부모의 명시적 base 인수·소유권 이전 뒤에만 코드 착수한다.

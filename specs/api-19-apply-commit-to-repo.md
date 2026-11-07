# SPEC — 승인된 진단 패치를 서비스 저장소에 커밋·PR 하고 그 커밋으로 재배포 (echo-was, API 서버 19)

- 상태: Draft (지민 결정 11/7 02:5x 「진단 두뇌, #98, 고친 코드 저장소에 넣기 내가 작업」)
- 기준: PR #101 브랜치 `feat/api-98-failed-deploy-diagnosis`(SPEC [api-98](api-98-failed-deploy-diagnosis.md) 7절 「재배포는 원래 실패 커밋」 한계를 푼다). 기준 head 는 PR 본문에 적는다.
- 짝: echo-ai-agent PR(진단 두뇌 간단판 + 서비스 저장소 APPLY_PATCH, `specs/ai-5-diagnosis-simple.md`)
- 마이그레이션 **0개** (반영 실행 `result` JSON 에 칸을 덧붙이고, 기존 `status`·`failure_code` 칸만 쓴다)
- API 문서: [docs/api/deploy-diagnosis.md](../docs/api/deploy-diagnosis.md) 「저장소 반영」

## 1. 목표

시나리오 3 의 마지막 칸 — 승인된 수정이 **고객 저장소에 커밋으로 남고, 재배포가 그 커밋을 빌드**한다. 러너(샌드박스 VM)는 읽기 자격뿐(push 없음)이므로 저장소 쓰기는 WAS 가 GitHub App 으로 한다.

## 2. 결정

| ID | 결정 |
|---|---|
| D-1 | 스위치 `AI_APPLY_COMMIT_ENABLED`(기본 false). 꺼지면 #101 동작 그대로. 병합 = 동작 불변 |
| D-2 | 시점: 러너 반영 결과(`ApplyPatchResult` SUCCEEDED)를 받은 요청 안, `redeploy_after_apply` 의 **행 잠금 트랜잭션 전**(GitHub 호출을 잠금 안에서 하지 않는다). 재배포 생성 지점 변경은 커밋 참조(`commit_sha`·`ref_value`) 교체뿐(#101 의 잠금·멱등 구조는 그대로) |
| D-3 | 로직은 새 모듈 `app/services/apply_commit_service.py`(+ `app/clients/repo_commit_writer.py`, `app/services/patch_text.py`) |
| D-4 | 자격: GitHub App `echoport-deploy` 설치 토큰, `repositories=[그 저장소]`, `permissions={contents: write, pull_requests: write}`. 개인키는 기존 `GITHUB_APP_PRIVATE_KEY_REF` |
| D-5 | 파일 내용은 실패 커밋에서 읽어 승인된 diff 를 러너와 같은 엄격 규칙으로 적용(문맥 불일치면 `REPO_PATCH_CONFLICT`). patchHash 를 다시 계산해 입력 사본과 대조 |
| D-6 | 브랜치 `echoport/fix-<반영 실행 id 앞 8자>`, 커밋 1개(부모 = 실패 커밋, 작성자 `EchoPort bot <bot@echoport.uk>`, 파일 모드는 실패 커밋의 모드를 보존 — 100644·100755 만 고칠 수 있다). 메시지: 제목 `fix: EchoPort AI 진단 수정 반영 — <원인 72자>` + 진단(분류)·승인 `@login`·진단/반영 실행 id·실패 배포(커밋)·패치 지문. 모든 문자열 `mask_secrets` |
| D-7 | PR: base = 실패 배포가 브랜치 ref 면 그 브랜치, 아니면 기본 브랜치. WAS 는 병합하지 않는다 |
| D-8 | 성공: 반영 실행 result 의 `appliedCommitSha`·`branch` 를 저장소 값으로, `pullRequestUrl` 추가, 러너 값은 `sandboxCommitSha`·`sandboxBranch`, `repoCommitted: true`. 재배포 ref = 그 커밋 |
| D-9 | 실패: 반영 실행을 `SUCCEEDED → FAILED`, `failure_code = REPO_…`, `appliedCommitSha: null`. 재배포를 만들지 않는다(패치 상태 `APPLY_FAILED`). 응답은 성공(러너 결과 요청 200) |
| D-10 | 가짜 반영(simulate)은 스위치가 켜져 있어도 GitHub 를 부르지 않는다(가짜 sha·`pullRequestUrl: null`, 재배포 = 원래 커밋 — 지어낸 sha 는 빌드할 수 없다) |
| D-11 | 멱등·동시성(검증 반영): 두 단계. ① 짧은 트랜잭션에서 반영 실행 행 `FOR UPDATE` → 재배포가 이미 연결됐거나 `repoCommitted`(`commitState=DONE`)면 기록된 값을 쓰고, 다른 요청이 쓰는 중(`commitState=IN_PROGRESS`, `commitClaimedAt` 부터 10분 안)이면 그 요청은 재배포 없이 바로 돌아간다(쓰는 요청이 재배포까지 잇는다). 아니면 `IN_PROGRESS` 를 부분 갱신으로 잡고 커밋(잠금 해제). ② **잠금 없이** GitHub 쓰기. ③ #101 재배포 잠금 트랜잭션 안에서 저장소 칸과 재배포 칸을 JSONB `||` **부분 갱신**(이미 기록된 `redeployRequestId` 를 지우지 않는다). 그래서 러너 결과와 재승인이 겹쳐도 GitHub 커밋·PR 생성 1회, 재배포 1개. 잡은 요청이 예외로 끝나면 표시를 풀고, 프로세스가 죽으면 10분 뒤 다른 요청이 다시 잡는다(D-14 로 이어 쓰기 안전) |
| D-12 | 스위치가 켜졌는데 App 이 설정되지 않았으면(source_client_mode ≠ github 등) `REPO_WRITE_UNAVAILABLE` 로 실패시킨다(조용히 원래 커밋을 다시 배포하지 않는다) |
| D-13 | 경로 검사(검증 반영): GitHub 를 부르기 전에 diff 머리 경로를 검사한다 — 정규화된 상대 경로만(`..`·`.` 조각, 절대 경로, 빈 조각, NUL·제어 문자, 백슬래시, 따옴표 거절), 맨 앞 `.github/`·어느 깊이든 `.git/` 거절, `old mode`/`new mode`·이름 바꾸기·복사 머리 거절, `--- a/X` 와 `+++ b/X` 가 같아야 함, **승인 화면의 `files` 집합 = diff 경로 집합**. 그다음 실패 커밋 tree(`git/trees?recursive=1`)에서 대상 모드를 읽어 100644·100755 가 아니면(심볼릭 링크 120000, 서브모듈 160000, 디렉터리) 거절. 모두 `REPO_PATH_REJECTED`, 재배포 0 |
| D-14 | 기존 브랜치(검증 반영): `echoport/fix-<run8>` 가 이미 있으면 그 head 커밋의 부모가 [실패 커밋] 이고 tree 가 이번에 만든 tree(같은 내용)일 때만 재사용한다. 아니면 `REPO_BRANCH_CONFLICT` 로 실패(남이 만든 브랜치를 배포하지 않는다). ref 만들기 422(경합) 뒤에도 같은 검사 |
| D-15 | GitHub 쓰기 성공 뒤 재배포 접수가 거절되면(예: `SERVICE_DELETING`·`MIGRATION_IN_PROGRESS`) 반영 실행은 `SUCCEEDED` 그대로, **브랜치·PR 은 저장소에 남고**(되돌리지 않는다) `appliedCommitSha`·`pullRequestUrl` 이 채워진 채 `redeployRequestId: null`·`redeployRejectedCode: <코드>` 를 기록한다. 사람이 그 커밋으로 다시 배포하거나 PR·브랜치를 정리한다 |

## 3. 실패 코드

`REPO_WRITE_FORBIDDEN`(토큰 요청 403·422 = App 권한 없음 또는 설치 대상 아님, 쓰기 호출 403·404) · `REPO_APP_NOT_INSTALLED` · `REPO_FILE_NOT_FOUND` · `REPO_FILE_NOT_TEXT` · `REPO_PATCH_CONFLICT` · `REPO_WRITE_FAILED`(5xx·전송 오류) · `REPO_WRITE_UNAVAILABLE`(App 미설정·개인키 없음) · `REPO_PATH_REJECTED`(D-13) · `REPO_BRANCH_CONFLICT`(D-14)

## 4. GitHub App 권한

**현재 (2026-11-07 09:04 KST 확인, 같은 날 11:13 KST 재확인 — 읽기 API만, 권한 변경 없음):** `GET /apps/echoport-deploy`(App 정의)와 `GET /orgs/Softbank-Hackathon-2026-Team-Echo/installations`(조직 설치 `177714197`)가 둘 다 `actions: write`, `contents: write`, `members: read`, `metadata: read`, `pull_requests: write`, 설치 `repository_selection: selected` 를 돌려준다. 이 SPEC 이 요구하는 contents·pull_requests 쓰기 권한(D-4)은 App 정의와 조직 설치 모두에 있다.

- 권한이 바뀐 시각은 App `updated_at` 2026-11-07 08:58 KST, 설치 `updated_at` 09:01 KST 로 미루어 본 것이다. 누가 바꿨는지는 확인하지 않았다.
- 권한이 있다는 것이 실제 쓰기 성공을 뜻하지는 않는다. 아직 실제 GitHub 쓰기 호출은 한 번도 하지 않았다(6절).

**이력 (2026-11-07 03:39 KST, 이 SPEC 을 처음 쓸 때의 기록 — 지금 상태가 아님):** 당시 읽기 결과는 `actions: write`, `contents: read`, `members: read`, `metadata: read` 였고, 「contents·pull_requests 쓰기가 없어 지금 켜면 `REPO_WRITE_FORBIDDEN`」으로 적었다. 위 「현재」로 대체한다.

**스위치를 켜기 전 확인할 것** (이 문서 PR 은 권한·스위치를 바꾸지 않는다):

1. 켜기 직전에 위 두 API 를 다시 읽어 contents·pull_requests 가 그대로 `write` 인지 본다. `read` 로 돌아가 있으면 먼저 조직 Settings → Developer settings → GitHub Apps → `echoport-deploy` → Permissions & events 에서 **Contents·Pull requests = Read and write** 로 바꾸고, 설치 화면에서 새 권한 요청을 승인한다.
2. 설치 Repository access 에 서비스 저장소(시연: `echo-demo-app`)가 들어 있는지 본다. 설치 저장소 목록 API 는 설치 토큰이 필요해 사람 계정으로는 404 라 **아직 확인하지 못했다**. 빠져 있으면 `REPO_WRITE_FORBIDDEN`(설치 대상 아님) 또는 `REPO_APP_NOT_INSTALLED` 가 난다.
3. 그 뒤 `deploy/base.yaml` 에 `AI_APPLY_COMMIT_ENABLED: "true"` 를 넣는 별도 PR 을 병합한다(지금 `deploy/base.yaml` 에는 이 줄이 없어 꺼져 있다).

## 5. 시험

- 단위 `tests/test_apply_commit.py` 16개: 브랜치 이름, 커밋 메시지(진단·승인자·실행 id), 비밀값 가림·제목 길이, diff 적용 성공·거절 5종, GitHub 쓰기(MockTransport: 토큰 권한 본문·base_tree·parents·author·ref·PR base, 기존 브랜치·PR 재사용, 403·422·설치 없음·파일 없음 → 코드, 개인키 없음, 토큰 로그 미노출)
- 통합 `tests/integration/test_apply_commit_to_repo.py` 6개(가짜 GitHub 쓰기 클라이언트): 성공 → pullRequestUrl·appliedCommitSha·branch·재배포 ref / 쓰기 거절 → FAILED·코드·재배포 없음 / 저장소 파일 불일치 → REPO_PATCH_CONFLICT / App 미설정 → REPO_WRITE_UNAVAILABLE / 스위치 꺼짐 → #101 그대로 / 가짜 진단 → GitHub 안 부름

- 검증 반영(D-11·D-13·D-14): 단위 — 경로 거절 13종(절대·`..`·`./`·`.github`·`.GitHub`·`.git`·하위 `.git`·빈 조각 2·백슬래시·NUL·따옴표)·빈 경로, files 불일치·모드 변경·이름 바꾸기·추가·diff 없음 7종, 정상 1, tree 모드 읽기·없음·잘림, 실행 비트 보존, 심볼릭 링크 모드 거절, 관계없는 기존 브랜치 3종 → `REPO_BRANCH_CONFLICT`, 쓰기 표시 TTL. 통합 — 위험 경로 6종 → `REPO_PATH_REJECTED`·재배포 0·GitHub 호출 0, files 불일치, 심볼릭 링크, 실행 비트 보존, **러너 결과 + 재승인 동시 → 커밋 요청 1·재배포 1**

## 6. 미검증

실제 GitHub 쓰기(운영·로컬 모두 실제 호출 안 함 — 2026-11-07 03:39 KST 작성 때는 App 권한이 없어서였고, 같은 날 09:01 KST 무렵 권한은 쓰기로 바뀌었지만 실제 호출은 아직 없다), 설치 대상 저장소 목록(4절 2번), 실제 러너와의 왕복, 재배포가 고친 커밋으로 빌드되는 실제 배포.

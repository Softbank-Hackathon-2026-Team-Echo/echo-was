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
| D-2 | 시점: 러너 반영 결과(`ApplyPatchResult` SUCCEEDED)를 받은 요청 안, `redeploy_after_apply` 의 재배포 생성 직전. 재배포 생성 지점 변경은 **커밋 참조(ref_value) 교체 한 곳**뿐(#101 수정과 충돌 최소화) |
| D-3 | 로직은 새 모듈 `app/services/apply_commit_service.py`(+ `app/clients/repo_commit_writer.py`, `app/services/patch_text.py`) |
| D-4 | 자격: GitHub App `echoport-deploy` 설치 토큰, `repositories=[그 저장소]`, `permissions={contents: write, pull_requests: write}`. 개인키는 기존 `GITHUB_APP_PRIVATE_KEY_REF` |
| D-5 | 파일 내용은 실패 커밋에서 읽어 승인된 diff 를 러너와 같은 엄격 규칙으로 적용(문맥 불일치면 `REPO_PATCH_CONFLICT`). patchHash 를 다시 계산해 입력 사본과 대조 |
| D-6 | 브랜치 `echoport/fix-<반영 실행 id 앞 8자>`, 커밋 1개(부모 = 실패 커밋, 작성자 `EchoPort bot <bot@echoport.uk>`, 파일 모드 100644). 메시지: 제목 `fix: EchoPort AI 진단 수정 반영 — <원인 72자>` + 진단(분류)·승인 `@login`·진단/반영 실행 id·실패 배포(커밋)·패치 지문. 모든 문자열 `mask_secrets` |
| D-7 | PR: base = 실패 배포가 브랜치 ref 면 그 브랜치, 아니면 기본 브랜치. WAS 는 병합하지 않는다 |
| D-8 | 성공: 반영 실행 result 의 `appliedCommitSha`·`branch` 를 저장소 값으로, `pullRequestUrl` 추가, 러너 값은 `sandboxCommitSha`·`sandboxBranch`, `repoCommitted: true`. 재배포 ref = 그 커밋 |
| D-9 | 실패: 반영 실행을 `SUCCEEDED → FAILED`, `failure_code = REPO_…`, `appliedCommitSha: null`. 재배포를 만들지 않는다(패치 상태 `APPLY_FAILED`). 응답은 성공(러너 결과 요청 200) |
| D-10 | 가짜 반영(simulate)은 스위치가 켜져 있어도 GitHub 를 부르지 않는다(가짜 sha·`pullRequestUrl: null`, 재배포 = 원래 커밋 — 지어낸 sha 는 빌드할 수 없다) |
| D-11 | 멱등: `repoCommitted` 가 있으면 GitHub 를 다시 부르지 않는다. 브랜치·PR 이 이미 있으면(422) 그것을 쓴다 |
| D-12 | 스위치가 켜졌는데 App 이 설정되지 않았으면(source_client_mode ≠ github 등) `REPO_WRITE_UNAVAILABLE` 로 실패시킨다(조용히 원래 커밋을 다시 배포하지 않는다) |

## 3. 실패 코드

`REPO_WRITE_FORBIDDEN`(토큰 요청 403·422 = App 권한 없음 또는 설치 대상 아님, 쓰기 호출 403·404) · `REPO_APP_NOT_INSTALLED` · `REPO_FILE_NOT_FOUND` · `REPO_FILE_NOT_TEXT` · `REPO_PATCH_CONFLICT` · `REPO_WRITE_FAILED`(5xx·전송 오류) · `REPO_WRITE_UNAVAILABLE`(App 미설정·개인키 없음)

## 4. GitHub App 권한 (2026-11-07 읽기 확인)

`GET /apps/echoport-deploy`·`GET /orgs/Softbank-Hackathon-2026-Team-Echo/installations`: 권한 `actions: write`, `contents: read`, `members: read`, `metadata: read`, 설치 `repository_selection: selected`. **contents·pull_requests 쓰기가 없어 지금 켜면 `REPO_WRITE_FORBIDDEN`** 이다. 켜기 전 사람이 할 일(이 PR 은 바꾸지 않는다):

1. GitHub → 조직 Settings → Developer settings → GitHub Apps → `echoport-deploy` → Permissions & events → Repository permissions: **Contents = Read and write**, **Pull requests = Read and write** → Save
2. 조직 Settings → GitHub Apps → `echoport-deploy` Configure → 새 권한 요청 승인(Review request → Accept)
3. 같은 화면 Repository access 에 서비스 저장소(시연: `echo-demo-app`)가 들어 있는지 확인
4. 그 뒤 `deploy/base.yaml` 에 `AI_APPLY_COMMIT_ENABLED: "true"` 를 넣는 별도 PR

## 5. 시험

- 단위 `tests/test_apply_commit.py` 16개: 브랜치 이름, 커밋 메시지(진단·승인자·실행 id), 비밀값 가림·제목 길이, diff 적용 성공·거절 5종, GitHub 쓰기(MockTransport: 토큰 권한 본문·base_tree·parents·author·ref·PR base, 기존 브랜치·PR 재사용, 403·422·설치 없음·파일 없음 → 코드, 개인키 없음, 토큰 로그 미노출)
- 통합 `tests/integration/test_apply_commit_to_repo.py` 6개(가짜 GitHub 쓰기 클라이언트): 성공 → pullRequestUrl·appliedCommitSha·branch·재배포 ref / 쓰기 거절 → FAILED·코드·재배포 없음 / 저장소 파일 불일치 → REPO_PATCH_CONFLICT / App 미설정 → REPO_WRITE_UNAVAILABLE / 스위치 꺼짐 → #101 그대로 / 가짜 진단 → GitHub 안 부름

## 6. 미검증

실제 GitHub 쓰기(App 권한이 없어 운영·로컬 모두 실제 호출 안 함), 실제 러너와의 왕복, 재배포가 고친 커밋으로 빌드되는 실제 배포.

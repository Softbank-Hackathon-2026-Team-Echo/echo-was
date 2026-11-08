# 독립 검토·승인 결속 검증 — 2026-11-08

후보 브랜치 `feat/independent-patch-review`, 기준 main ca918380. 기본 OFF·미병합·미배포.

- 최종 **전체 3,077 PASS, 실패/skip0**, 전용 로컬 PostgreSQL17 UTF-8. `TEST_DATABASE_URL=postgresql+asyncpg://echo_review_test@127.0.0.1:<임시 포트>/echo_review_test`, checkout/worker별 `_test` DB. 고객 DB/DDL 변경 없음.
- 새 실제 입력의 reviewPolicy, runner 결과 수신/저장/조회, 사용자가 본 reviewEvidenceHash 승인·재승인, checks_digest/정책 저장, APPLY 입력·결과 지문, 원격 커밋 직전 재검사 인수. 증거 삭제/변조·다른 커밋·모의·거절 결과 차단, 실패/패치 없음 결과 수락.
- 관련 저장소 반영 **23 PASS**. 가짜 GitHub writer로 수정 파일→수정 커밋→같은 환경 재배포 ref 연결 확인. 승인 증거를 없애면 원격 읽기/쓰기0. 실제 GitHub 쓰기나 고객 재배포 시험이 아니다.
- 기존 PRECHECK·보안 게이트·조직/권한·승인·결과 전송·배포 회귀 포함. 짝 infra 파일은 최신 main3bf3060의 임시 사본으로 로컬 HTTP 계약 확인.
- Ruff·format·mypy 통과. API/DB 새 스키마 마이그레이션 없음, 선택 JSON 필드만 확장.
- 실제 Claude Opus5.5 작성1+독립 검토1 및 GCP Docker 전후 시험은 AI 저장소 `docs/evidence/independent-review-live-20261108.json` 참고. 합성 소스이고 운영 WAS 제출은 하지 않았다.
- 최초 임시 PG SQL_ASCII 실패9건은 UTF-8 재생성으로 해결, 추가 시험 import 오타 수정, 오래된 infra 체크아웃을 잘못 지정한 선택 계약 실패는 최신 main 짝으로 통과. 실패 기록과 최종 근거를 구분한다.

출고 순서와 지원 범위는 [계약](ai-independent-review.md). #135/#138 미병합 Draft를 포함하지 않았고 공용 설정·서비스 변경 접점은 인계한다. 새 정책 운영 종단 인수는 WAS·러너·프론트 배포 후 별도이며 이번 결과를 실제 고객 배포 완료로 표시하지 않는다.

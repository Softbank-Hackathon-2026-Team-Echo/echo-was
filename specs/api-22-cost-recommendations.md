# API 서버 22 — 조회 전용 비용 시나리오

승인: COST1a / COST-RESUME1a. Refs https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/128
정본: 승인자가 제공한 COST-PROPOSAL.md AC1~8와 COST1-BUILD-HANDOFF.md. 이 파일은 서버 구현 범위만 요약한다.

## 범위·인수

1. AC1: 실제 cost router GET에 org viewer와 서비스 소유권을 적용하고 DB의 서빙·예비·배포 기록을 반환한다. 총원가/순절감/이전비/복구시간 미산정은 null과 이유로 표현한다.
2. AC2: TRAFFIC_ONLY는 모든 고정 자원 유지 가정. 고정비 차액 정확히 0, 전송비 미상 순절감은 null. 환경 전체 가격 차이를 서비스 절감으로 쓰지 않는다.
3. AC3: Decimal, 동일 범위·기간·단가 비교, 월 저장 단가 중복 환산 금지, 합계 후 반올림, 공유 inventory key 중복 방지, 음수 절감/0/null/불완전 손익분기 시험.
4. AC4: 지원·보안·데이터 위치·복구 자료를 판정하지 못하면 UNKNOWN/null. 안전 추천 없음. 외부 검사/쓰기 가능한 gate는 호출하지 않는다.
5. AC5: 카탈로그 목록가/계정 청구/크레딧 분리, onprem 미산정≠무료, DB 조회/계산/가격/청구 대상/자원 관측시각 구분.
6. AC6: 사람 정상 GET SELECT-only, 제품/외부 mutation 0. 기존 에이전트 인증의 last_used·감사 기록은 구분. 타 org 및 타 org/삭제 서비스 차단. 서버 결과는 요청 org/service를 반환하고 전역 캐시 없음.
7. AC7 서버 부분: 미산정/빈 결과/오류/stale 미확인 의미를 계약으로 제공한다. FE ko/ja/로딩/응답 경합/화면·실API 연동은 소유하지 않는다.
8. AC8 및 FR-OBS-03: 실제 양수 절감 사례는 자료 부족으로 미충족. 합성 fixture를 운영/청구 절감으로 사용하지 않는다. 전체 요구를 완료로 닫지 않는다.

API/필드/오류/산식 정본: docs/api/cost-recommendations.md. 신규 API이며 main/운영 계약이 아니다.

## 소유·금지

신규 leaf service/schema/tests/docs와 기존 cost_router 최소 연결. 공용 권한 시험표의 비용 GET 한 행만 추가한다. 다른 PR도 같은 시험표를 수정하므로 병합 때 일반 merge로 보존하며 타 기능 행은 편집하지 않는다.

config/dependencies/main/deploy/DB schema/단가·inventory·billing 원문/재난/CVE/DB·네트워크 작업은 수정하지 않는다. Docker/새 DB 서버/운영접속/새 유료 API/실 LLM/자원·자동이전·정리 0. 기능 branch commit/push/Draft PR까지만, 운영 merge/deploy 금지. FE/Slack/Notion 쓰기 0. 추가 클라우드 비용 $0.

## 검증

기준선 전체 시험 → 계산 RED/GREEN → 실제 ASGI GET RED/GREEN → 실제 DB 통합 권한/SQL spy(로컬 DB 없으면 명시 skip) → ruff/format/mypy → 전체 회귀 → Draft PR의 정확 head CI·원격 readback. CI 기본 PostgreSQL service는 기존 저장소 workflow를 그대로 사용하며 workflow/클라우드 자원/새 DB 서버를 따로 만들지 않는다.

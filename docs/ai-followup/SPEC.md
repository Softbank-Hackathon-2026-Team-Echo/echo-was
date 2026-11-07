# 작업② 최소 SPEC

출처: IMPLEMENTATION-HANDOFF.md 작업②. CONTRACT.md가 구현 판정 기준. 시작 main 69d17109d952573186dd934d31c754d3ce0dda76, 분석 2f77f9b26092303ff6e302a877240f24a5753840. 둘 사이 AI 후속 파일 diff 0; 보안 control budget 변경만 선행 main에 포함. 기존 원본 probe 7개로 serializer 공백/복구 결함을 확인했다(결함 재현의 성공이지 정상 동작 7개 아님).

AC: sandbox 완료·ID 지연의 명시적 대기, 일반 PRECHECK 미대상, legacy/null UNKNOWN, claim 및 stale/중단 UNKNOWN, 저장소 확정 성공/실패와 원격 불명 구분, 재배포 ID/거절과 실제 배포 결과 분리, 모의/OFF 원래 SHA 동작 유지, 단계 전용 시각/null deadline, 부모/단독 상세/승인 응답 일치, GET 무부작용·조직 권한 회귀, JSONB marker 부분 갱신 후 새 세션 조회.

TDD는 작은 상태 투영 → callback marker → HTTP/PG 순서로 RED/GREEN 증거를 남긴다. baseline 전체 suite를 실제 격리 PostgreSQL 17에서 먼저 실행한다. 원본 분석 probe/RED는 수정하지 않고 새 출력 경로만 쓴다. 네트워크 대역과 실PG를 구분하며 실제 클라우드/GitHub App/LLM/사용자 브라우저 인수는 금지·미실행이다.

DB 선택: 기존 agent_runs.result JSONB 및 patch_result를 재사용한다. 새 timestamp/skip marker 키만 추가하고 컬럼·index·revision·backfill은 추가하지 않는다. 현재 Alembic head 9d7fe177bcaf 단일. 다른 DB receipt/pair 열차와 migration 접점 0. schemaVersion 1은 응답 계약 버전이며 state revision 보장이 아니다.

원장 중복: 기존 echo-was#98(실패 진단·승인 후 재배포)은 닫힌 최초 연동이고 echo-was#88(반영 실행 상세 조회)은 기존 담당 Saccharine1211을 보존한다. 이번 원장은 echo-was#130(후속 읽기 상태)이며 기존 OPEN/Project4 In Progress/담당 Saccharine1211을 유지한다. 재생성하지 않는다. 형래/도영의 검토는 아직 합의되지 않은 초안이다.

중단 인수: 기존 9 tracked+6 untracked의 원형은 0a522cbb90f68c37699746d0fe468101270ee688에 보존했다. 최신 main 045bd9bf8814b1e4b85609a24e4f3619a921de07을 일반 merge한 구현 검증 head는 7393fba2efc2c4d42d25f6559c365f6f48c1cd72이다. 도현의 미병합 #121(요청 journal 재생)은 흡수하지 않았다. 새 main의 이미지 게이트·enum은 그대로 보존했다. 후속 읽기 상태의 제품 변경은 최초 인수본과 동일하다. 실PG 전체 기준선 2881 PASS/후보 2913 PASS, 실패·건너뜀 0. 상세 근거는 VERIFICATION.md를 따른다.

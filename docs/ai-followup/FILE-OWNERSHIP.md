# 작업② 파일 소유권

계약/서비스 소유자 1명: 현재 역할② WAS 상태 작업자. 재위임 없음. 부모가 정확 head를 수락해 ③ 구현을 재개하기 전까지 아래 파일 단독 소유.

- app/routers/precheck_router.py: 기존 주입 Clock을 GET 투영/승인 응답에 전달(권한·라우트 불변)
- app/schemas/precheck.py: 공개 additive 응답과 투영 연결
- app/services/apply_followup.py: 순수 읽기 판정
- app/services/apply_commit_service.py: 기존 callback의 저장소 완료 시각만
- app/services/diagnosis_service.py: 기존 callback 부분 갱신의 OFF/접수 시각만
- app/enums.py: 후속 읽기 enum만
- tests/test_apply_followup.py: 상태 계약·serializer 시험
- tests/integration/test_apply_followup_state.py: 실제 PG·HTTP 경계 시험
- tests/integration/{test_precheck_patches,test_apply_patch_runs,test_apply_run_detail}.py: 기존 키·값 단언은 모두 유지하고 새 선택 응답 필드의 literal 기대값만 추가(분석 원본 probe/RED 변경 0)
- docs/ai-followup/{CONTRACT,SPEC,FILE-OWNERSHIP,VERIFICATION}.md
- docs/api/prechecks.md: 계약 링크만

수정 금지: ③ 복구 scheduler/worker/새 job·recovery test, FE 제품·시험, 운영 manifest·설정·마이그레이션, 타인 PR 파일 혼합. 기존 test_apply_commit.py와 진단 시험은 원문 보존한다. app/worker/*·JobRepository·DeployRequestProcessor에는 손대지 않는다. 새 retry/복구 policy를 여기 넣지 않는다.

③ 서비스 소유권 이전은 부모가 이 PR head를 base로 지정한 이후에만 가능하다. 당시에는 이 명단의 서비스 3개/enum/schema 변경을 먼저 읽고 인계받는다.

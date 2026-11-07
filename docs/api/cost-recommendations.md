# 조회 전용 비용 시나리오 API

상태: 이번 기능 브랜치에서 구현한 신규 계약. main 미병합·운영 미배포. FE 합의/화면 연동과 실제 양수 절감 사례는 별도 미검증이다.

Refs https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/128

## 경로·권한

`GET /api/v1/orgs/{org_id}/costs/recommendations?serviceId={uuid}`

기존 cost router에 추가한다. `ViewerAccess`를 재사용한다. 사람 조직 viewer/developer/admin 및 해당 조직 읽기 전용 agent가 조회할 수 있다. 서비스는 조직 범위 `ServiceRepository.get_live_by_id`로 먼저 확인한다. 삭제 서비스와 다른 조직 서비스는 404다. 조직의 살아 있는 환경만 반환한다.

| 상황 | 응답 |
|---|---|
| 미인증 | 401 UNAUTHENTICATED |
| 조직 비회원·agent의 다른 조직·잘못된 org_id | 404 ORGANIZATION_NOT_FOUND |
| 다른 조직/삭제/없는 서비스 | 404 SERVICE_NOT_FOUND |
| serviceId 누락·UUID 아님 | 422 VALIDATION_ERROR |
| 자료/대안 없음 | 200 + null/빈 배열/missingReasons |
| 잘못된 기존 단가 카탈로그 | 기존 500 COST_CATALOG_INVALID |

`ApiResponse` 봉투의 `success=true`, `data` 안은 camelCase다. 값이 없으면 키를 생략하지 않고 null로 낸다. 금액은 USD JSON number/null, 내부는 Decimal이다.

## 최상위 data

| 키 | 의미 |
|---|---|
| organizationId / serviceId / serviceStatus | 요청 범위와 현재 서비스 DB 상태 |
| computedAt | 응답 계산 시각(UTC). 자원 실측/가격 확인 시각 아님 |
| placementReadAt | 서비스·연결·배포 DB 기록을 읽은 시각. 클라우드 상태 실측 아님 |
| inventoryObservedAt | null. 저장 inventory에 실자원 관측시각 없음 |
| currency / hoursPerMonth | USD / 730(월 추정 관례, 실사용 시간 아님) |
| freshness / confidence | UNVERIFIED / INSUFFICIENT_DATA. 임의 시간 임계값·확률 점수 없음 |
| currentMonthlyUsd | 서비스 귀속 총원가. 현재 자료로는 null |
| currentPlacement[] | 연결별 환경 ID/이름/kind, serviceEnvironmentId, 기존 role_of의 SERVING/STANDBY/NONE, trafficWeight, trafficAppliedAt, currentDeploymentId, deploymentStatus, deploymentRecordedAt |
| alternatives[] | 현재 서빙이 아닌 등록 환경의 TRAFFIC_ONLY 비교 가정. 등록됨이 배포됨·지원됨을 뜻하지 않음 |
| catalogEstimate | 과거 단가 snapshot × 조직 매칭 inventory의 부분 추정. 서비스 원가/절감액 아님 |
| billing[] | 기존 BillingResponse 재사용. 계정 범위 과거 청구, usageUsd / creditsUsd / netUsd, fetchedAt / days[].date / estimated 분리 |
| missingReasons[] | 미산정·미평가 근거 코드 |

## alternatives[]

- environmentId / environmentName / kind: 등록된 비교 대상.
- scenario=TRAFFIC_ONLY, status=NEEDS_EVIDENCE, isSafeRecommendation=false.
- hasCurrentDeployment: 해당 서비스의 현재 배포 연결이 있는지. 지원·복구·안전 판정 아님.
- currentMonthlyUsd / alternativeMonthlyUsd: null. 자원 ID·서비스 귀속·완전한 반복 비용이 없음.
- fixedCostSavingsMonthlyUsd: 정확히 0. 모든 고정 자원을 유지한다는 가정의 차액이다. 원가·청구가 무료라는 뜻이 아니다.
- netSavingsMonthlyUsd: null. 반복 전송·복제·백업비가 미산정이므로 고정비 차액을 순절감이라고 부르지 않는다.
- oneTimeMigrationUsd: null. 이전/중복 가동/백업 비용을 0으로 발명하지 않는다.
- breakEvenMonths: null. 완전 자료와 양수 순절감에서만 일회비/순절감으로 계산할 수 있다.
- recoverySeconds: null. 같은 앱·환경쌍·예비 모드의 측정 근거 없음. FE 상수 RTO를 복사하지 않는다.
- retainedInventoryKeys[]: 유지 가정의 카탈로그 키. 실자원 ID 아님. addedResourceIds/removedResourceIds는 항상 빈 배열이다. 종료 가능성은 판단하지 않는다.
- constraints[]: SUPPORT / SECURITY / DATA_RESIDENCY / RECOVERY 각각 status=UNKNOWN, reason, observedAt=null.
- assumptions[]: TRAFFIC_ONLY_ALL_RESOURCES_RETAINED, NO_RESOURCE_CLEANUP_ASSUMED.
- missingReasons[]: 아래 공통 이유와 대상별 이유.

지원쌍 계약은 다른 PR 소유다. 기존 보안 matrix/gate가 있다는 이유로 환경쌍이 안전하다고 판정하지 않는다. 특히 gate는 외부 검사/쓰기 가능하므로 호출하지 않는다. `NOT_EVALUATED`는 보안 데이터 자체가 전혀 없다는 뜻이 아니다. 판정 정책을 발명하지 않는다. 안전한 양수 추천은 이번 실제 응답에서 한 건도 만들지 않는다. 후보는 기존 환경 정렬(생성 시각/id)을 보존하며 비용순 추천으로 위장하지 않는다.

## 카탈로그 산식·증거

`catalogEstimate.scope=ORGANIZATION_CATALOG_PARTIAL`, `source=ESTIMATE`. monthlyUsd는 매칭한 고정비 부분만 합산하고 마지막에 센트 단위 HALF_UP 반올림한다. 전부 미매칭이면 0이 아닌 null이다.

- hour: 수량 × 저장 단가 × 730.
- GiB-month: 수량 × 저장 단가. 월 단가에 730을 다시 곱하지 않는다.
- 각 component의 원 단위로 월 금액을 직접 계산한 뒤 합산한다. GiB-month를 시간 단가로 나눴다가 다시 곱하지 않는다. inventory key 중복 제거 후 월 총합에만 최종 반올림하며 기존 시간·일 비용 API의 계산은 바꾸지 않는다.
- 여러 환경이 같은 kind+region inventory에 매칭되면 inventory key별 한 번만 센다. 공용 inventory도 한 번만 포함한다. 다른 키가 실제로 같은 물리 자원인지 증명할 자료가 없으므로 전체 실원가로 승격하지 않는다.
- inventories[]는 inventoryKey, 매칭 environmentIds, 반올림 전 monthlyUsd, 원래 note, components[]를 담는다.
- components[]는 name/priceKey/quantity/unit/unitPriceUsd/priceUnit/priceSource/priceCheckedAt/priceEffectiveDate/priceVerified/formula를 담는다.
- revision=null: 카탈로그 자체에 자료 revision 계약이 없다. 현재 배포 head나 계산시각을 자료 revision으로 복사하지 않는다.
- unpricedEnvironmentIds는 비용 미산정 환경이다. onprem도 포함한다. 온프레 기회비용·장비·전력·운영 인건비 미산정은 무료라는 뜻이 아니다.
- billing[].fetchedAt와 days[].date는 과거 수집/청구 대상일이다. 목록가에 크레딧·net 청구를 섞지 않고 서비스별로 배분하지 않는다. 현재 잔액·최신 요금은 확인하지 않았다.

순수 계산기 `calculate_savings`는 동일 기간·범위·단가 기준임이 확인된 입력만 비교한다(comparable). 음수/NaN/무한 비용 입력은 거부한다. 음수 절감은 그대로 비용 증가다. 순절감=고정비 차액+현재 반복비−대안 반복비, 일회비는 월비에서 빼지 않는다. 0/음수/불완전 순절감의 손익분기는 null이다. 실제 GET은 동일 자원 유지의 상징적 고정비 차액 0과 미상 비용 null 경로로 이 계산기를 사용한다. unit test의 양수 fixture는 운영 절감 사례가 아니다.

## missingReasons

공통: SERVICE_RESOURCE_ATTRIBUTION_UNKNOWN, INVENTORY_OBSERVED_AT_UNKNOWN, RECURRING_TRANSFER_COST_UNKNOWN, ONE_TIME_MIGRATION_COST_UNKNOWN, PAIR_SUPPORT_NOT_EVALUATED, PAIR_SECURITY_NOT_EVALUATED, DATA_RESIDENCY_NOT_EVALUATED, RECOVERY_TIME_NOT_MEASURED, CATALOG_FRESHNESS_UNVERIFIED.

상황별: TARGET_NOT_DEPLOYED, ONPREM_COST_UNASSESSED, NO_SERVING_PLACEMENT, NO_ALTERNATIVE_ENVIRONMENT.

## 조회·검증 경계

GET은 계획 생성/승인/이전·배포·가중치·DB 전환·정리·클라우드/LLM 호출을 하지 않는다. 사용자 화면 진입/대상 선택/새로고침은 이 GET만 쓰고 실행 CTA는 만들지 않는다. 브라우저의 늦은 응답 격리는 FE 담당이며 서버는 요청의 organizationId/serviceId를 항상 반환하고 전역 결과 캐시를 쓰지 않는다.

사람 viewer 정상 GET은 SELECT만 사용한다. 기존 에이전트 인증은 token last_used UPDATE 및 성공한 GET 감사 INSERT를 할 수 있다. 제품 mutation 0을 모든 DB 쓰기 0으로 확대하지 않는다.

로컬 ASGI는 실제 앱/router/service/schema를 통과하되 persistence/auth 자료를 fixture로 교체한다. integration 시험은 실제 PostgreSQL guard·Repository·DB 상태, 사람 GET SQL SELECT-only 및 외부 HTTP 금지를 검증한다. 로컬 DB가 없어 skip이면 미검증으로 남기고 기존 PR CI에서 실제 결과를 별도 확인한다. 운영 접속/수정·Docker/새 DB 서버 시작은 하지 않는다.

AC1~7 중 서버 응답/계산/권한/조회 경계만 대상이다. ko/ja UI, loading/error/empty/stale 렌더, projector/mobile, 실 API FE 연동은 별도다. FR-OBS-03 실제 양수 절감·실청구 감소는 미입증이다.

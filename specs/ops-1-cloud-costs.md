# SPEC 운영 1: 환경별 클라우드 비용 추정 + 청구 실측 보기 API

- 상태: Confirmed (지민 결정 C, 2026-11-05 14:05) · 노션 태스크 없음 — 킥오프 테마 "비용·관측" (Project #4)
- 기준: echo-was main `65b71d3b11098c84f06736b3d71c6390ad28bf09`
- 사실 확인: 2026-11-05 (아래 "확인된 사실"의 숫자는 읽기 전용 CLI·가격 API 실측)

## 1. 목적

킥오프 테마 "비용·관측"이 지금 화면 상수(목업)뿐이고 서버가 없다. 조직의 AWS·GCP 대여 비용을 **"지금은 사용량×단가 추정(ESTIMATE), 확정은 다음 날 청구 데이터(BILLING)"** 로 나눠 보여 주는 읽기 API 하나를 만든다(가이드 11절). 온프레는 비용 표에서 빼고 "온프레: 자체 장비, 청구 없음"으로만 표기한다.

## 2. 확인된 사실 (2026-11-05)

1. 프론트: 비용 화면·호출 없음. `echo-frontend/src/lib/api/evacuation.ts:85` `COST_BY_KIND = { onprem: 90, gcp: 58, aws: 55 }`(대피 후보 `scores.cost` 0~100 점수 상수, 가중치 0.1).
2. echo-was: 라우터는 `app/routers/`, 읽기 라우트 선례 `security_router.py`(`ViewerAccess`). YAML 라이브러리 의존성 없음 → 표준 `tomllib`·`json`만 쓴다. Dockerfile `COPY app ./app`이라 `app/data/`가 이미지에 들어간다.
3. AWS 청구: Cost Explorer가 읽힌다(계정 <ACCOUNT_ID>). 서비스별 금액은 크레딧이 상계된 순액(≈0). `RECORD_TYPE` 묶음으로 2026-11-04: Usage **6.7374**, Credit **-6.7374**, 순 0.0000 USD, `Estimated=true`. 값은 **계정 전체**(aws-tokyo + 서울 onprem 시연 EC2 + ECR 등)이고 비용 배분 태그가 없어 환경별로 나눌 수 없다. 요청당 $0.01, 관제 서버(GKE)의 AWS 역할에 `ce:GetCostAndUsage` 권한 없음.
4. GCP 청구: 결제 계정 연결됨, BigQuery 데이터셋 0개 → 청구 내보내기 없음 → 추정만.
5. 실제 자원(읽기 전용 CLI, Terraform 대조)과 공개 단가(AWS Price List API·Cloud Billing Catalog API·Google 가격 문서) — 4절 데이터 파일의 값 그대로. 계산 결과: aws-tokyo 0.4896/시간·11.75/일, gcp-seoul 0.3243·7.78, 공용 0.1846·4.43, 합계 0.9985·23.96(USD).
6. 목록가 추정(aws-tokyo 11.75/일)이 실측 사용 금액(계정 6.74/일)보다 크다 — 무료 플랜이 m7i-flex.large 시간을 $0 처리한 것으로 보이나 미검증. **이 차이를 숨기지 않고 응답 note로 보여 주는 것이 이 API의 정직성 포인트다.**

## 3. 설계 결정

| ID | 결정 | 이유 |
|---|---|---|
| D-1 | `GET /api/v1/orgs/{orgId}/costs` 하나. 뷰어 이상(에이전트 토큰 가능, `ViewerAccess`). 감사 줄 없음(읽기; 에이전트 GET은 기존 미들웨어가 기록). | 지민 지시: 조직 구성원 읽기, 감사 경로 불필요 |
| D-2 | 서버는 **ESTIMATE만 계산**한다(요청 때 데이터 파일 × 환경 목록, 외부 호출 0). 운영 서버가 클라우드 청구 API를 부르지 않는다. | 청구 API 권한(IRSA/WI)이 없고 요청당 과금, AWS 값은 계정 단위라 환경에 못 붙임. 시연 중 외부 실패로 화면이 깨지지 않게 |
| D-3 | BILLING 실측은 **관리자 스크립트** `python -m app.scripts.import_billing`이 Cost Explorer JSON 파일(운영자가 aws CLI로 받은 것)을 `app/data/cloud_billing.json`에 병합해 쓰고, 다음 배포로 실린다. 스크립트는 네트워크를 부르지 않는다. | **마이그레이션 0**(병렬 세션 secmatrix·residency와 alembic head 충돌 위험 회피), 하루 한 숫자라 표·쓰기 API·감사 경로가 과함. PUT 관리자 API는 만들지 않는다 |
| D-4 | 환경 항목의 `source`는 항상 `ESTIMATE`. BILLING은 별도 `billing[]` 블록(공급자·계정 단위, 일별)으로 준다. | 청구가 계정 단위라 환경 값으로 쓰면 거짓 정밀 |
| D-5 | 온프레(`kind = onprem`) 환경은 비용 계산에서 빼고 `excluded[]`에 `note = "온프레: 자체 장비, 청구 없음"`으로 넣는다. 서울 onprem 시연 EC2도 추정에 넣지 않는다. | 지민 결정 C |
| D-6 | 데이터 파일 3개(새 디렉터리 `app/data/`): `cloud_prices.toml`(단가·출처·확인일·확인 여부), `cloud_inventory.toml`(환경별·공용 자원 목록, 손으로 박음 — 자동 인벤터리 금지), `cloud_billing.json`(스크립트가 씀). 새 의존성 0. | 사람이 고치는 표는 주석 가능한 TOML, 기계가 쓰는 표는 JSON |
| D-7 | 자원 목록은 환경 `kind` + config `region`으로 맞춘다(aws `ap-northeast-1` → aws-tokyo 목록, gcp `asia-northeast3` → gcp-seoul 목록). 맞는 목록이 없는 aws·gcp 환경은 `hourlyUsd = null`, `components = []`, note "자원 목록 없음 — 추정하지 않음". | 환경 id는 DB마다 달라 파일에 박을 수 없다 |
| D-8 | 공용 자원(GCP 진입점 VM 2·관제 DB 디스크·샌드박스 VM·관제 API 전역 전달 규칙)은 `shared[]` 항목 하나로 보이고 합계에 넣는다. 조직의 aws·gcp 환경 중 하나라도 목록에 맞았을 때만 보인다(개인 조직에 플랫폼 비용이 보이지 않게). | 샌드박스 VM 포함 지시 + 환경에 억지로 나누지 않음 |
| D-9 | 계산: 시간 단가 항목 `hourly = quantity × unitPrice`, GB·월 항목 `hourly = quantity × unitPrice / 730`. 응답 `hourlyUsd`는 소수 4자리, `dailyUsd = round(정밀 hourly × 24, 2)`. 합계는 정밀 값 합에서 반올림. 금액 계산은 `Decimal`. | 반올림 누적 방지 |
| D-10 | 데이터 파일은 프로세스당 한 번 읽어 검사한다(`get_cost_catalog` 의존성, 캐시). 참조한 단가 키가 없거나, 단위가 `hour`/`GiB-month` 밖이거나, 수량이 음수면 시작/첫 요청에서 예외(시험이 실제 파일을 읽어 확인). 시험은 의존성 교체로 작은 카탈로그를 넣는다. | 잘못된 표가 조용히 0원이 되지 않게 |
| D-11 | 마이그레이션 0, 새 환경 변수 0, 새 의존성 0, 기존 API 모양 변경 0. | |

## 4. 데이터 파일 내용 (값 그대로 넣는다)

### `app/data/cloud_prices.toml` — 키마다 `unit_price_usd`, `unit`(`hour`|`GiB-month`), `description`, `source`(URL 또는 API·SKU 이름), `effective_date`, `checked_at = "2026-11-05"`, `verified`(bool), `note`(선택)

| 키 | unit_price_usd | unit | source | effective | verified |
|---|---|---|---|---|---|
| `aws.ap-northeast-1.eks-cluster` | 0.10 | hour | AWS Price List API AmazonEKS `APN1-AmazonEKS-Hours:perCluster` · https://aws.amazon.com/eks/pricing/ | 2026-10-01 | true |
| `aws.ap-northeast-1.ec2.m7i-flex.large` | 0.12369 | hour | AWS Price List API AmazonEC2 `APN1-BoxUsage:m7i-flex.large` (Linux, Shared) · https://aws.amazon.com/ec2/pricing/on-demand/ | 2026-11-01 | true |
| `aws.ap-northeast-1.ebs.gp3` | 0.096 | GiB-month | `APN1-EBS:VolumeUsage.gp3` · https://aws.amazon.com/ebs/pricing/ | 2026-11-01 | true |
| `aws.ap-northeast-1.nat-gateway` | 0.062 | hour | `APN1-NatGateway-Hours` · https://aws.amazon.com/vpc/pricing/ (처리량 $0.062/GB 별도) | 2026-11-01 | true |
| `aws.ap-northeast-1.alb` | 0.0243 | hour | AWSELB `APN1-LoadBalancerUsage` Application · https://aws.amazon.com/elasticloadbalancing/pricing/ (LCU $0.008 별도) | 2026-08-01 | true |
| `aws.ap-northeast-1.nlb` | 0.0243 | hour | AWSELB `APN1-LoadBalancerUsage` Network (NLCU $0.006 별도) | 2026-08-01 | true |
| `aws.ap-northeast-1.public-ipv4` | 0.005 | hour | AmazonVPC `APN1-PublicIPv4:InUseAddress` · https://aws.amazon.com/vpc/pricing/ | 2026-11-01 | true |
| `gcp.gke-zonal-cluster` | 0.10 | hour | Cloud Billing Catalog `CCD8-9BF1-090E` "Zonal Kubernetes Clusters" · https://cloud.google.com/kubernetes-engine/pricing (note: 결제 계정당 월 $74.40 무료 크레딧 — 존 클러스터 1개는 사실상 0) | 2026-11-04 | true |
| `gcp.asia-northeast3.e2-standard-2` | 0.085964 | hour | Catalog `6F81-5844-456A` "E2 Instance Core running in Seoul" 0.028026/vCPU·h ×2 + "E2 Instance Ram running in Seoul" 0.003739/GiB·h ×8 | 2026-11-04 | true |
| `gcp.asia-northeast3.e2-small` | 0.021491 | hour | 같은 SKU, 0.5 vCPU(공유 코어) + 2 GiB | 2026-11-04 | true |
| `gcp.asia-northeast3.pd-balanced` | 0.13 | GiB-month | Catalog "Balanced PD Capacity in Seoul" | 2026-11-04 | true |
| `gcp.external-ip-standard-vm` | 0.005 | hour | Catalog "External IP Charge on a Standard VM" (note: SKU 계층상 결제 계정 월 720시간까지 0) | 2026-11-04 | true |
| `gcp.cloud-nat-vm` | 0.0014 | hour | https://cloud.google.com/nat/pricing — VM 1대·시간(32대 이하), 처리량 $0.045/GiB 별도 | 2026-11-05 | true |
| `gcp.cloud-nat-ip` | 0.005 | hour | https://cloud.google.com/nat/pricing — NAT 외부 IP | 2026-11-05 | true |
| `gcp.forwarding-rules-first-5` | 0.025 | hour | https://cloud.google.com/vpc/network-pricing — 첫 5개 묶음 값(전역 표·미국 지역 표). **서울 지역 표 값 미확인** | 2026-11-05 | false |

### `app/data/cloud_inventory.toml` — 항목마다 `key`, `name`(표시), `provider`, `region`, (환경용) `match_kind`·`match_region`, `note`, `components[] = {name, kind, price, quantity, unit_label}`

**aws-tokyo** (`match_kind = "aws"`, `match_region = "ap-northeast-1"`, note: "목록가 추정. 데이터 전송·LB 용량 단위·NAT 처리량 제외. 계정이 무료 플랜이라 실제 사용 금액은 더 작을 수 있음(청구 실측 참고)")
| name | kind | price | quantity | unit_label |
|---|---|---|---|---|
| EKS 컨트롤 플레인 (echo-dev-eks) | COMPUTE | aws.ap-northeast-1.eks-cluster | 1 | 클러스터 |
| EC2 노드 m7i-flex.large (온디맨드) | COMPUTE | aws.ap-northeast-1.ec2.m7i-flex.large | 2 | 대 |
| PostgreSQL 볼륨 gp3 (클러스터 안 DB) | DATABASE | aws.ap-northeast-1.ebs.gp3 | 10 | GiB |
| PostgreSQL 복제 노출 NLB (echo-dev-pg-a) | NETWORK | aws.ap-northeast-1.nlb | 1 | 개 |
| NAT 게이트웨이 | NETWORK | aws.ap-northeast-1.nat-gateway | 1 | 개 |
| 앱 ALB (echo-dev-apps) | NETWORK | aws.ap-northeast-1.alb | 1 | 개 |
| 공인 IPv4 주소 | NETWORK | aws.ap-northeast-1.public-ipv4 | 5 | 개 |
| 노드 루트 볼륨 gp3 | STORAGE | aws.ap-northeast-1.ebs.gp3 | 40 | GiB |

**gcp-seoul** (`match_kind = "gcp"`, `match_region = "asia-northeast3"`, note: "목록가 추정. GKE 관리비는 무료 크레딧으로 상쇄될 수 있음. 관제(echo-was)도 같은 노드에서 돈다. 데이터 전송·NAT 처리량 제외. GCP 청구 내보내기 없음")
| name | kind | price | quantity | unit_label |
|---|---|---|---|---|
| GKE 존 클러스터 관리비 (echo-dev-gke) | COMPUTE | gcp.gke-zonal-cluster | 1 | 클러스터 |
| GKE 노드 e2-standard-2 | COMPUTE | gcp.asia-northeast3.e2-standard-2 | 2 | 대 |
| PostgreSQL 볼륨 pd-balanced (echo-pg) | DATABASE | gcp.asia-northeast3.pd-balanced | 10 | GiB |
| 지역 전달 규칙 (DB 외부 노출·인그레스 내부 LB) | NETWORK | gcp.forwarding-rules-first-5 | 1 | 묶음(규칙 2개) |
| Cloud NAT 게이트웨이 (노드 2대) | NETWORK | gcp.cloud-nat-vm | 2 | VM |
| Cloud NAT 외부 IP | NETWORK | gcp.cloud-nat-ip | 1 | 개 |
| 노드 부트 디스크 pd-balanced | STORAGE | gcp.asia-northeast3.pd-balanced | 100 | GiB |

**shared: gcp-platform** (`name = "공용 플랫폼 (고정 진입점·관제 DB·AI 샌드박스)"`, provider gcp, region asia-northeast3, note: "특정 환경에 속하지 않는 GCP 공용 자원")
| name | kind | price | quantity | unit_label |
|---|---|---|---|---|
| 고정 진입점 VM e2-small (entry, entry-b) | COMPUTE | gcp.asia-northeast3.e2-small | 2 | 대 |
| AI 샌드박스 VM e2-standard-2 (echo-sandbox-runner) | COMPUTE | gcp.asia-northeast3.e2-standard-2 | 1 | 대 |
| 관제 DB 볼륨 pd-balanced (echo-db) | DATABASE | gcp.asia-northeast3.pd-balanced | 10 | GiB |
| 관제 API 전역 전달 규칙 (http·https) | NETWORK | gcp.forwarding-rules-first-5 | 1 | 묶음(규칙 2개) |
| 진입점 외부 IP | NETWORK | gcp.external-ip-standard-vm | 2 | 개 |
| 샌드박스 Cloud NAT 게이트웨이 | NETWORK | gcp.cloud-nat-vm | 1 | VM |
| 샌드박스 Cloud NAT 외부 IP | NETWORK | gcp.cloud-nat-ip | 1 | 개 |
| 진입점·샌드박스 부트 디스크 pd-balanced | STORAGE | gcp.asia-northeast3.pd-balanced | 70 | GiB |

(계산 확인: aws-tokyo 0.48955534… → 0.4896 / 11.75, gcp-seoul 0.32431704… → 0.3243 / 7.78, gcp-platform 0.18459257… → 0.1846 / 4.43, 합계 0.99846495… → 0.9985 / 23.96)

### `app/data/cloud_billing.json` — 스크립트가 쓰는 표(처음 값은 11/04 실측 한 줄)

```json
{
  "providers": [
    {
      "provider": "aws",
      "scope": "ACCOUNT",
      "label": "AWS 계정 (도쿄 aws-tokyo + 서울 온프레 시연 EC2 등 계정 전체)",
      "source": "AWS Cost Explorer GetCostAndUsage DAILY UnblendedCost, RECORD_TYPE 묶음",
      "fetchedAt": "2026-11-05T05:50:00Z",
      "note": "크레딧이 사용 금액을 상계해 실제 청구는 0. 환경별로 나누지 못함(비용 배분 태그 없음).",
      "days": [{"date": "2026-11-04", "usageUsd": 6.7374, "creditsUsd": -6.7374, "netUsd": 0.0, "estimated": true}]
    },
    {"provider": "gcp", "scope": "ACCOUNT", "label": "GCP 프로젝트 team-echo-final", "source": null, "fetchedAt": null,
     "note": "BigQuery 청구 내보내기 없음 — 추정만 보여 준다.", "days": []}
  ]
}
```
(정확한 값은 원본 `ce_raw2.json`을 스크립트에 넣어 만든다: Usage 6.737445717, Credit -6.737445721 → 소수 4자리.)

## 5. API 계약

`GET /api/v1/orgs/{orgId}/costs` → 200 `ApiResponse[CloudCostsResponse]`. 401(로그인 없음)·404(다른 조직, 기존 규칙)·403은 기존 검사기 그대로. 모든 칸은 항상 있고 값이 없으면 null(공통 규칙). camelCase.

```json
{
  "success": true,
  "data": {
    "asOf": "2026-11-05T06:00:00Z",
    "currency": "USD",
    "totalHourlyUsd": 0.9985,
    "totalDailyUsd": 23.96,
    "environments": [
      {
        "environmentId": "dcec9925-7af6-4584-867a-12e878b84b1e",
        "environmentName": "aws-tokyo",
        "provider": "aws",
        "region": "ap-northeast-1",
        "source": "ESTIMATE",
        "hourlyUsd": 0.4896,
        "dailyUsd": 11.75,
        "asOf": "2026-11-05T06:00:00Z",
        "note": "목록가 추정. …",
        "components": [
          {"name": "EC2 노드 m7i-flex.large (온디맨드)", "kind": "COMPUTE", "quantity": 2, "unit": "대",
           "unitPriceUsd": 0.12369, "priceUnit": "hour", "hourlyUsd": 0.2474, "priceVerified": true,
           "priceSource": "AWS Price List API AmazonEC2 APN1-BoxUsage:m7i-flex.large …", "priceCheckedAt": "2026-11-05", "note": null}
        ]
      }
    ],
    "shared": [
      {"key": "gcp-platform", "name": "공용 플랫폼 (고정 진입점·관제 DB·AI 샌드박스)", "provider": "gcp", "region": "asia-northeast3",
       "source": "ESTIMATE", "hourlyUsd": 0.1846, "dailyUsd": 4.43, "asOf": "…", "note": "…", "components": []}
    ],
    "excluded": [
      {"environmentId": "0c415986-…", "environmentName": "onprem", "kind": "onprem", "note": "온프레: 자체 장비, 청구 없음"}
    ],
    "billing": [
      {"provider": "aws", "source": "BILLING", "available": true, "scope": "ACCOUNT", "label": "…", "fetchedAt": "2026-11-05T05:50:00Z",
       "note": "…", "days": [{"date": "2026-11-04", "usageUsd": 6.7374, "creditsUsd": -6.7374, "netUsd": 0.0, "estimated": true}]},
      {"provider": "gcp", "source": "BILLING", "available": false, "scope": "ACCOUNT", "label": "…", "fetchedAt": null,
       "note": "BigQuery 청구 내보내기 없음 — 추정만 보여 준다.", "days": []}
    ]
  }
}
```

- `environments[]`: 조직의 살아 있는 aws·gcp 환경, 환경 `createdAt`·`id` 순. 목록이 안 맞으면 D-7.
- `kind`: `COMPUTE | DATABASE | NETWORK | STORAGE | OTHER`(새 enum `CostComponentKind`). `source`: `ESTIMATE | BILLING`(새 enum `CostSource`). `provider`는 환경 `kind` 문자열(aws·gcp).
- `asOf` = 주입한 시계의 지금(UTC). `billing[].available` = `days`가 비어 있지 않음.
- `excluded[]`: `kind = onprem`(그리고 aws·gcp 밖의 종류) 환경. note는 onprem이면 위 문장, 그 밖은 "청구를 계산하지 않는 종류".
- 환경이 하나도 없으면 `environments = []`, `shared = []`, 합계 0, `billing`은 그대로.

## 6. 완료 기준 (AC)

| AC | 내용 | 시험 |
|---|---|---|
| AC-1 | 실제 데이터 파일을 읽어 카탈로그 검사를 통과하고, aws-tokyo·gcp-seoul·gcp-platform의 hourly/daily가 4절 계산 확인 값과 같다(0.4896/11.75, 0.3243/7.78, 0.1846/4.43, 합계 0.9985/23.96) | 단위 시험(실제 파일) |
| AC-2 | 없는 단가 키·잘못된 단위·음수 수량·중복 키는 카탈로그 읽기에서 예외 | 단위 시험(tmp 파일) |
| AC-3 | 조직에 aws(ap-northeast-1)·gcp(asia-northeast3)·onprem 환경 → 200, 두 환경 ESTIMATE + shared 1 + excluded onprem(note 문장 그대로) + billing aws available·gcp unavailable, 합계 = 항목 합 | 통합 시험(실제 DB, 기존 하네스) |
| AC-4 | 목록이 안 맞는 aws 환경(예 us-east-1) → `hourlyUsd`·`dailyUsd` null, components [], 합계에서 빠짐. aws·gcp 환경이 전부 안 맞으면 shared 없음 | 통합 시험 |
| AC-5 | 다른 조직 사용자 404(또는 기존 조직 검사 규칙대로), 로그인 없음 401, 뷰어·에이전트 토큰 200. 검사기·역할 표 완전성 시험 통과 | 통합 + 기존 시험 |
| AC-6 | `import_billing`: RECORD_TYPE 묶음 CE JSON(시험용 고정 파일) → usage·credits·net·estimated 계산, 같은 날짜는 덮어쓰고 다른 날짜는 유지, 날짜순 저장. 네트워크 호출 없음. 실제 파일 `cloud_billing.json`은 11/04 원본으로 만든 값 | 단위 시험 |
| AC-7 | 품질: ruff·format·mypy app 통과, 전체 pytest(TEST_DATABASE_URL) 건너뜀 0, alembic heads 하나(마이그레이션 0) | M1 재검증 |
| AC-8 | 문서: `docs/api/costs.md`(계약·데이터 파일·갱신 방법·ESTIMATE/BILLING 구분·제외 항목·단가 출처표), CLAUDE.md 구조·설계 문서 표에 한 줄씩, README에 청구 갱신 명령 한 단락 | 검토 |

## 7. 위임한 세부사항 (워커가 정하고 보고)

- 카탈로그 클래스 이름·캐시 방식, TOML 키 이름의 세부(4절 값과 뜻은 유지), 응답 스키마 클래스 이름, enum 위치(`app/enums.py`에 두 개 더하기 허용).
- `import_billing` 인자 모양(`--provider aws --input <CE JSON> [--output <경로>] [--fetched-at]`), gcp 입력은 받지 않아도 된다.
- 시험 파일 배치(단위 `tests/test_cost_*.py`, 통합 `tests/integration/test_costs_api.py`).

## 8. 하지 말 것 / 범위 밖

- 클라우드 청구·가격 API를 서버·시험·스크립트에서 부르기(네트워크 0), 자동 인벤터리(클러스터·클라우드 조회로 자원 목록 만들기)
- 마이그레이션·DB 표, PUT/POST 쓰기 API, 새 의존성·환경 변수
- 다른 세션 파일(보안 매트릭스·레지던시 작업의 서비스·스키마) 수정. 공용 파일(`app/main.py`, `app/enums.py`, 역할 표 시험, CLAUDE.md)은 줄 추가만
- 프론트·인프라 저장소 수정

## 9. 감수 위험

- 단가는 11/5 목록가 고정이라 가격이 바뀌면 파일을 고쳐야 한다(확인일 칸으로 보인다). 서울 지역 전달 규칙 단가 미확인(`priceVerified = false`).
- 자원 수가 바뀌면(노드 3대로 늘어남 등) 추정이 틀린다 — 자동 인벤터리를 하지 않기로 했다.
- BILLING은 배포해야 갱신된다. AWS 값은 계정 전체·크레딧 상계라 환경 비용과 바로 비교할 수 없다(응답 note로 밝힘).
- 같은 kind·region 환경이 조직에 둘이면 같은 추정이 두 번 나온다(시연 데이터에는 없음).

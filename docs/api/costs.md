# 클라우드 비용 API (운영 1)

조직의 AWS·GCP 대여 비용을 **지금은 사용량 × 단가 추정(`ESTIMATE`), 확정은 다음 날 청구 데이터(`BILLING`)** 로 나눠 보여 준다. 서버는 데이터 파일과 조직의 환경 목록으로 계산만 하고 외부를 부르지 않는다. 표·쓰기 API·마이그레이션이 없고, 청구 값은 관리자 스크립트로 파일에 넣어 다음 배포로 싣는다.

- 권한: 뷰어 이상(에이전트 토큰 가능). 로그인 없음 401, 다른 조직 404 `ORGANIZATION_NOT_FOUND`. 사람의 읽기는 감사 줄을 쓰지 않고, 에이전트 읽기는 기존 미들웨어가 기록한다.
- 금액은 모두 USD 이고 JSON 숫자다. 모든 칸은 항상 있고 값이 없으면 null 이다.

## ESTIMATE 와 BILLING

| | ESTIMATE | BILLING |
|---|---|---|
| 값 | 자원 목록 × 공개 목록가 | 클라우드 청구 데이터(일별 사용·크레딧·순 금액) |
| 단위 | 환경 하나 또는 공용 자원 하나 | **공급자 계정 전체** |
| 위치 | `environments[]`·`shared[]` (`source` 는 늘 `ESTIMATE`) | `billing[]` (`source` 는 늘 `BILLING`) |
| 갱신 | 요청 때 계산, 파일이 바뀌면 배포 | 스크립트로 파일 병합 → 배포 |

**청구 값을 환경 값으로 쓰지 않는다.** AWS 값은 계정 전체(aws-tokyo + 서울 온프레 시연 EC2 + ECR 등)이고 비용 배분 태그가 없어 환경별로 나눌 수 없다.

목록가 추정이 실측보다 클 수 있다. 2026-11-04 기준 aws-tokyo 추정은 11.75/일, 계정 전체 실측 사용 금액은 6.74/일이다. 무료 플랜이 `m7i-flex.large` 시간을 0으로 처리한 것으로 보이지만 확인하지 못했다. 이 차이를 숨기지 않고 각 항목의 `note` 로 보여 준다.

## GET `/api/v1/orgs/{orgId}/costs`

200 `ApiResponse[CloudCostsResponse]`.

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
        "note": "목록가 추정. 데이터 전송·LB 용량 단위·NAT 처리량 제외. 계정이 무료 플랜이라 실제 사용 금액은 더 작을 수 있음(청구 실측 참고)",
        "components": [
          {
            "name": "EC2 노드 m7i-flex.large (온디맨드)",
            "kind": "COMPUTE",
            "quantity": 2,
            "unit": "대",
            "unitPriceUsd": 0.12369,
            "priceUnit": "hour",
            "hourlyUsd": 0.2474,
            "priceVerified": true,
            "priceSource": "AWS Price List API AmazonEC2 APN1-BoxUsage:m7i-flex.large (Linux, Shared) · https://aws.amazon.com/ec2/pricing/on-demand/",
            "priceCheckedAt": "2026-11-05",
            "note": null
          }
        ]
      }
    ],
    "shared": [
      {
        "key": "gcp-platform",
        "name": "공용 플랫폼 (고정 진입점·관제 DB·AI 샌드박스)",
        "provider": "gcp",
        "region": "asia-northeast3",
        "source": "ESTIMATE",
        "hourlyUsd": 0.1846,
        "dailyUsd": 4.43,
        "asOf": "2026-11-05T06:00:00Z",
        "note": "특정 환경에 속하지 않는 GCP 공용 자원",
        "components": []
      }
    ],
    "excluded": [
      {"environmentId": "0c415986-…", "environmentName": "onprem", "kind": "onprem", "note": "온프레: 자체 장비, 청구 없음"}
    ],
    "billing": [
      {
        "provider": "aws",
        "source": "BILLING",
        "available": true,
        "scope": "ACCOUNT",
        "label": "AWS 계정 (도쿄 aws-tokyo + 서울 온프레 시연 EC2 등 계정 전체)",
        "fetchedAt": "2026-11-05T05:50:00Z",
        "note": "크레딧이 사용 금액을 상계해 실제 청구는 0. 환경별로 나누지 못함(비용 배분 태그 없음).",
        "days": [
          {"date": "2026-11-04", "usageUsd": 6.7374, "creditsUsd": -6.7374, "netUsd": 0.0, "estimated": true}
        ]
      },
      {
        "provider": "gcp",
        "source": "BILLING",
        "available": false,
        "scope": "ACCOUNT",
        "label": "GCP 프로젝트 team-echo-final",
        "fetchedAt": null,
        "note": "BigQuery 청구 내보내기 없음 — 추정만 보여 준다.",
        "days": []
      }
    ]
  }
}
```

### 칸 규칙

| 칸 | 규칙 |
|---|---|
| `asOf` | 주입한 시계의 지금(UTC). 최상위와 각 항목이 같은 값이다 |
| `environments[]` | 조직의 살아 있는 aws·gcp 환경, 환경 `createdAt`·`id` 순. `provider` 는 환경 `kind` 문자열이다 |
| `shared[]` | 공용 자원. 조직의 aws·gcp 환경 중 **하나라도 자원 목록에 맞았을 때만** 보이고 합계에 들어간다(개인 조직에 플랫폼 비용이 보이지 않게) |
| `excluded[]` | 비용 표에서 뺀 환경. `kind = onprem` 이면 note `온프레: 자체 장비, 청구 없음`, aws·gcp 밖의 다른 종류면 `청구를 계산하지 않는 종류` |
| `components[].kind` | `COMPUTE` · `DATABASE` · `NETWORK` · `STORAGE` · `OTHER` (`CostComponentKind`) |
| `components[].unit` · `priceUnit` | `unit` 은 수량의 단위(대·GiB·개), `priceUnit` 은 단가의 단위(`hour`·`GiB-month`) |
| `components[].note` | 그 단가의 메모(무료 크레딧·미확인 등). 없으면 null |
| `components[].priceVerified` | false 면 우리 리전의 값을 확인하지 못한 단가다 |
| `billing[].available` | `days` 가 비어 있지 않음 |

### 제외·없는 값

- **자원 목록이 안 맞는 aws·gcp 환경**(예 `us-east-1`): `environments[]` 에 그대로 나오되 `hourlyUsd`·`dailyUsd` 는 null, `components` 는 `[]`, note `자원 목록 없음 — 추정하지 않음`. 합계에서 빠진다. 전부 안 맞으면 `shared` 도 없다.
- **환경이 하나도 없으면** `environments = []`, `shared = []`, 합계 0, `billing` 은 그대로다.
- 추정에 넣지 않는 것: 데이터 전송, LB 용량 단위, NAT 처리량, 서울 온프레 시연 EC2.
- 같은 kind·region 환경이 조직에 둘이면 같은 추정이 두 번 나온다.

### 계산

- `hour` 항목은 `수량 × 단가`, `GiB-month` 항목은 `수량 × 단가 ÷ 730` 이 시간 금액이다. 계산은 `Decimal`, 반올림은 사사오입(`ROUND_HALF_UP`)이다.
- 응답 `hourlyUsd` 는 소수 4자리, `dailyUsd` 는 정밀 시간 금액 × 24 를 소수 2자리로 반올림한 값이다.
- 합계는 **정밀 값의 합**에서 반올림한다. 표시 값을 더한 값과 끝자리가 다를 수 있다.

## 데이터 파일 (`app/data/`)

| 파일 | 누가 쓰나 | 내용 |
|---|---|---|
| `cloud_prices.toml` | 사람 | 단가 키마다 `unit_price_usd`·`unit`·`description`·`source`·`effective_date`·`checked_at`·`verified`·`note`(선택) |
| `cloud_inventory.toml` | 사람 | 환경별(`environments`, `match_kind`·`match_region` 으로 맞춤)·공용(`shared`) 자원 목록. 자동 인벤터리는 없다 |
| `cloud_billing.json` | 스크립트 | 공급자별 일별 청구 실측 |

프로세스당 한 번 읽어 검사한다(`get_cost_catalog`, 첫 요청 때). 없는 단가 키, `hour`·`GiB-month` 밖의 단위, 음수 단가·수량, 소수 수량, 중복 키, 모르는 칸, 자원 없는 목록은 `CostCatalogError`(500 `COST_CATALOG_INVALID`)다. 잘못된 표가 조용히 0원이 되지 않게 하려는 것이다. 시험은 이 의존성을 작은 카탈로그로 바꾼다.

자원 수·단가가 바뀌면 파일을 고치고 `checked_at` 을 바꿔 배포한다. 자동으로 따라가지 않는다.

## 청구 값 갱신

운영자가 받은 Cost Explorer 응답을 스크립트로 병합한다. 스크립트는 네트워크·DB·환경 변수를 쓰지 않는다. Cost Explorer 는 요청당 $0.01 이고, 운영 서버(GKE)의 AWS 역할에는 `ce:GetCostAndUsage` 권한이 없어 서버가 직접 부르지 않는다.

```bash
aws ce get-cost-and-usage \
  --time-period Start=2026-11-04,End=2026-11-05 \
  --granularity DAILY --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=RECORD_TYPE > ce.json
uv run python -X utf8 -m app.scripts.import_billing --provider aws --input ce.json --fetched-at 2026-11-05T05:50:00Z
```

- 인자: `--provider aws`(지금은 aws 만), `--input`, `--output`(기본 `app/data/cloud_billing.json`), `--fetched-at`(ISO 8601, 시간대 필수. 비우면 지금).
- 입력은 `RECORD_TYPE` 하나로 묶은 DAILY·USD 응답이어야 한다. 아니면 파일을 건드리지 않고 종료 코드 1 이다.
- `usageUsd` 는 `Usage`, `creditsUsd` 는 `Credit` 레코드의 `UnblendedCost`, `netUsd` 는 **모든 레코드 종류의 정밀 합**(세금 등 포함)이다. 셋 다 소수 4자리다. `estimated` 는 응답의 `Estimated` 다.
- 같은 날짜는 덮어쓰고 다른 날짜는 유지하며 날짜순으로 저장한다. `label`·`note` 는 손으로 고친 값을 유지한다. 파일이 없으면 두 공급자 기본 항목으로 만든다.
- 파일을 커밋해 배포해야 응답에 실린다. GCP 는 BigQuery 청구 내보내기가 없어 항목이 비어 있다.

## 단가 출처

확인일은 모두 2026-11-05 다. 값의 원본은 `app/data/cloud_prices.toml` 이다.

| 키 | 단가(USD) | 단위 | 출처 | 유효일 | 확인 |
|---|---|---|---|---|---|
| `aws.ap-northeast-1.eks-cluster` | 0.10 | hour | AWS Price List API AmazonEKS `APN1-AmazonEKS-Hours:perCluster` · https://aws.amazon.com/eks/pricing/ | 2026-10-01 | 확인 |
| `aws.ap-northeast-1.ec2.m7i-flex.large` | 0.12369 | hour | AWS Price List API AmazonEC2 `APN1-BoxUsage:m7i-flex.large` (Linux, Shared) · https://aws.amazon.com/ec2/pricing/on-demand/ | 2026-11-01 | 확인 |
| `aws.ap-northeast-1.ebs.gp3` | 0.096 | GiB-month | `APN1-EBS:VolumeUsage.gp3` · https://aws.amazon.com/ebs/pricing/ | 2026-11-01 | 확인 |
| `aws.ap-northeast-1.nat-gateway` | 0.062 | hour | `APN1-NatGateway-Hours` · https://aws.amazon.com/vpc/pricing/ (처리량 $0.062/GB 별도) | 2026-11-01 | 확인 |
| `aws.ap-northeast-1.alb` | 0.0243 | hour | AWSELB `APN1-LoadBalancerUsage` Application · https://aws.amazon.com/elasticloadbalancing/pricing/ (LCU $0.008 별도) | 2026-08-01 | 확인 |
| `aws.ap-northeast-1.nlb` | 0.0243 | hour | AWSELB `APN1-LoadBalancerUsage` Network (NLCU $0.006 별도) | 2026-08-01 | 확인 |
| `aws.ap-northeast-1.public-ipv4` | 0.005 | hour | AmazonVPC `APN1-PublicIPv4:InUseAddress` · https://aws.amazon.com/vpc/pricing/ | 2026-11-01 | 확인 |
| `gcp.gke-zonal-cluster` | 0.10 | hour | Cloud Billing Catalog `CCD8-9BF1-090E` "Zonal Kubernetes Clusters" · https://cloud.google.com/kubernetes-engine/pricing (결제 계정당 월 $74.40 무료 크레딧 — 존 클러스터 1개는 사실상 0) | 2026-11-04 | 확인 |
| `gcp.asia-northeast3.e2-standard-2` | 0.085964 | hour | Catalog `6F81-5844-456A` "E2 Instance Core running in Seoul" 0.028026/vCPU·h ×2 + "E2 Instance Ram running in Seoul" 0.003739/GiB·h ×8 | 2026-11-04 | 확인 |
| `gcp.asia-northeast3.e2-small` | 0.021491 | hour | 같은 SKU, 0.5 vCPU(공유 코어) + 2 GiB | 2026-11-04 | 확인 |
| `gcp.asia-northeast3.pd-balanced` | 0.13 | GiB-month | Catalog "Balanced PD Capacity in Seoul" | 2026-11-04 | 확인 |
| `gcp.external-ip-standard-vm` | 0.005 | hour | Catalog "External IP Charge on a Standard VM" (SKU 계층상 결제 계정 월 720시간까지 0) | 2026-11-04 | 확인 |
| `gcp.cloud-nat-vm` | 0.0014 | hour | https://cloud.google.com/nat/pricing — VM 1대·시간(32대 이하), 처리량 $0.045/GiB 별도 | 2026-11-05 | 확인 |
| `gcp.cloud-nat-ip` | 0.005 | hour | https://cloud.google.com/nat/pricing — NAT 외부 IP | 2026-11-05 | 확인 |
| `gcp.forwarding-rules-first-5` | 0.025 | hour | https://cloud.google.com/vpc/network-pricing — 첫 5개 묶음 값(전역 표·미국 지역 표). **서울 지역 표 값 미확인** | 2026-11-05 | **미확인** |

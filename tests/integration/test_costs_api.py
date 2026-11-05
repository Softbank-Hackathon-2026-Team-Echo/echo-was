"""클라우드 비용 API 통합 시험(운영 1 AC-3~AC-5)."""

import datetime
from decimal import Decimal
from typing import Any

from httpx import Response
from sqlalchemy import select

from app.enums import ActorType, CostComponentKind, OrganizationRole
from app.main import app
from app.models import AuditLog, Environment
from app.services.cost_catalog import (
    BillingDay,
    BillingProvider,
    CloudPrice,
    CostCatalog,
    CostComponent,
    EnvironmentInventory,
    SharedInventory,
    get_cost_catalog,
)
from tests.integration.auth_support import (
    create_agent_token,
    create_member,
    create_organization,
    make_client,
    org_path,
)
from tests.integration.deploy_harness import DeployHarness
from tests.integration.disaster_support import (
    add_located_environment,
    aws_config,
    gcp_config,
    onprem_config,
)

ONPREM_NOTE = "온프레: 자체 장비, 청구 없음"


async def _add_tokyo(harness: DeployHarness, name: str = "aws-tokyo") -> Environment:
    return await add_located_environment(harness, name, "aws", aws_config("ap-northeast-1"))


async def _add_seoul(harness: DeployHarness) -> Environment:
    return await add_located_environment(harness, "gcp-seoul", "gcp", gcp_config("asia-northeast3"))


async def _add_onprem(harness: DeployHarness) -> Environment:
    return await add_located_environment(harness, "onprem", "onprem", onprem_config("lab"))


async def _get_costs(harness: DeployHarness) -> dict[str, Any]:
    response = await harness.client.get(org_path(harness.organization.id, "/costs"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    data: dict[str, Any] = body["data"]
    return data


def _small_catalog() -> CostCatalog:
    """정밀 값이 계산하기 쉬운 표: 환경 1.5/시간, 공용 0.25/시간(73 GiB × 2.5 ÷ 730)."""
    today = datetime.date(2026, 11, 5)
    return CostCatalog(
        prices={
            "node": CloudPrice(
                key="node",
                unit_price_usd=Decimal("0.75"),
                unit="hour",
                description="노드",
                source="노드 출처",
                effective_date=today,
                checked_at=today,
                verified=True,
            ),
            "disk": CloudPrice(
                key="disk",
                unit_price_usd=Decimal("2.5"),
                unit="GiB-month",
                description="디스크",
                source="디스크 출처",
                effective_date=today,
                checked_at=today,
                verified=False,
                note="확인 못 함",
            ),
        },
        environments=(
            EnvironmentInventory(
                key="tokyo",
                name="tokyo",
                provider="aws",
                region="ap-northeast-1",
                note="작은 표 메모",
                components=(
                    CostComponent(
                        name="노드",
                        kind=CostComponentKind.COMPUTE,
                        price="node",
                        quantity=2,
                        unit_label="대",
                    ),
                ),
                match_kind="aws",
                match_region="ap-northeast-1",
            ),
        ),
        shared=(
            SharedInventory(
                key="platform",
                name="공용",
                provider="gcp",
                region="asia-northeast3",
                note="공용 메모",
                components=(
                    CostComponent(
                        name="디스크",
                        kind=CostComponentKind.STORAGE,
                        price="disk",
                        quantity=73,
                        unit_label="GiB",
                    ),
                ),
            ),
        ),
        billing=(
            BillingProvider(
                provider="aws",
                scope="ACCOUNT",
                label="계정",
                source="시험",
                fetched_at=datetime.datetime(2026, 11, 5, 5, 50, tzinfo=datetime.UTC),
                note="청구 메모",
                days=(
                    BillingDay(
                        date=datetime.date(2026, 11, 4),
                        usage_usd=1.5,
                        credits_usd=-1.0,
                        net_usd=0.5,
                        estimated=False,
                    ),
                ),
            ),
        ),
    )


# --- AC-3 ---


async def test_get_costs_with_three_kinds_returns_estimates_shared_excluded_billing(
    harness: DeployHarness,
) -> None:
    # given
    tokyo = await _add_tokyo(harness)
    harness.clock.advance(1)
    seoul = await _add_seoul(harness)
    onprem = await _add_onprem(harness)

    # when
    data = await _get_costs(harness)

    # then: 시각·통화
    assert datetime.datetime.fromisoformat(data["asOf"]) == harness.clock.now()
    assert data["currency"] == "USD"

    # then: 환경 항목(ESTIMATE)
    by_name = {item["environmentName"]: item for item in data["environments"]}
    assert set(by_name) == {"aws-tokyo", "gcp-seoul"}
    aws = by_name["aws-tokyo"]
    assert (aws["environmentId"], aws["provider"], aws["region"], aws["source"]) == (
        str(tokyo.id),
        "aws",
        "ap-northeast-1",
        "ESTIMATE",
    )
    assert (aws["hourlyUsd"], aws["dailyUsd"]) == (0.4896, 11.75)
    assert aws["asOf"] == data["asOf"]
    assert aws["note"].startswith("목록가 추정.")
    gcp = by_name["gcp-seoul"]
    assert (gcp["environmentId"], gcp["provider"], gcp["region"], gcp["source"]) == (
        str(seoul.id),
        "gcp",
        "asia-northeast3",
        "ESTIMATE",
    )
    assert (gcp["hourlyUsd"], gcp["dailyUsd"]) == (0.3243, 7.78)

    # then: 공용 항목
    assert len(data["shared"]) == 1
    shared = data["shared"][0]
    assert (shared["key"], shared["provider"], shared["source"]) == (
        "gcp-platform",
        "gcp",
        "ESTIMATE",
    )
    assert (shared["hourlyUsd"], shared["dailyUsd"]) == (0.1846, 4.43)
    assert len(shared["components"]) == 8
    assert shared["asOf"] == data["asOf"]

    # then: 온프레는 비용 표 밖
    assert data["excluded"] == [
        {
            "environmentId": str(onprem.id),
            "environmentName": "onprem",
            "kind": "onprem",
            "note": ONPREM_NOTE,
        }
    ]

    # then: 청구 실측은 계정 단위 블록에만 있다.
    billing = {entry["provider"]: entry for entry in data["billing"]}
    assert billing["aws"]["source"] == "BILLING"
    assert billing["aws"]["available"] is True
    assert billing["aws"]["scope"] == "ACCOUNT"
    assert billing["aws"]["fetchedAt"] == "2026-11-05T05:50:00Z"
    assert billing["aws"]["days"] == [
        {
            "date": "2026-11-04",
            "usageUsd": 6.7374,
            "creditsUsd": -6.7374,
            "netUsd": 0.0,
            "estimated": True,
        }
    ]
    assert billing["gcp"]["source"] == "BILLING"
    assert billing["gcp"]["available"] is False
    assert (billing["gcp"]["fetchedAt"], billing["gcp"]["days"]) == (None, [])

    # then: 합계 = 항목 합(SPEC 계산 값. 이 표에서는 표시 값을 더한 값과도 같다).
    assert (data["totalHourlyUsd"], data["totalDailyUsd"]) == (0.9985, 23.96)
    items = (aws, gcp, shared)
    hourly_sum = sum(Decimal(str(item["hourlyUsd"])) for item in items)
    daily_sum = sum(Decimal(str(item["dailyUsd"])) for item in items)
    assert hourly_sum == Decimal(str(data["totalHourlyUsd"]))
    assert daily_sum == Decimal(str(data["totalDailyUsd"]))


async def test_get_costs_component_has_every_field_as_camel_case(harness: DeployHarness) -> None:
    await _add_tokyo(harness)
    await _add_seoul(harness)

    data = await _get_costs(harness)

    by_name = {item["environmentName"]: item for item in data["environments"]}
    ec2 = next(c for c in by_name["aws-tokyo"]["components"] if c["name"].startswith("EC2 노드"))
    source = ec2.pop("priceSource")
    assert source.startswith("AWS Price List API AmazonEC2 APN1-BoxUsage:m7i-flex.large")
    assert ec2 == {
        "name": "EC2 노드 m7i-flex.large (온디맨드)",
        "kind": "COMPUTE",
        "quantity": 2,
        "unit": "대",
        "unitPriceUsd": 0.12369,
        "priceUnit": "hour",
        "hourlyUsd": 0.2474,
        "priceVerified": True,
        "priceCheckedAt": "2026-11-05",
        "note": None,
    }
    rule = next(
        c for c in by_name["gcp-seoul"]["components"] if c["name"].startswith("지역 전달 규칙")
    )
    assert (rule["priceVerified"], rule["note"]) == (False, "서울 지역 표 값 미확인")


async def test_get_costs_lists_oldest_environment_first_and_skips_deleted(
    harness: DeployHarness,
) -> None:
    # given: created_at 은 DB 트랜잭션 시각이라 만든 순서가 곧 정렬 순서다.
    await _add_seoul(harness)
    await _add_tokyo(harness)
    gone = await _add_tokyo(harness, "aws-gone")
    async with harness.session_factory() as session, session.begin():
        row = await session.get(Environment, gone.id)
        assert row is not None
        row.mark_as_deleted(harness.clock.now())

    # when
    data = await _get_costs(harness)

    # then
    assert [item["environmentName"] for item in data["environments"]] == ["gcp-seoul", "aws-tokyo"]


async def test_get_costs_uses_injected_catalog(harness: DeployHarness) -> None:
    # given: 시험은 의존성 교체로 작은 카탈로그를 넣는다(harness 가 끝날 때 교체를 지운다).
    app.dependency_overrides[get_cost_catalog] = _small_catalog
    await _add_tokyo(harness)

    # when
    data = await _get_costs(harness)

    # then
    environment = data["environments"][0]
    assert (environment["hourlyUsd"], environment["dailyUsd"], environment["note"]) == (
        1.5,
        36.0,
        "작은 표 메모",
    )
    shared = data["shared"][0]
    assert (shared["key"], shared["hourlyUsd"], shared["dailyUsd"]) == ("platform", 0.25, 6.0)
    assert shared["components"][0]["priceUnit"] == "GiB-month"
    assert (data["totalHourlyUsd"], data["totalDailyUsd"]) == (1.75, 42.0)
    assert data["billing"][0]["days"][0]["netUsd"] == 0.5


# --- AC-4 ---


async def test_get_costs_unmatched_environment_is_null_and_left_out_of_totals(
    harness: DeployHarness,
) -> None:
    # given: 목록이 맞는 환경 + 안 맞는 환경
    await _add_tokyo(harness)
    await add_located_environment(harness, "aws-virginia", "aws", aws_config("us-east-1"))

    # when
    data = await _get_costs(harness)

    # then
    by_name = {item["environmentName"]: item for item in data["environments"]}
    unmatched = by_name["aws-virginia"]
    assert (unmatched["region"], unmatched["source"]) == ("us-east-1", "ESTIMATE")
    assert (unmatched["hourlyUsd"], unmatched["dailyUsd"]) == (None, None)
    assert unmatched["components"] == []
    assert unmatched["note"] == "자원 목록 없음 — 추정하지 않음"
    assert by_name["aws-tokyo"]["hourlyUsd"] == 0.4896
    assert len(data["shared"]) == 1
    # 합계는 aws-tokyo + 공용 정밀 값이다(0.48955534 + 0.18459257 = 0.67414791).
    assert (data["totalHourlyUsd"], data["totalDailyUsd"]) == (0.6741, 16.18)


async def test_get_costs_all_unmatched_has_no_shared_and_zero_total(
    harness: DeployHarness,
) -> None:
    await add_located_environment(harness, "aws-virginia", "aws", aws_config("us-east-1"))
    await add_located_environment(harness, "gcp-iowa", "gcp", gcp_config("us-central1"))

    data = await _get_costs(harness)

    assert [item["hourlyUsd"] for item in data["environments"]] == [None, None]
    assert data["shared"] == []
    assert (data["totalHourlyUsd"], data["totalDailyUsd"]) == (0.0, 0.0)


async def test_get_costs_only_onprem_has_no_shared(harness: DeployHarness) -> None:
    await _add_onprem(harness)

    data = await _get_costs(harness)

    assert data["environments"] == []
    assert data["shared"] == []
    assert [item["note"] for item in data["excluded"]] == [ONPREM_NOTE]
    assert (data["totalHourlyUsd"], data["totalDailyUsd"]) == (0.0, 0.0)


async def test_get_costs_without_environments_still_shows_billing(harness: DeployHarness) -> None:
    data = await _get_costs(harness)

    assert (data["environments"], data["shared"], data["excluded"]) == ([], [], [])
    assert (data["totalHourlyUsd"], data["totalDailyUsd"]) == (0.0, 0.0)
    assert [entry["provider"] for entry in data["billing"]] == ["aws", "gcp"]


# --- AC-5 ---


async def _audit_rows(harness: DeployHarness) -> list[AuditLog]:
    async with harness.session_factory() as session:
        return list((await session.scalars(select(AuditLog))).all())


def _assert_organization_not_found(response: Response) -> None:
    assert response.status_code == 404
    assert response.json()["code"] == "ORGANIZATION_NOT_FOUND"


async def test_get_costs_access_rules(harness: DeployHarness) -> None:
    # given
    await _add_tokyo(harness)
    now = harness.clock.now()
    path = org_path(harness.organization.id, "/costs")
    other_organization = await create_organization(harness.session_factory, "other-org")
    outsider = await create_member(
        harness.session_factory, other_organization, OrganizationRole.ADMIN, now
    )
    viewer = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.VIEWER, now
    )
    _, agent_token = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, now
    )

    # when / then: 로그인 없음 401, 다른 조직 404
    async with make_client() as anonymous_client:
        assert (await anonymous_client.get(path)).status_code == 401
    async with make_client(session_token=outsider.session_token) as outsider_client:
        _assert_organization_not_found(await outsider_client.get(path))

    # when / then: 뷰어 200, 사람의 읽기는 감사 줄을 쓰지 않는다.
    async with make_client(session_token=viewer.session_token) as viewer_client:
        viewer_response = await viewer_client.get(path)
    assert viewer_response.status_code == 200
    assert await _audit_rows(harness) == []

    # when / then: 에이전트 200, 읽기 줄은 기존 미들웨어가 쓴다.
    async with make_client(agent_token=agent_token) as agent_client:
        agent_response = await agent_client.get(path)
    assert agent_response.status_code == 200
    assert agent_response.json()["data"] == viewer_response.json()["data"]
    assert [row.actor_type for row in await _audit_rows(harness)] == [ActorType.AGENT]


async def test_get_costs_does_not_count_other_organizations_environments(
    harness: DeployHarness,
) -> None:
    # given: 다른 조직의 aws-tokyo 는 이 조직 응답에 없다.
    other_organization = await create_organization(harness.session_factory, "other-org")
    async with harness.session_factory() as session, session.begin():
        session.add(
            Environment(
                organization_id=other_organization.id,
                name="aws-tokyo",
                kind="aws",
                config=dict(aws_config("ap-northeast-1")),
                secret_ref="env:ECHO_SECRET_OTHER_ORG__AWS_TOKYO",
            )
        )

    # when
    data = await _get_costs(harness)

    # then
    assert (data["environments"], data["shared"]) == ([], [])
    assert data["totalHourlyUsd"] == 0.0

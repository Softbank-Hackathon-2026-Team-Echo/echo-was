"""공통 모델의 DB 제약과 시나리오(AC-4~AC-7)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.models import Deployment, Environment, Organization, Service, ServiceEnvironment
from tests.integration.factories import (
    SHOP_API_DIGEST,
    ShopApiExample,
    add_succeeded_deployment,
    make_digest,
    make_environment,
    make_link,
    make_service,
    seed_shop_api_example,
)

SessionFactory = async_sessionmaker[AsyncSession]


@pytest.fixture
async def shop_api(session_factory: SessionFactory) -> ShopApiExample:
    async with session_factory() as session, session.begin():
        return await seed_shop_api_example(session)


async def test_live_links_one_service_two_environments_share_same_digest(
    session_factory: SessionFactory, shop_api: ShopApiExample
) -> None:
    async with session_factory() as session:
        links = (
            await session.scalars(
                select(ServiceEnvironment)
                .where(
                    ServiceEnvironment.service_id == shop_api.service.id,
                    ServiceEnvironment.is_deleted.is_(False),
                )
                .options(
                    selectinload(ServiceEnvironment.current_deployment),
                    selectinload(ServiceEnvironment.environment),
                )
            )
        ).all()

    weight_by_environment = {link.environment.name: link.traffic_weight for link in links}
    digests = {link.current_deployment.image_digest for link in links if link.current_deployment}
    assert len(links) == 2
    assert weight_by_environment == {"aws-seoul": 90, "gcp-seoul": 10}
    assert digests == {SHOP_API_DIGEST}


async def test_environment_list_shows_three_kinds_in_same_columns(
    session_factory: SessionFactory, shop_api: ShopApiExample
) -> None:
    async with session_factory() as session:
        environments = (await session.scalars(select(Environment).order_by(Environment.name))).all()

    rows = [(env.name, env.kind, env.parsed_config().display_location()) for env in environments]
    assert rows == [
        ("aws-seoul", "aws", "ap-northeast-2"),
        ("gcp-seoul", "gcp", "asia-northeast3"),
        ("onprem-k3s", "onprem", "team-laptop"),
    ]


async def test_live_link_duplicate_rejected_and_allowed_after_soft_delete(
    session_factory: SessionFactory, shop_api: ShopApiExample
) -> None:
    # 주소 유일성이 아니라 (서비스, 환경) 유일성을 보려고 주소는 다르게 둔다.
    duplicate = ServiceEnvironment(
        service_id=shop_api.service.id,
        environment_id=shop_api.environments["aws-seoul"].id,
        public_host="shop-api.duplicate.apps.echo.test",
    )
    async with session_factory() as session:
        session.add(duplicate)
        with pytest.raises(IntegrityError, match="uq_service_environments_service_id_environ"):
            await session.flush()

    async with session_factory() as session, session.begin():
        link = await session.get_one(ServiceEnvironment, shop_api.aws_link.id)
        link.current_deployment_id = None
        link.mark_as_deleted(datetime.now(UTC))
        session.add(make_link(shop_api.service, shop_api.environments["aws-seoul"]))

    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ServiceEnvironment)
            .where(ServiceEnvironment.environment_id == shop_api.environments["aws-seoul"].id)
        )
    assert count == 2


@pytest.mark.parametrize(
    ("model", "index_name"),
    [(Service, "uq_services_name_live"), (Environment, "uq_environments_org_name_live")],
)
async def test_name_duplicate_rejected_and_allowed_after_soft_delete(
    session_factory: SessionFactory,
    organization: Organization,
    model: type[Service | Environment],
    index_name: str,
) -> None:
    def make() -> Service | Environment:
        if model is Service:
            return make_service("shop-api", organization)
        return make_environment("aws-seoul", organization)

    async with session_factory() as session, session.begin():
        original = make()
        session.add(original)

    async with session_factory() as session:
        session.add(make())
        with pytest.raises(IntegrityError, match=index_name):
            await session.flush()

    async with session_factory() as session, session.begin():
        stored = await session.get_one(model, original.id)
        stored.mark_as_deleted(datetime.now(UTC))
        session.add(make())

    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(model))
    assert count == 2


async def test_current_deployment_of_other_link_rejected(
    session_factory: SessionFactory, shop_api: ShopApiExample
) -> None:
    async with session_factory() as session:
        link = await session.get_one(ServiceEnvironment, shop_api.aws_link.id)
        link.current_deployment_id = shop_api.gcp_deployment.id
        with pytest.raises(
            IntegrityError, match="fk_service_environments_current_deployment_id_id_deployments"
        ):
            await session.flush()


async def test_current_deployment_of_same_link_allowed_and_link_id_unchanged(
    session_factory: SessionFactory, shop_api: ShopApiExample
) -> None:
    link_id = shop_api.aws_link.id
    async with session_factory() as session, session.begin():
        link = await session.get_one(ServiceEnvironment, link_id)
        new_deployment = await add_succeeded_deployment(session, link, make_digest("a1"))
        link.current_deployment_id = new_deployment.id
        await session.flush()
        assert link.id == link_id

    async with session_factory() as session:
        stored = await session.scalar(
            select(ServiceEnvironment)
            .where(ServiceEnvironment.id == link_id)
            .options(selectinload(ServiceEnvironment.current_deployment))
        )
        link_count = await session.scalar(select(func.count()).select_from(ServiceEnvironment))

    assert stored is not None
    assert stored.current_deployment is not None
    assert stored.current_deployment.id == new_deployment.id
    assert stored.current_deployment.image_digest == make_digest("a1")
    assert link_count == 2


async def test_rollback_deployment_points_to_original_and_copies_image(
    session_factory: SessionFactory, shop_api: ShopApiExample
) -> None:
    original = shop_api.aws_deployment
    async with session_factory() as session, session.begin():
        rollback = Deployment(
            service_environment_id=original.service_environment_id,
            image_repository=original.image_repository,
            image_digest=original.image_digest,
            config_snapshot=original.config_snapshot,
            rollback_of_id=original.id,
        )
        session.add(rollback)

    async with session_factory() as session:
        stored = await session.scalar(
            select(Deployment)
            .where(Deployment.id == rollback.id)
            .options(selectinload(Deployment.rollback_of))
        )

    assert stored is not None
    assert stored.status == "PENDING"
    assert stored.rollback_of is not None
    assert stored.rollback_of.id == original.id


async def test_updated_at_changes_on_orm_update(
    session_factory: SessionFactory, shop_api: ShopApiExample
) -> None:
    created_at = shop_api.service.created_at
    await asyncio.sleep(0.01)
    async with session_factory() as session, session.begin():
        service = await session.get_one(Service, shop_api.service.id)
        service.description = "바뀐 설명"

    assert service.updated_at > created_at
    assert service.created_at == created_at


Mutation = Callable[[AsyncSession, ShopApiExample], Awaitable[object]]


async def _set_link(session: AsyncSession, example: ShopApiExample, **values: object) -> None:
    link = await session.get_one(ServiceEnvironment, example.aws_link.id)
    for key, value in values.items():
        setattr(link, key, value)
    await session.flush()


async def _add_deployment(session: AsyncSession, example: ShopApiExample, **values: object) -> None:
    defaults: dict[str, object] = {
        "service_environment_id": example.aws_link.id,
        "image_repository": "ghcr.io/team-echo/shop-api",
        "image_digest": SHOP_API_DIGEST,
    }
    deployment = Deployment(**(defaults | values))
    session.add(deployment)
    await session.flush()


async def _point_rollback_to_self(session: AsyncSession, example: ShopApiExample) -> None:
    deployment_id = uuid.uuid4()
    await _add_deployment(session, example, id=deployment_id, rollback_of_id=deployment_id)


async def _set_environment_config_to_array(session: AsyncSession, example: ShopApiExample) -> None:
    # ORM 경로는 설정 양식 검사가 먼저 막으므로, DB 마지막 방어선을 SQL 로 직접 확인한다.
    await session.execute(
        text("UPDATE environments SET config = '[]'::jsonb WHERE id = :id"),
        {"id": example.environments["aws-seoul"].id},
    )


async def _rename_service(session: AsyncSession, example: ShopApiExample) -> None:
    service = await session.get_one(Service, example.service.id)
    service.name = "Shop_API"
    await session.flush()


async def _soft_delete_without_timestamp(session: AsyncSession, example: ShopApiExample) -> None:
    service = await session.get_one(Service, example.service.id)
    service.is_deleted = True
    await session.flush()


async def _delete_deployment_sql(session: AsyncSession, example: ShopApiExample) -> None:
    await session.execute(
        text("DELETE FROM deployments WHERE id = :id"), {"id": example.aws_deployment.id}
    )


async def _delete_deployment_orm(session: AsyncSession, example: ShopApiExample) -> None:
    link = await session.get_one(ServiceEnvironment, example.aws_link.id)
    link.current_deployment_id = None
    await session.flush()
    await session.delete(await session.get_one(Deployment, example.aws_deployment.id))
    await session.flush()


REJECTED: dict[str, tuple[Mutation, str]] = {
    "traffic_weight=-1": (
        lambda s, e: _set_link(s, e, traffic_weight=-1),
        "ck_service_environments_traffic_weight_range",
    ),
    "traffic_weight=101": (
        lambda s, e: _set_link(s, e, traffic_weight=101),
        "ck_service_environments_traffic_weight_range",
    ),
    "image_digest=sha256:xyz": (
        lambda s, e: _add_deployment(s, e, image_digest="sha256:xyz"),
        "ck_deployments_image_digest_format",
    ),
    "status=DONE": (
        lambda s, e: _add_deployment(s, e, status="DONE"),
        "ck_deployments_status_valid",
    ),
    "config=[]": (_set_environment_config_to_array, "ck_environments_config_is_object"),
    "rollback_of_id=id": (_point_rollback_to_self, "ck_deployments_rollback_of_not_self"),
    "name=Shop_API": (_rename_service, "ck_services_name_format"),
    "is_deleted without deleted_at": (
        _soft_delete_without_timestamp,
        "ck_services_soft_delete_consistency",
    ),
    "DELETE deployments (SQL)": (_delete_deployment_sql, "append-only"),
    "DELETE deployments (ORM)": (_delete_deployment_orm, "append-only"),
}


@pytest.mark.parametrize("case", REJECTED)
async def test_database_rejects_invalid_values(
    session_factory: SessionFactory, shop_api: ShopApiExample, case: str
) -> None:
    mutate, constraint = REJECTED[case]
    async with session_factory() as session:
        with pytest.raises(IntegrityError, match=constraint):
            await mutate(session, shop_api)


@pytest.mark.parametrize("weight", [0, 100])
async def test_database_accepts_traffic_weight_bounds(
    session_factory: SessionFactory, shop_api: ShopApiExample, weight: int
) -> None:
    async with session_factory() as session, session.begin():
        await _set_link(session, shop_api, traffic_weight=weight)

    async with session_factory() as session:
        link = await session.get_one(ServiceEnvironment, shop_api.aws_link.id)
    assert link.traffic_weight == weight

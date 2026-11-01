"""환경 저장 전 설정 양식 검사(AC-8)와 새 환경 종류 파일 하나 추가(AC-9, NFR-15)."""

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.adapters.aws import AwsEnvironmentConfig
from app.adapters.base import InvalidEnvironmentConfigError, UnknownEnvironmentKindError
from app.adapters.registry import discover
from app.core.exceptions import InvalidInputError
from app.models import Environment
from tests.integration.factories import make_environment

SessionFactory = async_sessionmaker[AsyncSession]
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def executed_statements(engine: AsyncEngine) -> Iterator[list[str]]:
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(args[2])

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    yield statements
    event.remove(engine.sync_engine, "before_cursor_execute", record)


async def _count_environments(session_factory: SessionFactory) -> int:
    async with session_factory() as session:
        return await session.scalar(select(func.count()).select_from(Environment)) or 0


async def test_flush_aws_without_region_raises_before_insert(
    session_factory: SessionFactory, executed_statements: list[str]
) -> None:
    secret_value = "should-not-leak"
    async with session_factory() as session:
        session.add(
            Environment(name="aws-seoul", kind="aws", config={"cluster_name": secret_value})
        )
        with pytest.raises(InvalidEnvironmentConfigError) as exc_info:
            await session.flush()

    error = exc_info.value
    assert error.code == "ENVIRONMENT_CONFIG_INVALID"
    assert error.fields == {"kind": "aws", "invalid_fields": ["region"]}
    assert secret_value not in repr(error.fields)
    assert not any("INSERT INTO environments" in statement for statement in executed_statements)
    assert await _count_environments(session_factory) == 0


@pytest.mark.parametrize(
    ("kind", "config", "error_type"),
    [
        ("azure", {"location": "koreacentral"}, UnknownEnvironmentKindError),
        (
            "aws",
            {"region": "ap-northeast-2", "cluster_name": "echo-eks", "extra_key": "x"},
            InvalidEnvironmentConfigError,
        ),
    ],
)
async def test_flush_unknown_kind_or_extra_key_rejected(
    session_factory: SessionFactory,
    kind: str,
    config: dict[str, object],
    error_type: type[InvalidInputError],
) -> None:
    async with session_factory() as session:
        session.add(Environment(name="env-a", kind=kind, config=config))
        with pytest.raises(error_type):
            await session.flush()

    assert await _count_environments(session_factory) == 0


async def test_update_config_or_kind_is_validated_on_flush(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session, session.begin():
        environment = make_environment("aws-seoul")
        session.add(environment)

    async with session_factory() as session:
        stored = await session.get_one(Environment, environment.id)
        stored.config = {**stored.config, "region": "Seoul"}
        with pytest.raises(InvalidEnvironmentConfigError) as exc_info:
            await session.flush()
    assert exc_info.value.fields["invalid_fields"] == ["region"]

    async with session_factory() as session:
        stored = await session.get_one(Environment, environment.id)
        stored.kind = "gcp"
        with pytest.raises(InvalidEnvironmentConfigError):
            await session.flush()


async def test_orm_insert_and_update_statements_rejected(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        with pytest.raises(InvalidInputError) as insert_error:
            await session.execute(
                insert(Environment).values(name="aws-seoul", kind="aws", config={})
            )
        with pytest.raises(InvalidInputError) as update_error:
            await session.execute(update(Environment).values(config={}))

    assert type(insert_error.value) is InvalidInputError
    assert type(update_error.value) is InvalidInputError
    assert await _count_environments(session_factory) == 0


async def test_in_place_config_mutation_is_not_saved(session_factory: SessionFactory) -> None:
    async with session_factory() as session, session.begin():
        environment = make_environment("aws-seoul")
        session.add(environment)

    async with session_factory() as session, session.begin():
        stored = await session.get_one(Environment, environment.id)
        stored.config["cluster_name"] = "changed-in-place"

    async with session_factory() as session:
        reloaded = await session.get_one(Environment, environment.id)
    assert reloaded.config["cluster_name"] == "echo-eks"


async def test_reassigned_config_is_saved_and_parsed(session_factory: SessionFactory) -> None:
    async with session_factory() as session, session.begin():
        environment = make_environment("aws-seoul")
        session.add(environment)

    async with session_factory() as session, session.begin():
        stored = await session.get_one(Environment, environment.id)
        stored.config = {**stored.config, "cluster_name": "echo-eks-2"}

    async with session_factory() as session:
        reloaded = await session.get_one(Environment, environment.id)
    parsed = reloaded.parsed_config()
    assert isinstance(parsed, AwsEnvironmentConfig)
    assert parsed.cluster_name == "echo-eks-2"
    assert reloaded.config == {"region": "ap-northeast-2", "cluster_name": "echo-eks-2"}


async def test_new_kind_file_saves_without_model_or_migration_change(
    session_factory: SessionFactory, azure_adapter_package: str
) -> None:
    discover(azure_adapter_package)
    config: dict[str, object] = {
        "subscription_id": "00000000-0000-0000-0000-000000000000",
        "location": "koreacentral",
        "cluster_name": "echo-aks",
    }

    async with session_factory() as session, session.begin():
        environment = Environment(name="azure-korea", kind="azure", config=dict(config))
        session.add(environment)

    async with session_factory() as session:
        reloaded = await session.get_one(Environment, environment.id)

    azure_module = sys.modules[f"{azure_adapter_package}.azure"]
    assert reloaded.config == config
    assert isinstance(reloaded.parsed_config(), azure_module.AzureEnvironmentConfig)
    assert reloaded.parsed_config().display_location() == "koreacentral"


def test_azure_is_not_mentioned_in_models_migrations_or_registry() -> None:
    paths = [
        *(PROJECT_ROOT / "app" / "models").glob("*.py"),
        *(PROJECT_ROOT / "alembic" / "versions").glob("*.py"),
        PROJECT_ROOT / "app" / "adapters" / "registry.py",
    ]
    assert paths
    assert not [path.name for path in paths if "azure" in path.read_text().lower()]

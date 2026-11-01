"""마이그레이션 왕복·alembic check·pg_catalog 비교(AC-2).

alembic check 는 CHECK 제약·부분 인덱스 조건·트리거를 비교하지 못한다. 그래서
① 마이그레이션이 만든 스키마를 기대 목록과 비교하고
② 모델 메타데이터로 만든 스키마(create_all)와 칸·제약·인덱스 정의를 하나하나 비교한다.
"""

from typing import Any

from alembic import command
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import Base
from tests.integration.conftest import run_alembic

MODEL_SCHEMA = "model_check"

EXPECTED_CONSTRAINTS = {
    ("deployments", "ck_deployments_config_snapshot_is_object"): (
        "CHECK ((jsonb_typeof(config_snapshot) = 'object'::text))"
    ),
    ("deployments", "ck_deployments_image_digest_format"): (
        "CHECK (((image_digest)::text ~ '^sha256:[0-9a-f]{64}$'::text))"
    ),
    ("deployments", "ck_deployments_rollback_of_not_self"): "CHECK ((rollback_of_id <> id))",
    ("deployments", "ck_deployments_status_valid"): (
        "CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, "
        "'IN_PROGRESS'::character varying, 'SUCCEEDED'::character varying, "
        "'FAILED'::character varying, 'CANCELLED'::character varying])::text[])))"
    ),
    ("deployments", "fk_deployments_rollback_of_id_deployments"): (
        "FOREIGN KEY (rollback_of_id) REFERENCES deployments(id) ON DELETE RESTRICT"
    ),
    ("deployments", "fk_deployments_service_environment_id_service_environments"): (
        "FOREIGN KEY (service_environment_id) REFERENCES service_environments(id) "
        "ON DELETE RESTRICT"
    ),
    ("deployments", "pk_deployments"): "PRIMARY KEY (id)",
    ("deployments", "uq_deployments_id_service_environment_id"): (
        "UNIQUE (id, service_environment_id)"
    ),
    ("environments", "ck_environments_config_is_object"): (
        "CHECK ((jsonb_typeof(config) = 'object'::text))"
    ),
    ("environments", "ck_environments_connection_checked_at_consistency"): (
        "CHECK ((((connection_status)::text = 'UNCHECKED'::text) = "
        "(connection_checked_at IS NULL)))"
    ),
    ("environments", "ck_environments_connection_error_consistency"): (
        "CHECK ((((connection_status)::text = 'FAILED'::text) = (connection_error IS NOT NULL)))"
    ),
    ("environments", "ck_environments_connection_status_valid"): (
        "CHECK (((connection_status)::text = ANY ((ARRAY['UNCHECKED'::character varying, "
        "'CONNECTED'::character varying, 'FAILED'::character varying])::text[])))"
    ),
    ("environments", "ck_environments_name_format"): (
        "CHECK (((name)::text ~ '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$'::text))"
    ),
    ("environments", "ck_environments_soft_delete_consistency"): (
        "CHECK ((is_deleted = (deleted_at IS NOT NULL)))"
    ),
    ("environments", "pk_environments"): "PRIMARY KEY (id)",
    ("service_environments", "ck_service_environments_soft_delete_consistency"): (
        "CHECK ((is_deleted = (deleted_at IS NOT NULL)))"
    ),
    ("service_environments", "ck_service_environments_traffic_weight_range"): (
        "CHECK (((traffic_weight >= 0) AND (traffic_weight <= 100)))"
    ),
    ("service_environments", "fk_service_environments_current_deployment_id_id_deployments"): (
        "FOREIGN KEY (current_deployment_id, id) "
        "REFERENCES deployments(id, service_environment_id) ON DELETE RESTRICT"
    ),
    ("service_environments", "fk_service_environments_environment_id_environments"): (
        "FOREIGN KEY (environment_id) REFERENCES environments(id) ON DELETE RESTRICT"
    ),
    ("service_environments", "fk_service_environments_service_id_services"): (
        "FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE RESTRICT"
    ),
    ("service_environments", "pk_service_environments"): "PRIMARY KEY (id)",
    ("services", "ck_services_name_format"): (
        "CHECK (((name)::text ~ '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$'::text))"
    ),
    ("services", "ck_services_soft_delete_consistency"): (
        "CHECK ((is_deleted = (deleted_at IS NOT NULL)))"
    ),
    ("services", "pk_services"): "PRIMARY KEY (id)",
}

EXPECTED_INDEXES = {
    "ix_deployments_service_environment_id_created_at": (
        "CREATE INDEX ix_deployments_service_environment_id_created_at ON deployments "
        "USING btree (service_environment_id, created_at)"
    ),
    "pk_deployments": "CREATE UNIQUE INDEX pk_deployments ON deployments USING btree (id)",
    "pk_environments": "CREATE UNIQUE INDEX pk_environments ON environments USING btree (id)",
    "pk_service_environments": (
        "CREATE UNIQUE INDEX pk_service_environments ON service_environments USING btree (id)"
    ),
    "pk_services": "CREATE UNIQUE INDEX pk_services ON services USING btree (id)",
    "uq_deployments_id_service_environment_id": (
        "CREATE UNIQUE INDEX uq_deployments_id_service_environment_id ON deployments "
        "USING btree (id, service_environment_id)"
    ),
    "uq_environments_name_live": (
        "CREATE UNIQUE INDEX uq_environments_name_live ON environments USING btree (name) "
        "WHERE (is_deleted = false)"
    ),
    "uq_service_environments_service_id_environment_id_live": (
        "CREATE UNIQUE INDEX uq_service_environments_service_id_environment_id_live "
        "ON service_environments USING btree (service_id, environment_id) "
        "WHERE (is_deleted = false)"
    ),
    "uq_services_name_live": (
        "CREATE UNIQUE INDEX uq_services_name_live ON services USING btree (name) "
        "WHERE (is_deleted = false)"
    ),
}

EXPECTED_TRIGGERS = {
    ("deployments", "trg_deployments_prevent_delete"): (
        "CREATE TRIGGER trg_deployments_prevent_delete BEFORE DELETE ON deployments "
        "FOR EACH ROW EXECUTE FUNCTION prevent_deployment_delete()"
    ),
}


def _snapshot(connection: Connection, schema: str) -> dict[str, dict[Any, Any]]:
    """schema 안 칸·제약·인덱스·트리거 정의를 스키마 이름 없이 모은다."""
    connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    prefix = f"{schema}."

    def rows(sql: str) -> list[Any]:
        return list(connection.execute(text(sql), {"schema": schema}))

    columns = {
        (row.table_name, row.column_name): (
            row.ordinal_position,
            row.data_type,
            row.character_maximum_length,
            row.is_nullable,
            row.column_default,
        )
        for row in rows(
            "SELECT table_name, column_name, ordinal_position, data_type, "
            "character_maximum_length, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name <> 'alembic_version'"
        )
    }
    constraints = {
        (row.table_name, row.conname): row.definition
        for row in rows(
            "SELECT c.relname AS table_name, con.conname, "
            "pg_get_constraintdef(con.oid) AS definition "
            "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname <> 'alembic_version'"
        )
    }
    indexes = {
        row.indexname: row.indexdef.replace(prefix, "")
        for row in rows(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = :schema AND tablename <> 'alembic_version'"
        )
    }
    triggers = {
        (row.table_name, row.tgname): row.definition.replace(prefix, "")
        for row in rows(
            "SELECT c.relname AS table_name, t.tgname, "
            "pg_get_triggerdef(t.oid) AS definition "
            "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND NOT t.tgisinternal"
        )
    }
    return {
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "triggers": triggers,
    }


CONNECTION_COLUMNS = {"connection_status", "connection_checked_at", "connection_error"}
CONNECTION_CHECKS = {
    "ck_environments_connection_status_valid",
    "ck_environments_connection_checked_at_consistency",
    "ck_environments_connection_error_consistency",
}


def _environment_columns_and_checks(connection: Connection) -> tuple[set[str], set[str]]:
    inspector = inspect(connection)
    columns = {column["name"] for column in inspector.get_columns("environments")}
    checks = {check["name"] or "" for check in inspector.get_check_constraints("environments")}
    return columns, checks


async def test_migration_downgrade_one_removes_connection_columns_and_upgrade_restores(
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO environments (name, kind, config) VALUES ('legacy-env', 'aws', '{}')")
        )
        await connection.run_sync(run_alembic(lambda config: command.downgrade(config, "-1")))
        columns_down, checks_down = await connection.run_sync(_environment_columns_and_checks)
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        columns_up, checks_up = await connection.run_sync(_environment_columns_and_checks)
        legacy_status = await connection.scalar(
            text("SELECT connection_status FROM environments WHERE name = 'legacy-env'")
        )
        await connection.run_sync(run_alembic(command.check))
        # downgrade -1 → upgrade head 는 지운 칸 번호를 다시 쓰지 않아 칸 위치가 달라진다.
        # 같은 세션의 다른 비교 시험이 실행 순서에 흔들리지 않게 표를 처음부터 다시 만든다.
        await connection.run_sync(run_alembic(lambda config: command.downgrade(config, "base")))
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))

    assert not columns_down & CONNECTION_COLUMNS
    assert not checks_down & CONNECTION_CHECKS
    assert CONNECTION_COLUMNS <= columns_up
    assert CONNECTION_CHECKS <= checks_up
    # 기존 행은 UNCHECKED 가 된다.
    assert legacy_status == "UNCHECKED"


async def test_migration_round_trip_upgrade_downgrade_upgrade(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(run_alembic(lambda config: command.downgrade(config, "base")))
        tables_after_downgrade = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_table_names()
        )
        functions_after_downgrade = await connection.scalar(
            text("SELECT count(*) FROM pg_proc WHERE proname = 'prevent_deployment_delete'")
        )
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        tables_after_upgrade = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_table_names()
        )

    assert tables_after_downgrade == ["alembic_version"]
    assert functions_after_downgrade == 0
    assert sorted(tables_after_upgrade) == [
        "alembic_version",
        "deployments",
        "environments",
        "service_environments",
        "services",
    ]


async def test_alembic_check_reports_no_differences(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        # 차이가 있으면 AutogenerateDiffsDetected 가 난다.
        await connection.run_sync(run_alembic(command.check))


async def test_migrated_schema_matches_expected_catalog(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        snapshot = await connection.run_sync(lambda c: _snapshot(c, "public"))

    assert snapshot["constraints"] == EXPECTED_CONSTRAINTS
    assert snapshot["indexes"] == EXPECTED_INDEXES
    assert snapshot["triggers"] == EXPECTED_TRIGGERS


async def test_model_metadata_matches_migrated_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(f'DROP SCHEMA IF EXISTS "{MODEL_SCHEMA}" CASCADE'))
        await connection.execute(text(f'CREATE SCHEMA "{MODEL_SCHEMA}"'))
        try:
            await connection.run_sync(
                lambda c: Base.metadata.create_all(
                    c.execution_options(schema_translate_map={None: MODEL_SCHEMA})
                )
            )
            from_models = await connection.run_sync(lambda c: _snapshot(c, MODEL_SCHEMA))
            from_migration = await connection.run_sync(lambda c: _snapshot(c, "public"))
        finally:
            await connection.execute(text(f'DROP SCHEMA "{MODEL_SCHEMA}" CASCADE'))

    assert from_models["columns"] == from_migration["columns"]
    assert from_models["constraints"] == from_migration["constraints"]
    assert from_models["indexes"] == from_migration["indexes"]
    # 트리거는 모델 메타데이터에 없고 마이그레이션에만 있다.
    assert from_models["triggers"] == {}

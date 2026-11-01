"""마이그레이션 왕복·alembic check·pg_catalog 비교(AC-2).

alembic check 는 CHECK 제약·부분 인덱스 조건·트리거를 비교하지 못한다. 그래서
① 마이그레이션이 만든 스키마를 기대 목록과 비교하고
② 모델 메타데이터로 만든 스키마(create_all)와 칸·제약·인덱스 정의를 하나하나 비교한다.
"""

from typing import Any

import pytest
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
    # --- API 서버 3: 원터치 배포 ---
    ("services", "ck_services_container_port_range"): (
        "CHECK (((container_port >= 1) AND (container_port <= 65535)))"
    ),
    ("services", "ck_services_delete_error_only_when_deleting"): (
        "CHECK (((delete_error IS NULL) OR ((status)::text = 'DELETING'::text)))"
    ),
    ("services", "ck_services_health_check_path_format"): (
        "CHECK (((health_check_path)::text ~~ '/%'::text))"
    ),
    ("services", "ck_services_health_check_timeout_range"): (
        "CHECK (((health_check_timeout_seconds >= 10) AND (health_check_timeout_seconds <= 1800)))"
    ),
    ("services", "ck_services_status_valid"): (
        "CHECK (((status)::text = ANY ((ARRAY['ACTIVE'::character varying, "
        "'DELETING'::character varying])::text[])))"
    ),
    ("builds", "ck_builds_commit_sha_format"): "CHECK ((commit_sha ~ '^[0-9a-f]{40}$'::text))",
    ("builds", "ck_builds_error_code_consistency"): (
        "CHECK ((((status)::text = 'FAILED'::text) = (error_code IS NOT NULL)))"
    ),
    ("builds", "ck_builds_finished_at_consistency"): (
        "CHECK ((((status)::text = ANY ((ARRAY['SUCCEEDED'::character varying, "
        "'FAILED'::character varying])::text[])) = (finished_at IS NOT NULL)))"
    ),
    ("builds", "ck_builds_image_consistency"): (
        "CHECK ((((status)::text = 'SUCCEEDED'::text) = "
        "((image_digest IS NOT NULL) AND (image_repository IS NOT NULL))))"
    ),
    ("builds", "ck_builds_image_digest_format"): (
        "CHECK (((image_digest)::text ~ '^sha256:[0-9a-f]{64}$'::text))"
    ),
    ("builds", "ck_builds_status_valid"): (
        "CHECK (((status)::text = ANY ((ARRAY['QUEUED'::character varying, "
        "'RUNNING'::character varying, 'SUCCEEDED'::character varying, "
        "'FAILED'::character varying])::text[])))"
    ),
    ("builds", "fk_builds_service_id_services"): (
        "FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE RESTRICT"
    ),
    ("builds", "pk_builds"): "PRIMARY KEY (id)",
    ("deploy_requests", "ck_deploy_requests_build_reused_consistency"): (
        "CHECK (((build_id IS NULL) = (build_reused IS NULL)))"
    ),
    ("deploy_requests", "ck_deploy_requests_cancel_reason_consistency"): (
        "CHECK ((((status)::text = 'CANCELLED'::text) = (cancel_reason IS NOT NULL)))"
    ),
    ("deploy_requests", "ck_deploy_requests_cancel_reason_valid"): (
        "CHECK (((cancel_reason)::text = ANY ((ARRAY['SUPERSEDED'::character varying, "
        "'SERVICE_DELETED'::character varying])::text[])))"
    ),
    ("deploy_requests", "ck_deploy_requests_commit_sha_format"): (
        "CHECK ((commit_sha ~ '^[0-9a-f]{40}$'::text))"
    ),
    ("deploy_requests", "ck_deploy_requests_config_snapshot_is_object"): (
        "CHECK ((jsonb_typeof(config_snapshot) = 'object'::text))"
    ),
    ("deploy_requests", "ck_deploy_requests_error_code_consistency"): (
        "CHECK ((((status)::text = 'FAILED'::text) = (error_code IS NOT NULL)))"
    ),
    ("deploy_requests", "ck_deploy_requests_finished_at_consistency"): (
        "CHECK ((((status)::text = ANY ((ARRAY['SUCCEEDED'::character varying, "
        "'FAILED'::character varying, 'CANCELLED'::character varying])::text[])) = "
        "(finished_at IS NOT NULL)))"
    ),
    ("deploy_requests", "ck_deploy_requests_last_health_result_valid"): (
        "CHECK (((last_health_result)::text = ANY ((ARRAY['OK'::character varying, "
        "'HTTP_ERROR'::character varying, 'UNREACHABLE'::character varying])::text[])))"
    ),
    ("deploy_requests", "ck_deploy_requests_ref_type_valid"): (
        "CHECK (((ref_type)::text = ANY ((ARRAY['BRANCH'::character varying, "
        "'COMMIT'::character varying])::text[])))"
    ),
    ("deploy_requests", "ck_deploy_requests_revert_status_valid"): (
        "CHECK (((revert_status)::text = ANY ((ARRAY['SUCCEEDED'::character varying, "
        "'FAILED'::character varying])::text[])))"
    ),
    ("deploy_requests", "ck_deploy_requests_simulate_is_object"): (
        "CHECK ((jsonb_typeof(simulate) = 'object'::text))"
    ),
    ("deploy_requests", "ck_deploy_requests_source_request_consistency"): (
        "CHECK ((((trigger)::text = 'STANDBY_SYNC'::text) = (source_request_id IS NOT NULL)))"
    ),
    ("deploy_requests", "ck_deploy_requests_status_valid"): (
        "CHECK (((status)::text = ANY ((ARRAY['QUEUED'::character varying, "
        "'BUILDING'::character varying, 'DEPLOYING'::character varying, "
        "'VERIFYING'::character varying, 'SUCCEEDED'::character varying, "
        "'FAILED'::character varying, 'CANCELLED'::character varying])::text[])))"
    ),
    ("deploy_requests", "ck_deploy_requests_trigger_valid"): (
        "CHECK (((trigger)::text = ANY ((ARRAY['MANUAL'::character varying, "
        "'STANDBY_SYNC'::character varying])::text[])))"
    ),
    ("deploy_requests", "fk_deploy_requests_build_id_builds"): (
        "FOREIGN KEY (build_id) REFERENCES builds(id) ON DELETE RESTRICT"
    ),
    ("deploy_requests", "fk_deploy_requests_deployment_id_deployments"): (
        "FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE RESTRICT"
    ),
    ("deploy_requests", "fk_deploy_requests_environment_id_environments"): (
        "FOREIGN KEY (environment_id) REFERENCES environments(id) ON DELETE RESTRICT"
    ),
    ("deploy_requests", "fk_deploy_requests_service_environment_id_service_environments"): (
        "FOREIGN KEY (service_environment_id) REFERENCES service_environments(id) "
        "ON DELETE RESTRICT"
    ),
    ("deploy_requests", "fk_deploy_requests_service_id_services"): (
        "FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE RESTRICT"
    ),
    ("deploy_requests", "fk_deploy_requests_source_request_id_deploy_requests"): (
        "FOREIGN KEY (source_request_id) REFERENCES deploy_requests(id) ON DELETE RESTRICT"
    ),
    ("deploy_requests", "fk_deploy_requests_superseded_by_id_deploy_requests"): (
        "FOREIGN KEY (superseded_by_id) REFERENCES deploy_requests(id) ON DELETE RESTRICT"
    ),
    ("deploy_requests", "pk_deploy_requests"): "PRIMARY KEY (id)",
    ("jobs", "ck_jobs_kind_valid"): (
        "CHECK (((kind)::text = ANY ((ARRAY['DEPLOY_REQUEST'::character varying, "
        "'SERVICE_TEARDOWN'::character varying])::text[])))"
    ),
    ("jobs", "ck_jobs_lease_consistency"): (
        "CHECK ((((status)::text = 'RUNNING'::text) = "
        "((lease_token IS NOT NULL) AND (lease_expires_at IS NOT NULL))))"
    ),
    ("jobs", "ck_jobs_lease_losses_non_negative"): "CHECK ((lease_losses >= 0))",
    ("jobs", "ck_jobs_status_valid"): (
        "CHECK (((status)::text = ANY ((ARRAY['QUEUED'::character varying, "
        "'RUNNING'::character varying, 'DONE'::character varying, "
        "'FAILED'::character varying, 'CANCELLED'::character varying])::text[])))"
    ),
    ("jobs", "pk_jobs"): "PRIMARY KEY (id)",
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
    # --- API 서버 3: 원터치 배포 ---
    "uq_services_public_host_live": (
        "CREATE UNIQUE INDEX uq_services_public_host_live ON services USING btree (public_host) "
        "WHERE (is_deleted = false)"
    ),
    "uq_service_environments_public_host_live": (
        "CREATE UNIQUE INDEX uq_service_environments_public_host_live "
        "ON service_environments USING btree (public_host) WHERE (is_deleted = false)"
    ),
    "pk_builds": "CREATE UNIQUE INDEX pk_builds ON builds USING btree (id)",
    "uq_builds_service_id_repo_url_commit_sha_not_failed": (
        "CREATE UNIQUE INDEX uq_builds_service_id_repo_url_commit_sha_not_failed ON builds "
        "USING btree (service_id, repo_url, commit_sha) WHERE ((status)::text <> 'FAILED'::text)"
    ),
    "pk_deploy_requests": (
        "CREATE UNIQUE INDEX pk_deploy_requests ON deploy_requests USING btree (id)"
    ),
    "ix_deploy_requests_build_id": (
        "CREATE INDEX ix_deploy_requests_build_id ON deploy_requests USING btree (build_id)"
    ),
    "ix_deploy_requests_service_environment_id_created_at": (
        "CREATE INDEX ix_deploy_requests_service_environment_id_created_at ON deploy_requests "
        "USING btree (service_environment_id, created_at)"
    ),
    "ix_deploy_requests_service_id_created_at": (
        "CREATE INDEX ix_deploy_requests_service_id_created_at ON deploy_requests "
        "USING btree (service_id, created_at)"
    ),
    "uq_deploy_requests_service_environment_id_active": (
        "CREATE UNIQUE INDEX uq_deploy_requests_service_environment_id_active ON deploy_requests "
        "USING btree (service_environment_id) WHERE ((status)::text = ANY "
        "((ARRAY['BUILDING'::character varying, 'DEPLOYING'::character varying, "
        "'VERIFYING'::character varying])::text[]))"
    ),
    "uq_deploy_requests_service_environment_id_queued": (
        "CREATE UNIQUE INDEX uq_deploy_requests_service_environment_id_queued ON deploy_requests "
        "USING btree (service_environment_id) WHERE ((status)::text = 'QUEUED'::text)"
    ),
    "pk_jobs": "CREATE UNIQUE INDEX pk_jobs ON jobs USING btree (id)",
    "ix_jobs_status_run_after": (
        "CREATE INDEX ix_jobs_status_run_after ON jobs USING btree (status, run_after)"
    ),
    "uq_jobs_subject_id_open": (
        "CREATE UNIQUE INDEX uq_jobs_subject_id_open ON jobs USING btree (subject_id) "
        "WHERE ((status)::text = ANY ((ARRAY['QUEUED'::character varying, "
        "'RUNNING'::character varying])::text[]))"
    ),
}

EXPECTED_TRIGGERS = {
    ("deployments", "trg_deployments_prevent_delete"): (
        "CREATE TRIGGER trg_deployments_prevent_delete BEFORE DELETE ON deployments "
        "FOR EACH ROW EXECUTE FUNCTION prevent_deployment_delete()"
    ),
    ("deploy_requests", "trg_deploy_requests_prevent_delete"): (
        "CREATE TRIGGER trg_deploy_requests_prevent_delete BEFORE DELETE ON deploy_requests "
        "FOR EACH ROW EXECUTE FUNCTION prevent_append_only_delete()"
    ),
}

# 기존 표에 칸을 더하기 전(접속 상태 revision) 시점.
BEFORE_ONE_TOUCH_DEPLOY = "6f6d92350314"
BEFORE_CONNECTION_STATUS = "b17994eaa7aa"


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
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_CONNECTION_STATUS))
        )
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
        "builds",
        "deploy_requests",
        "deployments",
        "environments",
        "jobs",
        "service_environments",
        "services",
    ]


ONE_TOUCH_DEPLOY_TABLES = {"builds", "deploy_requests", "jobs"}
ONE_TOUCH_DEPLOY_SERVICE_COLUMNS = {"repo_url", "public_host", "status", "simulated"}


def _one_touch_deploy_state(connection: Connection) -> tuple[set[str], set[str], int]:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names()) & ONE_TOUCH_DEPLOY_TABLES
    service_columns = {column["name"] for column in inspector.get_columns("services")}
    functions = connection.scalar(
        text("SELECT count(*) FROM pg_proc WHERE proname = 'prevent_append_only_delete'")
    )
    return tables, service_columns & ONE_TOUCH_DEPLOY_SERVICE_COLUMNS, int(functions or 0)


async def test_one_touch_deploy_revision_downgrade_removes_tables_and_upgrade_restores(
    engine: AsyncEngine,
) -> None:
    # 트랜잭션을 되돌려 칸 번호(attnum)가 바뀐 스키마를 남기지 않는다(PostgreSQL DDL 은 트랜잭션).
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_ONE_TOUCH_DEPLOY))
        )
        down = await connection.run_sync(_one_touch_deploy_state)
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        up = await connection.run_sync(_one_touch_deploy_state)
        await connection.run_sync(run_alembic(command.check))
        await transaction.rollback()

    assert down == (set(), set(), 0)
    assert up == (ONE_TOUCH_DEPLOY_TABLES, ONE_TOUCH_DEPLOY_SERVICE_COLUMNS, 1)


async def test_one_touch_deploy_revision_upgrade_with_existing_services_fails_clearly(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_ONE_TOUCH_DEPLOY))
        )
        await connection.execute(text("INSERT INTO services (name) VALUES ('legacy-service')"))
        with pytest.raises(RuntimeError, match="services has 1 rows") as raised:
            await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        await transaction.rollback()

    assert "expects services to be empty" in str(raised.value)


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

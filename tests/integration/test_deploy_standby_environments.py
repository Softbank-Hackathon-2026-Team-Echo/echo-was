"""첫 배포 때 예비 환경 함께 띄우기(이슈 #99 2번).

- standbyEnvironmentIds 는 접수 때 확인만 하고, 주 환경 성공 처리 때 연결을 만들고(없으면)
  기존 예비 따라가기와 같은 STANDBY_SYNC 를 넣는다. 주 환경이 실패하면 아무것도 만들지 않는다.
- 상세 응답 standbyEnvironmentIds·standbyDeployRequestIds(가칭).
- 예비 따라가기가 성공하면 그 연결은 예비(STANDBY)가 되고, 그 전(배포 없는 연결)에는 매트릭스·지문에
  잡히지 않는다(API 서버 10-4 규칙 그대로).
"""

import uuid
from typing import Any

from sqlalchemy import select

from app.audit.actions import AuditAction
from app.enums import DeployRequestTrigger
from app.models import AuditLog, Deployment, DeployRequest, ServiceEnvironment
from tests.integration.deploy_harness import DeployHarness, connection_of
from tests.integration.migration_support import Migration
from tests.integration.rollback_support import deploy_and_finish, read_all_logs, rollback


async def _connections(harness: DeployHarness, service_id: str) -> list[ServiceEnvironment]:
    async with harness.session_factory() as session:
        rows = await session.scalars(
            select(ServiceEnvironment)
            .where(ServiceEnvironment.service_id == uuid.UUID(service_id))
            .order_by(ServiceEnvironment.created_at, ServiceEnvironment.id)
        )
        return list(rows.all())


async def _syncs(harness: DeployHarness, service_id: str) -> list[DeployRequest]:
    async with harness.session_factory() as session:
        rows = await session.scalars(
            select(DeployRequest)
            .where(
                DeployRequest.service_id == uuid.UUID(service_id),
                DeployRequest.trigger == DeployRequestTrigger.STANDBY_SYNC,
            )
            .order_by(DeployRequest.created_at, DeployRequest.id)
        )
        return list(rows.all())


async def _deploy_with_standby(
    harness: DeployHarness,
    service_id: str,
    environment_id: object,
    standby: list[object],
    **extra: Any,
) -> dict[str, Any]:
    return await harness.deploy(
        service_id,
        str(environment_id),
        standbyEnvironmentIds=[str(item) for item in standby],
        **extra,
    )


async def test_first_deploy_with_standby_creates_connection_and_standby_sync(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shap-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    # 예비 환경에 미리 넣은 변수(배포 없는 연결)는 그 연결로 따라간다.
    put = await harness.client.put(
        f"{harness.services}/{service['id']}/environments/{gcp.id}/variables/DATABASE_URL",
        json={"value": f"sv-{uuid.uuid4()}"},
    )
    assert put.status_code == 201, put.text

    accepted = await _deploy_with_standby(harness, service["id"], aws.id, [gcp.id])

    assert accepted["standbyEnvironmentIds"] == [str(gcp.id)]
    assert accepted["standbyDeployRequestIds"] == []  # 아직 주 환경 성공 전
    await harness.run_until_idle()

    main = await harness.get_request(accepted["id"])
    assert main["status"] == "SUCCEEDED"
    (sync,) = await _syncs(harness, service["id"])
    assert main["standbyDeployRequestIds"] == [str(sync.id)]
    assert main["standbyEnvironmentIds"] == [str(gcp.id)]
    assert (sync.environment_id, sync.source_request_id, sync.build_reused) == (
        gcp.id,
        uuid.UUID(accepted["id"]),
        True,
    )
    synced = await harness.get_request(str(sync.id))
    assert (synced["trigger"], synced["status"]) == ("STANDBY_SYNC", "SUCCEEDED")
    assert synced["standbyEnvironmentIds"] == []
    detail = await harness.get_service(service["id"])
    assert connection_of(detail, "aws-seoul")["role"] == "SERVING"
    assert connection_of(detail, "gcp-seoul")["role"] == "STANDBY"
    connections = await _connections(harness, service["id"])
    assert [c.environment_id for c in connections] == [gcp.id, aws.id]  # gcp 는 PUT 때 만든 연결
    # 예비 환경 목록은 배포 기록·따라가기 요청에 옮기지 않는다(되돌리기가 다시 띄우지 않게).
    async with harness.session_factory() as session:
        deployments = (await session.scalars(select(Deployment))).all()
        main_row = await session.get_one(DeployRequest, uuid.UUID(accepted["id"]))
    assert all("standbyEnvironmentIds" not in d.config_snapshot for d in deployments)
    assert "standbyEnvironmentIds" not in sync.config_snapshot
    assert main_row.config_snapshot["standbyEnvironmentIds"] == [str(gcp.id)]
    gcp_deployment = next(d for d in deployments if d.service_environment_id == connections[0].id)
    assert set(gcp_deployment.config_snapshot["envVars"]) == {"DATABASE_URL"}
    messages = [line["code"] for line in await read_all_logs(harness, accepted["id"], "DEPLOY_JOB")]
    assert messages.count("STANDBY_SYNC_REQUESTED") == 1
    assert "STANDBY_SKIPPED" not in messages
    async with harness.session_factory() as session:
        audit = await session.scalar(
            select(AuditLog).where(AuditLog.action == AuditAction.DEPLOY_REQUESTED)
        )
    assert audit is not None and audit.details["standbyEnvironmentIds"] == [str(gcp.id)]

    # 다음 수동 배포는 예전처럼 예비를 따라가게 한다(새로 지정하지 않아도).
    again = await deploy_and_finish(harness, service["id"], aws.id)
    assert again["status"] == "SUCCEEDED"
    assert len(await _syncs(harness, service["id"])) == 2
    assert again["standbyEnvironmentIds"] == []


async def test_main_failure_creates_no_standby_connection_or_sync(harness: DeployHarness) -> None:
    service = await harness.create_service("shap-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")

    accepted = await _deploy_with_standby(
        harness, service["id"], aws.id, [gcp.id], simulate={"failAt": "VERIFYING"}
    )
    await harness.run_until_idle()

    main = await harness.get_request(accepted["id"])
    assert main["status"] == "FAILED"
    assert main["standbyDeployRequestIds"] == []
    assert await _syncs(harness, service["id"]) == []
    assert [c.environment_id for c in await _connections(harness, service["id"])] == [aws.id]


async def test_rollback_does_not_restart_requested_standby(harness: DeployHarness) -> None:
    service = await harness.create_service("shap-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    first = await _deploy_with_standby(harness, service["id"], aws.id, [gcp.id])
    await harness.run_until_idle()
    second = await deploy_and_finish(harness, service["id"], aws.id, commitSha="b" * 40)
    assert second["status"] == "SUCCEEDED"
    before = len(await _syncs(harness, service["id"]))

    back = await rollback(harness, first["id"])
    await harness.run_until_idle()

    done = await harness.get_request(back["id"])
    assert done["status"] == "SUCCEEDED"
    assert done["standbyEnvironmentIds"] == []
    # 되돌리기는 기존 규칙대로 예비 연결 하나만 따라가게 한다(중복·추가 연결 없음).
    assert len(await _syncs(harness, service["id"])) == before + 1
    assert len(await _connections(harness, service["id"])) == 2


async def test_standby_request_rules(harness: DeployHarness) -> None:
    service = await harness.create_service("shap-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    failed = await harness.add_environment("aws-tokyo", failed=True)
    deleted = await harness.add_environment("aws-osaka", deleted=True)

    async def request(standby: list[object]) -> Any:
        return await harness.request_deploy(
            service["id"],
            aws.id,
            branch="main",
            standbyEnvironmentIds=[str(item) for item in standby],
        )

    same = await request([aws.id])
    duplicate = await request([gcp.id, gcp.id])
    missing = await request([uuid.uuid4()])
    removed = await request([deleted.id])
    unhealthy = await request([failed.id])
    too_many = await request([uuid.uuid4() for _ in range(6)])

    assert (same.status_code, same.json()["code"]) == (422, "VALIDATION_ERROR")
    assert same.json()["details"] == [
        {"field": "standbyEnvironmentIds", "reason": "SAME_AS_ENVIRONMENT"}
    ]
    assert duplicate.json()["details"] == [
        {"field": "standbyEnvironmentIds", "reason": "DUPLICATE"}
    ]
    for response in (missing, removed):
        assert (response.status_code, response.json()["code"]) == (404, "ENVIRONMENT_NOT_FOUND")
    assert (unhealthy.status_code, unhealthy.json()["code"]) == (409, "ENVIRONMENT_UNHEALTHY")
    assert (too_many.status_code, too_many.json()["code"]) == (422, "VALIDATION_ERROR")
    assert await _connections(harness, service["id"]) == []


async def test_standby_deleted_before_success_is_skipped_with_log(harness: DeployHarness) -> None:
    service = await harness.create_service("shap-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    accepted = await _deploy_with_standby(harness, service["id"], aws.id, [gcp.id])
    deleted = await harness.client.delete(f"{harness.environments}/{gcp.id}")
    assert deleted.status_code == 204, deleted.text

    await harness.run_until_idle()

    main = await harness.get_request(accepted["id"])
    assert main["status"] == "SUCCEEDED"
    assert main["standbyDeployRequestIds"] == []
    assert await _syncs(harness, service["id"]) == []
    lines = await read_all_logs(harness, accepted["id"], "DEPLOY_JOB")
    skipped = [line for line in lines if line["code"] == "STANDBY_SKIPPED"]
    assert [(line["level"], line["message"]) for line in skipped] == [
        (
            "WARN",
            f"Requested standby environment {gcp.id} was not started: the environment was deleted.",
        )
    ]


async def test_requested_existing_standby_follows_once(migration: Migration) -> None:
    """이미 예비인 gcp 를 다시 지정해도 따라가기는 한 번(기존 반복이 넣는다)."""
    harness = migration.harness
    before = len(await _syncs(harness, migration.service_id))

    accepted = await _deploy_with_standby(
        harness, migration.service_id, migration.aws.id, [migration.gcp.id]
    )
    await harness.run_until_idle()

    main = await harness.get_request(accepted["id"])
    assert main["status"] == "SUCCEEDED"
    syncs = await _syncs(harness, migration.service_id)
    assert len(syncs) == before + 1
    assert main["standbyDeployRequestIds"] == [str(syncs[-1].id)]


async def test_deploying_into_standby_environment_skips_requested_standby(
    migration: Migration,
) -> None:
    """예비(gcp)에 직접 배포하면 서비스 중이 아니라 요청한 예비를 띄우지 않는다."""
    harness = migration.harness
    tokyo = await harness.add_environment("aws-tokyo")

    accepted = await _deploy_with_standby(
        harness, migration.service_id, migration.gcp.id, [tokyo.id]
    )
    await harness.run_until_idle()

    assert (await harness.get_request(accepted["id"]))["status"] == "SUCCEEDED"
    environments = {c.environment_id for c in await _connections(harness, migration.service_id)}
    assert tokyo.id not in environments
    codes = [line["code"] for line in await read_all_logs(harness, accepted["id"], "DEPLOY_JOB")]
    assert codes.count("STANDBY_SKIPPED") == 1

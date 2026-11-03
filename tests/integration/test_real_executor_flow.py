"""실제 실행기 교체에 따른 처리기·주소·환경 설정 통합 시험(SPEC api-3-2 AC-2·AC-3·AC-6·AC-7).

실제 실행기 자체는 단위 시험(스텁 전송)이 본다. 여기서는 처리기가 새 예외를 실패 코드로 닫는지,
실행기에 넘기는 값, 환경별 주소 형식, 환경 config 저장 모양을 실제 PostgreSQL 로 본다.
"""

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.enums import DeployRequestErrorCode
from app.executors.base import (
    BuildSpec,
    BuildStartError,
    DeployApplyError,
    DeploymentSpec,
    DeployTarget,
    SourceAccess,
)
from app.executors.fake import FakeBuildExecutor, FakeDeployExecutor
from app.models import Build, Environment, ServiceEnvironment
from app.services.deploy_log_messages import ERROR_MESSAGES
from app.services.onprem_pull_refresher import OnpremPullSecretRefresher
from tests.integration.deploy_harness import DeployHarness, FakeClock, connection_of
from tests.integration.factories import secret_ref_for
from tests.integration.rollback_support import deploy_and_finish, read_all_logs

SUFFIX = "k9x8y7z6"
INGRESS_DOMAIN = "aws-tokyo.apps.echo.test"
AWS_TOKYO_CONFIG: dict[str, object] = {
    "region": "ap-northeast-1",
    "cluster_name": "echo-dev-eks",
    "ingress_domain": INGRESS_DOMAIN,
    "ingress_group": "echo-apps",
}


class _RejectingBuildExecutor(FakeBuildExecutor):
    async def start(self, build: BuildSpec, source: SourceAccess, idempotency_key: str) -> str:
        raise BuildStartError(DeployRequestErrorCode.BUILD_DISPATCH_FAILED)


class _RejectingDeployExecutor(FakeDeployExecutor):
    async def apply(
        self, deployment: DeploymentSpec, target: DeployTarget, idempotency_key: str
    ) -> str:
        raise DeployApplyError(DeployRequestErrorCode.CLUSTER_UNAUTHORIZED)


class _RecordingDeployExecutor(FakeDeployExecutor):
    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock, 0)
        self.targets: list[DeployTarget] = []

    async def apply(
        self, deployment: DeploymentSpec, target: DeployTarget, idempotency_key: str
    ) -> str:
        self.targets.append(target)
        return await super().apply(deployment, target, idempotency_key)


async def _add_environment(
    harness: DeployHarness, name: str, kind: str, config: dict[str, object]
) -> Environment:
    environment = Environment(
        organization_id=harness.organization.id,
        name=name,
        kind=kind,
        config=dict(config),
        secret_ref=secret_ref_for(name, harness.organization.slug),
    )
    environment.record_connection_check(harness.clock.now(), None)
    async with harness.session_factory() as session, session.begin():
        session.add(environment)
    return environment


# --- AC-2·AC-3 처리기: 새 실패 코드 ---


async def test_build_start_error_fails_request_with_build_dispatch_failed(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    harness.replace_executors(build=_RejectingBuildExecutor(harness.clock, 0))

    finished = await deploy_and_finish(harness, service["id"], aws.id)

    async with harness.session_factory() as session:
        builds = list(await session.scalars(select(Build)))
    assert (finished["status"], finished["errorCode"]) == ("FAILED", "BUILD_DISPATCH_FAILED")
    assert finished["errorMessage"] == ERROR_MESSAGES[DeployRequestErrorCode.BUILD_DISPATCH_FAILED]
    assert finished["revertStatus"] is None and finished["deploymentId"] is None
    assert [(build.status, build.error_code) for build in builds] == [
        ("FAILED", "BUILD_DISPATCH_FAILED")
    ]


async def test_deploy_apply_error_fails_with_its_code_and_reverts(harness: DeployHarness) -> None:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    deployer = _RejectingDeployExecutor(harness.clock, 0)
    harness.replace_executors(deploy=deployer)

    finished = await deploy_and_finish(harness, service["id"], aws.id)
    job_log = await read_all_logs(harness, finished["id"], "DEPLOY_JOB")

    assert (finished["status"], finished["errorCode"]) == ("FAILED", "CLUSTER_UNAUTHORIZED")
    assert finished["deploymentId"] is not None
    assert finished["revertStatus"] == "SUCCEEDED"
    assert len(deployer.reverts) == 1
    assert ERROR_MESSAGES[DeployRequestErrorCode.CLUSTER_UNAUTHORIZED] in [
        line["message"] for line in job_log
    ]


async def test_deploy_target_carries_environment_connection_values(
    harness: DeployHarness,
) -> None:
    harness.host_suffixes.queue(SUFFIX)
    service = await harness.create_service("shop-api")
    aws = await _add_environment(harness, "aws-tokyo", "aws", AWS_TOKYO_CONFIG)
    deployer = _RecordingDeployExecutor(harness.clock)
    harness.replace_executors(deploy=deployer)

    await deploy_and_finish(harness, service["id"], aws.id)

    target = deployer.targets[0]
    assert target.environment_name == "aws-tokyo"
    assert target.environment_secret_ref == secret_ref_for("aws-tokyo")
    assert target.environment_config is not None
    assert target.environment_config.environment_host_domain() == INGRESS_DOMAIN
    assert target.main_public_host == f"shop-api-{SUFFIX}.apps.echo.test"
    assert target.public_host == f"shop-api-{SUFFIX}.{INGRESS_DOMAIN}"


# --- AC-6 환경별 주소 형식 ---


async def test_environment_with_ingress_domain_uses_sub_domain_host(
    harness: DeployHarness,
) -> None:
    harness.host_suffixes.queue(SUFFIX)
    service = await harness.create_service("shop-api")
    tokyo = await _add_environment(harness, "aws-tokyo", "aws", AWS_TOKYO_CONFIG)
    onprem = await harness.add_environment("onprem-k3s")

    tokyo_request = await deploy_and_finish(harness, service["id"], tokyo.id)
    onprem_request = await deploy_and_finish(harness, service["id"], onprem.id)
    detail = await harness.get_service(service["id"])

    assert tokyo_request["urls"]["environment"] == f"https://shop-api-{SUFFIX}.{INGRESS_DOMAIN}"
    assert (
        connection_of(detail, "aws-tokyo")["url"] == f"https://shop-api-{SUFFIX}.{INGRESS_DOMAIN}"
    )
    assert onprem_request["urls"]["environment"] == (
        f"https://shop-api-onprem-k3s-{SUFFIX}.apps.echo.test"
    )
    assert tokyo_request["urls"]["main"] == f"https://shop-api-{SUFFIX}.apps.echo.test"


async def test_adding_ingress_domain_keeps_existing_connection_host(
    harness: DeployHarness,
) -> None:
    harness.host_suffixes.queue(SUFFIX, "n3w5uff1")
    first = await harness.create_service("shop-api")
    second = await harness.create_service("billing")
    aws = await harness.add_environment("aws-seoul")
    await deploy_and_finish(harness, first["id"], aws.id)

    response = await harness.client.patch(
        f"{harness.environments}/{aws.id}",
        json={
            "config": {
                "region": "ap-northeast-2",
                "clusterName": "echo-eks",
                "ingressDomain": "aws-seoul.apps.echo.test",
            }
        },
    )
    async with harness.session_factory() as session:
        stored = await session.get_one(Environment, aws.id)
        stored.record_connection_check(harness.clock.now(), None)
        await session.commit()
    await deploy_and_finish(harness, first["id"], aws.id)
    await deploy_and_finish(harness, second["id"], aws.id)

    async with harness.session_factory() as session:
        hosts = {
            row.service_id: row.public_host
            for row in await session.scalars(select(ServiceEnvironment))
        }
    assert response.status_code == 200, response.text
    assert hosts[uuid.UUID(first["id"])] == f"shop-api-aws-seoul-{SUFFIX}.apps.echo.test"
    assert hosts[uuid.UUID(second["id"])] == "billing-n3w5uff1.aws-seoul.apps.echo.test"


async def test_ingress_domain_must_be_one_level_below_public_base_domain(
    harness: DeployHarness,
) -> None:
    bodies = [
        {"region": "ap-northeast-1", "clusterName": "eks", "ingressDomain": domain}
        for domain in ("a.aws-tokyo.apps.echo.test", "aws-tokyo.other.test", "apps.echo.test")
    ]

    responses = [
        await harness.client.post(
            harness.environments,
            json={
                "name": f"aws-{index}",
                "kind": "aws",
                "config": body,
                "secretRef": secret_ref_for(f"aws-{index}"),
            },
        )
        for index, body in enumerate(bodies)
    ]
    onprem = await harness.client.post(
        harness.environments,
        json={
            "name": "onprem-x",
            "kind": "onprem",
            "config": {"location": "x", "kubeContext": "y", "ingressDomain": INGRESS_DOMAIN},
            "secretRef": secret_ref_for("onprem-x"),
        },
    )

    for response in responses:
        body = response.json()
        assert response.status_code == 422, response.text
        assert body["code"] == "ENVIRONMENT_CONFIG_INVALID"
        assert body["details"] == [{"field": "config.ingressDomain", "reason": "INVALID_FORMAT"}]
    assert onprem.status_code == 422
    assert onprem.json()["details"] == [
        {"field": "config.ingressDomain", "reason": "UNKNOWN_FIELD"}
    ]


# --- AC-7 환경 config 저장 모양 ---


async def test_environment_without_new_fields_is_stored_and_returned_without_extra_keys(
    harness: DeployHarness,
) -> None:
    plain = await harness.client.post(
        harness.environments,
        json={
            "name": "aws-plain",
            "kind": "aws",
            "config": {"region": "ap-northeast-2", "clusterName": "echo-eks"},
            "secretRef": secret_ref_for("aws-plain"),
        },
    )
    rich = await harness.client.post(
        harness.environments,
        json={
            "name": "onprem-rich",
            "kind": "onprem",
            "config": {
                "location": "ec2",
                "kubeContext": "default",
                "apiUrl": "https://k3s-k2f6xje5.apps.echo.test",
                "tunnelTarget": "abc.cfargotunnel.com",
            },
            "secretRef": secret_ref_for("onprem-rich"),
        },
    )

    async with harness.session_factory() as session:
        stored = {
            row.name: row.config
            for row in await session.scalars(
                select(Environment).where(Environment.name.in_(["aws-plain", "onprem-rich"]))
            )
        }
    assert plain.status_code == rich.status_code == 201, (plain.text, rich.text)
    assert stored["aws-plain"] == {"region": "ap-northeast-2", "cluster_name": "echo-eks"}
    assert plain.json()["data"]["config"] == {"region": "ap-northeast-2", "clusterName": "echo-eks"}
    assert stored["onprem-rich"] == {
        "location": "ec2",
        "kube_context": "default",
        "api_url": "https://k3s-k2f6xje5.apps.echo.test",
        "tunnel_target": "abc.cfargotunnel.com",
    }


# --- 5.8 온프레 이미지 받기 자격 갱신 대상 ---


class _RecordingRefresher:
    def __init__(self) -> None:
        self.calls: list[tuple[DeployTarget, DeploymentSpec, timedelta]] = []

    async def refresh_pull_secret(
        self, target: DeployTarget, deployment: DeploymentSpec, min_remaining: timedelta
    ) -> bool:
        self.calls.append((target, deployment, min_remaining))
        return True


async def test_pull_refresher_checks_only_live_onprem_connections_with_current_deployment(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shop-api")
    other = await harness.create_service("billing")
    onprem = await harness.add_environment("onprem-k3s")
    aws = await harness.add_environment("aws-seoul")
    finished = await deploy_and_finish(harness, service["id"], onprem.id)
    await deploy_and_finish(harness, service["id"], aws.id)
    await harness.deploy(other["id"], onprem.id)  # 아직 현재 배포 없음
    refresher = _RecordingRefresher()

    refreshed = await OnpremPullSecretRefresher(
        harness.session_factory, refresher, harness.settings
    ).run_once()

    assert refreshed == 1
    [(target, deployment, minimum)] = refresher.calls
    assert target.environment_kind == "onprem"
    assert target.service_id == uuid.UUID(service["id"])
    assert deployment.deployment_id == uuid.UUID(finished["deploymentId"])
    assert minimum == timedelta(seconds=harness.settings.onprem_pull_min_remaining_seconds)

"""동시성 통합 시험(SPEC api-3 AC-7 일꾼 2개 + 접수 동시, AC-12 연결 생성 잠금)."""

import asyncio

from sqlalchemy import func, select, text

from app.models import Environment, ServiceEnvironment
from app.worker.runner import Worker
from tests.integration.deploy_harness import DeployHarness

ACTIVE_PER_CONNECTION_SQL = text(
    "SELECT max(n) FROM (SELECT count(*) AS n FROM deploy_requests "
    "WHERE status IN ('BUILDING', 'DEPLOYING', 'VERIFYING') "
    "GROUP BY service_environment_id) counts"
)


async def test_two_workers_and_concurrent_requests_never_run_two_on_one_connection(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    onprem = await harness.add_environment("onprem-k3s")
    workers = [harness.worker("worker-a"), harness.worker("worker-b")]
    observed: list[int] = []
    stop = asyncio.Event()

    async def sample() -> None:
        while not stop.is_set():
            async with harness.session_factory() as session:
                observed.append(await session.scalar(ACTIVE_PER_CONNECTION_SQL) or 0)
            await asyncio.sleep(0)

    async def drive(worker: Worker) -> None:
        # 두 일꾼이 같은 시계를 앞으로 돌린다. 합이 임대(30초)를 넘지 않게 해
        # 임대 상실을 만들지 않는다.
        for _ in range(60):
            if not await worker.run_once():
                harness.clock.advance(0.1)

    async def submit() -> list[int]:
        statuses = []
        for index in range(12):
            target = aws if index % 3 else onprem
            response = await harness.request_deploy(
                service["id"], target.id, branch=f"change-{index}"
            )
            statuses.append(response.status_code)
            await asyncio.sleep(0)
        return statuses

    sampler = asyncio.create_task(sample())
    results = await asyncio.gather(drive(workers[0]), drive(workers[1]), submit())
    await harness.run_until_idle()
    stop.set()
    await sampler

    assert results[2] == [202] * 12
    assert observed and max(observed) <= 1
    requests = await harness.list_requests(service["id"], size=100)
    statuses = {item["status"] for item in requests}
    assert statuses <= {"SUCCEEDED", "CANCELLED"}
    assert all(
        item["cancelReason"] == "SUPERSEDED" for item in requests if item["status"] == "CANCELLED"
    )
    detail = await harness.get_service(service["id"])
    roles = sorted(item["role"] for item in detail["environments"])
    assert roles == ["SERVING", "STANDBY"]


async def test_accept_waiting_for_service_lock_does_not_deadlock_with_success_processing(
    harness: DeployHarness,
) -> None:
    """성공 처리처럼 서비스 행을 잡고 연결 행을 두 번 고치는 트랜잭션(두 번째 UPDATE 는 같은
    트랜잭션이 만든 행이라 환경 FK 검사 FOR KEY SHARE 를 다시 잡는다)과 접수가 엇갈려도 교착이 없다.
    """
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    await harness.deploy(service["id"], aws.id)
    await harness.run_until_idle()

    async with harness.session_factory() as holder, holder.begin():
        await holder.execute(
            text("SELECT id FROM services WHERE id = :id FOR NO KEY UPDATE"),
            {"id": service["id"]},
        )
        await holder.execute(
            text("UPDATE service_environments SET traffic_weight = 100 WHERE service_id = :id"),
            {"id": service["id"]},
        )
        accept = asyncio.create_task(harness.request_deploy(service["id"], aws.id, branch="next"))
        for _ in range(100):
            # pg_stat_activity 는 트랜잭션마다 스냅숏이라 잡고 있는 트랜잭션 밖에서 본다.
            async with harness.session_factory() as probe:
                waiting = await probe.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                    )
                )
            if waiting:
                break
            await asyncio.sleep(0.02)
        assert waiting, "accept did not reach the service lock"
        await holder.execute(
            text("UPDATE service_environments SET traffic_weight = 100 WHERE service_id = :id"),
            {"id": service["id"]},
        )

    response = await asyncio.wait_for(accept, timeout=10)

    assert response.status_code == 202, response.text


async def test_environment_delete_and_first_deploy_never_leave_link_to_deleted_environment(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shop-api")
    outcomes = []
    for index in range(8):
        environment = await harness.add_environment(f"env-{index}")
        delete_response, deploy_response = await asyncio.gather(
            harness.client.delete(f"/api/v1/environments/{environment.id}"),
            harness.request_deploy(service["id"], environment.id),
        )
        outcomes.append((delete_response.status_code, deploy_response.status_code))

        async with harness.session_factory() as session:
            stored = await session.get_one(Environment, environment.id)
            live_links = await session.scalar(
                select(func.count())
                .select_from(ServiceEnvironment)
                .where(
                    ServiceEnvironment.environment_id == environment.id,
                    ServiceEnvironment.is_deleted.is_(False),
                )
            )
        assert not (stored.is_deleted and live_links), outcomes
        assert (stored.is_deleted, live_links) in {(True, 0), (False, 1)}

    assert set(outcomes) <= {(204, 404), (409, 202)}

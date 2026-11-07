from uuid import UUID

import httpx
import pytest

from app.executors.migration_pair_job import PairBoundJobExecutor, validate_pair_result
from app.services.migration_pair_registry import MigrationPairBlockedError, MigrationPairRegistry
from tests.test_migration_pairs import NOW, context, registry_data

PLAN = UUID(int=101)


async def test_invalid_job_pair_has_zero_http_transmissions() -> None:
    registry = MigrationPairRegistry.from_config(registry_data())
    snapshot = registry.snapshot(context(), "dbCutover", NOW)
    calls = []

    async def transport(request):
        calls.append(request)
        return httpx.Response(201, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), base_url="http://fixture"
    ) as client:
        executor = PairBoundJobExecutor(
            registry, client, namespace="echo-ops", image="fixture@sha256:" + "1" * 64
        )
        with pytest.raises(MigrationPairBlockedError):
            await executor.start(
                PLAN, context(), snapshot.model_copy(update={"source_db_id": "gcp-b-app"}), NOW
            )
    assert calls == []


async def test_job_manifest_carries_exact_snapshot_and_no_host_or_secret() -> None:
    registry = MigrationPairRegistry.from_config(registry_data())
    snapshot = registry.snapshot(context(), "dbCutover", NOW)
    manifests = []

    async def transport(request):
        import json

        manifests.append(json.loads(request.content))
        return httpx.Response(201, json={"metadata": {"name": "fixture-job"}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), base_url="http://fixture"
    ) as client:
        executor = PairBoundJobExecutor(
            registry, client, namespace="echo-ops", image="fixture@sha256:" + "1" * 64
        )
        await executor.start(PLAN, context(), snapshot, NOW)
    env = {
        e["name"]: e["value"]
        for e in manifests[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["SOURCE_DB"] == "aws-a-app"
    assert env["TARGET_DB"] == "gcp-b-app"
    assert env["DB_PAIR_CONTRACT_VERSION"] == "db-pair/v1"
    assert env["DB_PAIR_REGISTRY_SHA256"] == snapshot.registry_sha256
    assert env["EXPECTED_WRITER_DB"] == "aws-a-app"
    assert env["PLAN_ID"] == str(PLAN)
    assert not any("HOST" in name or "SECRET" in name for name in env)


@pytest.mark.parametrize(
    "change",
    [
        {"targetDbId": "onprem-demo"},
        {"sourceDbId": "gcp-b-app"},
        {"planId": str(UUID(int=999))},
        {"contractVersion": "v0"},
        {"registrySha256": "changed"},
        {"writerDbId": "aws-a-app"},
    ],
)
def test_result_mismatch_including_already_writer_never_succeeds(change) -> None:
    snapshot = MigrationPairRegistry.from_config(registry_data()).snapshot(
        context(), "dbCutover", NOW
    )
    receipt = {
        "planId": str(PLAN),
        "sourceDbId": snapshot.source_db_id,
        "targetDbId": snapshot.target_db_id,
        "contractVersion": snapshot.contract_version,
        "registrySha256": snapshot.registry_sha256,
        "writerDbId": snapshot.target_db_id,
        "alreadyWriter": True,
    }
    validate_pair_result(PLAN, snapshot, receipt)
    with pytest.raises(MigrationPairBlockedError):
        validate_pair_result(PLAN, snapshot, {**receipt, **change})

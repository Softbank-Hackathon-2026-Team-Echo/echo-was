"""명시 DB쌍 Job 송신 경계. 기존 worker 연결은 별도이며 legacy fallback은 없다."""

from datetime import datetime
from typing import Any
from uuid import UUID

import httpx

from app.enums import CutoverKind
from app.executors.base import CutoverRunTarget, DbCutoverJobKind, DbCutoverRequest
from app.executors.kubernetes.cutover_job import cutover_job_manifest
from app.schemas.migration_capability import MigrationPairContext, MigrationPairSnapshot
from app.services.migration_pair_registry import MigrationPairRegistry, blocked


def pair_job_manifest(
    plan_id: UUID, snapshot: MigrationPairSnapshot, *, namespace: str, image: str
) -> dict[str, Any]:
    if snapshot.job_kind is None:
        raise blocked("DB_JOB_OPERATION_REQUIRED")
    request = DbCutoverRequest(
        plan_id=plan_id,
        job_kind=DbCutoverJobKind(snapshot.job_kind),
        allow_promote=snapshot.operation == "disasterPromote",
        target=CutoverRunTarget(snapshot.target_environment_id, "registered"),
        result_kind=CutoverKind.ROLLBACK
        if snapshot.job_kind == "rollback"
        else CutoverKind.CUTOVER,
    )
    manifest = cutover_job_manifest(request, namespace=namespace, image=image)
    env = manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    env.extend({"name": name, "value": value} for name, value in snapshot.job_environment().items())
    return manifest


class PairBoundJobExecutor:
    def __init__(
        self,
        registry: MigrationPairRegistry,
        client: httpx.AsyncClient,
        *,
        namespace: str,
        image: str,
    ) -> None:
        self._registry = registry
        self._client = client
        self._namespace = namespace
        self._image = image

    async def start(
        self,
        plan_id: UUID,
        context: MigrationPairContext | dict[str, Any],
        snapshot: MigrationPairSnapshot,
        now: datetime,
    ) -> httpx.Response:
        """등록표·배포·writer·freshness를 다시 확인한 뒤에만 HTTP 송신한다."""
        manifest = self._registry.dispatch(
            context,
            snapshot.operation,
            snapshot,
            now,
            lambda pinned: pair_job_manifest(
                plan_id, pinned, namespace=self._namespace, image=self._image
            ),
        )
        # 기존 VAP/이미지는 v1 입력을 받지 않으므로 활성화 전에 infra 단계2가 필수다.
        # 400/422를 legacy 3-env Job으로 재전송하지 않는다.
        return await self._client.post(
            f"/apis/batch/v1/namespaces/{self._namespace}/jobs", json=manifest
        )


def validate_pair_result(
    plan_id: UUID, snapshot: MigrationPairSnapshot, receipt: dict[str, Any]
) -> None:
    """ALREADY_WRITER도 같은 계획·쌍·등록표·도착 writer 증거가 없으면 성공이 아니다."""
    expected = {
        "planId": str(plan_id),
        "sourceDbId": snapshot.source_db_id,
        "targetDbId": snapshot.target_db_id,
        "contractVersion": snapshot.contract_version,
        "registrySha256": snapshot.registry_sha256,
        "writerDbId": snapshot.target_db_id,
    }
    if any(receipt.get(name) != value for name, value in expected.items()):
        raise blocked("DB_PAIR_RESULT_MISMATCH")

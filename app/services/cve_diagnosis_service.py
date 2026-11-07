"""CVE proposals use the current deployment, never a fabricated failed request."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction
from app.audit.recorder import AuditActor, AuditRecorder
from app.core.clock import Clock
from app.core.exceptions import (
    ConflictError,
    FindingNotFoundError,
    PatchStaleError,
    PrecheckInProgressError,
    RemediationNotAvailableError,
    ServiceDeletingError,
)
from app.core.log_masking import mask_secrets_in_json
from app.core.repo_url import parse_repo_url
from app.enums import (
    AgentRunKind,
    AgentRunStatus,
    DeployRequestStatus,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingSource,
    ServiceStatus,
)
from app.models import AgentRun, Deployment, DeployRequest, Service, ServiceEnvironment
from app.repositories.agent_run_repository import AgentRunRepository
from app.repositories.cve_watch_repository import CveWatchRepository
from app.repositories.service_repository import ServiceRepository
from app.services.agent_run_service import RUN_DEADLINE, expire_overdue_runs, run_target
from app.services.precheck_service import PrecheckView, compute_input_hash, load_precheck_view


async def current_cve_request(session: AsyncSession, run: AgentRun) -> DeployRequest | None:
    snapshot = run.input_snapshot
    if snapshot.get("trigger") != "CVE_WATCH":
        if run.deploy_request_id is None:
            return None
        return await session.get(DeployRequest, run.deploy_request_id)
    target = snapshot.get("currentDeployment") or {}
    request = await session.scalar(
        select(DeployRequest)
        .join(Service, Service.id == DeployRequest.service_id)
        .join(Deployment, Deployment.id == DeployRequest.deployment_id)
        .join(ServiceEnvironment, ServiceEnvironment.current_deployment_id == Deployment.id)
        .where(
            DeployRequest.id == uuid.UUID(target["deployRequestId"]),
            Service.organization_id == run.organization_id,
            Service.is_deleted.is_(False),
            ServiceEnvironment.service_id == run.service_id,
            ServiceEnvironment.environment_id == run.target_environment_id,
            DeployRequest.service_id == run.service_id,
            DeployRequest.environment_id == run.target_environment_id,
            DeployRequest.commit_sha == snapshot["sourceCommitSha"],
            Deployment.image_digest == snapshot["imageDigest"],
            ServiceEnvironment.is_deleted.is_(False),
        )
    )
    if request is None:
        raise PatchStaleError("imageDigest", "CVE_TARGET_CHANGED")
    return request


async def check_cve_target(session: AsyncSession, run: AgentRun) -> None:
    if run.input_snapshot.get("trigger") == "CVE_WATCH":
        if run.kind == AgentRunKind.APPLY_PATCH:
            parent = await session.get(AgentRun, run.parent_run_id)
            if parent is None:
                raise PatchStaleError("imageDigest", "CVE_TARGET_CHANGED")
            run = parent
        await current_cve_request(session, run)


class CveDiagnosisService:
    def __init__(
        self, session: AsyncSession, clock: Clock, demo_service_ids: tuple[uuid.UUID, ...]
    ) -> None:
        self._session = session
        self._clock = clock
        self._demo_service_ids = demo_service_ids

    async def create_diagnosis(
        self, organization_id: uuid.UUID, finding_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> tuple[PrecheckView, bool]:
        now = self._clock.now()
        runs = AgentRunRepository(self._session)
        audit = AuditRecorder(self._session, self._clock)
        async with self._session.begin():
            findings = CveWatchRepository(self._session)
            finding = await findings.find_finding(organization_id, finding_id)
            if finding is None:
                raise FindingNotFoundError(finding_id=str(finding_id))
            remediation = finding.remediation or {}
            if (
                finding.control_id != SecurityControlId.IMG_1
                or finding.source != SecurityFindingSource.CVE_WATCH
                or remediation.get("kind") != "DEPENDENCY_BUMP"
                or not remediation.get("toVersion")
                or finding.service_id is None
            ):
                raise RemediationNotAvailableError(finding_id=str(finding_id))
            service = await ServiceRepository(self._session).get_live_by_id(
                organization_id, finding.service_id, lock=True
            )
            if service.status == ServiceStatus.DELETING:
                raise ServiceDeletingError(service_id=str(service.id))
            synthetic = finding.mode == SecurityFindingMode.SIMULATED
            if synthetic and service.id not in self._demo_service_ids:
                raise ConflictError(
                    "Synthetic advisory execution is limited to designated demo services"
                )
            requests = list(
                (
                    await self._session.scalars(
                        select(DeployRequest)
                        .join(Service, Service.id == DeployRequest.service_id)
                        .join(Deployment, DeployRequest.deployment_id == Deployment.id)
                        .join(
                            ServiceEnvironment,
                            ServiceEnvironment.current_deployment_id == Deployment.id,
                        )
                        .where(
                            Service.organization_id == organization_id,
                            DeployRequest.service_id == service.id,
                            DeployRequest.status == DeployRequestStatus.SUCCEEDED,
                            Deployment.image_digest == finding.image_digest,
                            ServiceEnvironment.is_deleted.is_(False),
                            ServiceEnvironment.service_id == service.id,
                            ServiceEnvironment.environment_id == DeployRequest.environment_id,
                            *(
                                [DeployRequest.environment_id == finding.environment_id]
                                if finding.environment_id
                                else []
                            ),
                        )
                        .order_by(DeployRequest.created_at.desc())
                    )
                ).all()
            )
            if not requests or finding.expires_at <= now:
                raise PatchStaleError("imageDigest", "CVE_TARGET_CHANGED")
            request = requests[0]
            deployment = await self._session.get(Deployment, request.deployment_id)
            assert deployment is not None
            await expire_overdue_runs(runs, audit, now, service_id=service.id)
            existing = await runs.find_open_for_service(service.id)
            if existing is not None:
                if existing.finding_id == finding.id:
                    return await load_precheck_view(self._session, existing), False
                raise PrecheckInProgressError(str(existing.id))
            dependencies = await findings.find_latest_snapshot(service.id, deployment.image_digest)
            snapshot = mask_secrets_in_json(
                {
                    "schemaVersion": 1,
                    "trigger": "CVE_WATCH",
                    "finding": {
                        "id": str(finding.id),
                        "controlId": finding.control_id,
                        "advisoryId": remediation.get("advisoryId"),
                        "aliases": remediation.get("aliases", []),
                        "severity": remediation.get("severity"),
                        "synthetic": synthetic,
                    },
                    "remediation": remediation,
                    "service": {
                        "id": str(service.id),
                        "name": service.name,
                        "repo": parse_repo_url(request.repo_url).full_name,
                        "containerPort": service.container_port,
                        "healthCheckPath": service.health_check_path,
                    },
                    "environment": {"id": str(request.environment_id)},
                    "imageDigest": deployment.image_digest,
                    "imageRef": f"{deployment.image_repository}@{deployment.image_digest}",
                    "sourceCommitSha": request.commit_sha,
                    "currentDeployment": {
                        "id": str(deployment.id),
                        "deployRequestId": str(request.id),
                    },
                    "packages": dependencies.packages if dependencies else [],
                }
            )
            run = AgentRun(
                organization_id=organization_id,
                service_id=service.id,
                kind=AgentRunKind.CHANGE_PROPOSAL,
                finding_id=finding.id,
                target_environment_id=request.environment_id,
                input_snapshot=snapshot,
                input_hash=compute_input_hash(snapshot),
                requested_by_user_id=actor_user_id,
                deadline_at=now + RUN_DEADLINE,
                created_at=now,
                updated_at=now,
                simulated=False,
                status=AgentRunStatus.QUEUED,
            )
            self._session.add(run)
            await self._session.flush()
            audit.record(
                organization_id=organization_id,
                actor=AuditActor.user(actor_user_id),
                action=AuditAction.CVE_DIAGNOSIS_REQUESTED,
                target=run_target(run),
                details={"findingId": str(finding.id), "syntheticAdvisory": synthetic},
            )
            return await load_precheck_view(self._session, run), True

"""고객 이미지 적용 전 C-IMG-1/2 판정. 이관 대상/DB/PRECHECK 를 요구하지 않는다."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.enums import (
    BuildStatus,
    DeployRequestStatus,
    SecurityCheckPhase,
    SecurityControlId,
    SecurityFindingSource,
    SecurityResult,
)
from app.models import Build, Deployment, DeployRequest, Environment, Service
from app.repositories.finding_repository import FindingRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.security_exception_repository import SecurityExceptionRepository
from app.services.security_exception_service import to_security_exception
from app.services.security_finding_service import to_security_finding
from app.services.security_rules import (
    POLICY_VERSION,
    SecurityDecision,
    SecurityScope,
    SecurityTarget,
    evaluate_control,
    get_rule,
)
from app.services.security_scan_collector import build_scan_source_ref

_REQUIRED_SOURCES = {
    SecurityControlId.IMG_1: (SecurityFindingSource.TRIVY_IMAGE,),
    SecurityControlId.IMG_2: (SecurityFindingSource.TRIVY_SECRET, SecurityFindingSource.GITLEAKS),
}


@dataclass(frozen=True)
class DeploySecurityCheck:
    decision: SecurityDecision
    artifact_matches: bool
    missing_sources: tuple[SecurityFindingSource, ...]

    @property
    def is_blocked(self) -> bool:
        return not self.artifact_matches or self.decision.is_blocked

    def log_data(self) -> dict[str, Any]:
        return {
            "checkType": "DEPLOY_IMAGE_V1",
            "policyVersion": self.decision.policy_version,
            "phase": self.decision.phase.value,
            "decidedAt": self.decision.decided_at.isoformat(),
            "artifactMatches": self.artifact_matches,
            "missingSources": list(self.missing_sources),
            "findingIds": [str(id_) for id_ in self.decision.finding_ids],
            "exceptionIds": [str(id_) for id_ in self.decision.exception_ids],
            "controls": [
                {
                    "controlId": control.control_id,
                    "result": control.result,
                    "effectiveResult": control.effective_result,
                    "isBlocking": control.is_blocking,
                    "reasons": list(control.reasons),
                }
                for control in self.decision.controls
            ],
        }


class DeploySecurityGate:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def check(
        self,
        request: DeployRequest,
        build: Build | None,
        deployment: Deployment,
        service: Service,
        environment: Environment,
        now: datetime,
    ) -> DeploySecurityCheck:
        """호출자가 잠근 서비스/요청의 트랜잭션 안에서 판정한다. 외부 I/O 없음."""
        organization = await OrganizationRepository(self._session).find_by_id(
            service.organization_id
        )
        assert organization is not None
        target = SecurityTarget(
            scope=SecurityScope(
                organization_id=service.organization_id,
                service_id=service.id,
                environment_id=environment.id,
                image_digest=deployment.image_digest,
            ),
            environment_kind=environment.kind,
            allow_self_approval=organization.allow_self_approval,
        )
        artifact_matches = (
            build is not None
            and build.status == BuildStatus.SUCCEEDED
            and not build.simulated
            and request.status == DeployRequestStatus.DEPLOYING
            and request.deployment_id == deployment.id
            and request.environment_id == environment.id
            and build.service_id == request.service_id == service.id
            and build.repo_url == request.repo_url
            and build.commit_sha == request.commit_sha
            and build.image_digest == deployment.image_digest
            and build.image_repository == deployment.image_repository
            and deployment.service_environment_id == request.service_environment_id
        )
        models = await FindingRepository(self._session).search_for_deploy(
            service.organization_id, deployment.image_digest, now
        )
        findings = [to_security_finding(model) for model in models]
        current_ids = {
            model.id
            for model in models
            if build is not None
            and (model.source_ref or "").startswith(build_scan_source_ref(build.id))
        }
        exceptions = [
            to_security_exception(model)
            for model in await SecurityExceptionRepository(self._session).search_for_judgement(
                service.organization_id, now
            )
        ]
        now = self._clock.now()
        controls = []
        missing_sources = []
        for control_id, sources in _REQUIRED_SOURCES.items():
            rule = get_rule(control_id)
            missing = []
            for source in sources:
                current = [
                    finding
                    for finding in findings
                    if finding.finding_id in current_ids and finding.source == source
                ]
                checked = evaluate_control(
                    rule, target, SecurityCheckPhase.PREPARATION, current, (), now
                )
                if not checked.finding_ids:
                    missing.append(source)
            missing_sources.extend(missing)
            # 같은 digest 의 다른 빌드 PASS 는 이번 소스 검사의 누락을 대신하지 않는다.
            # 유효한 과거 FAIL/UNKNOWN 과 CVE 경고는 계속 판정에 포함한다.
            usable = (
                findings
                if not missing
                else [
                    finding
                    for finding in findings
                    if finding.control_id != control_id
                    or finding.result in (SecurityResult.FAIL, SecurityResult.UNKNOWN)
                    or finding.is_ai
                ]
            )
            controls.append(
                evaluate_control(
                    rule, target, SecurityCheckPhase.PREPARATION, usable, exceptions, now
                )
            )
        return DeploySecurityCheck(
            SecurityDecision(
                POLICY_VERSION, SecurityCheckPhase.PREPARATION, None, now, tuple(controls)
            ),
            artifact_matches,
            tuple(missing_sources),
        )

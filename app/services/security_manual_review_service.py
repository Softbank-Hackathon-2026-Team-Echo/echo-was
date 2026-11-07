"""사람 점검 기록 API 유스케이스(C-IAM-1·C-TLS-2 등 사람 점검을 받는 통제).

수집기가 없는 선택 통제를 사람이 실제로 점검한 뒤 결과를 근거 링크와 함께 남긴다. 규칙
- 개발자 이상 사람만 부른다(검사기가 막는다, 에이전트 불가). 점검자는 부른 사람이다.
- 통제는 규칙표 출처에 사람 점검(`MANUAL_REVIEW`)이 있는 **선택 통제**만(아니면 422
  `MANUAL_REVIEW_NOT_ALLOWED`). 필수 통제 C-SEC-1 은 출처에 사람 점검이 있어도 받지 않는다
  (FAIL 한 건이 게이트를 BLOCKED 로 바꾼다). 결과는 PASS·WARN·FAIL 만.
- 근거 링크(https, 호스트·포트가 맞는 주소)와 요약은 반드시 있어야 한다(없거나 틀리면 422).
  자기 요청이라도 같다.
- 범위 칸은 그 통제가 정한 것만, 모두 채운다(규칙표 `scope_keys`, 기한부 예외와 같은 규칙).
  정한 칸이 없는 통제는 조직 전체다. 서비스·환경은 이 조직의 살아 있는 것이어야 한다(404).
- 만료는 앱 시계의 지금 + 규칙표 `max_age`(사람 점검 통제는 7일).
- 증거 행과 감사 줄(USER, `SECURITY_MANUAL_REVIEW_RECORDED`, 대상 `finding`, 이름표 = 통제 ID)을
  같은 트랜잭션에 남긴다. 요약 원문은 증거 행에만 있다.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction, AuditTargetType
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.core.clock import Clock
from app.core.exceptions import ErrorField, RequestFieldsInvalidError
from app.enums import SecurityControlId, SecurityFindingSource, SecurityResult
from app.models import Finding
from app.repositories.environment_repository import EnvironmentRepository
from app.repositories.finding_repository import FindingRepository
from app.repositories.service_repository import ServiceRepository
from app.services.security_finding_service import (
    MANUAL_REVIEW_RESULTS,
    build_manual_review,
    is_https_url,
    to_finding_model,
)
from app.services.security_rules import ScopeKey, SecurityScope, get_rule

logger = logging.getLogger(__name__)

# 사람 점검 API 가 받는 범위 칸(요청 JSON camelCase). 사람 점검 통제의 범위는 이 둘뿐이다.
MANUAL_REVIEW_SCOPE_FIELDS = {
    ScopeKey.SERVICE: "serviceId",
    ScopeKey.ENVIRONMENT: "environmentId",
}
MANUAL_REVIEW_NOT_ALLOWED_REASON = "MANUAL_REVIEW_NOT_ALLOWED"
MANUAL_REVIEW_RESULT_INVALID_REASON = "MANUAL_REVIEW_RESULT_INVALID"
EVIDENCE_URL_INVALID_REASON = "EVIDENCE_URL_INVALID"
REQUIRED_FOR_CONTROL_REASON = "REQUIRED_FOR_CONTROL"
NOT_ALLOWED_FOR_CONTROL_REASON = "NOT_ALLOWED_FOR_CONTROL"


@dataclass(frozen=True)
class RecordManualReviewCommand:
    control_id: SecurityControlId
    result: SecurityResult
    summary: str
    evidence_url: str
    service_id: uuid.UUID | None = None
    environment_id: uuid.UUID | None = None

    def value_of(self, key: ScopeKey) -> uuid.UUID | None:
        match key:
            case ScopeKey.SERVICE:
                return self.service_id
            case ScopeKey.ENVIRONMENT:
                return self.environment_id
            case _:
                return None


def manual_review_controls() -> tuple[SecurityControlId, ...]:
    """사람 점검 API 로 기록할 수 있는 통제.

    규칙표 출처에 MANUAL_REVIEW 가 있고, 범위가 맞고, **선택 통제**인 것. 필수 통제
    (C-SEC-1 등)는 규칙표가 사람 점검 출처를 받더라도 이 API 로는 받지 않는다 — 필수
    통제에 FAIL 을 남기면 그 칸의 게이트가 PASS→BLOCKED 로 바뀌어, 시연·운영 중 잘못 넣은
    한 건이 이전을 막고 지우는 API 도 없기 때문이다.
    """
    return tuple(
        control_id for control_id in SecurityControlId if _accepts_manual_review(control_id)
    )


def _accepts_manual_review(control_id: SecurityControlId) -> bool:
    rule = get_rule(control_id)
    return (
        SecurityFindingSource.MANUAL_REVIEW in rule.sources
        and not rule.is_mandatory
        and rule.scope_keys <= set(MANUAL_REVIEW_SCOPE_FIELDS)
    )


class SecurityManualReviewService:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._findings = FindingRepository(session)
        self._services = ServiceRepository(session)
        self._environments = EnvironmentRepository(session)
        self._audit = AuditRecorder(session, clock)

    async def record_review(
        self,
        organization_id: uuid.UUID,
        reviewer_user_id: uuid.UUID,
        command: RecordManualReviewCommand,
    ) -> Finding:
        """사람 점검 증거 한 건을 남기고 저장한 행을 돌려준다. 규칙에 맞지 않으면 422."""
        _check_command(command)
        now = self._clock.now()
        normalized = build_manual_review(
            control_id=command.control_id,
            result=command.result,
            scope=SecurityScope(
                organization_id=organization_id,
                service_id=command.service_id,
                environment_id=command.environment_id,
            ),
            reviewer_user_id=reviewer_user_id,
            basis_url=command.evidence_url,
            collected_at=now,
            summary=command.summary,
        )
        finding = to_finding_model(normalized)
        async with self._session.begin():
            if command.service_id is not None:
                await self._services.get_live_by_id(organization_id, command.service_id)
            if command.environment_id is not None:
                await self._environments.get_live_by_id(organization_id, command.environment_id)
            self._findings.add(finding)
            await self._session.flush()
            self._audit.record(
                organization_id=organization_id,
                actor=AuditActor.user(reviewer_user_id),
                action=AuditAction.SECURITY_MANUAL_REVIEW_RECORDED,
                target=AuditTarget(AuditTargetType.FINDING, finding.id, str(command.control_id)),
                details=_audit_details(command, finding),
            )
        logger.info(
            "security manual review recorded",
            extra={
                "action": "record_security_manual_review",
                "finding_id": str(finding.id),
                "organization_id": str(organization_id),
                "control_id": str(command.control_id),
                "result": str(command.result),
            },
        )
        return finding


def _check_command(command: RecordManualReviewCommand) -> None:
    """통제·결과·근거 링크·범위 칸을 한 번에 본다(통제가 안 맞으면 범위는 보지 않는다)."""
    if not _accepts_manual_review(command.control_id):
        raise RequestFieldsInvalidError(
            [ErrorField(field="controlId", reason=MANUAL_REVIEW_NOT_ALLOWED_REASON)]
        )
    details: list[ErrorField] = []
    if command.result not in MANUAL_REVIEW_RESULTS:
        details.append(ErrorField(field="result", reason=MANUAL_REVIEW_RESULT_INVALID_REASON))
    if not is_https_url(command.evidence_url):
        details.append(ErrorField(field="evidenceUrl", reason=EVIDENCE_URL_INVALID_REASON))
    required = get_rule(command.control_id).scope_keys
    for key, field_name in MANUAL_REVIEW_SCOPE_FIELDS.items():
        has_value = command.value_of(key) is not None
        if key in required and not has_value:
            details.append(ErrorField(field=field_name, reason=REQUIRED_FOR_CONTROL_REASON))
        elif key not in required and has_value:
            details.append(ErrorField(field=field_name, reason=NOT_ALLOWED_FOR_CONTROL_REASON))
    if details:
        raise RequestFieldsInvalidError(details)


def _audit_details(command: RecordManualReviewCommand, finding: Finding) -> dict[str, str]:
    details = {
        "result": str(command.result),
        "expiresAt": finding.expires_at.isoformat(),
    }
    if command.service_id is not None:
        details["serviceId"] = str(command.service_id)
    if command.environment_id is not None:
        details["environmentId"] = str(command.environment_id)
    return details

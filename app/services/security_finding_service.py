"""보안 증거 기록·조회(보안·거버넌스 2).

- 정규화한 증거(`NormalizedFinding`)를 `findings` 행으로 남긴다. 근거 줄은 JSON 목록이고
  값이 없는 칸은 키로 남기지 않는다. 비밀값 원문은 정규화 단계에서 이미 버렸다.
- 사람 점검 기록은 그 통제가 사람 점검을 받을 때만, 점검자·https 근거 링크와 함께 남긴다.
  만료는 수집 시각 + 통제 유효기간이다.
- 판정용 조회는 그 조직의 만료 전 증거만 돌려준다. 범위·출처·정책 버전 비교는 규칙표가 한다.
"""

import logging
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import RequestFieldInvalidError
from app.enums import (
    AiFindingConfirmation,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingReason,
    SecurityFindingSource,
    SecurityResult,
)
from app.models import Finding
from app.repositories.finding_repository import FindingRepository
from app.services.security_normalizers import FindingItem, NormalizedFinding
from app.services.security_rules import (
    POLICY_VERSION,
    SecurityFinding,
    SecurityScope,
    get_rule,
)

logger = logging.getLogger(__name__)

_MANUAL_REVIEW_RESULTS = frozenset({SecurityResult.PASS, SecurityResult.WARN, SecurityResult.FAIL})


class SecurityFindingService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._findings = FindingRepository(session)

    async def record(
        self, normalized: NormalizedFinding, source_ref: str | None = None
    ) -> uuid.UUID:
        """정규화한 증거 한 건을 남긴다."""
        model = to_finding_model(normalized, source_ref)
        async with self._session.begin():
            self._findings.add(model)
        logger.info(
            "security finding recorded",
            extra={
                "action": "record_security_finding",
                "finding_id": str(model.id),
                "organization_id": str(model.organization_id),
                "control_id": model.control_id,
                "result": model.result,
            },
        )
        return model.id

    async def record_manual_review(
        self,
        *,
        control_id: SecurityControlId,
        result: SecurityResult,
        scope: SecurityScope,
        reviewer_user_id: uuid.UUID,
        basis_url: str,
        collected_at: datetime,
    ) -> uuid.UUID:
        """사람 점검 기록 한 건. 그 통제가 사람 점검을 받지 않으면 거절한다."""
        rule = get_rule(control_id)
        if SecurityFindingSource.MANUAL_REVIEW not in rule.sources:
            raise RequestFieldInvalidError("controlId", "MANUAL_REVIEW_NOT_ALLOWED")
        if result not in _MANUAL_REVIEW_RESULTS:
            raise RequestFieldInvalidError("result", "MANUAL_REVIEW_RESULT_INVALID")
        if not _is_https_url(basis_url):
            raise RequestFieldInvalidError("basisUrl", "BASIS_URL_INVALID")
        finding = SecurityFinding(
            finding_id=uuid.uuid4(),
            control_id=control_id,
            result=result,
            mode=SecurityFindingMode.REAL,
            source=SecurityFindingSource.MANUAL_REVIEW,
            scope=scope,
            collected_at=collected_at,
            expires_at=collected_at + rule.max_age,
            policy_version=POLICY_VERSION,
            reviewer_user_id=reviewer_user_id,
            basis_url=basis_url,
        )
        normalized = NormalizedFinding(
            finding, SecurityFindingReason.MANUAL_REVIEW_RECORDED, (), 0, None
        )
        return await self.record(normalized)

    async def search_for_judgement(
        self, organization_id: uuid.UUID, now: datetime
    ) -> list[SecurityFinding]:
        async with self._session.begin():
            models = await self._findings.search_unexpired_by_organization(organization_id, now)
        return [to_security_finding(model) for model in models]


def to_finding_model(normalized: NormalizedFinding, source_ref: str | None = None) -> Finding:
    finding = normalized.finding
    scope = finding.scope
    confirmation = finding.confirmation
    if finding.is_ai and confirmation is None:
        confirmation = AiFindingConfirmation.UNCONFIRMED
    return Finding(
        id=finding.finding_id,
        organization_id=scope.organization_id,
        service_id=scope.service_id,
        environment_id=scope.environment_id,
        image_digest=scope.image_digest,
        config_commit=scope.config_commit,
        was_version=scope.was_version,
        control_id=finding.control_id,
        result=finding.result,
        mode=finding.mode,
        source=finding.source,
        reason=normalized.reason,
        policy_version=finding.policy_version,
        tool_version=normalized.tool_version,
        source_ref=source_ref,
        items=[_item_json(item) for item in normalized.items],
        total_items=normalized.total_items,
        reviewer_user_id=finding.reviewer_user_id,
        basis_url=finding.basis_url,
        confirmation=confirmation,
        collected_at=finding.collected_at,
        expires_at=finding.expires_at,
    )


def to_security_finding(model: Finding) -> SecurityFinding:
    return SecurityFinding(
        finding_id=model.id,
        control_id=SecurityControlId(model.control_id),
        result=SecurityResult(model.result),
        mode=SecurityFindingMode(model.mode),
        source=SecurityFindingSource(model.source),
        scope=SecurityScope(
            organization_id=model.organization_id,
            service_id=model.service_id,
            environment_id=model.environment_id,
            image_digest=model.image_digest,
            config_commit=model.config_commit,
            was_version=model.was_version,
        ),
        collected_at=model.collected_at,
        expires_at=model.expires_at,
        policy_version=model.policy_version,
        reviewer_user_id=model.reviewer_user_id,
        basis_url=model.basis_url,
        confirmation=AiFindingConfirmation(model.confirmation) if model.confirmation else None,
    )


def _item_json(item: FindingItem) -> dict[str, Any]:
    return {key: value for key, value in asdict(item).items() if value is not None}


def _is_https_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme == "https" and bool(parts.netloc)

"""보안 증거 기록·조회(보안·거버넌스 2).

- 정규화한 증거(`NormalizedFinding`)를 `findings` 행으로 남긴다. 근거 줄은 JSON 목록이고
  값이 없는 칸은 키로 남기지 않는다. 비밀값 원문은 정규화 단계에서 이미 버렸다.
- 사람 점검 기록은 그 통제가 사람 점검을 받을 때만, 점검자·https 근거 링크와 함께 남긴다.
  만료는 수집 시각 + 통제 유효기간이다.
- 판정용 조회는 그 조직의 만료 전 증거만 돌려준다. 범위·출처·정책 버전 비교는 규칙표가 한다.
"""

import ipaddress
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

MANUAL_REVIEW_RESULTS = frozenset({SecurityResult.PASS, SecurityResult.WARN, SecurityResult.FAIL})
# 사람 점검 요약을 담는 근거 줄의 itemId(칸을 따로 두지 않아 마이그레이션이 없다).
MANUAL_REVIEW_SUMMARY_ITEM_ID = "MANUAL_REVIEW_SUMMARY"


class SecurityFindingService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._findings = FindingRepository(session)

    async def record(
        self, normalized: NormalizedFinding, source_ref: str | None = None
    ) -> uuid.UUID:
        """정규화한 증거 한 건을 남긴다."""
        return (await self.record_all([normalized], source_ref))[0]

    async def record_all(
        self, normalized_findings: list[NormalizedFinding], source_ref: str | None = None
    ) -> list[uuid.UUID]:
        """같은 원문에서 나온 증거 여러 건을 한 트랜잭션으로 남긴다."""
        models = [to_finding_model(normalized, source_ref) for normalized in normalized_findings]
        async with self._session.begin():
            for model in models:
                self._findings.add(model)
        for model in models:
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
        return [model.id for model in models]

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
        normalized = build_manual_review(
            control_id=control_id,
            result=result,
            scope=scope,
            reviewer_user_id=reviewer_user_id,
            basis_url=basis_url,
            collected_at=collected_at,
        )
        return await self.record(normalized)

    async def search_for_judgement(
        self, organization_id: uuid.UUID, now: datetime
    ) -> list[SecurityFinding]:
        async with self._session.begin():
            models = await self._findings.search_unexpired_by_organization(organization_id, now)
        return [to_security_finding(model) for model in models]


def build_manual_review(
    *,
    control_id: SecurityControlId,
    result: SecurityResult,
    scope: SecurityScope,
    reviewer_user_id: uuid.UUID,
    basis_url: str,
    collected_at: datetime,
    summary: str | None = None,
) -> NormalizedFinding:
    """사람 점검 기록 한 건을 증거 형태로 만든다(저장 전, 순수 계산).

    만료는 수집 시각 + 통제 유효기간(규칙표 `max_age`)이다. 요약은 근거 줄 하나
    (`MANUAL_REVIEW_SUMMARY_ITEM_ID`, `target` = 요약)로 남긴다.
    """
    rule = get_rule(control_id)
    if SecurityFindingSource.MANUAL_REVIEW not in rule.sources:
        raise RequestFieldInvalidError("controlId", "MANUAL_REVIEW_NOT_ALLOWED")
    if result not in MANUAL_REVIEW_RESULTS:
        raise RequestFieldInvalidError("result", "MANUAL_REVIEW_RESULT_INVALID")
    if not is_https_url(basis_url):
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
    items: tuple[FindingItem, ...] = ()
    if summary is not None:
        items = (
            FindingItem(
                item_id=MANUAL_REVIEW_SUMMARY_ITEM_ID,
                target=summary,
                reason=SecurityFindingReason.MANUAL_REVIEW_RECORDED,
            ),
        )
    return NormalizedFinding(
        finding, SecurityFindingReason.MANUAL_REVIEW_RECORDED, items, len(items), None
    )


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
        environment_revision=finding.environment_revision,
        collection_key=finding.collection_key,
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
        environment_revision=model.environment_revision,
        collection_key=model.collection_key,
        reviewer_user_id=model.reviewer_user_id,
        basis_url=model.basis_url,
        confirmation=AiFindingConfirmation(model.confirmation) if model.confirmation else None,
    )


def _item_json(item: FindingItem) -> dict[str, Any]:
    return {key: value for key, value in asdict(item).items() if value is not None}


def is_https_url(value: str) -> bool:
    """근거 링크로 쓸 수 있는 https 주소인가(구조만 본다, 링크를 가져오지 않는다).

    - 공백·제어 문자가 하나라도 있으면 아니다(복사 실수, `https:// /review`).
    - scheme 은 `https` 만. 호스트가 있어야 하고, 이름(글자·숫자·`-` 의 점 구분 낱말)
      또는 대괄호 IPv6 주소여야 한다. 포트가 있으면 1~65535 정수여야 한다.
    - 파싱 예외(`https://[`)는 삼키고 False — 부르는 쪽이 422 로 바꾼다.
    """
    if any(char.isspace() or not char.isprintable() for char in value):
        return False
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or not hostname:
        return False
    if port == 0:
        return False
    if parts.netloc.rpartition("@")[2].startswith("["):
        return _is_ipv6(hostname)
    return _is_host_name(hostname)


def _is_ipv6(hostname: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(hostname), ipaddress.IPv6Address)
    except ValueError:
        return False


def _is_host_name(hostname: str) -> bool:
    labels = hostname.removesuffix(".").split(".")
    return all(
        0 < len(label) <= 63
        and not label.startswith("-")
        and not label.endswith("-")
        and all(char.isalnum() or char == "-" for char in label)
        for label in labels
    )

"""CVE 감시 API 입출력(SPEC sec-6-cve-watch)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.enums import SecurityResult
from app.schemas.response import ApiModel, ApiRequestModel
from app.services.cve_watch_service import CveWatchMatchView, CveWatchRunView, SimulatedAdvisory

_DEFAULT = SimulatedAdvisory()
type AdvisorySeverity = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]


class SimulatedAdvisoryRequest(ApiRequestModel):
    """가짜 권고. 칸을 비우면 React2Shell(Node.js RCE) 모양 기본값이다."""

    id: str = Field(default=_DEFAULT.advisory_id, min_length=1, max_length=64)
    summary: str = Field(default=_DEFAULT.summary, min_length=1, max_length=300)
    severity: AdvisorySeverity = "CRITICAL"
    ecosystem: str = Field(default=_DEFAULT.ecosystem, min_length=1, max_length=40)
    package: str = Field(default=_DEFAULT.package, min_length=1, max_length=200)
    introduced: str = Field(default=_DEFAULT.introduced, min_length=1, max_length=100)
    fixed: str | None = Field(default=_DEFAULT.fixed, min_length=1, max_length=100)

    def to_advisory(self) -> SimulatedAdvisory:
        return SimulatedAdvisory(
            advisory_id=self.id,
            summary=self.summary,
            severity=self.severity,
            ecosystem=self.ecosystem,
            package=self.package,
            introduced=self.introduced,
            fixed=self.fixed,
        )


class CveWatchInjectRequest(ApiRequestModel):
    advisory: SimulatedAdvisoryRequest = Field(default_factory=SimulatedAdvisoryRequest)
    # 주면 그 서비스만 맞춰 본다.
    service_id: uuid.UUID | None = None


class CveWatchMatchResponse(ApiModel):
    finding_id: uuid.UUID
    # false 면 같은 열쇠의 만료 전 증거가 이미 있어 새로 만들지 않았다(finding_id 는 그 증거).
    created: bool
    service_id: uuid.UUID
    environment_id: uuid.UUID
    image_digest: str
    advisory_id: str
    aliases: list[str]
    severity: str
    summary: str | None
    ecosystem: str
    package: str
    installed_version: str
    fixed_version: str | None
    result: SecurityResult

    @classmethod
    def from_view(cls, view: CveWatchMatchView) -> "CveWatchMatchResponse":
        return cls(
            finding_id=view.finding_id,
            created=view.created,
            service_id=view.service_id,
            environment_id=view.environment_id,
            image_digest=view.image_digest,
            advisory_id=view.advisory_id,
            aliases=list(view.aliases),
            severity=view.severity,
            summary=view.summary,
            ecosystem=view.ecosystem,
            package=view.package,
            installed_version=view.installed_version,
            fixed_version=view.fixed_version,
            result=view.result,
        )


class CveWatchRunResponse(ApiModel):
    simulated: bool
    checked_at: datetime
    # 살펴본 연결(서비스 × 환경, 현재 배포가 있는 것) 수.
    targets: int
    # 의존성 목록이 없어 맞춰 보지 못한 연결 수.
    targets_without_dependencies: int
    # 피드에 물어본 서로 다른 패키지 수.
    packages: int
    created: int
    duplicates: int
    skipped_low_severity: int
    skipped_known: int
    # 상세 상한 때문에 이번에 못 보고 다음 조회로 미룬 권고 수(0 이 아니면 결과가 불완전하다).
    deferred_advisories: int = 0
    # 피드 응답 안쪽 모양이 틀려 건너뛴 결과·상세 건수.
    feed_errors: int = 0
    # 가짜 증거 상한(조직당 20건)을 넘어 지운 옛 가짜 증거 수(주입만).
    retired_simulated: int = 0
    # 이번 주입이 상한(20건)보다 많은 증거를 낳아 만들지·돌려주지 않은 매치 수(주입만).
    omitted_simulated: int = 0
    matches: list[CveWatchMatchResponse]

    @classmethod
    def from_view(cls, view: CveWatchRunView) -> "CveWatchRunResponse":
        return cls(
            simulated=view.simulated,
            checked_at=view.checked_at,
            targets=view.targets,
            targets_without_dependencies=view.targets_without_dependencies,
            packages=view.packages,
            created=view.created_count,
            duplicates=view.duplicate_count,
            skipped_low_severity=view.skipped_low_severity,
            skipped_known=view.skipped_known,
            deferred_advisories=view.deferred_advisories,
            feed_errors=view.feed_errors,
            retired_simulated=view.retired_simulated,
            omitted_simulated=view.omitted_simulated,
            matches=[CveWatchMatchResponse.from_view(match) for match in view.matches],
        )

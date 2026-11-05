"""서비스 대시보드 지표 응답(운영 2, 프론트 계약 4-2). 모든 칸은 항상 있고 값이 없으면 null 이다."""

import uuid
from datetime import datetime

from pydantic import Field

from app.enums import ServiceMetricsStatus
from app.schemas.response import ApiModel
from app.services.service_metrics import (
    EnvironmentSeriesView,
    MetricsPointView,
    MetricsSummaryView,
    ServiceMetricsView,
)


class MetricsPointResponse(ApiModel):
    t: datetime
    rps: float
    success_rate: float | None
    p95_ms: float | None

    @classmethod
    def from_view(cls, view: MetricsPointView) -> "MetricsPointResponse":
        return cls(t=view.at, rps=view.rps, success_rate=view.success_rate, p95_ms=view.p95_ms)


class EnvironmentMetricsSeriesResponse(ApiModel):
    environment_id: uuid.UUID
    environment_name: str
    cloud_provider: str
    points: list[MetricsPointResponse]

    @classmethod
    def from_view(cls, view: EnvironmentSeriesView) -> "EnvironmentMetricsSeriesResponse":
        return cls(
            environment_id=view.environment_id,
            environment_name=view.environment_name,
            cloud_provider=view.cloud_provider,
            points=[MetricsPointResponse.from_view(point) for point in view.points],
        )


class ServiceMetricsSummaryResponse(ApiModel):
    success_rate: float | None
    p95_ms: float | None
    replication_lag_ms: float | None
    error_budget_left: float | None
    slo_target: float

    @classmethod
    def from_view(cls, view: MetricsSummaryView) -> "ServiceMetricsSummaryResponse":
        return cls(
            success_rate=view.success_rate,
            p95_ms=view.p95_ms,
            replication_lag_ms=view.replication_lag_ms,
            error_budget_left=view.error_budget_left,
            slo_target=view.slo_target,
        )


class ServiceMetricsResponse(ApiModel):
    time_range: str = Field(alias="range")
    step: str
    as_of: datetime
    metrics_status: ServiceMetricsStatus
    metrics_error_code: str | None
    series: list[EnvironmentMetricsSeriesResponse]
    summary: ServiceMetricsSummaryResponse
    # 값을 지어내지 않으므로 늘 false 다(계약 칸).
    simulated: bool

    @classmethod
    def from_view(cls, view: ServiceMetricsView) -> "ServiceMetricsResponse":
        return cls(
            time_range=view.window.range_text,
            step=view.window.step_text,
            as_of=view.as_of,
            metrics_status=view.status,
            metrics_error_code=view.error_code,
            series=[EnvironmentMetricsSeriesResponse.from_view(item) for item in view.series],
            summary=ServiceMetricsSummaryResponse.from_view(view.summary),
            simulated=False,
        )

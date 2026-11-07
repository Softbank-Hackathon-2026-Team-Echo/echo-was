"""시연 조직 스냅샷·복귀 API 응답·요청."""

import uuid
from datetime import datetime

from app.schemas.response import ApiModel, ApiRequestModel
from app.services.demo_snapshot_service import (
    DemoItemResult,
    DemoRestoreResult,
    DemoSnapshotView,
    DemoStepResult,
    SnapshotConnection,
    SnapshotEnvironment,
    SnapshotSecurityException,
    SnapshotService,
)


class RestoreDemoRequest(ApiRequestModel):
    """dryRun = true 면 아무것도 바꾸지 않고 할 일만 돌려준다."""

    dry_run: bool = False


class SnapshotConnectionResponse(ApiModel):
    service_environment_id: uuid.UUID
    environment_id: uuid.UUID
    environment_name: str
    environment_kind: str
    traffic_weight: int
    current_deployment_id: uuid.UUID | None
    deploy_request_id: uuid.UUID | None
    commit_sha: str | None
    image_digest: str | None

    @classmethod
    def build(cls, item: SnapshotConnection) -> "SnapshotConnectionResponse":
        return cls(
            service_environment_id=item.service_environment_id,
            environment_id=item.environment_id,
            environment_name=item.environment_name,
            environment_kind=item.environment_kind,
            traffic_weight=item.traffic_weight,
            current_deployment_id=item.current_deployment_id,
            deploy_request_id=item.deploy_request_id,
            commit_sha=item.commit_sha,
            image_digest=item.image_digest,
        )


class SnapshotServiceResponse(ApiModel):
    service_id: uuid.UUID
    name: str
    serving_environment_id: uuid.UUID | None
    connections: list[SnapshotConnectionResponse]

    @classmethod
    def build(cls, item: SnapshotService) -> "SnapshotServiceResponse":
        return cls(
            service_id=item.service_id,
            name=item.name,
            serving_environment_id=item.serving_environment_id,
            connections=[SnapshotConnectionResponse.build(c) for c in item.connections],
        )


class SnapshotEnvironmentResponse(ApiModel):
    environment_id: uuid.UUID
    name: str
    kind: str
    connection_status: str

    @classmethod
    def build(cls, item: SnapshotEnvironment) -> "SnapshotEnvironmentResponse":
        return cls(
            environment_id=item.environment_id,
            name=item.name,
            kind=item.kind,
            connection_status=item.connection_status,
        )


class SnapshotSecurityExceptionResponse(ApiModel):
    exception_id: uuid.UUID
    control_id: str
    service_id: uuid.UUID | None
    environment_id: uuid.UUID | None
    approved: bool
    revoked: bool
    expires_at: datetime

    @classmethod
    def build(cls, item: SnapshotSecurityException) -> "SnapshotSecurityExceptionResponse":
        return cls(
            exception_id=item.exception_id,
            control_id=item.control_id,
            service_id=item.service_id,
            environment_id=item.environment_id,
            approved=item.approved,
            revoked=item.revoked,
            expires_at=item.expires_at,
        )


class DemoSnapshotResponse(ApiModel):
    id: uuid.UUID
    taken_at: datetime
    taken_by_user_id: uuid.UUID | None
    schema_version: int
    services: list[SnapshotServiceResponse]
    environments: list[SnapshotEnvironmentResponse]
    security_exceptions: list[SnapshotSecurityExceptionResponse]
    allow_self_approval: bool

    @classmethod
    def from_view(cls, view: DemoSnapshotView) -> "DemoSnapshotResponse":
        state = view.state
        return cls(
            id=view.snapshot_id,
            taken_at=view.taken_at,
            taken_by_user_id=view.taken_by_user_id,
            schema_version=state.schema_version,
            services=[SnapshotServiceResponse.build(item) for item in state.services],
            environments=[SnapshotEnvironmentResponse.build(item) for item in state.environments],
            security_exceptions=[
                SnapshotSecurityExceptionResponse.build(item) for item in state.security_exceptions
            ],
            allow_self_approval=state.allow_self_approval,
        )


class DemoRestoreItemResponse(ApiModel):
    action: str
    status: str
    target_type: str | None
    target_id: uuid.UUID | None
    label: str | None
    code: str | None
    detail: str | None

    @classmethod
    def build(cls, item: DemoItemResult) -> "DemoRestoreItemResponse":
        return cls(
            action=str(item.action),
            status=str(item.status),
            target_type=item.target_type,
            target_id=item.target_id,
            label=item.label,
            code=item.code,
            detail=item.detail,
        )


class DemoRestoreStepResponse(ApiModel):
    step: str
    status: str
    items: list[DemoRestoreItemResponse]

    @classmethod
    def build(cls, step: DemoStepResult) -> "DemoRestoreStepResponse":
        return cls(
            step=str(step.step),
            status=str(step.status),
            items=[DemoRestoreItemResponse.build(item) for item in step.items],
        )


class DemoRestoreResponse(ApiModel):
    snapshot_id: uuid.UUID
    snapshot_taken_at: datetime
    dry_run: bool
    # 되돌리는 단계(MIGRATION_PLANS~DEPLOYMENTS)가 모두 OK·DONE.
    restored: bool
    # STARTED·WAITING 이 있다: 일꾼이 끝낸 뒤 다시 부르면 이어 간다.
    call_again: bool
    manual_required: list[str]
    steps: list[DemoRestoreStepResponse]

    @classmethod
    def from_result(cls, result: DemoRestoreResult) -> "DemoRestoreResponse":
        return cls(
            snapshot_id=result.snapshot_id,
            snapshot_taken_at=result.snapshot_taken_at,
            dry_run=result.dry_run,
            restored=result.restored,
            call_again=result.call_again,
            manual_required=[str(step) for step in result.manual_required],
            steps=[DemoRestoreStepResponse.build(step) for step in result.steps],
        )

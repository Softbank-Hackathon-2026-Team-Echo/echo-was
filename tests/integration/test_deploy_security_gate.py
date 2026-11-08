"""실제 적용 직전의 고객 빌드 보안 게이트. 외부 클러스터 대신 호출 기록 실행기를 쓴다."""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.audit.actions import AuditAction
from app.enums import (
    BuildStatus,
    DeployRequestStatus,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingSource,
    SecurityResult,
)
from app.executors.base import BuildFailed, BuildSucceeded
from app.models import AuditLog, Build, Deployment, DeployRequest, Finding, SecurityException
from tests.integration.deploy_harness import DeployHarness
from tests.integration.deploy_security_support import (
    RealDeployRecorder,
    RealScanningBuild,
)
from tests.integration.deploy_security_support import (
    install_scanned_executors as install,
)
from tests.integration.factories import add_user
from tests.integration.migration_support import Migration
from tests.integration.rollback_support import rollback
from tests.integration.test_security_scan_collect import _scan


async def request_deploy(harness: DeployHarness, **extra: Any) -> dict[str, Any]:
    service = await harness.create_service("shop-api")
    environment = await harness.add_environment("aws-seoul")
    return await harness.deploy(service["id"], environment.id, **extra)


@pytest.mark.parametrize(
    "scenario",
    [
        "absent",
        "wrong-digest",
        "vulnerability",
        "missing-source",
        "missing-image",
        "secret",
    ],
)
async def test_real_deploy_blocks_failed_or_missing_scan_before_cluster_apply(
    harness: DeployHarness,
    scenario: str,
) -> None:
    install(harness, scenario)
    accepted = await request_deploy(harness)
    await harness.run_until_idle()
    request = await harness.get_request(accepted["id"])
    assert request["status"] == "FAILED"
    assert request["errorCode"] == "SECURITY_GATE_BLOCKED"
    assert harness.executors.deploy.calls == []
    assert harness.executors.deploy.reverts == []


async def test_clean_initial_and_first_standby_deploy_do_not_require_migration_evidence(
    harness: DeployHarness,
) -> None:
    install(harness)
    gcp = await harness.add_environment("gcp-seoul")
    accepted = await request_deploy(harness, standbyEnvironmentIds=[str(gcp.id)])
    await harness.run_until_idle()
    request = await harness.get_request(accepted["id"])
    assert request["status"] == "SUCCEEDED"
    (standby_id,) = request["standbyDeployRequestIds"]
    assert (await harness.get_request(standby_id))["status"] == "SUCCEEDED"
    assert len(harness.executors.deploy.calls) == 2


async def test_scan_expiring_after_deployment_creation_blocks_apply_without_revert(
    harness: DeployHarness,
) -> None:
    install(harness)
    accepted = await request_deploy(harness)
    await harness.run_until(accepted["id"], {DeployRequestStatus.DEPLOYING})
    async with harness.session_factory.begin() as session:
        for finding in await session.scalars(select(Finding)):
            finding.collected_at = harness.clock.now() - timedelta(days=8)
            finding.expires_at = harness.clock.now() - timedelta(days=1)
    await harness.run_until_idle()
    request = await harness.get_request(accepted["id"])
    assert request["errorCode"] == "SECURITY_GATE_BLOCKED"
    assert harness.executors.deploy.calls == []
    assert harness.executors.deploy.reverts == []


async def audit(harness: DeployHarness, request_id: str) -> AuditLog:
    async with harness.session_factory() as session:
        return (
            await session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.target_id == uuid.UUID(request_id),
                    AuditLog.action == AuditAction.DEPLOY_SECURITY_CHECKED,
                )
                .order_by(AuditLog.created_at.desc())
            )
        ).first()


async def grant_exception(
    harness: DeployHarness,
    request_id: str,
    control: SecurityControlId,
) -> uuid.UUID:
    now = harness.clock.now()
    async with harness.session_factory.begin() as session:
        request = await session.get_one(DeployRequest, uuid.UUID(request_id))
        build = await session.get_one(Build, request.build_id)
        approver = await add_user(session, "security-approver")
        exception = SecurityException(
            organization_id=harness.organization.id,
            control_id=control,
            image_digest=build.image_digest,
            reason="Reviewed deployment exception",
            requested_by_user_id=harness.admin.user.id,
            approved_by_user_id=approver.id,
            approved_at=now,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(hours=1),
        )
        session.add(exception)
        await session.flush()
        return exception.id


@pytest.mark.parametrize("state", ["active", "revoked", "expired", "other-digest"])
async def test_exception_is_checked_at_apply_and_reported_as_warn(
    harness: DeployHarness,
    state: str,
) -> None:
    install(harness, "vulnerability")
    accepted = await request_deploy(harness)
    await harness.run_until(accepted["id"], {DeployRequestStatus.DEPLOYING})
    exception_id = await grant_exception(harness, accepted["id"], SecurityControlId.IMG_1)
    async with harness.session_factory.begin() as session:
        exception = await session.get_one(SecurityException, exception_id)
        if state == "revoked":
            exception.revoked_at = harness.clock.now()
            exception.revoked_by_user_id = harness.admin.user.id
        elif state == "expired":
            exception.created_at = harness.clock.now() - timedelta(hours=2)
            exception.expires_at = harness.clock.now()
        elif state == "other-digest":
            exception.image_digest = "sha256:" + "a" * 64
    await harness.run_until_idle()
    request = await harness.get_request(accepted["id"])
    decision = await audit(harness, accepted["id"])
    if state == "active":
        assert request["status"] == "SUCCEEDED"
        control = decision.details["controls"][0]
        assert (control["result"], control["effectiveResult"]) == ("FAIL", "WARN")
        assert decision.details["exceptionIds"] == [str(exception_id)]
    else:
        assert request["errorCode"] == "SECURITY_GATE_BLOCKED"
        assert harness.executors.deploy.calls == []
        assert decision.details["exceptionIds"] == []


@pytest.mark.parametrize(
    "mutation",
    [
        "policy",
        "simulated",
        "other-build",
        "partial-source",
        "deployment-digest",
        "fake-build",
    ],
)
async def test_stale_or_unbound_evidence_cannot_authorize_apply(
    harness: DeployHarness,
    mutation: str,
) -> None:
    install(harness)
    accepted = await request_deploy(harness)
    await harness.run_until(accepted["id"], {DeployRequestStatus.DEPLOYING})
    async with harness.session_factory.begin() as session:
        request = await session.get_one(DeployRequest, uuid.UUID(accepted["id"]))
        for finding in await session.scalars(select(Finding)):
            if mutation == "policy":
                finding.policy_version = "obsolete"
            elif mutation == "simulated":
                finding.mode = SecurityFindingMode.SIMULATED
            elif mutation == "other-build" or (
                mutation == "partial-source" and finding.source == SecurityFindingSource.GITLEAKS
            ):
                finding.source_ref = "build:" + str(uuid.uuid4()) + ":old-run"
        if mutation == "deployment-digest":
            deployment = await session.get_one(Deployment, request.deployment_id)
            deployment.image_digest = "sha256:" + "a" * 64
        elif mutation == "fake-build":
            build = await session.get_one(Build, request.build_id)
            build.simulated = True
    # 다른 빌드의 PASS 만 있을 때 재수집도 실패한 상황.
    if mutation in ("other-build", "policy"):
        harness.executors.build._scan = lambda *_: None
    await harness.run_until_idle()
    assert (await harness.get_request(accepted["id"]))["errorCode"] == "SECURITY_GATE_BLOCKED"
    assert harness.executors.deploy.calls == []


async def test_missing_source_can_only_use_an_explicit_exception(harness: DeployHarness) -> None:
    install(harness, "missing-source")
    accepted = await request_deploy(harness)
    await harness.run_until(accepted["id"], {DeployRequestStatus.DEPLOYING})
    exception_id = await grant_exception(harness, accepted["id"], SecurityControlId.IMG_2)
    await harness.run_until_idle()
    assert (await harness.get_request(accepted["id"]))["status"] == "SUCCEEDED"
    decision = await audit(harness, accepted["id"])
    assert decision.details["exceptionIds"] == [str(exception_id)]
    assert decision.details["controls"][1]["effectiveResult"] == "WARN"


async def test_manual_redeploy_rechecks_reused_build_and_preserves_previous_version(
    harness: DeployHarness,
) -> None:
    install(harness)
    accepted = await request_deploy(harness)
    await harness.run_until_idle()
    first = await harness.get_request(accepted["id"])
    assert first["status"] == "SUCCEEDED"
    async with harness.session_factory.begin() as session:
        request = await session.get_one(DeployRequest, uuid.UUID(accepted["id"]))
        service_id, environment_id = str(request.service_id), request.environment_id
        for finding in await session.scalars(select(Finding)):
            if finding.control_id == SecurityControlId.IMG_1:
                finding.result = SecurityResult.FAIL
    redeploy = await harness.deploy(service_id, environment_id)
    await harness.run_until_idle()
    result = await harness.get_request(redeploy["id"])
    assert result["build"]["reused"] is True
    assert result["errorCode"] == "SECURITY_GATE_BLOCKED"
    assert len(harness.executors.deploy.calls) == 1
    assert harness.executors.deploy.reverts == []


@pytest.mark.parametrize(
    "changes",
    [
        {"repository": "other/repo"},
        {"commit_sha": "0" * 40},
        {"repository": None},
        {"scanned_at": None},
    ],
)
async def test_scan_provenance_must_match_real_build(
    harness: DeployHarness,
    changes: dict[str, Any],
) -> None:
    install(harness)
    harness.executors.build.scan_changes = changes
    accepted = await request_deploy(harness)
    await harness.run_until_idle()
    assert (await harness.get_request(accepted["id"]))["errorCode"] == "SECURITY_GATE_BLOCKED"
    assert harness.executors.deploy.calls == []


async def test_interruption_between_build_success_and_collection_recovers_original_scan(
    harness: DeployHarness,
) -> None:
    install(harness)
    accepted = await request_deploy(harness)
    await harness.run_until(accepted["id"], {DeployRequestStatus.DEPLOYING})
    async with harness.session_factory.begin() as session:
        for finding in await session.scalars(select(Finding)):
            finding.source_ref = "legacy-run"
    harness.clock.advance(60)
    await harness.run_until_idle()
    assert (await harness.get_request(accepted["id"]))["status"] == "SUCCEEDED"
    assert len(harness.executors.build.scan_reads) == 2
    async with harness.session_factory() as session:
        findings = list(await session.scalars(select(Finding)))
    assert len(findings) == 6
    assert len({finding.collected_at for finding in findings}) == 1


async def test_rollback_is_checked_even_when_original_build_was_successful(
    migration: Migration,
) -> None:
    harness = migration.harness
    # 기존 성공 이력은 가짜 빌드 증거다. 실제 실행기로 바꿨을 때 신뢰하지 않아야 한다.
    async with harness.session_factory() as session:
        original = (
            await session.scalars(
                select(DeployRequest).where(
                    DeployRequest.service_id == uuid.UUID(migration.service_id),
                    DeployRequest.environment_id == migration.aws.id,
                    DeployRequest.status == "SUCCEEDED",
                )
            )
        ).first()
    assert original is not None
    await harness.deploy(migration.service_id, migration.aws.id)
    await harness.run_until_idle()
    accepted = await rollback(harness, str(original.id))
    harness.replace_executors(deploy=RealDeployRecorder(harness.clock, 0))
    await harness.run_until_idle()
    assert (await harness.get_request(accepted["id"]))["errorCode"] == "SECURITY_GATE_BLOCKED"
    assert harness.executors.deploy.calls == []
    assert harness.executors.deploy.reverts == []


async def test_ai_diagnosis_redeploy_uses_the_same_gate(migration: Migration) -> None:
    from tests.integration.test_failed_deploy_diagnosis import (
        _approve,
        _data,
        _failed_deploy,
        _request_diagnosis,
    )

    harness = migration.harness
    failed = await _failed_deploy(migration)
    diagnosis = _data(await _request_diagnosis(migration, failed["id"]))
    approved = await _approve(migration, diagnosis["id"], diagnosis["patches"][0])
    assert approved.status_code == 200, approved.text
    redeploy_id = _data(approved)["apply"]["redeployRequestId"]
    assert redeploy_id is not None
    harness.replace_executors(deploy=RealDeployRecorder(harness.clock, 0))
    await harness.run_until_idle()
    assert (await harness.get_request(redeploy_id))["errorCode"] == "SECURITY_GATE_BLOCKED"
    assert harness.executors.deploy.calls == []


class FailedAfterImageBuild(RealScanningBuild):
    async def poll(self, external_ref: str) -> Any:
        result = await super().poll(external_ref)
        return BuildFailed(artifact=result) if isinstance(result, BuildSucceeded) else result


async def test_failed_workflow_collects_verified_artifact_without_promoting_build(
    harness: DeployHarness,
) -> None:
    install(harness, "vulnerability")
    harness.replace_executors(
        build=FailedAfterImageBuild(
            harness.clock,
            0,
            scan=harness.executors.build._scan,
        )
    )
    accepted = await request_deploy(harness)
    await harness.run_until_idle()
    request = await harness.get_request(accepted["id"])
    assert request["errorCode"] == "BUILD_FAILED"
    async with harness.session_factory() as session:
        build = (await session.scalars(select(Build))).one()
        findings = list(await session.scalars(select(Finding)))
    assert build.status == BuildStatus.FAILED
    assert build.image_digest is None
    assert len(findings) == 3
    assert any(finding.result == SecurityResult.FAIL for finding in findings)
    assert harness.executors.deploy.calls == []


async def test_standby_sync_rechecks_evidence_after_primary_success(harness: DeployHarness) -> None:
    install(harness)
    gcp = await harness.add_environment("gcp-seoul")
    accepted = await request_deploy(harness, standbyEnvironmentIds=[str(gcp.id)])
    await harness.run_until(accepted["id"], {DeployRequestStatus.SUCCEEDED})
    main = await harness.get_request(accepted["id"])
    (standby_id,) = main["standbyDeployRequestIds"]
    async with harness.session_factory.begin() as session:
        for finding in await session.scalars(select(Finding)):
            if finding.control_id == SecurityControlId.IMG_1:
                finding.result = SecurityResult.FAIL
    await harness.run_until_idle()
    assert (await harness.get_request(standby_id))["errorCode"] == "SECURITY_GATE_BLOCKED"
    assert (await harness.get_request(accepted["id"]))["status"] == "SUCCEEDED"
    assert len(harness.executors.deploy.calls) == 1
    assert harness.executors.deploy.reverts == []


async def test_resume_after_possible_apply_still_runs_revert_if_security_now_blocks(
    harness: DeployHarness,
) -> None:
    install(harness, "vulnerability")
    accepted = await request_deploy(harness)
    await harness.run_until(accepted["id"], {DeployRequestStatus.DEPLOYING})
    async with harness.session_factory.begin() as session:
        request = await session.get_one(DeployRequest, uuid.UUID(accepted["id"]))
        request.deploy_requested_at = harness.clock.now()
    await harness.run_until_idle()
    assert (await harness.get_request(accepted["id"]))["errorCode"] == "SECURITY_GATE_BLOCKED"
    assert harness.executors.deploy.calls == []
    assert len(harness.executors.deploy.reverts) == 1


async def test_current_pass_never_hides_other_valid_fail_for_same_digest(
    harness: DeployHarness,
) -> None:
    from app.services.security_finding_service import SecurityFindingService
    from app.services.security_normalizers import normalize_trivy_image
    from app.services.security_rules import SecurityScope

    install(harness)
    accepted = await request_deploy(harness)
    await harness.run_until(accepted["id"], {DeployRequestStatus.DEPLOYING})
    async with harness.session_factory() as session:
        build = (await session.scalars(select(Build))).one()
    report = _scan()(str(build.id), build.image_digest)
    finding = normalize_trivy_image(
        report.trivy_report,
        scope=SecurityScope(harness.organization.id, image_digest=build.image_digest),
        collected_at=harness.clock.now(),
        tool_version="0.75.0",
    )
    async with harness.session_factory() as session:
        await SecurityFindingService(session).record(finding, "another-valid-scan")
    await harness.run_until_idle()
    assert (await harness.get_request(accepted["id"]))["errorCode"] == "SECURITY_GATE_BLOCKED"
    assert harness.executors.deploy.calls == []


@pytest.mark.parametrize("age_seconds,expected", [(600, "SUCCEEDED"), (8 * 24 * 3600, "FAILED")])
async def test_policy_rollover_recollects_original_scan_without_renewing_ttl(
    harness: DeployHarness, age_seconds: int, expected: str
) -> None:
    from app.services.security_rules import POLICY_VERSION

    install(harness)
    harness.executors.build.scan_changes["scanned_at"] = harness.clock.now() - timedelta(
        seconds=age_seconds
    )
    accepted = await request_deploy(harness)
    await harness.run_until(accepted["id"], {DeployRequestStatus.DEPLOYING})
    async with harness.session_factory.begin() as session:
        rows = (await session.scalars(select(Finding))).all()
        scanned_at = rows[0].collected_at
        for row in rows:
            row.policy_version = "sec-rules-2026.11.06"
    await harness.run_until_idle()
    assert (await harness.get_request(accepted["id"]))["status"] == expected
    async with harness.session_factory() as session:
        refreshed = (
            await session.scalars(select(Finding).where(Finding.policy_version == POLICY_VERSION))
        ).all()
    assert len(refreshed) == 3
    assert all(row.collected_at == scanned_at for row in refreshed)
    assert all(row.expires_at <= scanned_at + timedelta(days=7) for row in refreshed)
    assert bool(harness.executors.deploy.calls) == (expected == "SUCCEEDED")

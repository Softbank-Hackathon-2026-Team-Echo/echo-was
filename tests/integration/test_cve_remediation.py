"""CVE targets, immutable approval, real-run routing and SBOM input binding."""

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import AgentRun, ServiceDependencySnapshot, ServiceEnvironment
from tests.integration.migration_support import Migration
from tests.integration.runner_support import (
    add_runner,
    claim_run,
    create_precheck,
    run_patch,
    run_result,
)
from tests.integration.test_cve_watch import _path, _seed
from tests.integration.test_failed_deploy_diagnosis import DIAGNOSIS_CAPABILITIES, _approve, _claim


async def proposal(migration):
    await _seed(migration)
    migration.harness.use_settings(
        cve_remediation_enabled=True,
        patch_apply_enabled=True,
        cve_remediation_demo_service_ids=(uuid.UUID(migration.service_id),),
    )
    injected = await migration.author_client.post(
        _path(migration, "/cve-watch:inject"),
        json={
            "serviceId": migration.service_id,
            "advisory": {
                "id": "SIM-STARLETTE",
                "ecosystem": "PyPI",
                "package": "starlette",
                "introduced": "0",
                "fixed": "0.49.1",
            },
        },
    )
    fid = injected.json()["data"]["matches"][0]["findingId"]
    response = await migration.author_client.post(_path(migration, f"/findings/{fid}/diagnosis"))
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def test_sbom_is_bound_to_original_precheck_input(migration: Migration):
    precheck = await create_precheck(migration)
    runner = await add_runner(migration.harness.session_factory)
    claimed = await claim_run(runner)
    source = claimed.run["inputSnapshot"]["source"]
    payload = run_result(migration) | {
        "sbom": {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "components": [{"purl": "pkg:pypi/urllib3@2.2.0"}],
        }
    }
    response = await claimed.post("result", payload)
    assert response.status_code == 200, response.text
    async with migration.harness.session_factory() as session:
        snapshots = list(
            (
                await session.scalars(
                    select(ServiceDependencySnapshot).where(
                        ServiceDependencySnapshot.source_ref == precheck["id"]
                    )
                )
            ).all()
        )
    assert len(snapshots) == 1
    assert str(snapshots[0].service_id) == migration.service_id
    assert snapshots[0].image_digest == source["imageDigest"]
    assert snapshots[0].packages[0]["version"] == "2.2.0"


def cve_result(migration):
    patch = run_patch("dep")
    from app.services.run_result import compute_patch_hash

    patch["files"] = ["pyproject.toml"]
    patch["diff"] = (
        "--- a/pyproject.toml\n+++ b/pyproject.toml\n@@ -1 +1 @@\n"
        "-starlette==0.47.1\n+starlette==0.49.1\n"
    )
    patch["patchHash"] = compute_patch_hash(patch["diff"])
    patch["checks"] = [
        {"name": name, "result": "PASS"}
        for name in ("UV_LOCK", "DOCKER_BUILD", "TARGET_VERSION", "TRIVY", "ADVISORY", "HEALTHZ")
    ]

    # Protocol fixture exercises lifecycle; real diff semantics belong to runner policy tests.
    payload = run_result(migration) | {
        "patches": [patch],
        "diagnosis": {
            "result": "DIAGNOSED",
            "category": "DEPENDENCY",
            "cause": "starlette dependency update",
            "fixable": True,
            "unfixableReason": None,
            "evidence": [{"source": "ADVISORY", "text": "SIM-STARLETTE"}],
        },
    }
    return patch, payload


async def test_cve_approval_claim_uses_current_source_and_never_simulates(migration: Migration):
    run = await proposal(migration)
    runner = await add_runner(migration.harness.session_factory)
    claimed = await _claim(runner, DIAGNOSIS_CAPABILITIES)
    patch, payload = cve_result(migration)
    response = await claimed.post("result", payload)
    assert response.status_code == 200, response.text
    approved = await _approve(migration, run["id"], patch)
    assert approved.status_code == 200, approved.text
    applied = await _claim(runner, DIAGNOSIS_CAPABILITIES)
    snapshot = applied.run["inputSnapshot"]
    assert snapshot["trigger"] == "CVE_WATCH"
    assert (
        snapshot["diagnosis"]["sourceCommitSha"] == claimed.run["inputSnapshot"]["sourceCommitSha"]
    )
    assert snapshot["patch"]["diff"] == patch["diff"]
    assert applied.run["kind"] == "APPLY_PATCH"
    async with migration.harness.session_factory() as session:
        row = await session.get(AgentRun, uuid.UUID(run["id"]))
        assert row.deploy_request_id is None and row.finding_id is not None


async def test_stale_cve_is_rejected_before_runner_claim(migration: Migration):
    run = await proposal(migration)
    async with migration.harness.session_factory() as session, session.begin():
        row = await session.get(AgentRun, uuid.UUID(run["id"]))
        connection = await session.scalar(
            select(ServiceEnvironment).where(
                ServiceEnvironment.service_id == row.service_id,
                ServiceEnvironment.environment_id == row.target_environment_id,
            )
        )
        connection.current_deployment_id = None
    runner = await add_runner(migration.harness.session_factory)
    from tests.integration.runner_support import claim

    response = await claim(runner, DIAGNOSIS_CAPABILITIES)
    assert response.status_code == 200 and response.json()["data"]["run"] is None
    async with migration.harness.session_factory() as session:
        row = await session.get(AgentRun, uuid.UUID(run["id"]))
        assert row.failure_code == "CVE_TARGET_CHANGED"


async def test_synthetic_run_rejected_outside_designated_service(migration: Migration):
    await _seed(migration)
    migration.harness.use_settings(cve_remediation_enabled=True)
    injected = await migration.author_client.post(
        _path(migration, "/cve-watch:inject"),
        json={
            "advisory": {
                "ecosystem": "PyPI",
                "package": "starlette",
                "introduced": "0",
                "fixed": "0.49.1",
            }
        },
    )
    fid = injected.json()["data"]["matches"][0]["findingId"]
    response = await migration.author_client.post(_path(migration, f"/findings/{fid}/diagnosis"))
    assert response.status_code == 409


@pytest.mark.parametrize(
    "mode",
    [
        "committed",
        "no_writer",
        "stale_after_apply",
        "stale_before_approve",
        "wrong_hash",
        "unverified",
    ],
)
async def test_cve_approval_and_redeploy_boundaries(migration: Migration, mode):
    from app.clients.repo_commit_writer import FakeRepoCommitWriter
    from tests.integration.test_apply_commit_to_repo import REPO_SHA, SANDBOX_SHA, _use_writer
    from tests.integration.test_failed_deploy_diagnosis import _detail

    run = await proposal(migration)
    migration.harness.use_settings(ai_apply_commit_enabled=mode != "no_writer")
    writer = FakeRepoCommitWriter(
        files={"pyproject.toml": "starlette==0.47.1\n"}, commit_sha=REPO_SHA
    )
    _use_writer(writer)
    runner = await add_runner(migration.harness.session_factory)
    claimed = await _claim(runner, DIAGNOSIS_CAPABILITIES)
    patch, payload = cve_result(migration)
    if mode == "unverified":
        patch["checks"] = []
    submitted = await claimed.post("result", payload)
    if mode == "unverified":
        assert submitted.status_code == 422
        return
    assert submitted.status_code == 200, submitted.text
    if mode == "wrong_hash":
        response = await _approve(
            migration, run["id"], {**patch, "patchHash": "sha256:" + "0" * 64}
        )
        assert response.status_code == 409
        return

    async def stale():
        async with migration.harness.session_factory() as session, session.begin():
            row = await session.get(AgentRun, uuid.UUID(run["id"]))
            connection = await session.scalar(
                select(ServiceEnvironment).where(
                    ServiceEnvironment.service_id == row.service_id,
                    ServiceEnvironment.environment_id == row.target_environment_id,
                )
            )
            connection.current_deployment_id = None

    if mode == "stale_before_approve":
        await stale()
        assert (await _approve(migration, run["id"], patch)).status_code == 409
        return
    approved = await _approve(migration, run["id"], patch)
    assert approved.status_code == 200, approved.text
    apply = await _claim(runner, DIAGNOSIS_CAPABILITIES)
    if mode == "stale_after_apply":
        await stale()
    done = await apply.post(
        "result",
        {
            "status": "SUCCEEDED",
            "failureCode": None,
            "simulated": False,
            "appliedCommitSha": SANDBOX_SHA,
            "branch": f"applied/{apply.run_id}",
        },
    )
    assert done.status_code == 200, done.text
    result = (await _detail(migration, run["id"]))["patches"][0]["apply"]
    if mode == "committed":
        redeploy = await migration.harness.get_request(result["redeployRequestId"])
        assert redeploy["commitSha"] == REPO_SHA != claimed.run["inputSnapshot"]["sourceCommitSha"]
        assert writer.requests[0].files == {"pyproject.toml": "starlette==0.49.1\n"}
    else:
        assert not writer.requests
        assert result["redeployRequestId"] is None
        assert result["redeployRejectedCode"] == (
            "CVE_TARGET_CHANGED" if mode == "stale_after_apply" else "REPO_WRITE_UNAVAILABLE"
        )


async def test_real_customer_sbom_optional_acceptance(migration: Migration):
    path = os.getenv("CVE_CUSTOMER_SBOM_FILE")
    if not path:
        pytest.skip("manual acceptance: set CVE_CUSTOMER_SBOM_FILE to a real Trivy CycloneDX file")
    sbom = json.loads(await asyncio.to_thread(Path(path).read_text))
    precheck = await create_precheck(migration)
    digest = "sha256:e54e9f5bbd28c648887c225f237e46189e2363b5224dda61d9b56ce3dd7e96dc"
    from app.services.precheck_service import compute_input_hash

    async with migration.harness.session_factory() as session, session.begin():
        row = await session.get(AgentRun, uuid.UUID(precheck["id"]))
        snapshot = dict(row.input_snapshot)
        snapshot["source"] = {
            **snapshot["source"],
            "imageDigest": digest,
            "imageRef": "asia-northeast3-docker.pkg.dev/team-echo-final/echo/app@" + digest,
        }
        row.input_snapshot = snapshot
        row.input_hash = compute_input_hash(snapshot)
    runner = await add_runner(migration.harness.session_factory)
    claimed = await claim_run(runner)
    response = await claimed.post("result", run_result(migration) | {"sbom": sbom})
    assert response.status_code == 200, response.text
    async with migration.harness.session_factory() as session:
        stored = await session.scalar(
            select(ServiceDependencySnapshot).where(
                ServiceDependencySnapshot.source_ref == precheck["id"]
            )
        )
        assert str(stored.service_id) == migration.service_id
        assert stored.image_digest == digest and stored.source == "RUNNER_SBOM"
        assert stored.package_count > 50


async def test_cve_finding_cannot_cross_org_and_viewer_cannot_execute(migration: Migration):
    from app.enums import OrganizationRole
    from tests.integration.auth_support import (
        create_member,
        create_organization,
        make_client,
        org_path,
    )

    run = await proposal(migration)
    fid = run["diagnosis"]["findingId"]
    harness = migration.harness
    other_org = await create_organization(harness.session_factory, "cve-other-org")
    developer = await create_member(
        harness.session_factory, other_org, OrganizationRole.DEVELOPER, harness.clock.now()
    )
    async with make_client(session_token=developer.session_token) as client:
        response = await client.post(org_path(other_org.id, f"/security/findings/{fid}/diagnosis"))
        assert response.status_code == 404
    viewer = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.VIEWER, harness.clock.now()
    )
    async with make_client(session_token=viewer.session_token) as client:
        response = await client.post(_path(migration, f"/findings/{fid}/diagnosis"))
        assert response.status_code == 403

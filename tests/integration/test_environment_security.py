"""실제 PostgreSQL에서 자동 증거의 교체·격리·실패 보존 및 매트릭스 연동을 검증한다."""

from datetime import timedelta

from sqlalchemy import select

from app.enums import SecurityControlId as C
from app.enums import SecurityFindingSource as S
from app.enums import SecurityResult as R
from app.models import Environment, Finding
from app.services.environment_security_collector import (
    EnvironmentEvidenceService,
    EnvironmentSecurityCollector,
    normalize_environment_checks,
)
from app.services.security_finding_service import SecurityFindingService
from app.services.security_rules import SecurityScope
from tests.integration.auth_support import org_path
from tests.integration.deploy_harness import DeployHarness
from tests.test_environment_security import complete, profile


async def setup(harness: DeployHarness):
    env = await harness.add_environment("aws-seoul")
    p = profile().model_copy(
        update={
            "organization_id": harness.organization.id,
            "environment_id": env.id,
        }
    )
    harness.use_settings(security_environment_profiles=(p,))
    return env, p


def normalized(p, env, now, result=R.PASS):
    checks = complete(p, result)
    checks = {
        c: [v.model_copy(update={"observed_at": now}) for v in values]
        for c, values in checks.items()
    }
    revision = p.revision(env.config, env.secret_ref)
    return revision, normalize_environment_checks(checks, p, revision, now)


async def save(harness, p, env, at, result=R.PASS):
    revision, values = normalized(p, env, at, result)
    async with harness.session_factory() as session:
        return await EnvironmentEvidenceService(session).record_snapshot(p, revision, values, at)


async def test_same_stream_replaced_history_kept_and_old_completion_ignored(harness: DeployHarness):
    env, p = await setup(harness)
    now = harness.clock.now()
    assert await save(harness, p, env, now, R.FAIL) == 2
    assert await save(harness, p, env, now + timedelta(seconds=10)) == 2
    assert await save(harness, p, env, now + timedelta(seconds=5), R.FAIL) == 0
    async with harness.session_factory() as session:
        rows = (await session.scalars(select(Finding))).all()
    assert len(rows) == 4
    assert sum(row.superseded_at is None for row in rows) == 2
    assert all(row.superseded_at is not None for row in rows if row.result == R.FAIL)
    async with harness.session_factory() as session:
        active = await SecurityFindingService(session).search_for_judgement(
            p.organization_id, now + timedelta(seconds=20)
        )
    assert len(active) == 2 and all(f.result == R.PASS for f in active)


async def test_manual_fail_and_other_stream_survive_success(harness: DeployHarness):
    env, p = await setup(harness)
    now = harness.clock.now()
    async with harness.session_factory() as session:
        await SecurityFindingService(session).record_manual_review(
            control_id=C.IAM_1,
            result=R.FAIL,
            scope=SecurityScope(p.organization_id, environment_id=env.id),
            reviewer_user_id=harness.organization.created_by_user_id,
            basis_url="https://example.org/review",
            collected_at=now,
        )
    assert await save(harness, p, env, now) == 2
    async with harness.session_factory() as session:
        active = await SecurityFindingService(session).search_for_judgement(p.organization_id, now)
    assert len(active) == 3
    assert any(f.source == S.MANUAL_REVIEW and f.result == R.FAIL for f in active)


async def test_configuration_changed_during_read_discards_snapshot(harness: DeployHarness):
    env, p = await setup(harness)
    async with harness.session_factory() as session, session.begin():
        row = await session.get(Environment, env.id)
        row.config = {**row.config, "cluster_name": "changed"}
    assert await save(harness, p, env, harness.clock.now()) == 0


async def test_wrong_org_cannot_replace_evidence(harness: DeployHarness):
    from uuid import uuid4

    env, p = await setup(harness)
    assert await save(harness, p, env, harness.clock.now()) == 2
    wrong = p.model_copy(update={"organization_id": uuid4()})
    assert await save(harness, wrong, env, harness.clock.now() + timedelta(seconds=10)) == 0


async def test_incomplete_snapshot_rejected(harness: DeployHarness):
    import pytest

    env, p = await setup(harness)
    now = harness.clock.now()
    revision, values = normalized(p, env, now)
    async with harness.session_factory() as session:
        with pytest.raises(ValueError):
            await EnvironmentEvidenceService(session).record_snapshot(p, revision, values[:1], now)


async def test_reader_failure_records_unknown_both_controls(harness: DeployHarness):
    env, p = await setup(harness)

    class BrokenReader:
        async def inspect(self, *args):
            raise PermissionError("sensitive raw response")

        async def aclose(self):
            pass

    collector = EnvironmentSecurityCollector(
        harness.session_factory, harness.clock, harness.settings, BrokenReader()
    )
    assert await collector.run_once() == 2
    async with harness.session_factory() as session:
        rows = (await session.scalars(select(Finding))).all()
    assert {row.control_id for row in rows} == {C.IAM_1, C.TLS_2}
    assert all(row.result == R.UNKNOWN for row in rows)
    assert "sensitive" not in str([row.items for row in rows])


async def test_matrix_uses_evidence_then_invalidates_on_config_change(
    harness: DeployHarness, monkeypatch
):
    monkeypatch.setattr("app.security_environment.revision.get_settings", lambda: harness.settings)
    env, p = await setup(harness)
    assert await save(harness, p, env, harness.clock.now()) == 2
    response = await harness.client.get(org_path(p.organization_id, "/security/matrix"))
    assert response.status_code == 200
    data = response.json()["data"]
    # API contract and serialized control results, not the pure calculator alone.
    import json

    serialized = json.dumps(data)
    assert "C-IAM-1" in serialized and "C-TLS-2" in serialized
    controls = {
        c["controlId"]: c for cell in data["rows"][0]["cells"].values() for c in cell["controls"]
    }
    assert controls["C-IAM-1"]["result"] == "PASS"
    assert controls["C-TLS-2"]["result"] == "PASS"
    async with harness.session_factory() as session, session.begin():
        row = await session.get(Environment, env.id)
        row.config = {**row.config, "cluster_name": "other"}
    response = await harness.client.get(org_path(p.organization_id, "/security/matrix"))
    assert response.status_code == 200
    assert "SCOPE_MISMATCH" in json.dumps(response.json())


async def test_policy_refresh_selects_only_current_real_builds_in_org(harness: DeployHarness):
    from uuid import uuid4

    from app.repositories.environment_evidence_repository import EnvironmentEvidenceRepository
    from tests.integration.deploy_security_support import install_scanned_executors
    from tests.integration.test_deploy_security_gate import request_deploy

    install_scanned_executors(harness)
    accepted = await request_deploy(harness)
    await harness.run_until_idle()
    request = await harness.get_request(accepted["id"])
    assert request["status"] == "SUCCEEDED"
    async with harness.session_factory() as session:
        repository = EnvironmentEvidenceRepository(session)
        ids = await repository.current_builds(harness.organization.id)
        assert len(ids) == 1
        assert await repository.current_builds(uuid4()) == []


async def test_auto_evidence_is_reused_by_execution_gate_and_invalidated(
    harness: DeployHarness, monkeypatch
):
    from uuid import UUID

    from app.services.security_gate import SecurityGate
    from tests.integration.migration_support import serving_and_standby
    from tests.integration.security_gate_support import enable_gate

    migration = await serving_and_standby(harness)
    enable_gate(migration)
    p = profile().model_copy(
        update={"organization_id": harness.organization.id, "environment_id": migration.gcp.id}
    )
    harness.use_settings(security_environment_profiles=(p,))
    monkeypatch.setattr("app.security_environment.revision.get_settings", lambda: harness.settings)
    assert await save(harness, p, migration.gcp, harness.clock.now()) == 2
    plan = await migration.create_plan()

    async def decide():
        async with harness.session_factory() as session:
            return await SecurityGate(
                session, harness.clock, harness.tls_checker, harness.settings.was_version
            ).evaluate_plan(UUID(plan["id"]))

    first = await decide()
    assert all(c.result == R.PASS for c in first.controls if c.control_id in (C.IAM_1, C.TLS_2))
    async with harness.session_factory() as session, session.begin():
        row = await session.get(Environment, migration.gcp.id)
        row.config = {**row.config, "cluster_name": "changed"}
    second = await decide()
    checks = [c for c in second.controls if c.control_id in (C.IAM_1, C.TLS_2)]
    assert len(checks) == 2 and all(c.result == R.UNKNOWN and not c.is_blocking for c in checks)
    assert migration.traffic_keys() == []


async def test_three_environment_matrix_and_compare_include_current_evidence(
    harness: DeployHarness, monkeypatch
):
    from tests.integration.test_security_compare_api import _compare

    envs = [
        await harness.add_environment(name) for name in ("aws-seoul", "gcp-seoul", "onprem-k3s")
    ]
    profiles = tuple(
        profile().model_copy(
            update={"organization_id": harness.organization.id, "environment_id": env.id}
        )
        for env in envs
    )
    harness.use_settings(security_environment_profiles=profiles)
    monkeypatch.setattr("app.security_environment.revision.get_settings", lambda: harness.settings)
    for env, p in zip(envs, profiles, strict=True):
        assert await save(harness, p, env, harness.clock.now(), R.WARN) == 2
    response = await harness.client.get(org_path(harness.organization.id, "/security/matrix"))
    assert response.status_code == 200
    rows = response.json()["data"]["rows"]
    assert len(rows) == 3
    for row in rows:
        selected = [
            c
            for cell in row["cells"].values()
            for c in cell["controls"]
            if c["controlId"] in ("C-IAM-1", "C-TLS-2")
        ]
        assert len(selected) == 2 and all(
            c["result"] == "WARN" and c["findingIds"] for c in selected
        )
    compared = await _compare(harness, envs[0].id, envs[1].id)
    import json

    assert "C-IAM-1" in json.dumps(compared) and "C-TLS-2" in json.dumps(compared)


async def test_manual_api_fail_survives_two_successful_automatic_cycles(harness, monkeypatch):
    from app.services.security_rules import POLICY_VERSION
    from tests.integration.test_security_manual_reviews_api import _body, _path

    env, p = await setup(harness)
    monkeypatch.setattr("app.security_environment.revision.get_settings", lambda: harness.settings)
    posted = await harness.client.post(_path(harness), json=_body(env, C.TLS_2, result="FAIL"))
    assert posted.status_code == 201, posted.text
    for delta in (1, 2):
        assert await save(harness, p, env, harness.clock.now() + timedelta(seconds=delta)) == 2
    harness.clock.advance(seconds=3)
    response = await harness.client.get(org_path(p.organization_id, "/security/matrix"))
    controls = [
        c for cell in response.json()["data"]["rows"][0]["cells"].values() for c in cell["controls"]
    ]
    assert next(c for c in controls if c["controlId"] == "C-TLS-2")["result"] == "FAIL"
    async with harness.session_factory() as session:
        manual = await session.scalar(select(Finding).where(Finding.source == S.MANUAL_REVIEW))
        assert manual.result == R.FAIL and manual.superseded_at is None
        assert manual.policy_version == POLICY_VERSION


async def test_old_policy_manual_is_preserved_but_requires_new_review(harness, monkeypatch):
    from tests.integration.test_security_manual_reviews_api import _body, _path

    env, p = await setup(harness)
    monkeypatch.setattr("app.security_environment.revision.get_settings", lambda: harness.settings)
    posted = await harness.client.post(_path(harness), json=_body(env, C.TLS_2, result="FAIL"))
    assert posted.status_code == 201
    async with harness.session_factory() as session, session.begin():
        manual = await session.scalar(select(Finding).where(Finding.source == S.MANUAL_REVIEW))
        manual.policy_version = "sec-rules-2026.11.06"
    response = await harness.client.get(org_path(p.organization_id, "/security/matrix"))
    assert "POLICY_VERSION_MISMATCH" in response.text
    async with harness.session_factory() as session:
        manual = await session.scalar(select(Finding).where(Finding.source == S.MANUAL_REVIEW))
        assert manual.result == R.FAIL and manual.superseded_at is None

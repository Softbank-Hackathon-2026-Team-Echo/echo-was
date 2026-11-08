"""실제 DB로 환경별 실패 격리·읽기 전용 점검·제한 역할 저장을 확인한다."""

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.enums import SecurityResult as R
from app.models import Finding
from app.security_environment.types import ReaderAccess
from app.services.environment_security_collector import EnvironmentSecurityCollector
from app.services.environment_security_preflight import inspect_configuration
from tests.integration.test_environment_security import setup
from tests.test_environment_security import complete


async def test_one_environment_persistence_failure_does_not_skip_next(harness, monkeypatch):
    env, p = await setup(harness)
    other = p.model_copy(update={"environment_id": env.id})
    harness.use_settings(security_environment_profiles=(p,))
    reader = AsyncMock()
    collector = EnvironmentSecurityCollector(
        harness.session_factory, harness.clock, harness.settings, reader
    )
    # Use a distinct failed profile followed by the healthy one.
    from uuid import uuid4

    failed = p.model_copy(update={"environment_id": uuid4()})
    collector._settings = harness.settings.model_copy(
        update={"security_environment_profiles": (failed, other)}
    )
    reader.inspect.return_value = {
        c: [v.model_copy(update={"observed_at": harness.clock.now()}) for v in values]
        for c, values in complete(p).items()
    }
    assert await collector.run_once() == 2
    assert collector.failed_environments == 1
    reader.inspect.assert_awaited_once()


async def test_preflight_reports_identity_and_old_policy_without_writes(harness):
    env, p = await setup(harness)
    from tests.integration.test_environment_security import save

    assert await save(harness, p, env, harness.clock.now(), R.FAIL) == 2
    async with harness.session_factory() as session, session.begin():
        values = (await session.scalars(select(Finding))).all()
        for value in values:
            value.policy_version = "previous-policy"
    report = await inspect_configuration(
        harness.session_factory, harness.settings, harness.clock.now()
    )
    assert not report["configurationReady"]
    assert report["environments"][0]["organizationOldPolicyActiveCount"] == 2
    assert report["operationalAcceptance"] == "NOT_VERIFIED"
    async with harness.session_factory() as session:
        values = (await session.scalars(select(Finding))).all()
        assert len(values) == 2 and all(
            v.result == R.FAIL and v.superseded_at is None for v in values
        )
    updated = p.model_copy(
        update={
            "reader": ReaderAccess(
                google_service_account="reader@example.iam.gserviceaccount.com",
                aws_role_arn="arn:aws:iam::123456789012:role/reader",
            )
        }
    )
    harness.use_settings(security_environment_profiles=(updated,))
    report = await inspect_configuration(
        harness.session_factory, harness.settings, harness.clock.now()
    )
    assert report["configurationReady"]
    assert report["policyTransition"] == "REVIEW_REQUIRED"


async def test_restricted_db_role_collects_but_cannot_change_execution_or_verdict(harness):
    infra = os.environ.get("ECHO_INFRA_OBSERVER_DIR")
    if not infra:
        pytest.skip("ECHO_INFRA_OBSERVER_DIR needed for cross-repository DB grant contract")
    env, p = await setup(harness)
    sql = "\n".join(
        line
        for line in (Path(infra) / "collector-db.sql").read_text().splitlines()
        if not line.lstrip().startswith("--")
    )
    async with harness.session_factory() as session, session.begin():
        # asyncpg cannot prepare multiple statements. Execute the DO block separately.
        end = sql.index("END $$;") + len("END $$;")
        await session.execute(text(sql[:end]))
        for statement in sql[end:].split(";"):
            if statement.strip() and any(
                line.strip() and not line.strip().startswith("--")
                for line in statement.splitlines()
            ):
                await session.execute(text(statement))
        # New database default PUBLIC CREATE must not grant schema ownership to a reader.
        await session.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))

    class RestrictedSession(AsyncSession):
        async def __aenter__(self):
            await super().__aenter__()
            await self.execute(text("SET ROLE echo_security_collector"))
            await self.commit()
            return self

        async def __aexit__(self, *args):
            await self.rollback()
            await self.execute(text("RESET ROLE"))
            await self.commit()
            await super().__aexit__(*args)

    factory = async_sessionmaker(
        harness.session_factory.kw["bind"], class_=RestrictedSession, expire_on_commit=False
    )
    reader = AsyncMock()
    reader.inspect.return_value = {
        c: [v.model_copy(update={"observed_at": harness.clock.now()}) for v in values]
        for c, values in complete(p).items()
    }
    collector = EnvironmentSecurityCollector(factory, harness.clock, harness.settings, reader)
    assert await collector.run_once() == 2
    harness.clock.advance(seconds=1)
    assert await collector.run_once() == 2
    assert collector.failed_environments == 0
    for statement in (
        "UPDATE environments SET config = '{}'::jsonb",
        "UPDATE findings SET result = 'PASS'",
        "DELETE FROM findings",
        "SELECT * FROM jobs",
        "CREATE TABLE unauthorized (id int)",
    ):
        async with factory() as session:
            with pytest.raises(DBAPIError) as denied:
                await session.execute(text(statement))
            assert denied.value.orig.sqlstate == "42501"


async def test_real_https_observation_is_persisted_and_visible_in_matrix(harness, monkeypatch):
    import importlib.util

    from app.clients.environment_transport import inspect_entrypoint
    from app.clients.secret_client import EnvVarSecretClient
    from app.core.clock import SystemClock
    from app.enums import SecurityControlId as C
    from app.security_environment.types import TransportTarget
    from app.services.environment_security_collector import (
        EnvironmentEvidenceService,
        normalize_environment_checks,
    )
    from tests.integration.auth_support import org_path

    infra = os.environ.get("ECHO_INFRA_WEIGHTS_APP")
    if not infra:
        pytest.skip("ECHO_INFRA_WEIGHTS_APP needed for actual HTTPS consumer contract")
    folder = Path(infra).parent
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location(
        "security_https_fixture", folder / "test_security_https.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    env, p = await setup(harness)
    with fixture.tls_fixture() as (url, ca, calls):
        # Only this local socket fixture bypasses the managed-address boundary.
        # Production validation and safe_address remain covered by negative unit tests.
        target = TransportTarget.model_construct(
            id="origin",
            kind="entrypoint",
            url=url,
            ca_file=ca,
            route="aws",
            token_ref="env:ECHO_SECRET_SECURITY_READ_TOKEN",
        )
        p = p.model_copy(update={"transports": (target,)})
        harness.use_settings(security_environment_profiles=(p,))
        monkeypatch.setenv("ECHO_SECRET_SECURITY_READ_TOKEN", "fixture-read-token")
        monkeypatch.setattr(
            "app.clients.environment_transport.safe_address", lambda ip: ip == "127.0.0.1"
        )
        monkeypatch.setattr(
            "app.security_environment.revision.get_settings", lambda: harness.settings
        )
        transport = await inspect_entrypoint(target, EnvVarSecretClient(), SystemClock())
        assert len(calls) == 1
        assert transport[0].result == R.FAIL
        now = SystemClock().now()
        harness.clock._now = now
        checks = {
            C.TLS_2: transport,
            C.IAM_1: [
                value.model_copy(update={"observed_at": now}) for value in complete(p)[C.IAM_1]
            ],
        }
        revision = p.revision(env.config, env.secret_ref)
        normalized = normalize_environment_checks(checks, p, revision, now)
        async with harness.session_factory() as session:
            assert (
                await EnvironmentEvidenceService(session).record_snapshot(
                    p, revision, normalized, now
                )
                == 2
            )
        response = await harness.client.get(org_path(p.organization_id, "/security/matrix"))
        assert response.status_code == 200
        controls = [
            c
            for cell in response.json()["data"]["rows"][0]["cells"].values()
            for c in cell["controls"]
        ]
        tls = next(c for c in controls if c["controlId"] == "C-TLS-2")
        assert tls["result"] == "FAIL" and tls["findingIds"]


async def test_unknown_is_recorded_and_one_shot_reports_incomplete(harness):
    env, p = await setup(harness)
    reader = AsyncMock()
    reader.inspect.side_effect = PermissionError("fixture denied")
    collector = EnvironmentSecurityCollector(
        harness.session_factory, harness.clock, harness.settings, reader
    )
    assert await collector.run_once() == 2
    assert collector.failed_environments == 1
    async with harness.session_factory() as session:
        findings = (await session.scalars(select(Finding))).all()
        assert len(findings) == 2 and all(value.result == R.UNKNOWN for value in findings)

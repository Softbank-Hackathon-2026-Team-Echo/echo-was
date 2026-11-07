"""WAS 상태 전이 시험. Repository·sandbox는 명시적 mock, 실제 DB/AI 시험이 아니다."""

import asyncio
import importlib
import importlib.util
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.test_cve_autofix import context


class Session:
    def __init__(self):
        self.audit = []

    @asynccontextmanager
    async def begin(self):
        yield

    def add(self, row):
        self.audit.append(row)


class Repo:
    def __init__(self, finding, packages):
        self.finding, self.packages = finding, packages

    async def find_finding(self, org, fid):
        return (
            self.finding
            if str(self.finding.organization_id) == str(org) and self.finding.id == fid
            else None
        )

    async def find_context(self, finding):
        return "fixture/minimist", self.packages, finding.image_digest


class Client:
    calls = 0
    mode = "ok"
    input_hash = "sha256:" + "f" * 64

    def __init__(self, binding):
        self.binding = binding

    async def execute(self, run_id):
        Client.calls += 1
        if Client.mode == "cancel":
            raise asyncio.CancelledError()
        if Client.mode == "timeout":
            await asyncio.sleep(1)
        if Client.mode == "fail":
            raise RuntimeError("must not expose this secret")
        return dict(
            run_id=run_id,
            status="READY_FOR_REVIEW",
            input_hash=self.input_hash,
            source_finding_id=self.binding["source_finding_id"],
            cve_id="CVE-2021-44906",
            files=["package-lock.json", "package.json"],
            checks=["INSTALL", "BUILD", "TEST", "RESCAN"],
            source_head="a" * 40,
            source_artifact_kind="FIXTURE_SOURCE_TREE",
            source_artifact_digest=self.binding["image_digest"],
            real_image_digest=None,
            real_image_verified=False,
            deployment_ready=False,
            vulnerabilities_resolved=False,
            patch_hash="sha256:" + "b" * 64,
            rescan_sha256=self.binding["rescan_sha256"],
            scan_scope="captured_osv_package_version_response",
            manifest_sha256="c" * 64,
            lock_sha256="d" * 64,
            model_mocked=False,
            ai_reviewed=False,
            event_simulated=True,
            execution_simulated=False,
            merge_performed=False,
            deploy_performed=False,
        )


def setup(tmp_path, enabled=True):
    assert importlib.util.find_spec("app.services.cve_autofix_service") is not None, (
        "WAS service missing"
    )
    module = importlib.import_module("app.services.cve_autofix_service")
    finding, binding, packages = context()
    finding.expires_at = datetime.now(UTC) + timedelta(hours=1)
    binding["source_head"] = "a" * 40
    binding["rescan_sha256"] = "e" * 64
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps([binding]))
    settings = SimpleNamespace(
        cve_autofix_enabled=enabled,
        cve_autofix_fixture_enabled=True,
        cve_autofix_registry_path=registry,
        cve_autofix_timeout_seconds=0.05,
    )
    session = Session()
    clock = SimpleNamespace(now=lambda: datetime.now(UTC))
    service = module.CveAutofixService(session, clock, settings, client_factory=Client)
    service._repository = Repo(finding, packages)
    Client.calls = 0
    Client.mode = "ok"
    return service, finding, session


async def test_request_persists_receipt_and_duplicate_never_runs_twice(tmp_path):
    service, finding, session = setup(tmp_path)
    first = await service.request(finding.organization_id, finding.id, uuid.uuid4())
    second = await service.request(finding.organization_id, finding.id, uuid.uuid4())
    assert first == second and first["status"] == "READY_FOR_REVIEW"
    assert finding.mode == "SIMULATED" and Client.calls == 1
    assert len(session.audit) == 1


@pytest.mark.parametrize(
    "mode,status", [("fail", "FAILED"), ("timeout", "TIMED_OUT"), ("cancel", "CANCELLED")]
)
async def test_request_failure_timeout_cancel_never_claims_ready(tmp_path, mode, status):
    service, finding, _ = setup(tmp_path)
    Client.mode = mode
    if mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await service.request(finding.organization_id, finding.id, uuid.uuid4())
    else:
        receipt = await service.request(finding.organization_id, finding.id, uuid.uuid4())
        assert receipt["status"] == status
    assert finding.remediation["autofix"]["status"] == status
    assert "secret" not in json.dumps(finding.remediation)
    await service.request(finding.organization_id, finding.id, uuid.uuid4())
    assert Client.calls == 1


async def test_disabled_and_other_org_fail_before_execution(tmp_path):
    service, finding, _ = setup(tmp_path, False)
    with pytest.raises(Exception, match="AUTOFIX_DISABLED"):
        await service.request(finding.organization_id, finding.id, uuid.uuid4())
    assert Client.calls == 0
    service._settings.cve_autofix_enabled = True
    from app.services.cve_autofix_rules import CveAutofixRejectedError

    with pytest.raises(CveAutofixRejectedError):
        await service.request(uuid.uuid4(), finding.id, uuid.uuid4())
    assert Client.calls == 0


def test_cve_flags_default_off():
    from app.core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://fixture:fixture@127.0.0.1:1/fixture_test", _env_file=None
    )
    assert getattr(settings, "cve_autofix_enabled", None) is False
    assert getattr(settings, "cve_autofix_fixture_enabled", None) is False
    assert settings.cve_watch_enabled is False


async def test_diagnosis_router_returns_receipt_with_existing_human_org_scope():
    from app.routers.cve_watch_router import request_finding_diagnosis

    calls = []

    class Service:
        async def request_diagnosis(self, org, fid, **kwargs):
            calls.append((org, fid, kwargs))
            return {
                "run_id": str(uuid.uuid4()),
                "status": "RUNNING",
                "input_hash": "sha256:" + "f" * 64,
                "source_finding_id": str(fid),
                "cve_id": "CVE-2021-44906",
            }

    org, fid, actor = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    access = SimpleNamespace(organization_id=org, user=SimpleNamespace(id=actor))
    settings = SimpleNamespace(cve_autofix_enabled=True)
    result = await request_finding_diagnosis(access, fid, Service(), settings)
    assert result.data.status == "RUNNING"
    assert calls[0][0:2] == (org, fid) and calls[0][2]["actor_user_id"] == actor


async def test_cancel_running_receipt_is_persistent_and_idempotent_when_flags_off(tmp_path):
    service, finding, session = setup(tmp_path)
    finding.remediation = {
        **finding.remediation,
        "autofix": {
            "run_id": str(uuid.uuid4()),
            "status": "RUNNING",
            "source_finding_id": str(finding.id),
            "cve_id": "CVE-2021-44906",
            "input_hash": Client.input_hash,
        },
    }
    service._settings.cve_autofix_enabled = False
    assert hasattr(service, "cancel"), "persistent cancellation missing"
    first = await service.cancel(finding.organization_id, finding.id, uuid.uuid4())
    second = await service.cancel(finding.organization_id, finding.id, uuid.uuid4())
    assert first == second and first["status"] == "CANCELLED"
    assert Client.calls == 0 and len(session.audit) == 1


async def test_fixture_receipt_cannot_claim_real_image_or_deployment_readiness(tmp_path):
    service, finding, _ = setup(tmp_path)
    receipt = await service.request(finding.organization_id, finding.id, uuid.uuid4())
    from app.schemas.cve_autofix import CveAutofixReceiptResponse

    response = CveAutofixReceiptResponse.model_validate(receipt)
    assert response.status == "READY_FOR_REVIEW"
    assert response.source_artifact_kind == "FIXTURE_SOURCE_TREE"
    assert response.source_artifact_digest == finding.image_digest
    assert response.real_image_digest is None
    assert response.real_image_verified is False
    assert response.deployment_ready is False
    assert response.vulnerabilities_resolved is False
    assert "image_digest" not in receipt

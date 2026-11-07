"""취소/중복/receipt 경계. DB mock과 실제 Seatbelt 프로세스를 구분한다."""

import asyncio
import os
import uuid
from datetime import timedelta

import pytest

from tests.test_cve_autofix_service import Client, setup


async def test_cancel_stops_inflight_execution_before_deadline(tmp_path):
    service, finding, _ = setup(tmp_path)
    service._settings.cve_autofix_timeout_seconds = 2
    entered, stopped = asyncio.Event(), asyncio.Event()

    class BlockingClient(Client):
        async def execute(self, run_id):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    service._client_factory = BlockingClient
    task = asyncio.create_task(service.request(finding.organization_id, finding.id, uuid.uuid4()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        cancelled = await service.cancel(finding.organization_id, finding.id, uuid.uuid4())
        assert cancelled["status"] == "CANCELLED"
        await asyncio.wait_for(stopped.wait(), 0.3)
        assert (await task)["status"] == "CANCELLED"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("run_id", "10000000-0000-0000-0000-000000000001"),
        ("input_hash", "wrong"),
        ("source_finding_id", "10000000-0000-0000-0000-000000000002"),
        ("source_head", "f" * 40),
        ("source_artifact_kind", "OCI_IMAGE"),
        ("source_artifact_digest", "sha256:" + "f" * 64),
        ("real_image_verified", True),
        ("real_image_digest", "sha256:" + "f" * 64),
        ("deployment_ready", True),
        ("vulnerabilities_resolved", True),
        ("status", "SUCCEEDED"),
        ("rescan_sha256", "f" * 64),
        ("execution_simulated", True),
        ("model_mocked", True),
        ("patch_hash", "unbound"),
        ("scan_scope", "OCI_IMAGE"),
        ("manifest_sha256", None),
        ("lock_sha256", None),
    ],
)
async def test_forged_receipt_never_becomes_reviewable(tmp_path, field, bad):
    service, finding, _ = setup(tmp_path)

    class ForgedClient(Client):
        async def execute(self, run_id):
            return {**await super().execute(run_id), field: bad}

    service._client_factory = ForgedClient
    result = await service.request(finding.organization_id, finding.id, uuid.uuid4())
    assert result["status"] == "FAILED"
    assert finding.remediation["autofix"]["status"] == "FAILED"


async def test_deadline_crossed_before_final_persistence_never_claims_ready(tmp_path):
    service, finding, _ = setup(tmp_path)
    now = service._clock.now()
    service._clock.now = lambda: now

    class LateClient(Client):
        async def execute(self, run_id):
            result = await super().execute(run_id)
            service._clock.now = lambda: now + timedelta(seconds=10)
            return result

    service._client_factory = LateClient
    result = await service.request(finding.organization_id, finding.id, uuid.uuid4())
    assert result["status"] == "TIMED_OUT"


async def test_concurrent_duplicate_reserves_once_and_cancelled_late_success_is_discarded(tmp_path):
    service, finding, session = setup(tmp_path)
    service._settings.cve_autofix_timeout_seconds = 2
    entered, release = asyncio.Event(), asyncio.Event()

    class LateClient(Client):
        async def execute(self, run_id):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass  # 취소를 무시하는 늦은 성공도 영속 취소를 덮으면 안 된다.
            return await super().execute(run_id)

    service._client_factory = LateClient
    task = asyncio.create_task(service.request(finding.organization_id, finding.id, uuid.uuid4()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        duplicate = await service.request(finding.organization_id, finding.id, uuid.uuid4())
        assert duplicate["status"] == "RUNNING" and len(session.audit) == 1
        cancelled = await service.cancel(finding.organization_id, finding.id, uuid.uuid4())
        release.set()
        assert await task == cancelled
        assert finding.remediation["autofix"]["status"] == "CANCELLED"
        assert Client.calls == 1
        assert await service.request(finding.organization_id, finding.id, uuid.uuid4()) == cancelled
        assert Client.calls == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_lost_process_reservation_times_out_without_reexecution(tmp_path):
    service, finding, _ = setup(tmp_path)
    finding.remediation = {
        **finding.remediation,
        "autofix": {
            "run_id": str(uuid.uuid4()),
            "status": "RUNNING",
            "deadline_at": (service._clock.now() - timedelta(seconds=1)).isoformat(),
        },
    }
    result = await service.request(finding.organization_id, finding.id, uuid.uuid4())
    assert result["status"] == "TIMED_OUT" and result["failure_code"] == "PROCESS_LOST"
    assert Client.calls == 0


async def test_scope_changed_during_execution_is_stale(tmp_path):
    service, finding, _ = setup(tmp_path)

    class ChangedClient(Client):
        async def execute(self, run_id):
            result = await super().execute(run_id)
            finding.image_digest = "sha256:" + "0" * 64
            return result

    service._client_factory = ChangedClient
    result = await service.request(finding.organization_id, finding.id, uuid.uuid4())
    assert result["status"] == "STALE"


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
async def test_real_sandbox_process_is_reaped_on_cancel_or_timeout(tmp_path, monkeypatch, mode):
    import sys

    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt required; not OCI execution evidence")
    from app.clients.cve_autofix import run_sandboxed
    from app.services.cve_autofix_rules import CveAutofixRejectedError

    entered = asyncio.Event()
    processes = []
    create = asyncio.create_subprocess_exec

    async def observe(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        entered.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe)
    task = asyncio.create_task(
        run_sandboxed(
            ["/bin/sleep", "10"],
            tmp_path,
            "(version 1)(allow default)",
            {},
            0.05 if mode == "timeout" else 10,
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    if mode == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(CveAutofixRejectedError, match="TIMEOUT"):
            await task
    assert len(processes) == 1 and processes[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)


@pytest.mark.parametrize("operation", ["CANCEL", "TIMEOUT"])
async def test_service_cancellation_reaps_real_sandbox_process(tmp_path, monkeypatch, operation):
    import sys

    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt required; not OCI execution evidence")
    from app.clients.cve_autofix import run_sandboxed

    service, finding, _ = setup(tmp_path)
    service._settings.cve_autofix_timeout_seconds = 2 if operation == "CANCEL" else 0.1
    entered = asyncio.Event()
    processes = []
    create = asyncio.create_subprocess_exec

    async def observe(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        entered.set()
        return process

    class ProcessClient(Client):
        async def execute(self, run_id):
            await run_sandboxed(
                ["/bin/sleep", "10"], tmp_path, "(version 1)(allow default)", {}, 10
            )
            return await super().execute(run_id)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe)
    service._client_factory = ProcessClient
    task = asyncio.create_task(service.request(finding.organization_id, finding.id, uuid.uuid4()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if operation == "CANCEL":
            await service.cancel(finding.organization_id, finding.id, uuid.uuid4())
        result = await asyncio.wait_for(task, 1)
        assert result["status"] == ("CANCELLED" if operation == "CANCEL" else "TIMED_OUT")
        assert len(processes) == 1 and processes[0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("real_image_verified", True),
        ("deployment_ready", True),
        ("vulnerabilities_resolved", True),
        ("real_image_digest", "sha256:" + "f" * 64),
        ("status", "SUCCEEDED"),
    ],
)
async def test_api_schema_rejects_fixture_promotion(tmp_path, field, bad):
    from pydantic import ValidationError

    from app.schemas.cve_autofix import CveAutofixReceiptResponse

    service, finding, _ = setup(tmp_path)
    result = await service.request(finding.organization_id, finding.id, uuid.uuid4())
    with pytest.raises(ValidationError):
        CveAutofixReceiptResponse.model_validate({**result, field: bad})

"""CVE finding → 결정론적 fixture 수정 → 검증된 검토 대기 영수증. merge/deploy 경로 없음."""

import asyncio
import json
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction, AuditTargetType
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.clients.cve_autofix import LocalCveSandboxClient
from app.core.clock import Clock
from app.core.config import Settings
from app.core.exceptions import FindingNotFoundError
from app.repositories.cve_autofix_repository import CveAutofixRepository
from app.services.cve_autofix_rules import CveAutofixRejectedError, reject, validate_context


def read_registry(path: Path | None) -> list[dict[str, Any]]:
    if path is None or path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
        reject("REGISTRY_NOT_CONFIGURED")
    result = json.loads(path.read_text())
    if (
        not isinstance(result, list)
        or len(result) > 20
        or any(not isinstance(row, dict) for row in result)
    ):
        reject("REGISTRY_INVALID")
    return result


class CveAutofixService:
    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        settings: Settings,
        *,
        client_factory: Callable[[dict[str, Any]], Any] = LocalCveSandboxClient,
    ) -> None:
        self._session = session
        self._clock = clock
        self._settings = settings
        self._repository = CveAutofixRepository(session)
        self._client_factory = client_factory

    async def request(
        self,
        organization_id: uuid.UUID,
        finding_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> dict[str, Any]:
        if not self._settings.cve_autofix_enabled or not self._settings.cve_autofix_fixture_enabled:
            reject("AUTOFIX_DISABLED")
        bindings = await asyncio.to_thread(read_registry, self._settings.cve_autofix_registry_path)
        selected = [
            row
            for row in bindings
            if row.get("organization_id") == str(organization_id)
            and row.get("source_finding_id") == str(finding_id)
        ]
        if len(selected) != 1:
            reject("BINDING_NOT_UNIQUE")
        binding = selected[0]
        client = self._client_factory(binding)
        async with self._session.begin():
            finding = await self._repository.find_finding(organization_id, finding_id)
            if finding is None:
                raise FindingNotFoundError("finding not found")
            previous = (finding.remediation or {}).get("autofix")
            if previous:
                # 응답 유실·중복 event·실패 재시도는 기존 예약을 유지한다. 자동 재실행 없음.
                if (
                    previous.get("status") == "RUNNING"
                    and datetime.fromisoformat(previous["deadline_at"]) <= self._clock.now()
                ):
                    previous = {**previous, "status": "TIMED_OUT", "failure_code": "PROCESS_LOST"}
                    finding.remediation = {**(finding.remediation or {}), "autofix": previous}
                return dict(previous)
            repository, packages, digest = await self._repository.find_context(finding)
            validate_context(finding, str(organization_id), binding, packages, repository)
            if digest != binding["image_digest"] or finding.expires_at <= self._clock.now():
                reject("DEPLOYMENT_OR_FINDING_STALE")
            run_id = str(uuid.uuid4())
            receipt = {
                "run_id": run_id,
                "status": "RUNNING",
                "input_hash": client.input_hash,
                "source_finding_id": str(finding_id),
                "cve_id": binding["cve_id"],
                "source_artifact_kind": binding["image_digest_kind"],
                "source_artifact_digest": binding["image_digest"],
                "real_image_digest": None,
                "real_image_verified": False,
                "deployment_ready": False,
                "vulnerabilities_resolved": False,
                "event_simulated": True,
                "ai_reviewed": False,
                "merge_performed": False,
                "deploy_performed": False,
                "deadline_at": (
                    self._clock.now()
                    + timedelta(seconds=self._settings.cve_autofix_timeout_seconds)
                ).isoformat(),
            }
            finding.remediation = {**(finding.remediation or {}), "autofix": receipt}
            AuditRecorder(self._session, self._clock).record(
                organization_id=organization_id,
                actor=AuditActor.user(actor_user_id),
                action=AuditAction.CVE_DIAGNOSIS_REQUESTED,
                target=AuditTarget(AuditTargetType.FINDING, finding_id),
                details={
                    "runId": run_id,
                    "executor": "DETERMINISTIC_DEPENDENCY_BUMP",
                    "simulated": True,
                },
            )
        # 먼저 예약을 커밋하고 외부 프로세스를 시작한다. 중단돼도 다음 요청은 재실행하지 않는다.
        cancelled = False
        try:
            async with asyncio.timeout(self._settings.cve_autofix_timeout_seconds):
                result = await self._execute_until_cancelled(
                    client, run_id, organization_id, finding_id
                )
            self._validate_receipt(result, receipt, binding)
            receipt = {**receipt, **result}
        except asyncio.CancelledError:
            cancelled = True
            receipt = {**receipt, "status": "CANCELLED", "failure_code": "CANCELLED"}
        except TimeoutError:
            receipt = {**receipt, "status": "TIMED_OUT", "failure_code": "TIMEOUT"}
        except CveAutofixRejectedError as exc:
            # 고정 사유만 보존한다. stderr·명령 인수·비밀·임의 응답을 API에 싣지 않는다.
            receipt = {**receipt, "status": "FAILED", "failure_code": exc.code}
        except Exception:
            receipt = {**receipt, "status": "FAILED", "failure_code": "SANDBOX_FAILED"}
        async with self._session.begin():
            finding = await self._repository.find_finding(organization_id, finding_id)
            if finding is None:
                raise FindingNotFoundError("finding not found")
            current = (finding.remediation or {}).get("autofix", {})
            if current.get("run_id") == run_id and current.get("status") == "CANCELLED":
                return dict(current)
            if current.get("run_id") != run_id or current.get("status") != "RUNNING":
                reject("RUN_NOT_ACTIVE")
            if receipt[
                "status"
            ] == "READY_FOR_REVIEW" and self._clock.now() >= datetime.fromisoformat(
                receipt["deadline_at"]
            ):
                receipt = {**receipt, "status": "TIMED_OUT", "failure_code": "TIMEOUT"}
            if receipt["status"] == "READY_FOR_REVIEW":
                repository, packages, digest = await self._repository.find_context(finding)
                try:
                    validate_context(finding, str(organization_id), binding, packages, repository)
                    if digest != binding["image_digest"] or finding.expires_at <= self._clock.now():
                        reject("DEPLOYMENT_OR_FINDING_STALE")
                except CveAutofixRejectedError:
                    receipt = {**receipt, "status": "STALE", "failure_code": "SCOPE_CHANGED"}
            receipt = {**receipt, "finished_at": self._clock.now().isoformat()}
            finding.remediation = {**(finding.remediation or {}), "autofix": receipt}
        if cancelled:
            raise asyncio.CancelledError
        return receipt

    async def _execute_until_cancelled(
        self, client: Any, run_id: str, organization_id: uuid.UUID, finding_id: uuid.UUID
    ) -> dict[str, Any]:
        execution = asyncio.create_task(client.execute(run_id))
        try:
            while not execution.done():
                await asyncio.wait({execution}, timeout=0.05)
                async with self._session.begin():
                    finding = await self._repository.find_finding(organization_id, finding_id)
                    current = (finding.remediation or {}).get("autofix", {}) if finding else {}
                    active = current.get("run_id") == run_id and current.get("status") == "RUNNING"
                if not active:
                    execution.cancel()
                    raise asyncio.CancelledError
            result: dict[str, Any] = await execution
            return result
        finally:
            if not execution.done():
                execution.cancel()
            # run_sandboxed가 프로세스 그룹을 회수할 때까지 기다린다.
            await asyncio.gather(execution, return_exceptions=True)

    async def cancel(
        self,
        organization_id: uuid.UUID,
        finding_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> dict[str, Any]:
        # OFF여도 이미 시작된 작업은 취소할 수 있다. 완료/취소를 되감거나 재실행하지 않는다.
        async with self._session.begin():
            finding = await self._repository.find_finding(organization_id, finding_id)
            if finding is None:
                raise FindingNotFoundError("finding not found")
            receipt = (finding.remediation or {}).get("autofix")
            if not receipt or finding.mode != "SIMULATED" or finding.source != "CVE_WATCH":
                reject("RUN_NOT_ACTIVE")
            if receipt.get("status") != "RUNNING":
                return dict(receipt)
            receipt = {
                **receipt,
                "status": "CANCELLED",
                "failure_code": "CANCELLED",
                "finished_at": self._clock.now().isoformat(),
            }
            finding.remediation = {**(finding.remediation or {}), "autofix": receipt}
            AuditRecorder(self._session, self._clock).record(
                organization_id=organization_id,
                actor=AuditActor.user(actor_user_id),
                action=AuditAction.CVE_DIAGNOSIS_REQUESTED,
                target=AuditTarget(AuditTargetType.FINDING, finding_id),
                details={"runId": receipt["run_id"], "operation": "CANCEL", "simulated": True},
            )
            return receipt

    @staticmethod
    def _validate_receipt(
        result: dict[str, Any], reserved: dict[str, Any], binding: dict[str, Any]
    ) -> None:
        for key in ("run_id", "input_hash", "source_finding_id", "cve_id"):
            if result.get(key) != reserved[key]:
                reject("RECEIPT_MISMATCH")
        if (
            result.get("status") != "READY_FOR_REVIEW"
            or result.get("source_head") != binding["source_head"]
            or result.get("source_artifact_kind") != "FIXTURE_SOURCE_TREE"
            or result.get("source_artifact_digest") != binding["image_digest"]
            or "image_digest" in result
            or result.get("real_image_digest") is not None
            or result.get("real_image_verified") is not False
            or result.get("deployment_ready") is not False
            or result.get("vulnerabilities_resolved") is not False
            or result.get("files") != ["package-lock.json", "package.json"]
            or result.get("checks") != ["INSTALL", "BUILD", "TEST", "RESCAN"]
            or result.get("event_simulated") is not True
            or result.get("ai_reviewed") is not False
            or result.get("merge_performed") is not False
            or result.get("deploy_performed") is not False
            or result.get("execution_simulated") is not False
            or result.get("model_mocked") is not False
            or result.get("rescan_sha256") != binding["rescan_sha256"]
            or result.get("scan_scope") != "captured_osv_package_version_response"
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(result.get("patch_hash", "")))
            or any(
                not re.fullmatch(r"[0-9a-f]{64}", str(result.get(key, "")))
                for key in ("manifest_sha256", "lock_sha256")
            )
        ):
            reject("RECEIPT_NOT_VERIFIED")

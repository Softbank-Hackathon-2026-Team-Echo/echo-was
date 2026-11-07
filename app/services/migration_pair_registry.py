"""서버 등록표만으로 DB쌍을 고른다. provider kind는 선택 키가 아니다."""

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.core.exceptions import ConflictError, ErrorField
from app.schemas.migration_capability import (
    OPERATIONS,
    DatabasePairRegistration,
    DatabaseRegistration,
    MigrationCapabilityResponse,
    MigrationPairCapabilitiesResponse,
    MigrationPairConfiguration,
    MigrationPairContext,
    MigrationPairSnapshot,
    Operation,
)


class MigrationPairBlockedError(ConflictError):
    code = "MIGRATION_PAIR_UNSUPPORTED"


def blocked(reason: str) -> MigrationPairBlockedError:
    return MigrationPairBlockedError(details=[ErrorField(field="migrationPair", reason=reason)])


class MigrationPairRegistry:
    def __init__(self, configuration: MigrationPairConfiguration) -> None:
        self._configuration = configuration
        canonical = json.dumps(
            configuration.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self._sha256 = hashlib.sha256(canonical.encode()).hexdigest()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "MigrationPairRegistry":
        return cls(MigrationPairConfiguration.model_validate(config))

    def _resolve(
        self, context: MigrationPairContext
    ) -> tuple[DatabaseRegistration, DatabaseRegistration, DatabasePairRegistration]:
        def registration(environment_id: Any) -> DatabaseRegistration:
            matches = [
                item
                for item in self._configuration.databases
                if item.organization_id == context.organization_id
                and item.service_id == context.service_id
                and item.environment_id == environment_id
            ]
            if len(matches) != 1:
                raise blocked("DB_BINDING_UNREGISTERED")
            return matches[0]

        source = registration(context.source_environment_id)
        target = registration(context.target_environment_id)
        matches = [
            pair
            for pair in self._configuration.pairs
            if pair.source_db_id == source.id and pair.target_db_id == target.id
        ]
        if len(matches) != 1:
            raise blocked("DB_DIRECTION_UNREGISTERED")
        return source, target, matches[0]

    def snapshot(
        self, context: MigrationPairContext | dict[str, Any], operation: Operation, now: datetime
    ) -> MigrationPairSnapshot:
        current = MigrationPairContext.model_validate(context)
        if operation not in OPERATIONS:
            raise blocked("DB_OPERATION_UNKNOWN")
        source, target, pair = self._resolve(current)
        if source.environment_kind not in (
            "aws",
            "gcp",
            "onprem",
        ) or target.environment_kind not in ("aws", "gcp", "onprem"):
            raise blocked("ENVIRONMENT_KIND_UNKNOWN")
        if source.id == target.id or current.source_environment_id == current.target_environment_id:
            raise blocked("DB_PAIR_SAME")
        if current.source_deployment_id is None or current.target_deployment_id is None:
            raise blocked("DEPLOYMENT_MISSING")
        if (
            source.deployment_id != current.source_deployment_id
            or target.deployment_id != current.target_deployment_id
        ):
            raise blocked("DB_DEPLOYMENT_CHANGED")
        job_kind = self._validate_operation(source, target, pair, operation, now)
        return MigrationPairSnapshot(
            **current.model_dump(),
            registry_revision=self._configuration.revision,
            registry_sha256=self._sha256,
            source_db_id=source.id,
            target_db_id=target.id,
            operation=operation,
            job_kind=job_kind,
        )

    def _validate_operation(
        self,
        source: DatabaseRegistration,
        target: DatabaseRegistration,
        pair: DatabasePairRegistration,
        operation: Operation,
        now: datetime,
    ) -> Any:
        if now.tzinfo is None or now.utcoffset() is None:
            raise blocked("EVIDENCE_CLOCK_UNKNOWN")
        required = [
            ("ROUTE", pair.route),
            ("WRITER_PATH", pair.writer_path),
            ("WRITER", pair.writer),
        ]
        if operation != "appTraffic":
            required += [("DATA_SYNC", pair.data_sync), ("JOB_CONTRACT", pair.job_contract)]
        for label, evidence in required:
            if (
                evidence.observed_at is None
                or evidence.observed_at.tzinfo is None
                or evidence.observed_at.utcoffset() is None
                or not evidence.evidence_id
                or evidence.status == "UNKNOWN"
            ):
                raise blocked(f"{label}_UNKNOWN")
            age = (now - evidence.observed_at).total_seconds()
            if age < 0:
                raise blocked(f"{label}_UNKNOWN")
            if age > self._configuration.max_age_seconds:
                raise blocked(f"{label}_STALE")
            if evidence.status != "VERIFIED":
                raise blocked(f"{label}_FAILED")
        if pair.writer_db_id != source.id:
            raise blocked("DB_WRITER_MISMATCH")
        if operation == "appTraffic":
            return None
        slots = (source.adapter_slot, target.adapter_slot)
        kind_by_slot = {"A": "aws", "B": "gcp"}
        if source.environment_kind != kind_by_slot.get(
            source.adapter_slot
        ) or target.environment_kind != kind_by_slot.get(target.adapter_slot):
            raise blocked("DB_ADAPTER_UNSUPPORTED")
        if (
            pair.adapter != "legacy-ab-v1"
            or source.database_name != "app"
            or target.database_name != "app"
            or slots not in (("A", "B"), ("B", "A"))
        ):
            raise blocked("DB_ADAPTER_UNSUPPORTED")
        if operation == "disasterPromote":
            if slots != ("A", "B"):
                raise blocked("DISASTER_PROMOTE_DIRECTION_UNSUPPORTED")
            return "promote"
        if operation == "failback" and slots != ("B", "A"):
            raise blocked("FAILBACK_DIRECTION_UNSUPPORTED")
        return "cutover" if slots == ("A", "B") else "rollback"

    def capabilities(
        self, context: MigrationPairContext | dict[str, Any], now: datetime
    ) -> MigrationPairCapabilitiesResponse:
        current = MigrationPairContext.model_validate(context)
        results: list[MigrationCapabilityResponse] = []
        source_id = target_id = None
        for operation in OPERATIONS:
            try:
                snapshot = self.snapshot(current, operation, now)
            except MigrationPairBlockedError as exc:
                reason = exc.details[0].reason if exc.details else "DB_PAIR_UNKNOWN"
                is_stale = reason.endswith("_STALE")
                is_unknown = is_stale or reason.endswith("_UNKNOWN") or "UNREGISTERED" in reason
                results.append(
                    MigrationCapabilityResponse(
                        supported=False,
                        status="UNKNOWN" if is_unknown else "UNSUPPORTED",
                        reasons=[reason],
                        evidence_freshness="STALE" if is_stale else "UNKNOWN",
                        evidence_ids=[],
                    )
                )
            else:
                source_id, target_id = snapshot.source_db_id, snapshot.target_db_id
                _, _, pair = self._resolve(current)
                evidence = [pair.route, pair.writer_path, pair.writer]
                if operation != "appTraffic":
                    evidence += [pair.data_sync, pair.job_contract]
                results.append(
                    MigrationCapabilityResponse(
                        supported=True,
                        status="SUPPORTED",
                        reasons=[],
                        evidence_freshness="FRESH",
                        evidence_ids=sorted(
                            {item.evidence_id for item in evidence if item.evidence_id}
                        ),
                    )
                )
        return MigrationPairCapabilitiesResponse(
            registry_revision=self._configuration.revision,
            source_environment_id=current.source_environment_id,
            target_environment_id=current.target_environment_id,
            source_db_registry_id=source_id,
            target_db_registry_id=target_id,
            app_traffic=results[0],
            db_cutover=results[1],
            disaster_promote=results[2],
            failback=results[3],
        )

    def dispatch(
        self,
        context: MigrationPairContext | dict[str, Any],
        operation: Operation,
        snapshot: MigrationPairSnapshot | None,
        now: datetime,
        submit: Callable[[MigrationPairSnapshot], Any],
    ) -> Any:
        current = self.snapshot(context, operation, now)
        if snapshot != current:
            raise blocked("DB_PAIR_SNAPSHOT_CHANGED")
        return submit(current)

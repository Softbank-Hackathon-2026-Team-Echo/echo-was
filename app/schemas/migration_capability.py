"""명시 DB 등록표·계획 snapshot·지원 조회의 공통 자료형."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

RegistryId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
Operation = Literal["appTraffic", "dbCutover", "disasterPromote", "failback"]
OPERATIONS: tuple[Operation, ...] = ("appTraffic", "dbCutover", "disasterPromote", "failback")


class RegistryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PairEvidence(RegistryModel):
    status: Literal["VERIFIED", "FAILED", "UNKNOWN"] = "UNKNOWN"
    observed_at: datetime | None = None
    evidence_id: str | None = Field(default=None, max_length=128)


class DatabaseRegistration(RegistryModel):
    id: RegistryId
    organization_id: UUID
    service_id: UUID
    environment_id: UUID
    deployment_id: UUID
    environment_kind: str = Field(min_length=1, max_length=63)
    adapter_slot: Literal["A", "B", "ONPREM", "GCP_B2", "AWS_ONPREM"]
    database_name: Literal["app", "demo"]


class DatabasePairRegistration(RegistryModel):
    source_db_id: RegistryId
    target_db_id: RegistryId
    adapter: Literal["legacy-ab-v1", "unimplemented"]
    route: PairEvidence = Field(default_factory=PairEvidence)
    writer_path: PairEvidence = Field(default_factory=PairEvidence)
    data_sync: PairEvidence = Field(default_factory=PairEvidence)
    job_contract: PairEvidence = Field(default_factory=PairEvidence)
    writer_db_id: RegistryId | None = None
    writer: PairEvidence = Field(default_factory=PairEvidence)


class MigrationPairConfiguration(RegistryModel):
    revision: str = Field(default="unconfigured", min_length=1, max_length=128)
    max_age_seconds: int = Field(default=300, ge=1, le=3600, strict=True)
    databases: tuple[DatabaseRegistration, ...] = ()
    pairs: tuple[DatabasePairRegistration, ...] = ()

    @model_validator(mode="after")
    def validate_unique_bindings(self) -> "MigrationPairConfiguration":
        identities = [item.id for item in self.databases]
        bindings = [
            (item.organization_id, item.service_id, item.environment_id) for item in self.databases
        ]
        directions = [(item.source_db_id, item.target_db_id) for item in self.pairs]
        if (
            len(set(identities)) != len(identities)
            or len(set(bindings)) != len(bindings)
            or len(set(directions)) != len(directions)
        ):
            raise ValueError("duplicate database identity, binding or direction")
        if any(
            source == target or source not in identities or target not in identities
            for source, target in directions
        ):
            raise ValueError("invalid database direction")
        by_id = {item.id: item for item in self.databases}
        for source, target in directions:
            left, right = by_id[source], by_id[target]
            if (left.organization_id, left.service_id) != (right.organization_id, right.service_id):
                raise ValueError("cross-scope database direction")
        return self


class MigrationPairContext(RegistryModel):
    organization_id: UUID
    service_id: UUID
    source_environment_id: UUID
    target_environment_id: UUID
    source_deployment_id: UUID | None
    target_deployment_id: UUID | None


class MigrationPairSnapshot(MigrationPairContext):
    contract_version: Literal["db-pair/v1"] = "db-pair/v1"
    registry_revision: str
    registry_sha256: str
    source_db_id: RegistryId
    target_db_id: RegistryId
    operation: Operation
    job_kind: Literal["cutover", "rollback", "promote"] | None

    def job_environment(self) -> dict[str, str]:
        return {
            "SOURCE_DB": self.source_db_id,
            "TARGET_DB": self.target_db_id,
            "EXPECTED_WRITER_DB": self.source_db_id,
            "DB_PAIR_CONTRACT_VERSION": self.contract_version,
            "DB_PAIR_REGISTRY_SHA256": self.registry_sha256,
            "ORGANIZATION_ID": str(self.organization_id),
            "SERVICE_ID": str(self.service_id),
            "SOURCE_ENVIRONMENT_ID": str(self.source_environment_id),
            "TARGET_ENVIRONMENT_ID": str(self.target_environment_id),
            "SOURCE_DEPLOYMENT_ID": str(self.source_deployment_id),
            "TARGET_DEPLOYMENT_ID": str(self.target_deployment_id),
        }


class MigrationCapabilityResponse(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda key: (
            key.split("_")[0] + "".join(part.title() for part in key.split("_")[1:])
        ),
        populate_by_name=True,
        extra="forbid",
    )
    supported: bool
    status: Literal["SUPPORTED", "UNSUPPORTED", "UNKNOWN"]
    reasons: list[str]
    evidence_freshness: Literal["FRESH", "STALE", "UNKNOWN"]
    evidence_ids: list[str]


class MigrationPairCapabilitiesResponse(BaseModel):
    model_config = MigrationCapabilityResponse.model_config
    contract_version: Literal["db-pair/v1"] = "db-pair/v1"
    registry_revision: str
    creation_gate_enforced: bool = False
    approval_gate_enforced: bool = False
    worker_gate_enforced: bool = False
    execution_ready: bool = False
    source_environment_id: UUID
    target_environment_id: UUID
    source_db_registry_id: str | None
    target_db_registry_id: str | None
    app_traffic: MigrationCapabilityResponse
    db_cutover: MigrationCapabilityResponse
    disaster_promote: MigrationCapabilityResponse
    failback: MigrationCapabilityResponse

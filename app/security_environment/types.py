"""관리자가 지정한 환경 보안 대상과 관측 결과. 비밀값은 받지 않는다."""

import hashlib
import ipaddress
import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.secrets import parse_secret_ref
from app.enums import SecurityFindingSource, SecurityResult

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:/@-]{0,190}$")]
Name = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
EVIDENCE_TTL = timedelta(minutes=30)
COLLECTOR_VERSION = "environment-security-v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IamSubject(StrictModel):
    id: Name
    kind: Literal["kubernetes", "aws", "gcp"]
    name: Identifier
    namespace: Name | None = None
    project_id: Name | None = None
    # 허용된 세부 권한. 기존 관리 역할 자체를 허용표에 넣어 PASS로 바꾸지 않는다.
    allowed_actions: tuple[Identifier, ...] = ()
    allowed_resources: tuple[str, ...] = ()
    allowed_namespaces: tuple[Name, ...] = ()
    workloads: tuple[Name, ...] = ()
    must_be_distinct_from: tuple[Name, ...] = ()

    @model_validator(mode="after")
    def validate_identity(self) -> "IamSubject":
        if self.kind == "kubernetes" and (
            not self.namespace or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", self.name)
        ):
            raise ValueError("kubernetes subject requires namespace")
        if self.kind == "gcp" and (
            not self.project_id
            or not re.fullmatch(r"[a-z0-9-]+@[a-z0-9.-]+\.iam\.gserviceaccount\.com", self.name)
        ):
            raise ValueError("gcp subject requires project")
        if self.kind == "aws" and not re.fullmatch(
            r"arn:aws:iam::[0-9]{12}:role/[a-zA-Z0-9_+=,.@/\-]+", self.name
        ):
            raise ValueError("invalid role ARN")
        return self


class TransportTarget(StrictModel):
    id: Name
    kind: Literal["http", "entrypoint", "database"]
    # http/entrypoint만 쓴다. database는 보호된 ConfigMap 관측을 사용한다.
    url: str | None = None
    token_ref: str | None = None
    ca_file: str | None = None
    route: Identifier | None = None
    snapshot_namespace: Name = "echo-security"
    snapshot_name: Name = "transport-evidence"
    snapshot_revision: Annotated[str, Field(min_length=1, max_length=64)] = "v1"

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid managed transport URL")
        host = parsed.hostname
        if host.lower() in ("localhost", "metadata.google.internal"):
            raise ValueError("unsafe managed transport host")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        ):
            raise ValueError("unsafe managed transport address")
        return value.rstrip("/")

    @field_validator("token_ref")
    @classmethod
    def validate_secret_ref(cls, value: str | None) -> str | None:
        if value is not None:
            parse_secret_ref(value)
        return value

    @model_validator(mode="after")
    def validate_target(self) -> "TransportTarget":
        if self.kind != "database" and self.url is None:
            raise ValueError("transport URL required")
        if self.kind == "entrypoint" and (not self.token_ref or not self.route):
            raise ValueError("entrypoint requires read token and route")
        if self.kind == "entrypoint":
            parsed = urlsplit(self.url or "")
            if parsed.scheme != "https":
                raise ValueError("read token requires HTTPS")
            ipaddress.ip_address(parsed.hostname or "")
            if parsed.path not in ("", "/"):
                raise ValueError("entrypoint URL must be an origin")
        if self.kind == "database" and (self.url or self.token_ref or self.ca_file):
            raise ValueError("database observations must use the protected snapshot")
        return self


class EnvironmentSecurityProfile(StrictModel):
    organization_id: uuid.UUID
    environment_id: uuid.UUID
    subjects: tuple[IamSubject, ...] = Field(min_length=1, max_length=32)
    transports: tuple[TransportTarget, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_ids(self) -> "EnvironmentSecurityProfile":
        ids = [s.id for s in self.subjects]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate subject")
        paths = [p.id for p in self.transports]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate transport")
        for subject in self.subjects:
            if any(
                other not in ids or other == subject.id for other in subject.must_be_distinct_from
            ):
                raise ValueError("unknown separation subject")
        return self

    def revision(self, config: dict[str, object], secret_ref: str | None) -> str:
        raw = json.dumps(
            {"profile": self.model_dump(mode="json"), "config": config, "secretRef": secret_ref},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode()).hexdigest()


class EvidenceCheck(StrictModel):
    id: Identifier
    result: Literal[
        SecurityResult.PASS, SecurityResult.WARN, SecurityResult.FAIL, SecurityResult.UNKNOWN
    ]
    reason: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
    observed_at: AwareDatetime
    # 외부 원문 대신 고정 식별자·이유 코드만 저장한다.
    source: SecurityFindingSource


class TransportObservation(StrictModel):
    schema_version: Literal[1] = 1
    revision: Annotated[str, Field(min_length=1, max_length=64)]
    observed_at: AwareDatetime
    checks: tuple[EvidenceCheck, ...] = Field(min_length=1, max_length=128)


def unknown(
    check_id: str, reason: str, now: datetime, source: SecurityFindingSource
) -> EvidenceCheck:
    return EvidenceCheck(
        id=check_id, result=SecurityResult.UNKNOWN, reason=reason, observed_at=now, source=source
    )

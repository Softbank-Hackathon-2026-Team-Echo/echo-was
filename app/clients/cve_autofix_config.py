"""관리자가 정적으로 등록하는 CVE fixture 실행 계약. HTTP 본문에서는 받지 않는다."""

import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CveFixtureBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    organization_id: uuid.UUID
    service_id: uuid.UUID
    environment_id: uuid.UUID
    source_finding_id: uuid.UUID
    # 기존 모의 finding/snapshot 키와의 결속만 유지한다. OCI image 신원이 아니다.
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    image_digest_kind: Literal["FIXTURE_SOURCE_TREE"]
    repository: Literal["fixture/minimist"]
    cve_id: Literal["CVE-2021-44906"]
    ecosystem: Literal["npm"]
    package: Literal["minimist"]
    from_version: Literal["1.2.5"]
    fixed_version: Literal["1.2.6"]
    fixture: Literal[True]
    source_dir: Path
    source_head: str = Field(pattern=r"^[0-9a-f]{40}$")
    files_sha256: dict[str, str]
    cache_dir: Path
    node: Path
    npm_cli: Path
    osv_path: Path
    osv_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rescan_path: Path
    rescan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixed_metadata: dict[str, Any]
    output_root: Path
    timeout_seconds: float = Field(default=30, gt=0, le=120)

    @field_validator(
        "source_dir", "cache_dir", "node", "npm_cli", "osv_path", "rescan_path", "output_root"
    )
    @classmethod
    def validate_path(cls, path: Path) -> Path:
        if (
            not path.is_absolute()
            or path.resolve() != path
            or any(c in str(path) for c in ("\x00", "\n"))
        ):
            raise ValueError("canonical absolute local path required")
        return path

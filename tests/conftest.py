import os
import sys
import textwrap
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

# Settings 가 DATABASE_URL 을 필수로 요구한다. 단위 테스트는 실제 DB 에 붙지 않는다.
# 환경변수가 .env 보다 우선하므로, .env 가 있어도 앱 엔진은 이 닿지 않는 주소를 쓴다.
# DB 통합 테스트는 TEST_DATABASE_URL 로 따로 붙는다(tests/integration/conftest.py).
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")

from app.adapters import base, registry  # noqa: E402

EXTRA_PACKAGE_PREFIX = "extra_adapters_"
# 새 환경 종류를 "파일 하나"로 더하는 모습 그대로 쓴 양식 파일.
# 레지스트리·모델·마이그레이션은 건드리지 않는다.
AZURE_ADAPTER_SOURCE = textwrap.dedent(
    """
    from app.adapters.base import EnvironmentConfig, NonEmptyStr, register_environment_kind


    @register_environment_kind("azure")
    class AzureEnvironmentConfig(EnvironmentConfig):
        subscription_id: NonEmptyStr
        location: NonEmptyStr
        cluster_name: NonEmptyStr

        def display_location(self) -> str:
            return self.location
    """
)


@pytest.fixture
def isolated_environment_kinds(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """테스트 안에서 등록한 환경 종류를 테스트가 끝나면 원래대로 되돌린다."""
    registry.discover()
    monkeypatch.setattr(base, "_registered_kinds", dict(base._registered_kinds))
    monkeypatch.setattr(registry, "_discovered_packages", set(registry._discovered_packages))
    yield
    for name in [name for name in sys.modules if name.startswith(EXTRA_PACKAGE_PREFIX)]:
        del sys.modules[name]


@pytest.fixture
def azure_adapter_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_environment_kinds: None
) -> str:
    """tmp_path 에 azure.py 하나가 든 어댑터 패키지를 만들고 그 패키지 이름을 돌려준다."""
    package_name = f"{EXTRA_PACKAGE_PREFIX}{uuid.uuid4().hex}"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    (package_dir / "azure.py").write_text(AZURE_ADAPTER_SOURCE)
    monkeypatch.syspath_prepend(str(tmp_path))
    return package_name

"""환경 설정 양식 레지스트리: 패키지 안 모듈을 자동으로 불러와 종류를 등록하고, 저장 전 검사를 한다.

새 환경 종류 추가는 이 패키지에 파일 하나를 더하는 것으로 끝난다. 이 파일·모델·마이그레이션은
고치지 않는다(NFR-15).
"""

import importlib
import pkgutil
from collections.abc import Mapping

from pydantic import ValidationError

from app.adapters import base
from app.adapters.base import (
    EnvironmentConfig,
    InvalidEnvironmentConfigError,
    UnknownEnvironmentKindError,
)

DEFAULT_PACKAGE = "app.adapters"
_ROOT_FIELD = "(root)"

_discovered_packages: set[str] = set()


def discover(package_name: str = DEFAULT_PACKAGE) -> None:
    """패키지 안 모듈을 모두 import 해 종류 등록을 실행한다. 패키지마다 한 번만 한다."""
    if package_name in _discovered_packages:
        return
    package = importlib.import_module(package_name)
    for module_info in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package_name}.{module_info.name}")
    _discovered_packages.add(package_name)


def get_environment_config_class(kind: str) -> type[EnvironmentConfig]:
    discover()
    config_class = base.registered_kinds().get(kind)
    if config_class is None:
        raise UnknownEnvironmentKindError("unknown environment kind", kind=kind)
    return config_class


def search_environment_kinds() -> list[str]:
    discover()
    return sorted(base.registered_kinds())


def validate_environment_config(kind: str, config: Mapping[str, object]) -> EnvironmentConfig:
    """kind 의 설정 양식으로 config 를 검사해 양식 객체를 돌려준다.

    위반이면 InvalidEnvironmentConfigError 를 낸다. 예외에는 위반한 필드 경로만 담는다.
    pydantic 오류를 원인(__cause__)으로 잇지 않는다 — 그 안에 입력 값이 들어 있어 로그로 샐 수 있다.
    """
    config_class = get_environment_config_class(kind)
    try:
        return config_class.model_validate(config)
    except ValidationError as exc:
        invalid_fields = sorted(
            {
                ".".join(str(part) for part in error["loc"]) or _ROOT_FIELD
                for error in exc.errors(include_input=False, include_url=False)
            }
        )
        raise InvalidEnvironmentConfigError(
            "invalid environment config", kind=kind, invalid_fields=invalid_fields
        ) from None

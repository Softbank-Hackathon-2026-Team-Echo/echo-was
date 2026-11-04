"""환경 종류 레지스트리: 패키지 안 모듈을 자동으로 불러와 종류를 등록하고, 저장 전 검사를 한다.

새 환경 종류 추가는 이 패키지에 파일 하나(설정 양식 + 동작 클래스)를 더하는 것으로 끝난다.
이 파일·모델·마이그레이션·라우터·서비스는 고치지 않는다(NFR-15).
"""

import importlib
import pkgutil
from collections.abc import Mapping

from pydantic import ValidationError
from pydantic_core import ErrorDetails

from app.adapters import base
from app.adapters.base import (
    ClusterReadPort,
    EnvironmentAdapter,
    EnvironmentConfig,
    InvalidEnvironmentConfigError,
    MissingEnvironmentAdapterError,
    UnknownEnvironmentKindError,
)
from app.core.exceptions import ErrorField

DEFAULT_PACKAGE = "app.adapters"
_ROOT_FIELD = "(root)"
CONFIG_FIELD_PREFIX = "config"

# 응답 details 의 이유 코드. 입력 값·정규식·pydantic 메시지는 넣지 않는다.
REASON_MISSING = "MISSING"
REASON_UNKNOWN_FIELD = "UNKNOWN_FIELD"
REASON_INVALID_FORMAT = "INVALID_FORMAT"

_discovered_packages: set[str] = set()


def discover(package_name: str = DEFAULT_PACKAGE) -> None:
    """패키지 안 모듈을 모두 import 해 종류 등록을 실행한다. 패키지마다 한 번만 한다.

    불러온 뒤 설정 양식과 동작 클래스가 짝을 이루지 않는 종류가 있으면
    MissingEnvironmentAdapterError 를 낸다(이때는 탐색 완료로 기억하지 않는다).
    """
    if package_name in _discovered_packages:
        return
    package = importlib.import_module(package_name)
    for module_info in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package_name}.{module_info.name}")
    _check_every_kind_has_adapter()
    _discovered_packages.add(package_name)


def _check_every_kind_has_adapter() -> None:
    config_kinds = set(base.registered_kinds())
    adapter_kinds = set(base.registered_adapters())
    if config_kinds != adapter_kinds:
        raise MissingEnvironmentAdapterError(
            "environment kinds without a config/adapter pair: "
            + ", ".join(sorted(config_kinds ^ adapter_kinds))
        )


def get_environment_config_class(kind: str) -> type[EnvironmentConfig]:
    discover()
    config_class = base.registered_kinds().get(kind)
    if config_class is None:
        raise UnknownEnvironmentKindError("unknown environment kind", kind=kind)
    return config_class


def get_environment_adapter(kind: str, reader: ClusterReadPort | None = None) -> EnvironmentAdapter:
    """kind 의 동작 객체를 만든다. 서비스는 이 함수를 의존성으로 주입받는다.

    reader 가 없으면(기본) 가짜로 동작하는 어댑터가 가짜 값을 돌려준다(API 서버 12).
    """
    discover()
    adapter_class = base.registered_adapters().get(kind)
    if adapter_class is None:
        raise UnknownEnvironmentKindError("unknown environment kind", kind=kind)
    return adapter_class(reader=reader)


def search_environment_kinds() -> list[str]:
    discover()
    return sorted(base.registered_kinds())


def validate_environment_config(
    kind: str, config: Mapping[str, object], *, by_alias: bool = False
) -> EnvironmentConfig:
    """kind 의 설정 양식으로 config 를 검사해 양식 객체를 돌려준다.

    - by_alias=False(기본): DB 쪽. snake_case 키만 받는다(모델 이벤트, parsed_config()).
    - by_alias=True: API 쪽. camelCase 키만 받는다.

    위반이면 InvalidEnvironmentConfigError 를 낸다. 예외에는 위반한 필드 경로와 이유 코드만 담는다.
    pydantic 오류를 원인(__cause__)으로 잇지 않는다 — 그 안에 입력 값이 들어 있어 로그로 샐 수 있다.
    """
    config_class = get_environment_config_class(kind)
    try:
        return config_class.model_validate(config, by_alias=by_alias, by_name=not by_alias)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False, include_context=False)
        invalid_fields = sorted(
            {".".join(str(part) for part in error["loc"]) or _ROOT_FIELD for error in errors}
        )
        raise InvalidEnvironmentConfigError(
            kind, invalid_fields, _error_details(config_class, errors)
        ) from None


def _error_details(
    config_class: type[EnvironmentConfig], errors: list[ErrorDetails]
) -> list[ErrorField]:
    """pydantic 오류를 `config.<camelCase 경로>` + 이유 코드로 바꾼다. 같은 칸·이유는 한 번만."""
    details: dict[tuple[str, str], ErrorField] = {}
    for error in errors:
        reason = _reason(error["type"])
        loc = [str(part) for part in error["loc"]]
        if loc and reason != REASON_UNKNOWN_FIELD:
            # 양식의 필드 이름(snake_case)을 API 표기(alias)로 바꾼다.
            field_info = config_class.model_fields.get(loc[0])
            if field_info is not None and field_info.alias:
                loc[0] = field_info.alias
        # 모르는 칸은 들어온 키를 그대로 쓴다.
        field = ".".join([CONFIG_FIELD_PREFIX, *loc])
        details.setdefault((field, reason), ErrorField(field=field, reason=reason))
    return list(details.values())


def _reason(error_type: str) -> str:
    if error_type == "missing":
        return REASON_MISSING
    if error_type == "extra_forbidden":
        return REASON_UNKNOWN_FIELD
    return REASON_INVALID_FORMAT

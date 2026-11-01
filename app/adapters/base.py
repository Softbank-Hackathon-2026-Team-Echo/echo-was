"""환경 설정 양식의 기반 클래스와 종류 등록 데코레이터.

환경 종류(kind)마다 이 패키지에 파일을 하나 두고, 그 안에서 `EnvironmentConfig` 를 상속한 양식을
`@register_environment_kind("<kind>")` 로 등록한다. 종류 이름은 데코레이터 인자(클래스 변수)로만
두고 저장되는 config JSON 에는 넣지 않는다.

이 패키지는 app.models 를 import 하지 않는다(의존 방향은 models → adapters 하나).
"""

from abc import abstractmethod
from collections.abc import Callable
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.core.exceptions import InvalidInputError

# 빈 문자열과 앞뒤 공백을 거부한다. 값을 바꾸지 않으므로(strip 안 함) 저장 값이 입력 값과 같다.
NonEmptyStr = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S(?:.*\S)?$")]


class UnknownEnvironmentKindError(InvalidInputError):
    code = "UNKNOWN_ENVIRONMENT_KIND"


class InvalidEnvironmentConfigError(InvalidInputError):
    """설정 양식 위반. fields 의 invalid_fields 에 필드 경로만 담고 입력 값은 담지 않는다."""

    code = "ENVIRONMENT_CONFIG_INVALID"


class EnvironmentConfig(BaseModel):
    """환경 종류별 설정 양식. 모르는 키는 거부하고, 만든 뒤에는 바꿀 수 없다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ClassVar[str]

    @abstractmethod
    def display_location(self) -> str:
        """환경 카드(F3)의 '위치' 칸에 보일 값."""


# kind → 설정 양식 클래스. 조회는 registry 모듈을 거친다.
_registered_kinds: dict[str, type[EnvironmentConfig]] = {}


def register_environment_kind[C: EnvironmentConfig](kind: str) -> Callable[[type[C]], type[C]]:
    """설정 양식 클래스를 환경 종류 `kind` 로 등록한다. 같은 이름의 다른 클래스는 거부한다."""

    def decorator(config_class: type[C]) -> type[C]:
        registered = _registered_kinds.get(kind)
        if registered is not None and registered is not config_class:
            raise ValueError(f"environment kind already registered: {kind}")
        config_class.kind = kind
        _registered_kinds[kind] = config_class
        return config_class

    return decorator


def registered_kinds() -> dict[str, type[EnvironmentConfig]]:
    """지금까지 등록된 종류의 사본. 자동 탐색 없이 현재 상태만 본다."""
    return dict(_registered_kinds)

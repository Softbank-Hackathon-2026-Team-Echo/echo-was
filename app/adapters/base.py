"""환경 종류별 설정 양식·동작 인터페이스의 기반 클래스와 종류 등록 데코레이터.

환경 종류(kind)마다 이 패키지에 파일을 하나 두고, 그 안에서 두 가지를 등록한다.

- 설정 양식: `EnvironmentConfig` 를 상속하고 `@register_environment_kind("<kind>")` 로 등록한다.
- 동작 클래스: `EnvironmentAdapter` 를 상속하고
  `@register_environment_adapter("<kind>")` 로 등록한다.

종류 이름은 데코레이터 인자(클래스 변수)로만 두고 저장되는 config JSON 에는 넣지 않는다.
동작 클래스가 없는 종류는 registry.discover() 가 오류로 알린다.

config 키는 API 에서 camelCase, DB 에서 snake_case 다. 양식 하나가 두 표기를 모두 알고,
호출하는 쪽이 어느 표기만 받을지 고른다(registry.validate_environment_config 의 by_alias).

이 패키지는 app.models 를 import 하지 않는다(의존 방향은 models → adapters 하나).
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from pydantic.alias_generators import to_camel

from app.core.exceptions import ErrorField, ExternalError, InvalidInputError
from app.core.secrets import SecretValue
from app.enums import ConnectionErrorCode

# 빈 문자열과 앞뒤 공백을 거부한다. 값을 바꾸지 않으므로(strip 안 함) 저장 값이 입력 값과 같다.
NonEmptyStr = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S(?:.*\S)?$")]

# 쿠버네티스 /version 의 gitVersion 모양. 예: v1.31.0, v1.31.1-gke.1000, v1.31.4+k3s1
KUBERNETES_VERSION_PATTERN = r"^v\d+\.\d+\.\d+([-+].*)?$"


class UnknownEnvironmentKindError(InvalidInputError):
    code = "UNKNOWN_ENVIRONMENT_KIND"


class InvalidEnvironmentConfigError(InvalidInputError):
    """설정 양식 위반.

    fields 의 invalid_fields 에는 로그용 필드 경로만, details 에는 응답용 칸 경로와
    이유 코드만 담는다. 입력 값은 어디에도 담지 않는다.
    """

    code = "ENVIRONMENT_CONFIG_INVALID"

    def __init__(
        self, kind: str, invalid_fields: list[str], details: list[ErrorField] | None = None
    ) -> None:
        super().__init__(
            "invalid environment config",
            details=details,
            kind=kind,
            invalid_fields=invalid_fields,
        )


class MissingEnvironmentAdapterError(Exception):
    """설정 양식은 있는데 동작 클래스가 없는(또는 그 반대인) 종류. 앱 시작 때 드러낸다."""


class EnvironmentConfig(BaseModel):
    """환경 종류별 설정 양식. 모르는 키는 거부하고, 만든 뒤에는 바꿀 수 없다.

    필드 이름은 snake_case 로 쓰고, camelCase 별칭은 자동으로 붙는다.
    기본 직렬화는 snake_case(DB 저장)다.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        alias_generator=to_camel,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=False,
        loc_by_alias=False,
    )

    kind: ClassVar[str]

    @abstractmethod
    def display_location(self) -> str:
        """환경 카드(F3)의 '위치' 칸에 보일 값."""


class ClusterInfo(BaseModel):
    """클러스터에서 읽은 정보. 저장하지 않는다. simulated=True 면 가짜 어댑터가 만든 값이다."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    server_version: Annotated[str, StringConstraints(pattern=KUBERNETES_VERSION_PATTERN)]
    node_count: Annotated[int, Field(ge=0)]
    simulated: bool


class ConnectionCheckError(ExternalError):
    """어댑터가 접속 확인·클러스터 정보 읽기에 실패했다. reason 은 정해진 이유 코드다.

    메시지에 비밀값·설정 값을 넣지 않는다.
    """

    code = "CONNECTION_CHECK_FAILED"

    def __init__(self, reason: ConnectionErrorCode) -> None:
        super().__init__("connection check failed", reason=reason)
        self.reason = reason


class EnvironmentAdapter(ABC):
    """환경 종류 하나의 동작 클래스. 종류마다 다른 클러스터 조작을 같은 메서드로 부른다.

    - 두 메서드 모두 async 다. 동기 SDK 는 asyncio.to_thread 로 감싼다.
    - 실패는 ConnectionCheckError(reason) 로 낸다. 그 밖의 예외는 호출 쪽이 UNKNOWN 으로 다룬다.
    - 제한 시간은 호출 쪽이 건다. 어댑터는 따로 오래 기다리지 않는다.
    - credentials 는 SecretValue 다. 값을 로그·예외 메시지에 넣지 않는다.
    """

    kind: ClassVar[str]

    @abstractmethod
    async def check_connection(self, config: EnvironmentConfig, credentials: SecretValue) -> None:
        """클러스터에 붙을 수 있는지 확인한다. 실패하면 ConnectionCheckError."""

    @abstractmethod
    async def read_cluster_info(
        self, config: EnvironmentConfig, credentials: SecretValue
    ) -> ClusterInfo:
        """클러스터 버전·노드 수를 읽는다. 실패하면 ConnectionCheckError."""


class SimulatedEnvironmentAdapter(EnvironmentAdapter):
    """네트워크 호출 없이 고정 값을 돌려주는 가짜 어댑터. 결과에 simulated=True 를 단다.

    실제 클러스터가 생기기 전까지 쓴다. 하위 클래스는 고정 값 두 개만 정한다.
    """

    simulated_server_version: ClassVar[str]
    simulated_node_count: ClassVar[int]

    async def check_connection(self, config: EnvironmentConfig, credentials: SecretValue) -> None:
        return None

    async def read_cluster_info(
        self, config: EnvironmentConfig, credentials: SecretValue
    ) -> ClusterInfo:
        return ClusterInfo(
            server_version=self.simulated_server_version,
            node_count=self.simulated_node_count,
            simulated=True,
        )


# kind → 설정 양식 클래스 / 동작 클래스. 조회는 registry 모듈을 거친다.
_registered_kinds: dict[str, type[EnvironmentConfig]] = {}
_registered_adapters: dict[str, type[EnvironmentAdapter]] = {}


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


def register_environment_adapter[A: EnvironmentAdapter](
    kind: str,
) -> Callable[[type[A]], type[A]]:
    """동작 클래스를 환경 종류 `kind` 로 등록한다. 같은 이름의 다른 클래스는 거부한다."""

    def decorator(adapter_class: type[A]) -> type[A]:
        registered = _registered_adapters.get(kind)
        if registered is not None and registered is not adapter_class:
            raise ValueError(f"environment adapter already registered: {kind}")
        adapter_class.kind = kind
        _registered_adapters[kind] = adapter_class
        return adapter_class

    return decorator


def registered_kinds() -> dict[str, type[EnvironmentConfig]]:
    """지금까지 등록된 설정 양식의 사본. 자동 탐색 없이 현재 상태만 본다."""
    return dict(_registered_kinds)


def registered_adapters() -> dict[str, type[EnvironmentAdapter]]:
    """지금까지 등록된 동작 클래스의 사본. 자동 탐색 없이 현재 상태만 본다."""
    return dict(_registered_adapters)

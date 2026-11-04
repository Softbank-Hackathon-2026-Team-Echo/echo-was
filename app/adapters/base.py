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
from typing import Annotated, Any, ClassVar, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
)
from pydantic.alias_generators import to_camel

from app.core.exceptions import ErrorField, ExternalError, InvalidInputError
from app.core.secrets import SecretValue
from app.enums import ConnectionErrorCode

# 빈 문자열과 앞뒤 공백을 거부한다. 값을 바꾸지 않으므로(strip 안 함) 저장 값이 입력 값과 같다.
NonEmptyStr = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S(?:.*\S)?$")]

# 소문자 도메인(레이블 둘 이상). 예: aws-tokyo.echoport.uk
DomainName = Annotated[
    str,
    StringConstraints(
        max_length=125,
        pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)+$",
    ),
]
# 쿠버네티스 이름(RFC 1123 label). IngressClass·ALB 그룹 이름에 쓴다.
KubernetesName = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
]

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

    @model_serializer(mode="wrap")
    def _drop_unset_optional_fields(self, handler: SerializerFunctionWrapHandler) -> Any:
        """값이 없는(None) 선택 칸은 키로 남기지 않는다.

        새 선택 칸을 쓰지 않는 환경의 저장 config·응답이 칸을 더하기 전과 글자까지 같다. 롤링 중
        옛 파드(extra=forbid)가 읽어도 걸리지 않는다. 필수 칸은 None 이 될 수 없어 영향이 없다.
        """
        data = handler(self)
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if value is not None}
        return data

    @abstractmethod
    def display_location(self) -> str:
        """환경 카드(F3)의 '위치' 칸에 보일 값."""

    def environment_host_domain(self) -> str | None:
        """환경별 주소를 둘 하위 도메인. None 이면 대표 주소 도메인 아래 한 단계 형식을 쓴다."""
        return None


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


class ClusterReadPort(Protocol):
    """실제 어댑터가 클러스터에서 읽는 두 가지. 구현은 `app.executors.kubernetes.reader`다.

    `app.adapters` 는 `app.executors` 를 import 하지 않는다(access.py 가 이 패키지의 설정 양식을
    import 하므로 순환이 된다). 그래서 어댑터는 이 Protocol 만 알고, 구현은 밖에서 넣는다.
    두 메서드는 실패를 `ConnectionCheckError(reason)` 로만 낸다. token 은 onprem 의 서비스 계정
    토큰이고 aws·gcp 는 None 이다(워크로드 신원으로 붙는다).
    """

    async def read_version(self, config: EnvironmentConfig, token: SecretValue | None) -> str: ...

    async def count_nodes(self, config: EnvironmentConfig, token: SecretValue | None) -> int: ...


class EnvironmentAdapter(ABC):
    """환경 종류 하나의 동작 클래스. 종류마다 다른 클러스터 조작을 같은 메서드로 부른다.

    - 두 메서드 모두 async 다. 동기 SDK 는 asyncio.to_thread 로 감싼다.
    - 실패는 ConnectionCheckError(reason) 로 낸다. 그 밖의 예외는 호출 쪽이 UNKNOWN 으로 다룬다.
    - 제한 시간은 호출 쪽이 건다. 어댑터는 따로 오래 기다리지 않는다.
    - credentials 는 SecretValue 다. 값을 로그·예외 메시지에 넣지 않는다.
    - reader 는 실제 클러스터 읽기 포트다. 없으면(None) 어댑터가 가짜로 동작하는 종류도 있다.
    """

    kind: ClassVar[str]

    def __init__(self, reader: ClusterReadPort | None = None) -> None:
        self._reader = reader

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


class ClusterBackedEnvironmentAdapter(SimulatedEnvironmentAdapter):
    """실제 클러스터에 붙는 어댑터의 공통 틀(API 서버 12).

    - reader 가 없으면(`ENVIRONMENT_ADAPTER_MODE=simulated`, 기본) 가짜 값을 돌려준다.
    - reader 가 있으면: 접속 확인 = 인증된 `/version` 한 번, 정보 읽기 = `/version` + 노드 수.
      접속 확인이 되면 노드 목록이 막혀도(`AUTH_FAILED`) 접속 상태는 CONNECTED 로 남는다
      (호출 쪽 EnvironmentConnectionService 의 단계 구분).
    - aws·gcp 는 credentials(자리 표시 비밀)를 읽지 않는다(워크로드 신원). onprem 은 서비스가
      이미 조회해 넘긴 credentials 를 서비스 계정 토큰으로 쓴다(`uses_credentials_as_token`).
    """

    uses_credentials_as_token: ClassVar[bool] = False

    def _token(self, credentials: SecretValue) -> SecretValue | None:
        return credentials if self.uses_credentials_as_token else None

    async def check_connection(self, config: EnvironmentConfig, credentials: SecretValue) -> None:
        if self._reader is None:
            return await super().check_connection(config, credentials)
        await self._reader.read_version(config, self._token(credentials))
        return None

    async def read_cluster_info(
        self, config: EnvironmentConfig, credentials: SecretValue
    ) -> ClusterInfo:
        if self._reader is None:
            return await super().read_cluster_info(config, credentials)
        token = self._token(credentials)
        server_version = await self._reader.read_version(config, token)
        node_count = await self._reader.count_nodes(config, token)
        return ClusterInfo(server_version=server_version, node_count=node_count, simulated=False)


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

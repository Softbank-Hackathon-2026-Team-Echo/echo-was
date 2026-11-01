import ast
import logging
import sys
import textwrap
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.adapters import base
from app.adapters.aws import AwsEnvironmentConfig
from app.adapters.base import (
    ClusterInfo,
    ConnectionCheckError,
    EnvironmentAdapter,
    EnvironmentConfig,
    InvalidEnvironmentConfigError,
    MissingEnvironmentAdapterError,
    NonEmptyStr,
    SimulatedEnvironmentAdapter,
    UnknownEnvironmentKindError,
    register_environment_adapter,
    register_environment_kind,
)
from app.adapters.gcp import GcpEnvironmentConfig
from app.adapters.onprem import OnpremEnvironmentConfig
from app.adapters.registry import (
    discover,
    get_environment_adapter,
    get_environment_config_class,
    search_environment_kinds,
    validate_environment_config,
)
from app.core.exceptions import ErrorField, InvalidInputError
from app.core.secrets import SecretValue
from app.enums import ConnectionErrorCode
from tests.adapter_contract import FailureCase, assert_adapter_contract, make_fake_credentials
from tests.conftest import EXTRA_PACKAGE_PREFIX

ADAPTERS_DIR = Path(__file__).resolve().parents[1] / "app" / "adapters"

# 계약 검사용 설정 예시(DB 표기). 새 종류를 더하면 여기에도 예시를 더한다(빠지면 시험이 실패한다).
VALID_CONFIGS: dict[str, dict[str, object]] = {
    "aws": {"region": "ap-northeast-2", "cluster_name": "echo-eks"},
    "gcp": {"project_id": "echo-hack-123", "region": "asia-northeast3", "cluster_name": "echo-gke"},
    "onprem": {"location": "team-laptop", "kube_context": "k3d-echo"},
}
API_CONFIGS: dict[str, dict[str, object]] = {
    "aws": {"region": "ap-northeast-2", "clusterName": "echo-eks"},
    "gcp": {"projectId": "echo-hack-123", "region": "asia-northeast3", "clusterName": "echo-gke"},
    "onprem": {"location": "team-laptop", "kubeContext": "k3d-echo"},
}


def test_search_environment_kinds_discovers_builtin_kinds() -> None:
    assert search_environment_kinds() == ["aws", "gcp", "onprem"]


@pytest.mark.parametrize(
    ("kind", "config_class", "location"),
    [
        ("aws", AwsEnvironmentConfig, "ap-northeast-2"),
        ("gcp", GcpEnvironmentConfig, "asia-northeast3"),
        ("onprem", OnpremEnvironmentConfig, "team-laptop"),
    ],
)
def test_validate_environment_config_valid_returns_kind_config_with_location(
    kind: str, config_class: type[EnvironmentConfig], location: str
) -> None:
    parsed = validate_environment_config(kind, VALID_CONFIGS[kind])

    assert type(parsed) is config_class
    assert parsed.kind == kind
    assert parsed.display_location() == location
    assert parsed.model_dump(mode="json") == VALID_CONFIGS[kind]
    # 종류 이름은 클래스 변수일 뿐 config JSON 에 들어가지 않는다.
    assert "kind" not in parsed.model_dump(mode="json")


@pytest.mark.parametrize(
    ("kind", "config", "invalid_fields"),
    [
        ("aws", {"cluster_name": "echo-eks"}, ["region"]),
        ("aws", {"region": "AP-NORTHEAST-2", "cluster_name": "echo-eks"}, ["region"]),
        ("aws", {"region": "ap-northeast-2", "cluster_name": ""}, ["cluster_name"]),
        ("aws", {"region": "ap-northeast-2", "cluster_name": " echo"}, ["cluster_name"]),
        (
            "aws",
            {"region": "ap-northeast-2", "cluster_name": "echo-eks", "extra_key": "x"},
            ["extra_key"],
        ),
        (
            "gcp",
            {"project_id": "Echo-Hack", "region": "asia-northeast3", "cluster_name": "c"},
            ["project_id"],
        ),
        (
            "gcp",
            {"project_id": "echo-hack-123", "region": "Asia-Northeast3", "cluster_name": "c"},
            ["region"],
        ),
        ("onprem", {"location": "", "kube_context": ""}, ["kube_context", "location"]),
        ("onprem", [], ["(root)"]),
    ],
)
def test_validate_environment_config_invalid_raises_with_field_paths_only(
    kind: str, config: object, invalid_fields: list[str]
) -> None:
    with pytest.raises(InvalidEnvironmentConfigError) as exc_info:
        validate_environment_config(kind, config)  # type: ignore[arg-type]

    error = exc_info.value
    assert error.code == "ENVIRONMENT_CONFIG_INVALID"
    assert error.status_code == 422
    assert isinstance(error, InvalidInputError)
    assert error.fields == {"kind": kind, "invalid_fields": invalid_fields}
    # 입력 값이 원인 예외로 이어져 로그에 새지 않는다.
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_validate_environment_config_error_does_not_carry_input_values() -> None:
    secret_like = "AKIA-not-a-real-key"

    with pytest.raises(InvalidEnvironmentConfigError) as exc_info:
        validate_environment_config("aws", {"region": secret_like, "access_key": secret_like})

    assert secret_like not in repr(exc_info.value.fields)
    assert secret_like not in str(exc_info.value)


def test_validate_environment_config_unknown_kind_raises() -> None:
    with pytest.raises(UnknownEnvironmentKindError) as exc_info:
        validate_environment_config("azure", {"location": "koreacentral"})

    assert exc_info.value.code == "UNKNOWN_ENVIRONMENT_KIND"
    assert exc_info.value.status_code == 422
    assert isinstance(exc_info.value, InvalidInputError)


def test_environment_config_is_frozen() -> None:
    parsed = validate_environment_config("aws", VALID_CONFIGS["aws"])

    with pytest.raises(ValidationError):
        parsed.region = "us-east-1"  # type: ignore[attr-defined]


def test_register_environment_kind_same_name_other_class_raises(
    isolated_environment_kinds: None,
) -> None:
    with pytest.raises(ValueError, match="already registered"):

        @register_environment_kind("aws")
        class OtherAwsConfig(EnvironmentConfig):
            def display_location(self) -> str:
                return ""


def test_discover_new_kind_file_registers_without_touching_registry(
    azure_adapter_package: str,
) -> None:
    discover(azure_adapter_package)

    config_class = get_environment_config_class("azure")
    parsed = validate_environment_config(
        "azure", {"subscription_id": "sub-1", "location": "koreacentral", "cluster_name": "aks"}
    )

    assert "azure" in search_environment_kinds()
    assert config_class.__module__ == f"{azure_adapter_package}.azure"
    assert type(parsed) is config_class
    assert parsed.display_location() == "koreacentral"


def test_azure_kind_is_gone_after_isolated_test() -> None:
    assert "azure" not in search_environment_kinds()


def test_adapters_package_does_not_import_models() -> None:
    for path in ADAPTERS_DIR.glob("*.py"):
        tree = ast.parse(path.read_text())
        imported = [
            node.module if isinstance(node, ast.ImportFrom) else alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for alias in node.names
        ]
        assert not any(name and name.startswith("app.models") for name in imported), path.name


# --- config 키 표기(API camelCase / DB snake_case)와 응답 details ---


@pytest.mark.parametrize("kind", ["aws", "gcp", "onprem"])
def test_validate_environment_config_api_keys_normalize_to_snake_case(kind: str) -> None:
    parsed = validate_environment_config(kind, API_CONFIGS[kind], by_alias=True)

    assert parsed.model_dump(mode="json") == VALID_CONFIGS[kind]
    assert parsed.model_dump(mode="json", by_alias=True) == API_CONFIGS[kind]


def test_validate_environment_config_db_mode_rejects_camel_case_keys() -> None:
    with pytest.raises(InvalidEnvironmentConfigError) as exc_info:
        validate_environment_config("aws", API_CONFIGS["aws"])

    assert exc_info.value.fields["invalid_fields"] == ["clusterName", "cluster_name"]


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (
            {"region": "seoul"},
            [
                ErrorField("config.region", "INVALID_FORMAT"),
                ErrorField("config.clusterName", "MISSING"),
            ],
        ),
        (
            {"region": "ap-northeast-2", "clusterName": "echo-eks", "accessKey": "x"},
            [ErrorField("config.accessKey", "UNKNOWN_FIELD")],
        ),
        (
            {"region": "ap-northeast-2", "cluster_name": "echo-eks"},
            [
                ErrorField("config.clusterName", "MISSING"),
                ErrorField("config.cluster_name", "UNKNOWN_FIELD"),
            ],
        ),
    ],
)
def test_validate_environment_config_api_mode_details_have_camel_paths_and_reasons(
    config: dict[str, object], expected: list[ErrorField]
) -> None:
    with pytest.raises(InvalidEnvironmentConfigError) as exc_info:
        validate_environment_config("aws", config, by_alias=True)

    details = exc_info.value.details or []
    assert sorted(details, key=lambda d: d.field) == sorted(expected, key=lambda d: d.field)
    assert "seoul" not in repr(details)


def test_environment_config_json_schema_uses_camel_case_keys() -> None:
    schema = GcpEnvironmentConfig.model_json_schema(by_alias=True)

    assert set(schema["properties"]) == {"projectId", "region", "clusterName"}
    assert schema["additionalProperties"] is False


# --- 동작 클래스 등록 ---


def test_builtin_kinds_have_simulated_adapters() -> None:
    adapters = base.registered_adapters()

    assert sorted(adapters) == ["aws", "gcp", "onprem"]
    assert all(issubclass(cls, SimulatedEnvironmentAdapter) for cls in adapters.values())
    assert isinstance(get_environment_adapter("aws"), EnvironmentAdapter)


def test_get_environment_adapter_unknown_kind_raises() -> None:
    with pytest.raises(UnknownEnvironmentKindError):
        get_environment_adapter("azure")


def test_register_environment_adapter_same_name_other_class_raises(
    isolated_environment_kinds: None,
) -> None:
    with pytest.raises(ValueError, match="already registered"):

        @register_environment_adapter("aws")
        class OtherAwsAdapter(SimulatedEnvironmentAdapter):
            simulated_server_version = "v1.0.0"
            simulated_node_count = 1


def test_discover_kind_file_without_adapter_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_environment_kinds: None
) -> None:
    package_name = f"{EXTRA_PACKAGE_PREFIX}{uuid.uuid4().hex}"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    (package_dir / "nomad.py").write_text(
        textwrap.dedent(
            """
            from app.adapters.base import EnvironmentConfig, NonEmptyStr, register_environment_kind


            @register_environment_kind("nomad")
            class NomadEnvironmentConfig(EnvironmentConfig):
                region: NonEmptyStr

                def display_location(self) -> str:
                    return self.region
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(MissingEnvironmentAdapterError, match="nomad"):
        discover(package_name)
    # 실패한 탐색은 기억하지 않아 다시 불러도 같은 오류가 난다.
    with pytest.raises(MissingEnvironmentAdapterError):
        discover(package_name)
    assert package_name in sys.modules


# --- 공통 계약 (AC-10) ---


@pytest.mark.parametrize("kind", search_environment_kinds())
async def test_adapter_contract_passes_for_every_registered_kind(kind: str) -> None:
    assert kind in VALID_CONFIGS, f"add a contract config example for kind {kind!r}"

    await assert_adapter_contract(kind, VALID_CONFIGS[kind])


@pytest.mark.parametrize(
    ("kind", "server_version", "node_count"),
    [("aws", "v1.31.0", 3), ("gcp", "v1.31.1-gke.1000", 3), ("onprem", "v1.31.4+k3s1", 1)],
)
async def test_simulated_adapter_returns_fixed_values_marked_simulated(
    kind: str, server_version: str, node_count: int
) -> None:
    adapter = get_environment_adapter(kind)
    config = validate_environment_config(kind, VALID_CONFIGS[kind])

    info = await adapter.read_cluster_info(config, make_fake_credentials())

    assert info == ClusterInfo(server_version=server_version, node_count=node_count, simulated=True)


def test_cluster_info_rejects_non_kubernetes_version_and_negative_nodes() -> None:
    with pytest.raises(ValidationError):
        ClusterInfo(server_version="1.31", node_count=1, simulated=True)
    with pytest.raises(ValidationError):
        ClusterInfo(server_version="v1.31.0", node_count=-1, simulated=True)


class _TestConfig(EnvironmentConfig):
    region: NonEmptyStr

    def display_location(self) -> str:
        return self.region


def _register_test_kind(kind: str, adapter_class: type[EnvironmentAdapter]) -> None:
    register_environment_kind(kind)(type(f"{kind}Config", (_TestConfig,), {}))
    register_environment_adapter(kind)(adapter_class)


class _FailingAdapter(SimulatedEnvironmentAdapter):
    """credentials 가 'deny-' 로 시작하면 AUTH_FAILED 로 실패한다."""

    simulated_server_version = "v1.31.0"
    simulated_node_count = 1

    async def check_connection(self, config: EnvironmentConfig, credentials: SecretValue) -> None:
        if credentials.get_secret_value().startswith("deny-"):
            raise ConnectionCheckError(ConnectionErrorCode.AUTH_FAILED)


async def test_adapter_contract_failure_case_with_known_reason_passes(
    isolated_environment_kinds: None,
) -> None:
    _register_test_kind("failing", _FailingAdapter)

    await assert_adapter_contract(
        "failing",
        {"region": "r1"},
        failure_cases=[FailureCase({"region": "r1"}, SecretValue(f"deny-{uuid.uuid4().hex}"))],
    )


class _UnknownReasonAdapter(_FailingAdapter):
    async def check_connection(self, config: EnvironmentConfig, credentials: SecretValue) -> None:
        if credentials.get_secret_value().startswith("deny-"):
            raise ConnectionCheckError("NOT_A_CODE")  # type: ignore[arg-type]


class _SyncAdapter(EnvironmentAdapter):
    def check_connection(self, config: EnvironmentConfig, credentials: SecretValue) -> None:  # type: ignore[override]
        return None

    async def read_cluster_info(
        self, config: EnvironmentConfig, credentials: SecretValue
    ) -> ClusterInfo:
        return ClusterInfo(server_version="v1.31.0", node_count=1, simulated=True)


class _WrongShapeAdapter(SimulatedEnvironmentAdapter):
    simulated_server_version = "v1.31.0"
    simulated_node_count = 1

    async def read_cluster_info(  # type: ignore[override]
        self, config: EnvironmentConfig, credentials: SecretValue
    ) -> dict[str, object]:
        return {"serverVersion": "v1.31.0", "nodeCount": 1}


class _LoggingSecretAdapter(SimulatedEnvironmentAdapter):
    simulated_server_version = "v1.31.0"
    simulated_node_count = 1

    async def check_connection(self, config: EnvironmentConfig, credentials: SecretValue) -> None:
        logging.getLogger("bad.adapter").debug("token %s", credentials.get_secret_value())


@pytest.mark.parametrize(
    ("adapter_class", "match"),
    [
        (_UnknownReasonAdapter, "reason outside"),
        (_SyncAdapter, "must be async"),
        (_WrongShapeAdapter, "dict"),
        (_LoggingSecretAdapter, "secret leaked"),
    ],
)
async def test_adapter_contract_violations_fail(
    isolated_environment_kinds: None, adapter_class: type[EnvironmentAdapter], match: str
) -> None:
    kind = f"bad{uuid.uuid4().hex[:8]}"
    _register_test_kind(kind, adapter_class)

    with pytest.raises(AssertionError, match=match):
        await assert_adapter_contract(
            kind,
            {"region": "r1"},
            failure_cases=[FailureCase({"region": "r1"}, SecretValue(f"deny-{uuid.uuid4().hex}"))]
            if adapter_class is _UnknownReasonAdapter
            else [],
        )


async def test_new_kind_file_adapter_passes_contract(azure_adapter_package: str) -> None:
    discover(azure_adapter_package)

    await assert_adapter_contract(
        "azure", {"subscription_id": "sub-1", "location": "koreacentral", "cluster_name": "aks"}
    )

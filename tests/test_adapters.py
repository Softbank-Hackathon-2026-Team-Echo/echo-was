import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.adapters.aws import AwsEnvironmentConfig
from app.adapters.base import (
    EnvironmentConfig,
    InvalidEnvironmentConfigError,
    UnknownEnvironmentKindError,
    register_environment_kind,
)
from app.adapters.gcp import GcpEnvironmentConfig
from app.adapters.onprem import OnpremEnvironmentConfig
from app.adapters.registry import (
    discover,
    get_environment_config_class,
    search_environment_kinds,
    validate_environment_config,
)
from app.core.exceptions import InvalidInputError

ADAPTERS_DIR = Path(__file__).resolve().parents[1] / "app" / "adapters"

VALID_CONFIGS: dict[str, dict[str, object]] = {
    "aws": {"region": "ap-northeast-2", "cluster_name": "echo-eks"},
    "gcp": {"project_id": "echo-hack-123", "region": "asia-northeast3", "cluster_name": "echo-gke"},
    "onprem": {"location": "team-laptop", "kube_context": "k3d-echo"},
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

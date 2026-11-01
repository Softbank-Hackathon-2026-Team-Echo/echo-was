import json
import pickle

import pytest

from app.clients.secret_client import EnvVarSecretClient, SecretLookupError
from app.core.secrets import (
    InvalidSecretRefError,
    SecretRef,
    SecretStore,
    SecretValue,
    parse_secret_ref,
)
from app.enums import ConnectionErrorCode


@pytest.mark.parametrize(
    ("value", "store", "location"),
    [
        (
            "env:ECHO_SECRET_AWS_SEOUL_KUBECONFIG",
            SecretStore.ENV,
            "ECHO_SECRET_AWS_SEOUL_KUBECONFIG",
        ),
        (
            "k8s:echo-system/aws-seoul/kubeconfig",
            SecretStore.K8S,
            "echo-system/aws-seoul/kubeconfig",
        ),
        ("k8s:ns/sec/.dockerconfig_json-1", SecretStore.K8S, "ns/sec/.dockerconfig_json-1"),
        ("aws-sm:echo/aws-seoul", SecretStore.AWS_SM, "echo/aws-seoul"),
        (
            "aws-sm:arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:echo-AbCdEf",
            SecretStore.AWS_SM,
            "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:echo-AbCdEf",
        ),
        ("gcp-sm:projects/echo-hack-123/secrets/gke", SecretStore.GCP_SM, None),
        ("gcp-sm:projects/echo-hack-123/secrets/gke/versions/latest", SecretStore.GCP_SM, None),
    ],
)
def test_parse_secret_ref_valid_formats_split_on_first_colon(
    value: str, store: SecretStore, location: str | None
) -> None:
    ref = parse_secret_ref(value)

    assert ref.store is store
    assert ref.location == (location or value.partition(":")[2])


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ECHO_SECRET_X",
        "env:",
        "env:DATABASE_URL",
        "env:echo_secret_lower",
        "env:ECHO_SECRET_",
        "env:ECHO_SECRET_X\n",
        "vault:echo/x",
        "k8s:Echo/sec/key",
        "k8s:ns/sec",
        "k8s:ns/sec/key/extra",
        "k8s:ns/sec/key with space",
        "aws-sm:",
        "aws-sm:has space",
        "gcp-sm:projects/p/secrets",
        "gcp-sm:projects/p/secrets/s/versions/",
        "gcp-sm:secrets/s",
        "env:ECHO_SECRET_" + "A" * 300,
    ],
)
def test_parse_secret_ref_invalid_raises_without_value_or_pattern(value: str) -> None:
    with pytest.raises(InvalidSecretRefError) as exc_info:
        parse_secret_ref(value)

    assert str(exc_info.value) == "INVALID_SECRET_REF"
    assert exc_info.value.__cause__ is None


def test_secret_value_hides_value_in_repr_str_json_and_pickle_text() -> None:
    secret = "s3cr3t-value-for-test"
    value = SecretValue(secret)

    assert secret not in repr(value)
    assert secret not in str(value)
    assert secret not in f"{value}"
    assert secret not in json.dumps({"credentials": value}, default=str)
    assert value.get_secret_value() == secret
    assert pickle.loads(pickle.dumps(value)).get_secret_value() == secret


async def test_env_var_secret_client_reads_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECHO_SECRET_UNIT_TEST", "kubeconfig-body")

    value = await EnvVarSecretClient().get_secret(
        SecretRef(SecretStore.ENV, "ECHO_SECRET_UNIT_TEST")
    )

    assert value.get_secret_value() == "kubeconfig-body"


@pytest.mark.parametrize("env_value", [None, ""])
async def test_env_var_secret_client_missing_or_empty_raises_not_found(
    monkeypatch: pytest.MonkeyPatch, env_value: str | None
) -> None:
    if env_value is None:
        monkeypatch.delenv("ECHO_SECRET_UNIT_TEST", raising=False)
    else:
        monkeypatch.setenv("ECHO_SECRET_UNIT_TEST", env_value)

    with pytest.raises(SecretLookupError) as exc_info:
        await EnvVarSecretClient().get_secret(SecretRef(SecretStore.ENV, "ECHO_SECRET_UNIT_TEST"))

    assert exc_info.value.reason is ConnectionErrorCode.SECRET_NOT_FOUND


@pytest.mark.parametrize("store", [SecretStore.K8S, SecretStore.AWS_SM, SecretStore.GCP_SM])
async def test_env_var_secret_client_other_stores_unsupported(store: SecretStore) -> None:
    with pytest.raises(SecretLookupError) as exc_info:
        await EnvVarSecretClient().get_secret(SecretRef(store, "anything"))

    assert exc_info.value.reason is ConnectionErrorCode.SECRET_STORE_UNSUPPORTED

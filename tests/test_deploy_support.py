"""고객 앱 주소 규칙(SPEC api-3-1 §2): suffix 생성, 레이블 자르기, 대표·환경별 주소."""

import re

import pytest

from app.services.deploy_support import (
    PUBLIC_HOST_LABEL_MAX_LENGTH,
    connection_public_host,
    generate_host_suffix,
    public_host_label,
    service_public_host,
)

SUFFIX = "a1b2c3d4"


def test_generate_host_suffix_returns_8_lowercase_alphanumerics() -> None:
    suffixes = {generate_host_suffix() for _ in range(200)}

    assert all(re.fullmatch(r"[a-z0-9]{8}", suffix) for suffix in suffixes)
    # 무작위 값이라 200번 뽑아 겹치는 일은 사실상 없다.
    assert len(suffixes) == 200


def test_service_public_host_is_one_label_under_domain() -> None:
    assert service_public_host("shop", SUFFIX, "echoport.uk") == "shop-a1b2c3d4.echoport.uk"


def test_connection_public_host_joins_service_environment_and_suffix() -> None:
    host = connection_public_host("shop", "aws-seoul", SUFFIX, "echoport.uk")

    assert host == "shop-aws-seoul-a1b2c3d4.echoport.uk"


def test_public_host_label_at_63_chars_is_not_truncated() -> None:
    prefix = "a" * 54

    assert public_host_label(prefix, SUFFIX) == f"{prefix}-{SUFFIX}"


def test_public_host_label_63_char_name_and_environment_truncates_prefix_to_54() -> None:
    service_name = "s" * 62 + "1"
    environment_name = "e" * 62 + "2"

    main = public_host_label(service_name, SUFFIX)
    environment = public_host_label(f"{service_name}-{environment_name}", SUFFIX)

    for label in (main, environment):
        assert len(label) <= PUBLIC_HOST_LABEL_MAX_LENGTH
        assert label == f"{service_name[:54]}-{SUFFIX}"


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        # 54자로 자른 끝이 `-` 면 떼어 `--` 가 생기지 않게 한다.
        ("a" * 53 + "-" + "b" * 10, "a" * 53 + f"-{SUFFIX}"),
        ("shop-" + "x" * 48 + "-aws-seoul", "shop-" + "x" * 48 + f"-{SUFFIX}"),
        ("a" * 52 + "--b" + "c" * 10, "a" * 52 + f"-{SUFFIX}"),
    ],
)
def test_public_host_label_truncation_drops_trailing_hyphens(prefix: str, expected: str) -> None:
    label = public_host_label(prefix, SUFFIX)

    assert label == expected
    assert len(label) <= PUBLIC_HOST_LABEL_MAX_LENGTH
    assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", label)

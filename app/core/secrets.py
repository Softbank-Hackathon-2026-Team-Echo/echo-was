"""비밀값 타입과 비밀 참조(secret_ref) 형식.

DB 에는 비밀값을 두지 않고 비밀 저장소의 보관 위치(secret_ref)만 둔다. 형식은 `<저장소>:<위치>` 이고
저장소 이름은 첫 콜론에서만 나눈다.

- `env:<NAME>` — 서버 프로세스 환경변수. NAME 은 `ECHO_SECRET_` 로 시작해야 한다. API 로 서버 자체
  설정(DATABASE_URL 등)을 열쇠로 지정하지 못하게 하기 위해서다. 환경 API 에서는 한 겹 더 좁혀
  `ECHO_SECRET_<조직 키>__` 로 시작해야 한다(organization_env_secret_prefix). 다른 조직의 열쇠를
  가리키지 못하게 하기 위해서다.
- `k8s:<namespace>/<secret>/<key>` — Kubernetes Secret 의 키 하나.
- `aws-sm:<이름 또는 ARN>` — AWS Secrets Manager.
- `gcp-sm:projects/<project>/secrets/<secret>[/versions/<version>]` — GCP Secret Manager.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from pydantic import SecretStr

SECRET_REF_MAX_LENGTH = 255

# Kubernetes 이름 규칙(RFC 1123 label). app.models.base 의 이름 규칙과 같은 값이다.
_RFC1123_LABEL = r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
_ENV_NAME = re.compile(r"^ECHO_SECRET_[A-Z0-9_]+$")
_K8S_LOCATION = re.compile(rf"^{_RFC1123_LABEL}/{_RFC1123_LABEL}/[-._a-zA-Z0-9]+$")
_AWS_SM_LOCATION = re.compile(r"^\S+$")
_GCP_SM_LOCATION = re.compile(r"^projects/[^/\s]+/secrets/[^/\s]+(?:/versions/[^/\s]+)?$")


class SecretValue(SecretStr):
    """메모리에서만 쓰는 비밀값. repr·str·직렬화는 `**********` 로 가리고,
    값은 get_secret_value() 로만 꺼낸다. 저장·로그·응답·예외 메시지에 넣지 않는다.
    """


class SecretStore(StrEnum):
    ENV = "env"
    K8S = "k8s"
    AWS_SM = "aws-sm"
    GCP_SM = "gcp-sm"


_LOCATION_PATTERNS: dict[SecretStore, re.Pattern[str]] = {
    SecretStore.ENV: _ENV_NAME,
    SecretStore.K8S: _K8S_LOCATION,
    SecretStore.AWS_SM: _AWS_SM_LOCATION,
    SecretStore.GCP_SM: _GCP_SM_LOCATION,
}


class InvalidSecretRefError(ValueError):
    """secret_ref 형식 위반. 메시지에 입력 값·정규식을 넣지 않는다."""

    def __init__(self) -> None:
        super().__init__("INVALID_SECRET_REF")


@dataclass(frozen=True)
class SecretRef:
    store: SecretStore
    location: str


def parse_secret_ref(value: str) -> SecretRef:
    """secret_ref 문자열을 저장소와 위치로 나눈다. 형식이 틀리면 InvalidSecretRefError."""
    if len(value) > SECRET_REF_MAX_LENGTH:
        raise InvalidSecretRefError
    store_name, separator, location = value.partition(":")
    if not separator:
        raise InvalidSecretRefError
    try:
        store = SecretStore(store_name)
    except ValueError:
        raise InvalidSecretRefError from None
    if not _LOCATION_PATTERNS[store].fullmatch(location):
        raise InvalidSecretRefError
    return SecretRef(store=store, location=location)


ORGANIZATION_KEY_SEPARATOR = "__"


def organization_secret_key(slug: str) -> str:
    """조직 열쇠 키: slug 의 `-` 를 `_` 로 바꾸고 대문자(`team-echo` → `TEAM_ECHO`).

    slug 에는 하이픈 연속이 없어 키에 `__` 가 생기지 않는다. 그래서 `a-corp` 와 `a-corp-x` 가
    서로의 열쇠 이름을 가리킬 수 없다.
    """
    return slug.replace("-", "_").upper()


def organization_env_secret_prefix(slug: str) -> str:
    """이 조직의 env 비밀 참조가 시작해야 하는 이름(`ECHO_SECRET_TEAM_ECHO__`)."""
    return f"ECHO_SECRET_{organization_secret_key(slug)}{ORGANIZATION_KEY_SEPARATOR}"


def is_secret_ref_in_organization(ref: SecretRef, slug: str) -> bool:
    """env 참조는 조직 열쇠 이름으로 시작해야 한다. 형식만 검사하는 다른 저장소는 묶지 않는다."""
    if ref.store is not SecretStore.ENV:
        return True
    prefix = organization_env_secret_prefix(slug)
    return ref.location.startswith(prefix) and len(ref.location) > len(prefix)

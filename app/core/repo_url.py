"""소스 저장소 주소 형식. 지금은 github.com 저장소만 받는다.

받는 모양: `https://github.com/<owner>/<repo>` (끝의 `/`·`.git` 허용). 저장은 끝 `/`·`.git` 을 뗀
정규형으로 한다. owner·repo 는 영문·숫자·`-`·`_`·`.` 100자 이하다. 다른 호스트·`http`·자격 증명이
들어간 주소는 받지 않는다.
"""

import re
from dataclasses import dataclass

REPO_URL_MAX_LENGTH = 300
GITHUB_HOST = "github.com"
_REPO_URL = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]{1,100})/(?P<name>[A-Za-z0-9_.-]{1,100})/?$"
)
_GIT_SUFFIX = ".git"


class InvalidRepoUrlError(ValueError):
    """저장소 주소 형식 위반. 메시지에 입력 값을 넣지 않는다."""

    def __init__(self) -> None:
        super().__init__("INVALID_REPO_URL")


@dataclass(frozen=True)
class RepoLocator:
    owner: str
    name: str

    @property
    def url(self) -> str:
        return f"https://{GITHUB_HOST}/{self.owner}/{self.name}"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


def parse_repo_url(value: str) -> RepoLocator:
    """주소를 owner·name 으로 나눈다. 형식이 틀리면 InvalidRepoUrlError."""
    if len(value) > REPO_URL_MAX_LENGTH:
        raise InvalidRepoUrlError
    match = _REPO_URL.fullmatch(value)
    if match is None:
        raise InvalidRepoUrlError
    owner, name = match["owner"], match["name"]
    if name.endswith(_GIT_SUFFIX):
        name = name.removesuffix(_GIT_SUFFIX)
    if owner in {".", ".."} or name in {"", ".", ".."}:
        raise InvalidRepoUrlError
    return RepoLocator(owner=owner, name=name)


def normalize_repo_url(value: str) -> str:
    return parse_repo_url(value).url

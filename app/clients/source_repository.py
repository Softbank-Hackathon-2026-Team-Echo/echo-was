"""소스 저장소 클라이언트: 저장소 접근 확인, 브랜치·커밋을 40자 SHA 로 고정, 빌드용 읽기 토큰 발급.

구현은 두 가지다.
- FakeSourceRepositoryClient(`source_client_mode = fake`, 기본): 네트워크 호출 없이 owner·ref
  이름 규칙으로 결과를 정한다. 결과는 simulated 다.
- GithubAppSourceRepositoryClient(`github`, app/clients/github_source_repository.py): GitHub App
  설치 토큰으로 비공개 저장소를 읽고, 설치가 없으면 인증 없이 공개 저장소를 읽는다.

오류는 세 가지로만 바꾼다(AppError).
- RepoNotAccessibleError(404): 저장소가 없거나 비공개인데 App 설치가 없다(GitHub 가 둘을 같은
  404 로 준다). App 설치 안내 주소를 함께 준다.
- RefNotFoundError(422): 브랜치·커밋이 없다.
- SourceProviderUnavailableError(502): 한도 초과·5xx·시간 초과.

읽기 토큰은 SecretValue 로만 다루고 저장·로그·응답에 넣지 않는다.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

from app.core.exceptions import (
    RefNotFoundError,
    RepoNotAccessibleError,
    SourceProviderUnavailableError,
)
from app.core.repo_url import RepoLocator
from app.core.secrets import SecretValue

FULL_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class RepoAccess:
    """저장소 접근 결과. private 는 GitHub 응답 값이다."""

    private: bool
    default_branch: str


class SourceRepositoryClient(Protocol):
    @property
    def simulated(self) -> bool:
        """가짜 구현이면 True. 이 클라이언트로 등록한 서비스에 저장한다."""
        ...

    async def check_access(self, repo: RepoLocator) -> RepoAccess:
        """접근할 수 있으면 RepoAccess.

        아니면 RepoNotAccessibleError 또는 SourceProviderUnavailableError.
        """
        ...

    async def resolve_ref(self, repo: RepoLocator, ref: str) -> str:
        """브랜치 이름이나 커밋 SHA(7~40자)를 40자 커밋 SHA 로 고정한다."""
        ...

    async def issue_read_token(self, repo: RepoLocator) -> SecretValue | None:
        """빌드 직전에 부른다. 설치가 있으면 1시간짜리 읽기 토큰, 공개 저장소면 None."""
        ...


class FakeSourceRepositoryClient:
    """네트워크 호출 없는 가짜 구현.

    - owner 가 `private-` 로 시작: 비공개 + App 설치 있음(읽기 토큰 발급).
    - `denied-`·`missing-` 로 시작: REPO_NOT_ACCESSIBLE.
    - `flaky-` 로 시작: SOURCE_PROVIDER_UNAVAILABLE.
    - ref 가 `missing-` 로 시작: REF_NOT_FOUND. 40자 SHA 는 그대로, 그 밖에는 sha1(저장소 + ref).
    """

    def __init__(self, install_url: str | None) -> None:
        self._install_url = install_url

    @property
    def simulated(self) -> bool:
        return True

    async def check_access(self, repo: RepoLocator) -> RepoAccess:
        self._raise_for_owner(repo)
        return RepoAccess(private=_is_private_owner(repo), default_branch="main")

    async def resolve_ref(self, repo: RepoLocator, ref: str) -> str:
        self._raise_for_owner(repo)
        if ref.startswith("missing-"):
            raise RefNotFoundError
        if FULL_COMMIT_SHA.fullmatch(ref):
            return ref
        return hashlib.sha1(f"{repo.url}@{ref}".encode(), usedforsecurity=False).hexdigest()

    async def issue_read_token(self, repo: RepoLocator) -> SecretValue | None:
        self._raise_for_owner(repo)
        if not _is_private_owner(repo):
            return None
        return SecretValue(f"fake-installation-token-for-{repo.full_name}")

    def _raise_for_owner(self, repo: RepoLocator) -> None:
        owner = repo.owner.lower()
        if owner.startswith(("denied-", "missing-")):
            raise RepoNotAccessibleError(self._install_url)
        if owner.startswith("flaky-"):
            raise SourceProviderUnavailableError


def _is_private_owner(repo: RepoLocator) -> bool:
    return repo.owner.lower().startswith("private-")

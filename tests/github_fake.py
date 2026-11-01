"""GitHub API 가짜 전송(httpx.MockTransport). 실제 GitHub 에 닿지 않는다.

App 개인키는 시험이 시작될 때 메모리에서 만든다(저장소에 키 파일·문자열을 두지 않는다).
설치 토큰·커밋 SHA 는 시험 코드 안 상수다.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

APP_ID = "123456"
INSTALLATION_ID = 4242
INSTALLATION_TOKEN = "ghs_test_installation_token_do_not_log_7f3a9c"
COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"
API_BASE = "https://api.github.test"


def _generate_private_key_pem() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


TEST_APP_PRIVATE_KEY_PEM, TEST_APP_PUBLIC_KEY_PEM = _generate_private_key_pem()


@dataclass
class FakeGithub:
    """저장소 상태를 정해 두고 GitHub REST 응답을 흉내 낸다.

    - installed: App 이 설치된 저장소 full_name → 비공개 여부.
    - public: 설치 없이 읽을 수 있는 공개 저장소.
    - refs: 있는 ref(브랜치·SHA). 그 밖의 ref 는 404.
    - override: 경로 → 고정 응답(한도 초과·5xx 시험용).
    """

    installed: dict[str, bool] = field(default_factory=dict)
    public: set[str] = field(default_factory=set)
    refs: set[str] = field(default_factory=lambda: {"main", COMMIT_SHA})
    override: Callable[[httpx.Request], httpx.Response | None] | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override is not None:
            response = self.override(request)
            if response is not None:
                return response
        path = request.url.path
        auth = request.headers.get("authorization", "")
        parts = path.strip("/").split("/")
        if parts[0] == "app" and parts[1] == "installations":
            if not auth.startswith("Bearer ") or auth == f"Bearer {INSTALLATION_TOKEN}":
                return httpx.Response(401)
            return httpx.Response(
                201, json={"token": INSTALLATION_TOKEN, "expires_at": "2026-11-02T04:00:00Z"}
            )
        if parts[0] != "repos" or len(parts) < 3:
            return httpx.Response(404)
        full_name = f"{parts[1]}/{parts[2]}"
        rest = parts[3:]
        if rest == ["installation"]:
            if full_name in self.installed:
                return httpx.Response(200, json={"id": INSTALLATION_ID})
            return httpx.Response(404)
        has_token = auth == f"Bearer {INSTALLATION_TOKEN}"
        visible = (full_name in self.installed and has_token) or full_name in self.public
        if not visible:
            return httpx.Response(404, json={"message": "Not Found"})
        if not rest:
            private = self.installed.get(full_name, False)
            return httpx.Response(200, json={"private": private, "default_branch": "main"})
        if rest[0] == "commits":
            ref = "/".join(rest[1:])
            if ref in self.refs:
                return httpx.Response(200, json={"sha": COMMIT_SHA})
            return httpx.Response(404 if ref.isalnum() else 422)
        return httpx.Response(404)

    def bodies(self) -> list[str]:
        return [
            json.dumps(dict(request.headers)) + request.content.decode()
            for request in self.requests
        ]

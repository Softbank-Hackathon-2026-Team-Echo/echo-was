"""GitHub App 개인키·설치 토큰 노출 0회 시험(SPEC api-3 AC-13, FR-16).

실제 GitHub App 클라이언트를 가짜 전송(MockTransport)에 붙여 비공개 저장소를 등록·배포하고,
로그(caplog)·모든 응답 본문·DB 의 모든 표 텍스트에서 비밀 문자열을 찾는다.
"""

import logging

import pytest
from sqlalchemy import text

from app.clients.github_source_repository import GithubAppSourceRepositoryClient
from app.clients.secret_client import EnvVarSecretClient
from app.core.logging import JsonFormatter
from app.core.secrets import parse_secret_ref
from tests.github_fake import (
    API_BASE,
    APP_ID,
    INSTALLATION_TOKEN,
    TEST_APP_PRIVATE_KEY_PEM,
    FakeGithub,
)
from tests.integration.conftest import TABLES
from tests.integration.deploy_harness import DeployHarness

KEY_ENV = "ECHO_SECRET_GITHUB_APP_PRIVATE_KEY"


def _private_key_fragments() -> list[str]:
    """PEM 전체와 본문(base64) 줄들. 일부만 새어도 잡도록 줄 단위로도 찾는다."""
    body = [line for line in TEST_APP_PRIVATE_KEY_PEM.splitlines() if not line.startswith("-----")]
    return [TEST_APP_PRIVATE_KEY_PEM, "".join(body), *body]


async def test_private_repo_deploy_never_exposes_app_key_or_installation_token(
    harness: DeployHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for noisy in ("httpx", "httpcore"):
        caplog.set_level(logging.DEBUG, logger=noisy)
    monkeypatch.setenv(KEY_ENV, TEST_APP_PRIVATE_KEY_PEM)
    github = FakeGithub(installed={"team-echo/billing": True})
    harness.source_client = GithubAppSourceRepositoryClient(
        api_base_url=API_BASE,
        app_id=APP_ID,
        private_key_ref=parse_secret_ref(f"env:{KEY_ENV}"),
        secret_client=EnvVarSecretClient(),
        install_url=harness.settings.github_app_install_url,
        timeout_seconds=10,
        clock=harness.clock,
        transport=github.transport(),
    )
    aws = await harness.add_environment("aws-seoul")

    service = await harness.create_service("billing", "https://github.com/team-echo/billing")
    accepted = await harness.deploy(service["id"], aws.id, branch="main")
    await harness.run_until_idle()
    finished = await harness.get_request(accepted["id"])
    await harness.get_service(service["id"])
    await harness.list_requests(service["id"])
    async with harness.session_factory() as session:
        dumps = [
            await session.scalar(
                text(f"SELECT coalesce(string_agg(t::text, '\n'), '') FROM {table} t")
            )
            for table in TABLES
        ]

    app_jwts = {
        request.headers["authorization"].removeprefix("Bearer ")
        for request in github.requests
        if request.url.path.endswith("/installation")
    }
    secrets = [*_private_key_fragments(), INSTALLATION_TOKEN, *app_jwts]
    formatter = JsonFormatter()
    logs = [formatter.format(record) for record in caplog.records] + [caplog.text]
    haystacks = {"logs": "\n".join(logs), "responses": "\n".join(harness.responses)}
    haystacks["db"] = "\n".join(dump or "" for dump in dumps)

    assert service["private"] is True and service["simulated"] is False
    assert finished["status"] == "SUCCEEDED"
    assert harness.executors.build.received_tokens == 1
    assert app_jwts
    # 대조군: 비밀이 아닌 요청 id 는 세 곳 모두에서 찾혀야 검색 대상이 제대로 모인 것이다.
    assert all(accepted["id"] in haystack for haystack in haystacks.values())
    leaks = [
        (where, index)
        for where, haystack in haystacks.items()
        for index, secret in enumerate(secrets)
        if secret and secret in haystack
    ]
    assert leaks == []

"""실행기 공통 계약 시험. 실제 실행기(인프라 3·5 이후)도 같은 검사를 통과해야 한다.

- 같은 멱등 키로 다시 부르면 같은 작업(external_ref)을 돌려준다.
- 끝나기 전 poll 은 진행 중, 끝나면 성공 결과를 준다.
- external_ref 에 비밀값(읽기 토큰)이 들어가지 않는다.
- 빌드 결과는 sha256 digest 다(배포는 digest 로만 한다).
"""

import re
import uuid
from collections.abc import Awaitable, Callable

from app.core.secrets import SecretValue
from app.executors.base import (
    BuildExecutor,
    BuildRunning,
    BuildSpec,
    BuildSucceeded,
    DeployApplied,
    DeployExecutor,
    DeploymentSpec,
    DeployPending,
    DeployTarget,
    Reverted,
    SourceAccess,
    TeardownExecutor,
    TeardownPending,
    TeardownRemoved,
)

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
READ_TOKEN = "ghs_contract_read_token"
COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"

TARGET = DeployTarget(
    service_id=uuid.uuid4(),
    service_name="shop-api",
    service_environment_id=uuid.uuid4(),
    environment_id=uuid.uuid4(),
    environment_kind="aws",
    public_host="shop-api.aws-seoul.apps.echo.test",
    container_port=8080,
)
DEPLOYMENT = DeploymentSpec(
    deployment_id=uuid.uuid4(),
    image_repository="registry.echo.test/shop-api",
    image_digest="sha256:" + "a" * 64,
    config_snapshot={"containerPort": 8080},
)

Advance = Callable[[], None]


async def assert_build_executor_contract(executor: BuildExecutor, finish: Advance) -> None:
    build = BuildSpec(
        build_id=uuid.uuid4(),
        service_id=TARGET.service_id,
        service_name="shop-api",
        repo_url="https://github.com/team-echo/shop-api",
        commit_sha=COMMIT_SHA,
    )
    source = SourceAccess(build.repo_url, COMMIT_SHA, SecretValue(READ_TOKEN))
    key = str(build.build_id)

    first = await executor.start(build, source, key)
    again = await executor.start(build, source, key)
    before = await executor.poll(first)
    finish()
    after = await executor.poll(first)

    assert first == again
    assert READ_TOKEN not in first
    assert isinstance(before, BuildRunning)
    assert isinstance(after, BuildSucceeded)
    assert DIGEST.fullmatch(after.image_digest)


async def assert_deploy_executor_contract(executor: DeployExecutor, finish: Advance) -> None:
    key = str(DEPLOYMENT.deployment_id)

    first = await executor.apply(DEPLOYMENT, TARGET, key)
    again = await executor.apply(DEPLOYMENT, TARGET, key)
    before = await executor.poll(first)
    finish()
    after = await executor.poll(first)
    reverted = await executor.revert(TARGET, None, key)

    assert first == again
    assert isinstance(before, DeployPending)
    assert isinstance(after, DeployApplied)
    assert isinstance(reverted, Reverted)


async def assert_teardown_executor_contract(executor: TeardownExecutor, finish: Advance) -> None:
    key = f"{TARGET.service_environment_id}:0"

    first = await executor.remove(TARGET, key)
    again = await executor.remove(TARGET, key)
    before = await executor.poll(first)
    finish()
    after = await executor.poll(first)
    other_attempt = await executor.remove(TARGET, f"{TARGET.service_environment_id}:1")

    assert first == again
    assert other_attempt != first
    assert isinstance(before, TeardownPending)
    assert isinstance(after, TeardownRemoved)


ContractCheck = Callable[..., Awaitable[None]]

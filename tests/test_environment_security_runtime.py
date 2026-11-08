"""전용 신원 선택·실행기 fallback 거절·실제 metadata 신원 확인."""

from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.adapters.aws import AwsEnvironmentConfig
from app.adapters.gcp import GcpEnvironmentConfig
from app.adapters.onprem import OnpremEnvironmentConfig
from app.clients.environment_security import LiveEnvironmentSecurityReader
from app.core.clock import SystemClock
from app.core.config import Settings
from app.executors.base import ClusterEnvironment
from app.executors.google_metadata import GoogleMetadataClient, MetadataAuthError
from app.security_environment.access import reader_environment
from app.security_environment.types import ReaderAccess
from tests.test_environment_security import profile

EXECUTOR = "arn:aws:iam::123456789012:role/executor"
READER = "arn:aws:iam::123456789012:role/security-reader"
GSA = "security-reader@example.iam.gserviceaccount.com"


def environment(kind="aws"):
    configs = {
        "aws": AwsEnvironmentConfig(
            region="ap-northeast-1", cluster_name="test", role_arn=EXECUTOR
        ),
        "gcp": GcpEnvironmentConfig(
            project_id="example", region="asia-northeast3", cluster_name="test"
        ),
        "onprem": OnpremEnvironmentConfig(
            location="seoul", kube_context="test", api_url="https://kube.example.org"
        ),
    }
    return ClusterEnvironment(uuid4(), kind, "test", configs[kind], "env:ECHO_SECRET_EXECUTOR")


def configured(**kwargs):
    return profile().model_copy(update={"reader": ReaderAccess(**kwargs)})


@pytest.mark.parametrize("kind", ["aws", "gcp", "onprem"])
def test_missing_identity_never_falls_back(kind):
    with pytest.raises(ValueError):
        reader_environment(profile(), environment(kind), EXECUTOR)


@pytest.mark.parametrize("role", [None, EXECUTOR])
def test_missing_or_shared_aws_role_rejected(role):
    with pytest.raises(ValueError):
        reader_environment(
            configured(google_service_account=GSA, aws_role_arn=role), environment(), EXECUTOR
        )


def test_aws_role_is_overridden_without_mutating_executor():
    original = environment()
    selected = reader_environment(
        configured(google_service_account=GSA, aws_role_arn=READER), original, EXECUTOR
    )
    assert selected.config.role_arn == READER
    assert original.config.role_arn == EXECUTOR
    assert selected.environment_id == original.environment_id


def test_onprem_token_reference_is_separate():
    original = environment("onprem")
    with pytest.raises(ValueError):
        reader_environment(configured(onprem_token_ref=original.secret_ref), original, EXECUTOR)
    selected = reader_environment(
        configured(onprem_token_ref="env:ECHO_SECRET_READER"), original, EXECUTOR
    )
    assert selected.secret_ref == "env:ECHO_SECRET_READER"
    assert original.secret_ref == "env:ECHO_SECRET_EXECUTOR"


async def test_wrong_metadata_identity_stops_before_any_kube_or_iam_call():
    reader = LiveEnvironmentSecurityReader(Settings(executor_aws_role_arn=EXECUTOR), SystemClock())
    reader._metadata.service_account_email = AsyncMock(
        return_value="executor@example.iam.gserviceaccount.com"
    )
    reader._provider.credentials_for_security_read = AsyncMock(
        side_effect=AssertionError("not called")
    )
    try:
        with pytest.raises(ValueError, match="unexpected collector"):
            await reader.inspect(
                configured(google_service_account=GSA, aws_role_arn=READER), environment()
            )
        reader._provider.credentials_for_security_read.assert_not_awaited()
        assert reader._provider._default_aws_role_arn is None
    finally:
        await reader.aclose()


@pytest.mark.parametrize("body,valid", [(GSA, True), ("", False), ("default", False)])
async def test_metadata_email_endpoint_contract(body, valid):
    def respond(request):
        assert request.url.path.endswith("/service-accounts/default/email")
        assert request.headers["Metadata-Flavor"] == "Google"
        return httpx.Response(200, text=body)

    client = GoogleMetadataClient(
        "http://metadata.test", SystemClock(), transport=httpx.MockTransport(respond)
    )
    try:
        if valid:
            assert await client.service_account_email() == GSA
        else:
            with pytest.raises(MetadataAuthError):
                await client.service_account_email()
    finally:
        await client.aclose()


@pytest.mark.parametrize("mode", ["once", "disabled", "preflight"])
async def test_dedicated_entrypoint_lifecycle_without_executor(mode, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.worker import environment_security as runtime

    settings = Settings(
        security_gate_enabled=True,
        security_environment_collector_enabled=mode != "disabled",
        security_environment_profiles=(profile(),),
    )
    engine = SimpleNamespace(dispose=AsyncMock())
    reader = SimpleNamespace(aclose=AsyncMock())
    collector = SimpleNamespace(run_once=AsyncMock(return_value=2), failed_environments=0)
    reader_factory = Mock(return_value=reader)
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime, "get_session_factory", Mock())
    monkeypatch.setattr(runtime, "get_engine", lambda: engine)
    monkeypatch.setattr(runtime, "LiveEnvironmentSecurityReader", reader_factory)
    monkeypatch.setattr(runtime, "EnvironmentSecurityCollector", Mock(return_value=collector))
    monkeypatch.setattr(
        runtime, "inspect_configuration", AsyncMock(return_value={"configurationReady": True})
    )
    if mode == "disabled":
        with pytest.raises(ValueError):
            await runtime.main(once=True)
        reader_factory.assert_not_called()
    elif mode == "preflight":
        assert await runtime.main(preflight=True) == 0
        reader_factory.assert_not_called()
    else:
        assert await runtime.main(once=True) == 0
        reader.aclose.assert_awaited_once()
        collector.run_once.assert_awaited_once()
    engine.dispose.assert_awaited_once()

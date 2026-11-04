"""실제 클러스터 접속 어댑터 시험(SPEC api-12). 클라우드·클러스터는 모두 가짜 전송이다(DB 없음)."""

import logging
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.adapters.base import ClusterInfo, ConnectionCheckError, EnvironmentConfig
from app.adapters.registry import get_environment_adapter, validate_environment_config
from app.clients.secret_client import EnvVarSecretClient
from app.core.config import EnvironmentAdapterMode, Settings
from app.core.secrets import SecretValue
from app.dependencies import ClusterReaderHolder, get_adapter_resolver
from app.enums import ConnectionErrorCode
from app.executors.google_metadata import GoogleMetadataClient
from app.executors.kubernetes.access import ClusterAccessProvider
from app.executors.kubernetes.api import KubeHttpClients
from app.executors.kubernetes.reader import ClusterReader
from app.services.environment_connection_service import (
    ConnectionTarget,
    EnvironmentConnectionService,
)
from tests.real_executor_fakes import (
    EKS_ENDPOINT,
    GKE_ENDPOINT,
    GOOGLE_ACCESS_TOKEN,
    GOOGLE_ID_TOKEN,
    METADATA_BASE,
    ONPREM_API_URL,
    ONPREM_TOKEN_ENV,
    DictSecretClient,
    FakeCloud,
    RecordingSleep,
    TestClock,
    onprem_service_account_token,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/echo-dev-executor"
AWS_CONFIG: dict[str, Any] = {
    "region": "ap-northeast-1",
    "clusterName": "echo-dev-eks",
    "roleArn": ROLE_ARN,
}
GCP_CONFIG: dict[str, Any] = {
    "projectId": "team-echo-final",
    "region": "asia-northeast3",
    "clusterName": "echo-dev-gke",
}
ONPREM_CONFIG: dict[str, Any] = {
    "location": "ec2",
    "kubeContext": "default",
    "apiUrl": ONPREM_API_URL,
}
ONPREM_DB_CONFIG: dict[str, Any] = {
    "location": "ec2",
    "kube_context": "default",
    "api_url": ONPREM_API_URL,
}
PLACEHOLDER = SecretValue("fake-executor-placeholder")
ONPREM_TOKEN = SecretValue(onprem_service_account_token())


@dataclass
class StubCluster:
    """쿠버네티스 창구 흉내: /version 과 /api/v1/nodes 만 읽는다. 쓰기 요청은 기록해 둔다."""

    version: str = "v1.35.9-eks-577a51e"
    node_count: int = 2
    statuses: dict[str, list[int]] = field(default_factory=dict)  # 경로 → 앞으로 줄 상태 코드
    version_body: Any = None
    api_body: Any = field(default_factory=lambda: {"kind": "APIVersions", "versions": ["v1"]})
    transport_failures: int = 0
    requests: list[httpx.Request] = field(default_factory=list)

    def factory(self) -> Any:
        def build(server: str, ca_pem: str | None, timeout: float) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                base_url=server, transport=httpx.MockTransport(self.handle), timeout=timeout
            )

        return build

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.transport_failures > 0:
            self.transport_failures -= 1
            raise httpx.ConnectError("boom")
        queued = self.statuses.get(request.url.path)
        if queued:
            return httpx.Response(queued.pop(0), text="not-for-logs")
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return httpx.Response(401)
        if request.url.path == "/api":
            return httpx.Response(200, json=self.api_body)
        if request.url.path == "/version":
            body = (
                self.version_body if self.version_body is not None else {"gitVersion": self.version}
            )
            return httpx.Response(200, json=body)
        if request.url.path == "/api/v1/nodes":
            return httpx.Response(200, json={"items": [{} for _ in range(self.node_count)]})
        return httpx.Response(404)

    def non_get(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method != "GET"]


@dataclass
class Rig:
    cloud: FakeCloud
    cluster: StubCluster
    sleep: RecordingSleep
    reader: ClusterReader


def _rig(*, default_role: str | None = None) -> Rig:
    clock = TestClock()
    cloud = FakeCloud(clock)
    cluster = StubCluster()
    sleep = RecordingSleep()
    provider = ClusterAccessProvider(
        metadata=GoogleMetadataClient(METADATA_BASE, clock, transport=cloud.transport()),
        secret_client=DictSecretClient({}),
        clock=clock,
        default_aws_role_arn=default_role,
        transport=cloud.transport(),
    )
    reader = ClusterReader(provider, KubeHttpClients(cluster.factory(), 5.0), sleep=sleep)
    return Rig(cloud, cluster, sleep, reader)


def _slow_attempt_reader(rig: Rig, seconds_per_attempt: float) -> ClusterReader:
    ticks = iter(range(1000))  # 시도 하나가 시작 때와 끝날 때 읽는 시계: 0 → seconds_per_attempt
    reader = rig.reader

    def monotonic() -> float:
        return next(ticks) * seconds_per_attempt

    reader._monotonic = monotonic  # type: ignore[attr-defined]
    return reader


def _config(kind: str, raw: dict[str, Any]) -> EnvironmentConfig:
    return validate_environment_config(kind, raw, by_alias=True)


def _credentials(kind: str) -> SecretValue:
    return ONPREM_TOKEN if kind == "onprem" else PLACEHOLDER


CASES = [
    ("aws", AWS_CONFIG, EKS_ENDPOINT),
    ("gcp", GCP_CONFIG, GKE_ENDPOINT),
    ("onprem", ONPREM_CONFIG, ONPREM_API_URL),
]


@pytest.mark.parametrize(("kind", "raw", "_server"), CASES)
async def test_real_adapter_reads_version_and_node_count_without_writes(
    kind: str, raw: dict[str, Any], _server: str
) -> None:
    rig = _rig()
    adapter = get_environment_adapter(kind, reader=rig.reader)
    config = _config(kind, raw)

    assert await adapter.check_connection(config, _credentials(kind)) is None
    info = await adapter.read_cluster_info(config, _credentials(kind))

    assert info == ClusterInfo(server_version=rig.cluster.version, node_count=2, simulated=False)
    assert rig.cluster.non_get() == []
    assert {request.url.path for request in rig.cluster.requests} == {
        "/version",
        "/api",
        "/api/v1/nodes",
    }


@pytest.mark.parametrize("kind", ["aws", "gcp", "onprem"])
async def test_without_reader_adapter_stays_simulated(kind: str) -> None:
    adapter = get_environment_adapter(kind)
    raw = {"aws": AWS_CONFIG, "gcp": GCP_CONFIG, "onprem": ONPREM_CONFIG}[kind]

    info = await adapter.read_cluster_info(_config(kind, raw), _credentials(kind))

    assert info.simulated is True


async def test_aws_and_gcp_do_not_use_the_placeholder_secret_value() -> None:
    rig = _rig()
    marker = SecretValue("placeholder-must-never-be-sent")
    for kind, raw in (("aws", AWS_CONFIG), ("gcp", GCP_CONFIG)):
        await get_environment_adapter(kind, reader=rig.reader).check_connection(
            _config(kind, raw), marker
        )

    assert all("placeholder-must-never-be-sent" not in str(r.headers) for r in rig.cluster.requests)


async def test_onprem_uses_the_resolved_service_account_token() -> None:
    rig = _rig()
    await get_environment_adapter("onprem", reader=rig.reader).check_connection(
        _config("onprem", ONPREM_CONFIG), ONPREM_TOKEN
    )

    assert rig.cluster.requests[0].headers["authorization"] == (
        f"Bearer {ONPREM_TOKEN.get_secret_value()}"
    )


@pytest.mark.parametrize(
    ("kind", "raw"), [("aws", AWS_CONFIG), ("gcp", GCP_CONFIG), ("onprem", ONPREM_CONFIG)]
)
async def test_cluster_401_after_token_refresh_is_auth_failed(
    kind: str, raw: dict[str, Any]
) -> None:
    rig = _rig()
    rig.cluster.statuses["/version"] = [401, 401]

    with pytest.raises(ConnectionCheckError) as raised:
        await get_environment_adapter(kind, reader=rig.reader).check_connection(
            _config(kind, raw), _credentials(kind)
        )

    assert raised.value.reason is ConnectionErrorCode.AUTH_FAILED
    assert len(rig.cluster.requests) == 2  # 처음 + 토큰을 다시 얻은 뒤 한 번
    assert rig.sleep.delays == []


async def test_stale_gke_token_is_refreshed_once_and_succeeds() -> None:
    rig = _rig()
    rig.cluster.statuses["/version"] = [401]

    await get_environment_adapter("gcp", reader=rig.reader).check_connection(
        _config("gcp", GCP_CONFIG), PLACEHOLDER
    )

    assert rig.cloud.metadata_token_calls == 2


async def test_cluster_403_is_auth_failed() -> None:
    rig = _rig()
    rig.cluster.statuses["/version"] = [403]

    with pytest.raises(ConnectionCheckError) as raised:
        await get_environment_adapter("gcp", reader=rig.reader).check_connection(
            _config("gcp", GCP_CONFIG), PLACEHOLDER
        )

    assert raised.value.reason is ConnectionErrorCode.AUTH_FAILED


async def test_sts_denial_is_auth_failed() -> None:
    rig = _rig()
    rig.cloud.sts_status = 403

    with pytest.raises(ConnectionCheckError) as raised:
        await get_environment_adapter("aws", reader=rig.reader).check_connection(
            _config("aws", AWS_CONFIG), PLACEHOLDER
        )

    assert raised.value.reason is ConnectionErrorCode.AUTH_FAILED
    assert rig.cluster.requests == []


@pytest.mark.parametrize("status", [500, 502, 503, 429])
async def test_transient_error_is_retried_once_then_succeeds(status: int) -> None:
    rig = _rig()
    rig.cluster.statuses["/version"] = [status]

    await get_environment_adapter("onprem", reader=rig.reader).check_connection(
        _config("onprem", ONPREM_CONFIG), ONPREM_TOKEN
    )

    assert rig.sleep.delays == [0.5]
    assert len(rig.cluster.requests) == 3  # /version 실패 + /version 재시도 + /api


@pytest.mark.parametrize("status", [500, 503, 429])
async def test_transient_error_twice_is_unreachable(status: int) -> None:
    rig = _rig()
    rig.cluster.statuses["/version"] = [status, status]

    with pytest.raises(ConnectionCheckError) as raised:
        await get_environment_adapter("onprem", reader=rig.reader).check_connection(
            _config("onprem", ONPREM_CONFIG), ONPREM_TOKEN
        )

    assert raised.value.reason is ConnectionErrorCode.UNREACHABLE
    assert rig.sleep.delays == [0.5]


async def test_connection_failure_is_unreachable_after_one_retry() -> None:
    rig = _rig()
    rig.cluster.transport_failures = 2

    with pytest.raises(ConnectionCheckError) as raised:
        await get_environment_adapter("onprem", reader=rig.reader).check_connection(
            _config("onprem", ONPREM_CONFIG), ONPREM_TOKEN
        )

    assert raised.value.reason is ConnectionErrorCode.UNREACHABLE
    assert rig.sleep.delays == [0.5]


async def test_one_connection_failure_then_success() -> None:
    rig = _rig()
    rig.cluster.transport_failures = 1

    await get_environment_adapter("onprem", reader=rig.reader).check_connection(
        _config("onprem", ONPREM_CONFIG), ONPREM_TOKEN
    )


async def test_aws_without_role_is_config_invalid() -> None:
    rig = _rig(default_role=None)
    raw = {"region": "ap-northeast-1", "clusterName": "echo-dev-eks"}

    with pytest.raises(ConnectionCheckError) as raised:
        await get_environment_adapter("aws", reader=rig.reader).check_connection(
            _config("aws", raw), PLACEHOLDER
        )

    assert raised.value.reason is ConnectionErrorCode.CONFIG_INVALID


async def test_aws_falls_back_to_the_default_role() -> None:
    rig = _rig(default_role=ROLE_ARN)
    raw = {"region": "ap-northeast-1", "clusterName": "echo-dev-eks"}

    await get_environment_adapter("aws", reader=rig.reader).check_connection(
        _config("aws", raw), PLACEHOLDER
    )

    assert rig.cloud.sts_calls[0]["RoleArn"] == [ROLE_ARN]


async def test_onprem_without_api_url_is_config_invalid() -> None:
    rig = _rig()
    raw = {"location": "ec2", "kubeContext": "default"}

    with pytest.raises(ConnectionCheckError) as raised:
        await get_environment_adapter("onprem", reader=rig.reader).check_connection(
            _config("onprem", raw), ONPREM_TOKEN
        )

    assert raised.value.reason is ConnectionErrorCode.CONFIG_INVALID
    assert rig.cluster.requests == []


@pytest.mark.parametrize(
    "version_body", [{"major": "1"}, {"gitVersion": "not-a-version"}, ["x"], {"gitVersion": 7}]
)
async def test_version_body_that_is_not_kubernetes_is_config_invalid(version_body: Any) -> None:
    rig = _rig()
    rig.cluster.version_body = version_body

    with pytest.raises(ConnectionCheckError) as raised:
        await get_environment_adapter("onprem", reader=rig.reader).check_connection(
            _config("onprem", ONPREM_CONFIG), ONPREM_TOKEN
        )

    assert raised.value.reason is ConnectionErrorCode.CONFIG_INVALID


async def test_version_404_is_config_invalid() -> None:
    rig = _rig()
    rig.cluster.statuses["/version"] = [404]

    with pytest.raises(ConnectionCheckError) as raised:
        await get_environment_adapter("onprem", reader=rig.reader).check_connection(
            _config("onprem", ONPREM_CONFIG), ONPREM_TOKEN
        )

    assert raised.value.reason is ConnectionErrorCode.CONFIG_INVALID


async def test_zero_nodes_is_a_valid_answer() -> None:
    rig = _rig()
    rig.cluster.node_count = 0

    info = await get_environment_adapter("onprem", reader=rig.reader).read_cluster_info(
        _config("onprem", ONPREM_CONFIG), ONPREM_TOKEN
    )

    assert info.node_count == 0


async def test_forbidden_node_list_is_auth_failed_only_for_cluster_info() -> None:
    rig = _rig()
    rig.cluster.statuses["/api/v1/nodes"] = [403]
    adapter = get_environment_adapter("onprem", reader=rig.reader)
    config = _config("onprem", ONPREM_CONFIG)

    await adapter.check_connection(config, ONPREM_TOKEN)
    with pytest.raises(ConnectionCheckError) as raised:
        await adapter.read_cluster_info(config, ONPREM_TOKEN)

    assert raised.value.reason is ConnectionErrorCode.AUTH_FAILED


async def test_service_keeps_connected_when_only_node_list_is_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _rig()
    rig.cluster.statuses["/api/v1/nodes"] = [403]
    monkeypatch.setenv(ONPREM_TOKEN_ENV, ONPREM_TOKEN.get_secret_value())
    service = EnvironmentConnectionService(
        EnvVarSecretClient(), lambda kind: get_environment_adapter(kind, reader=rig.reader), 5.0
    )

    result = await service.check_connection(
        ConnectionTarget(
            environment_id=uuid.uuid4(),
            kind="onprem",
            config=ONPREM_DB_CONFIG,
            secret_ref=f"env:{ONPREM_TOKEN_ENV}",
        )
    )

    assert result.connection_error is None
    assert result.cluster_info is None
    assert result.cluster_info_error is ConnectionErrorCode.AUTH_FAILED


async def test_service_reports_failed_with_reason_when_cluster_rejects_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _rig()
    rig.cluster.statuses["/version"] = [401, 401]
    monkeypatch.setenv(ONPREM_TOKEN_ENV, ONPREM_TOKEN.get_secret_value())
    service = EnvironmentConnectionService(
        EnvVarSecretClient(), lambda kind: get_environment_adapter(kind, reader=rig.reader), 5.0
    )

    result = await service.check_connection(
        ConnectionTarget(
            environment_id=uuid.uuid4(),
            kind="onprem",
            config=ONPREM_DB_CONFIG,
            secret_ref=f"env:{ONPREM_TOKEN_ENV}",
        )
    )

    assert result.connection_error is ConnectionErrorCode.AUTH_FAILED


@pytest.mark.parametrize("scenario", ["ok", "sts_denied", "cluster_401", "cluster_503", "no_role"])
async def test_no_secret_strings_leak_into_logs_exceptions_or_results(
    scenario: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for noisy in ("httpx", "httpcore", "botocore"):  # 운영 로그 설정(configure_logging)과 같게
        logging.getLogger(noisy).setLevel(logging.WARNING)
    rig = _rig(default_role=ROLE_ARN if scenario != "no_role" else None)
    if scenario == "sts_denied":
        rig.cloud.sts_status = 403
    elif scenario == "cluster_401":
        rig.cluster.statuses["/version"] = [401, 401]
    elif scenario == "cluster_503":
        rig.cluster.statuses["/version"] = [503, 503]
    raw = AWS_CONFIG if scenario != "no_role" else {"region": "ap-northeast-1", "clusterName": "c"}
    outcome = ""
    adapter = get_environment_adapter("aws", reader=rig.reader)
    try:
        info = await adapter.read_cluster_info(_config("aws", raw), PLACEHOLDER)
        outcome = repr(info)
    except ConnectionCheckError as error:
        outcome = f"{error!r} {error!s} {error.args}"

    haystack = caplog.text + outcome
    for secret in (
        GOOGLE_ID_TOKEN,
        GOOGLE_ACCESS_TOKEN,
        "k8s-aws-v1.",
        "not-for-logs",
        "fake-executor-placeholder",
        EKS_ENDPOINT,
        "echo-dev-eks",
    ):
        assert secret not in haystack


def _request() -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


async def test_resolver_is_simulated_by_default() -> None:
    settings = Settings()
    assert settings.environment_adapter_mode is EnvironmentAdapterMode.SIMULATED

    resolver = await get_adapter_resolver(_request(), settings, TestClock())

    assert resolver is get_environment_adapter
    assert (
        await resolver("aws").read_cluster_info(_config("aws", AWS_CONFIG), PLACEHOLDER)
    ).simulated


async def test_resolver_in_real_mode_injects_the_reader_and_reuses_it() -> None:
    settings = Settings(environment_adapter_mode=EnvironmentAdapterMode.REAL)
    request = _request()
    clock = TestClock()

    first = await get_adapter_resolver(request, settings, clock)
    second = await get_adapter_resolver(request, settings, clock)

    assert first("aws")._reader is not None  # type: ignore[attr-defined]
    assert first("aws")._reader is second("gcp")._reader  # type: ignore[attr-defined]
    holder = request.app.state.cluster_reader_holder
    assert isinstance(holder, ClusterReaderHolder)
    await holder.aclose()


def test_unknown_adapter_mode_is_a_startup_error() -> None:
    with pytest.raises(ValueError):
        Settings(environment_adapter_mode="maybe")  # type: ignore[arg-type]


@pytest.mark.parametrize("code", ["IDPCommunicationError", "Throttling", "ServiceUnavailable"])
async def test_sts_transient_400_is_retried_as_transient(code: str) -> None:
    rig = _rig()
    rig.cloud.sts_status = 400
    rig.cloud.sts_error_body = f"<ErrorResponse><Error><Code>{code}</Code></Error></ErrorResponse>"

    with pytest.raises(ConnectionCheckError) as raised:
        await get_environment_adapter("aws", reader=rig.reader).check_connection(
            _config("aws", AWS_CONFIG), PLACEHOLDER
        )

    assert raised.value.reason is ConnectionErrorCode.UNREACHABLE
    assert rig.sleep.delays == [0.5]
    assert len(rig.cloud.sts_calls) == 2


async def test_sts_400_with_other_code_is_auth_failed_without_retry() -> None:
    rig = _rig()
    rig.cloud.sts_status = 400
    rig.cloud.sts_error_body = "<Error><Code>InvalidIdentityToken</Code></Error>"

    with pytest.raises(ConnectionCheckError) as raised:
        await get_environment_adapter("aws", reader=rig.reader).check_connection(
            _config("aws", AWS_CONFIG), PLACEHOLDER
        )

    assert raised.value.reason is ConnectionErrorCode.AUTH_FAILED
    assert rig.sleep.delays == []


async def test_slow_failed_attempt_is_not_retried() -> None:
    rig = _rig()
    rig.cluster.statuses["/version"] = [503, 503]
    reader = _slow_attempt_reader(rig, seconds_per_attempt=2.0)

    with pytest.raises(ConnectionCheckError) as raised:
        await get_environment_adapter("onprem", reader=reader).check_connection(
            _config("onprem", ONPREM_CONFIG), ONPREM_TOKEN
        )

    assert raised.value.reason is ConnectionErrorCode.UNREACHABLE
    assert rig.sleep.delays == []
    assert len(rig.cluster.requests) == 1


def test_adapter_modules_do_not_import_executors_or_models() -> None:
    """순환 import 방지(SPEC 8.1): 어댑터 파일은 app.executors·app.models 를 import 하지 않는다."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app" / "adapters"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            for module in modules:
                assert not module.startswith(("app.executors", "app.models")), (path.name, module)


async def test_check_connection_also_requires_the_authenticated_api_list() -> None:
    rig = _rig()
    await get_environment_adapter("onprem", reader=rig.reader).check_connection(
        _config("onprem", ONPREM_CONFIG), ONPREM_TOKEN
    )

    assert [request.url.path for request in rig.cluster.requests] == ["/version", "/api"]


@pytest.mark.parametrize("status", [401, 403])
async def test_open_version_but_api_list_denied_is_auth_failed(status: int) -> None:
    """/version 은 비로그인으로 열려 있어도(200) 출입증이 인정되지 않으면 접속 성공이 아니다."""
    rig = _rig()
    rig.cluster.statuses["/api"] = [status, status]

    with pytest.raises(ConnectionCheckError) as raised:
        await get_environment_adapter("onprem", reader=rig.reader).check_connection(
            _config("onprem", ONPREM_CONFIG), ONPREM_TOKEN
        )

    assert raised.value.reason is ConnectionErrorCode.AUTH_FAILED


async def test_api_list_with_unexpected_body_is_config_invalid() -> None:
    rig = _rig()
    rig.cluster.api_body = {"kind": "Status"}

    with pytest.raises(ConnectionCheckError) as raised:
        await get_environment_adapter("onprem", reader=rig.reader).check_connection(
            _config("onprem", ONPREM_CONFIG), ONPREM_TOKEN
        )

    assert raised.value.reason is ConnectionErrorCode.CONFIG_INVALID


async def test_cluster_info_does_not_call_the_api_list() -> None:
    rig = _rig()
    await get_environment_adapter("onprem", reader=rig.reader).read_cluster_info(
        _config("onprem", ONPREM_CONFIG), ONPREM_TOKEN
    )

    assert "/api" not in {request.url.path for request in rig.cluster.requests}

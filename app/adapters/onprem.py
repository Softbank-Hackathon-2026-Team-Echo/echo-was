from app.adapters.base import (
    EnvironmentConfig,
    NonEmptyStr,
    SimulatedEnvironmentAdapter,
    register_environment_adapter,
    register_environment_kind,
)


@register_environment_kind("onprem")
class OnpremEnvironmentConfig(EnvironmentConfig):
    """온프레미스(k3s 등) 환경. location 은 사람이 읽는 설치 위치(예: team-laptop)다."""

    location: NonEmptyStr
    kube_context: NonEmptyStr

    def display_location(self) -> str:
        return self.location


@register_environment_adapter("onprem")
class OnpremEnvironmentAdapter(SimulatedEnvironmentAdapter):
    """온프레미스(k3s) 동작 클래스 — **가짜(simulated)**. 네트워크 호출 없이 고정 값을 돌려준다.

    실제 k3s 접속은 인프라 파트가 클러스터를 만든 뒤 별도 작업에서 이 클래스를 바꾼다.
    """

    simulated_server_version = "v1.31.4+k3s1"
    simulated_node_count = 1

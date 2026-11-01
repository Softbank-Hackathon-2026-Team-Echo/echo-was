from app.adapters.base import EnvironmentConfig, NonEmptyStr, register_environment_kind


@register_environment_kind("onprem")
class OnpremEnvironmentConfig(EnvironmentConfig):
    """온프레미스(k3s 등) 환경. location 은 사람이 읽는 설치 위치(예: team-laptop)다."""

    location: NonEmptyStr
    kube_context: NonEmptyStr

    def display_location(self) -> str:
        return self.location

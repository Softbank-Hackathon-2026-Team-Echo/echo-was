from app.adapters.base import EnvironmentConfig
from app.adapters.registry import get_environment_config_class, search_environment_kinds


class EnvironmentKindService:
    """등록된 환경 종류 목록. 새 종류 파일을 더하면 코드 변경 없이 여기에 나타난다."""

    def search_environment_kinds(self) -> list[type[EnvironmentConfig]]:
        """종류 이름 순으로 설정 양식 클래스를 돌려준다."""
        return [get_environment_config_class(kind) for kind in search_environment_kinds()]

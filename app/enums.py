"""Model·Schema 가 함께 쓰는 Enum 을 한곳에 둔다. 값은 DB 에 저장되는 코드(UPPER_SNAKE)다.

환경 종류(aws·gcp·onprem)는 Enum 으로 두지 않는다. 새 종류를 파일 하나로 더할 수 있도록
설정 양식 레지스트리(app.adapters)의 키로 관리한다.
"""

from enum import StrEnum


class DeploymentStatus(StrEnum):
    """배포 상태. 허용 전환은 용어 사전의 '배포 상태'를 따른다. 끝 상태는 되감지 않는다."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class EnvironmentConnectionStatus(StrEnum):
    """환경 접속 상태. 등록·다시 확인·config/secretRef 수정 때 바뀐다. 단건 조회는 바꾸지 않는다."""

    UNCHECKED = "UNCHECKED"
    CONNECTED = "CONNECTED"
    FAILED = "FAILED"


class ConnectionErrorCode(StrEnum):
    """접속 확인·클러스터 정보 읽기 실패 이유. DB CHECK 를 걸지 않고 앱이 이 값만 쓴다."""

    TIMEOUT = "TIMEOUT"
    SECRET_REF_MISSING = "SECRET_REF_MISSING"
    SECRET_REF_INVALID = "SECRET_REF_INVALID"
    SECRET_NOT_FOUND = "SECRET_NOT_FOUND"
    SECRET_STORE_UNSUPPORTED = "SECRET_STORE_UNSUPPORTED"
    CONFIG_INVALID = "CONFIG_INVALID"
    AUTH_FAILED = "AUTH_FAILED"
    UNREACHABLE = "UNREACHABLE"
    UNKNOWN = "UNKNOWN"

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

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


def _is_none(value: object) -> bool:
    return value is None


class ApiModel(BaseModel):
    """API 입출력 스키마의 베이스. 서버 내부는 snake_case, JSON 은 camelCase 로 주고받는다."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ApiRequestModel(ApiModel):
    """요청 본문 스키마의 베이스. camelCase 키만 받고, 모르는 칸은 거부한다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=False, validate_by_name=False)


class ErrorDetail(ApiModel):
    """필드별 실패 사유. field 는 camelCase 필드 경로, reason 은 고정 코드다.

    value 는 안내 값(예: installUrl 의 GitHub App 설치 주소)이 있을 때만 싣는다.
    """

    field: str
    reason: str
    value: str | None = Field(default=None, exclude_if=_is_none)


class ApiResponse[T](ApiModel):
    """모든 JSON 응답의 공통 봉투. 성공·실패가 같은 구조를 쓴다.

    성공 시 code=None, 실패 시 도메인 예외의 code 를 담는다. details 는 검증 실패 시에만 채운다.
    봉투의 code·message·details 는 값이 없으면 뺀다. 성공 응답의 data 안은 그대로 둔다 —
    data 안의 칸은 값이 없어도 null 로 항상 들어 있다. 실패 응답은 data 를 싣지 않는다
    (exception_handlers.error_response).
    """

    success: bool = True
    code: str | None = Field(default=None, exclude_if=_is_none)
    message: str | None = Field(default=None, exclude_if=_is_none)
    data: T | None = None
    details: list[ErrorDetail] | None = Field(default=None, exclude_if=_is_none)


class Page[T](ApiModel):
    """페이지 번호 기반 목록. page 는 0부터 시작한다."""

    items: list[T]
    total: int
    page: int
    size: int


DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

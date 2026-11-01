from sqlalchemy.exc import IntegrityError


def violated_constraint_name(exc: IntegrityError) -> str | None:
    """무결성 오류를 낸 제약·인덱스 이름. 이름으로 도메인 오류(이름 중복 등)를 가른다."""
    # asyncpg 예외는 DBAPI 어댑터 예외의 원인(__cause__)으로 붙어 있고 constraint_name 을 가진다.
    driver_error = getattr(exc.orig, "__cause__", None)
    name = getattr(driver_error, "constraint_name", None)
    return name if isinstance(name, str) else None

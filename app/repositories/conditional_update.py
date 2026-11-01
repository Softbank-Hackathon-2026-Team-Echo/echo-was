"""조건부 UPDATE 도우미: `UPDATE … SET … WHERE id = :id AND <조건>` 의 영향받은 행 수로 성패를 안다.

상태는 이 방식으로만 바꾼다. 0행이면 다른 트랜잭션이 먼저 바꾼 것이므로 결과를 버리고 다시 읽는다.
세션에 올라온 객체는 갱신하지 않는다(synchronize_session=False). 다음 읽기는 populate_existing 으로
DB 값을 다시 가져온다.
"""

import uuid
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession


async def update_where(
    session: AsyncSession,
    model: type[Any],
    row_id: uuid.UUID,
    conditions: list[ColumnElement[bool]],
    values: dict[str, Any],
) -> bool:
    statement = (
        update(model)
        .where(model.id == row_id, *conditions)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    result = cast(CursorResult[Any], await session.execute(statement))
    return result.rowcount == 1
